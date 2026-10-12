"""Owner-scoped, versioned knowledge. Inferred preferences require a bound decision."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from server.harness.reporting import safe_text
from server.models import MemoryCreate, MemoryDecision, MemoryUpdate
from server.services import ServiceError, view
from server.storage.database import encode, fingerprint, now, uid
from server.tools.fuyao import redact

KINDS = {'preference': '研究偏好', 'research_fact': '历史研究事实', 'hypothesis': '研究假设', 'open_question': '待解决问题'}


def expired(content):
    value = content.get('expires_at')
    return bool(value and datetime.fromisoformat(value).timestamp() <= datetime.now(timezone.utc).timestamp())


class MemoryService:
    def __init__(self, service):
        self.service, self.db = service, service.db

    def event(self, conn, owner, content, kind, payload):
        run_id = content.get('source_run_id')
        if run_id:
            self.db.event(conn, owner, run_id, kind, payload)

    def revision(self, conn, row, content, status):
        version = row['version'] + 1
        digest = fingerprint(content)
        conn.execute('UPDATE memories SET content_json=?,version=?,status=?,content_sha256=?,updated_at=? WHERE id=?',
                     (encode(content), version, status, digest, now(), row['id']))
        conn.execute('INSERT INTO memory_versions VALUES(?,?,?,?,?,?)', (row['id'], version, status, encode(content), digest, now()))

    def expire(self, owner):
        self.service.ensure_owner(owner)
        with self.db.connect() as conn:
            for row in conn.execute("SELECT * FROM memories WHERE session_id=? AND status IN ('active','pending')", (owner,)).fetchall():
                content = json.loads(row['content_json'])
                if expired(content):
                    self.revision(conn, row, content, 'expired')
                    self.event(conn, owner, content, 'memory.expired', {'memory_id': row['id']})

    def get(self, owner, memory_id):
        self.expire(owner)
        row = self.service.resource('memories', memory_id, owner)
        if row['status'] == 'deleted':
            raise ServiceError(404, '记忆已删除')
        return row

    def list(self, owner, q='', include_inactive=True):
        self.expire(owner)
        rows = self.db.all("SELECT * FROM memories WHERE session_id=? AND status!='deleted' ORDER BY updated_at DESC LIMIT 200", (owner,))
        return [view(row) for row in rows if (include_inactive or row['status'] == 'active')
                and (not q or q.casefold() in encode(json.loads(row['content_json'])).casefold())]

    def prepare(self, owner, payload: MemoryCreate, previous=None):
        self.service.ensure_owner(owner)
        if payload.thread_id:
            self.service.resource('threads', payload.thread_id, owner)
        if any(not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', s) for s in payload.symbols):
            raise ServiceError(422, '标的需使用完整的 A 股代码')
        expires = payload.expires_at
        if expires and expires.timestamp() <= datetime.now(timezone.utc).timestamp():
            raise ServiceError(422, '过期时间需要晚于现在')
        c = {'kind': payload.kind, 'content': payload.content, 'tags': payload.tags, 'symbols': payload.symbols,
             'expires_at': expires.isoformat() if expires else None, 'valid_as_of': None, 'evidence_ids': [],
             'derived_metric_ids': [], 'source_run_id': None, 'source_artifact_id': None, 'claim_index': None,
             'origin': 'explicit', 'write_basis': '用户明确保存' if previous is None else '用户明确修正',
             'trust': 'user_assertion' if payload.kind in {'hypothesis', 'open_question'} else 'user_preference'}
        if payload.kind == 'research_fact':
            artifact = self.service.artifacts.verify(owner, payload.source_artifact_id)
            report = artifact['content']
            if report['validation_status'] != 'passed' or payload.claim_index >= len(report['claims']):
                raise ServiceError(422, '请选择通过校验的事实结论')
            claim = report['claims'][payload.claim_index]
            if claim['kind'] != 'fact' or not claim.get('as_of') or not claim.get('evidence_ids'):
                raise ServiceError(422, '推断与不确定信息不能保存为研究事实')
            if not expires:
                expires = datetime.now(timezone.utc) + timedelta(days=1)
            if expires > datetime.now(timezone.utc) + timedelta(days=30):
                raise ServiceError(422, '研究事实的有效期最多 30 天，保留其历史时点')
            c.update(content=claim['text'], valid_as_of=claim['as_of'], expires_at=expires.isoformat(),
                evidence_ids=claim['evidence_ids'], derived_metric_ids=claim.get('derived_metric_ids', []),
                source_run_id=artifact['run_id'], source_artifact_id=artifact['id'], source_version=artifact['version'],
                claim_index=payload.claim_index, claim_hash=fingerprint(claim), reference=claim.get('reference'),
                unit=claim.get('unit'), method=claim.get('method'), trust='verified_historical_fact',
                write_basis='由已验证报告结论保存，时点仅限原历史资料')
            if not c['symbols']:
                c['symbols'] = re.findall(r'\d{6}\.(?:SH|SZ|BJ)', claim.get('symbol', ''))[:3]
        if not safe_text(c['content']) or any(not safe_text(tag) for tag in c['tags']):
            raise ServiceError(422, '记忆内容包含不支持的投资建议或确定性表达')
        return redact(c, self.service.settings.secrets)

    def insert(self, owner, content, thread_id=None, status='active'):
        self.service.ensure_owner(owner)
        record_id, timestamp, digest = uid(), now(), fingerprint(content)
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            count = conn.execute("SELECT COUNT(*) FROM memories WHERE session_id=? AND status!='deleted'", (owner,)).fetchone()[0]
            if count >= 200:
                raise ServiceError(409, '记忆已达 200 条，请先整理或删除旧记录')
            conn.execute('INSERT INTO memories(id,session_id,thread_id,content_json,version,created_at,updated_at,status,content_sha256) VALUES(?,?,?,?,1,?,?,?,?)',
                         (record_id, owner, thread_id, encode(content), timestamp, timestamp, status, digest))
            conn.execute('INSERT INTO memory_versions VALUES(?,1,?,?,?,?)', (record_id, status, encode(content), digest, timestamp))
            self.event(conn, owner, content, 'memory.proposed' if status == 'pending' else 'memory.saved', {'memory_id': record_id, 'kind': content['kind'], 'status': status})
        return self.get(owner, record_id)

    def create(self, owner, payload):
        return self.insert(owner, self.prepare(owner, payload), payload.thread_id)

    def update(self, owner, memory_id, payload: MemoryUpdate):
        previous = self.get(owner, memory_id)
        if previous['version'] != payload.expected_version:
            raise ServiceError(409, '记忆已变化，请重新打开编辑器')
        c = self.prepare(owner, payload, previous)
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM memories WHERE id=? AND session_id=?', (memory_id, owner)).fetchone()
            if row['version'] != payload.expected_version or row['status'] == 'deleted':
                raise ServiceError(409, '记忆版本已变化')
            self.revision(conn, row, c, 'pending' if row['status'] == 'pending' else 'active')
            conn.execute('UPDATE memories SET thread_id=? WHERE id=?', (payload.thread_id, memory_id))
            self.event(conn, owner, c, 'memory.corrected', {'memory_id': memory_id, 'version': row['version']+1})
        return self.get(owner, memory_id)

    def delete(self, owner, memory_id, expected_version):
        record = self.get(owner, memory_id)
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM memories WHERE id=? AND session_id=?', (memory_id, owner)).fetchone()
            if row['version'] != expected_version or row['status'] == 'deleted':
                raise ServiceError(409, '记忆版本已变化')
            self.revision(conn, row, record['content'], 'deleted')
            self.event(conn, owner, record['content'], 'memory.deleted', {'memory_id': memory_id})
        return {'id': memory_id, 'status': 'deleted'}

    def versions(self, owner, memory_id):
        self.get(owner, memory_id)
        return [view(r) for r in self.db.all('SELECT * FROM memory_versions WHERE memory_id=? ORDER BY version', (memory_id,))]

    def propose(self, owner, run_id):
        run = self.service.resource('runs', run_id, owner)
        report = self.service.artifacts.latest(owner, run_id)
        self.service.artifacts.verify(owner, report['id'])
        if report['content']['validation_status'] != 'passed':
            raise ServiceError(422, '报告尚未通过校验，无法推断偏好候选')
        calls = self.service.collection(owner, run_id, 'tool_calls')
        bars = [c for c in calls if c['tool'] == 'get_daily_bars' and c['quality'] == 'available']
        modes = {c['parameters']['adjust'] for c in bars}
        if len(modes) != 1:
            return []
        mode = next(iter(modes))
        label = {'forward': '前复权', 'backward': '后复权', 'none': '未复权'}[mode]
        content = self.prepare(owner, MemoryCreate(kind='preference', content=f'历史价格研究优先使用{label}口径', tags=['历史价格']))
        content.update(origin='inferred', write_basis='由本次研究口径推断的候选，等待用户确认', source_run_id=run_id,
            source_artifact_id=report['id'], basis={'observed_adjust': mode, 'evidence_ids': [e for c in bars for e in c['result']['evidence_ids']]})
        for item in self.list(owner):
            if item['content'].get('source_run_id') == run_id and item['content'].get('basis') == content['basis']:
                return [item]
        return [self.insert(owner, content, status='pending')]

    def decide(self, owner, memory_id, payload: MemoryDecision):
        self.get(owner, memory_id)
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM memories WHERE id=? AND session_id=?', (memory_id, owner)).fetchone()
            if row['status'] != 'pending' or row['version'] != payload.expected_version or row['content_sha256'] != payload.content_fingerprint:
                raise ServiceError(409, '候选记忆已变化、过期或已处理')
            content = json.loads(row['content_json'])
            if expired(content) or fingerprint(content) != row['content_sha256']:
                raise ServiceError(409, '候选内容无法确认')
            content.update(confirmed_at=now(), write_basis='用户确认推断候选' if payload.decision == 'approve' else '用户拒绝推断候选')
            self.revision(conn, row, content, 'active' if payload.decision == 'approve' else 'rejected')
            self.event(conn, owner, content, 'memory.decided', {'memory_id': memory_id, 'decision': payload.decision})
        return self.get(owner, memory_id)

    def select(self, owner, run_id):
        run = self.service.resource('runs', run_id, owner)
        goal = run['goal']
        link = self.db.one('SELECT content_json FROM run_links WHERE run_id=?', (run_id,))
        if link:
            linked = json.loads(link['content_json'])
            goal += ' ' + linked['parent_goal'] + ' ' + linked.get('basis_goal', '')
        symbols = set(re.findall(r'\d{6}\.(?:SH|SZ|BJ)', goal.upper()))
        selected = []
        for memory in self.list(owner, include_inactive=False):
            c = memory['content']
            if memory['thread_id'] and memory['thread_id'] != run['thread_id']:
                continue
            if c['symbols'] and not symbols.intersection(c['symbols']):
                continue
            if c['tags'] and c['kind'] != 'preference' and not any(tag.casefold() in goal.casefold() for tag in c['tags']) and not symbols.intersection(c['symbols']):
                continue
            selected.append(memory)
            if len(selected) == 8:
                break
        with self.db.connect() as conn:
            for m in selected:
                conn.execute('INSERT OR IGNORE INTO run_memory_usages(run_id,session_id,memory_id,memory_version,content_json,content_sha256,selected_at) VALUES(?,?,?,?,?,?,?)',
                    (run_id, owner, m['id'], m['version'], encode(m['content']), m['content_sha256'], now()))
            if selected:
                self.db.event(conn, owner, run_id, 'memory.selected', {'memory_ids': [m['id'] for m in selected], 'count': len(selected)})

    def usages(self, owner, run_id):
        self.service.resource('runs', run_id, owner)
        return [view(r) for r in self.db.all('SELECT * FROM run_memory_usages WHERE run_id=? AND session_id=? ORDER BY selected_at', (run_id, owner))]

    def valid_for_context(self, owner, run_id):
        self.expire(owner)
        result = []
        for item in self.usages(owner, run_id):
            row = self.db.owned('memories', item['memory_id'], owner)
            valid = bool(row and row['status'] == 'active' and row['version'] == item['memory_version']
                         and row['content_sha256'] == item['content_sha256'] and fingerprint(json.loads(row['content_json'])) == item['content_sha256'])
            if valid and item['content']['kind'] == 'research_fact':
                try:
                    artifact = self.service.artifacts.verify(owner, item['content']['source_artifact_id'])
                    valid = fingerprint(artifact['content']['claims'][item['content']['claim_index']]) == item['content']['claim_hash']
                except (ServiceError, IndexError):
                    valid = False
                if not valid:
                    with self.db.connect() as conn:
                        conn.execute('BEGIN IMMEDIATE')
                        current = conn.execute('SELECT * FROM memories WHERE id=? AND session_id=?',
                                               (item['memory_id'], owner)).fetchone()
                        if current and current['status'] == 'active' and current['version'] == item['memory_version']:
                            self.revision(conn, current, json.loads(current['content_json']), 'conflict')
                            self.event(conn, owner, item['content'], 'memory.conflict', {
                                'memory_id': item['memory_id'], 'reason': 'source_verification_failed'})
            if valid:
                result.append(item)
            elif item['status'] != 'withdrawn':
                with self.db.connect() as conn:
                    conn.execute("UPDATE run_memory_usages SET status='withdrawn',withdrawn_at=? WHERE run_id=? AND memory_id=?", (now(), run_id, item['memory_id']))
                    self.db.event(conn, owner, run_id, 'memory.withdrawn', {'memory_id': item['memory_id'], 'reason': 'changed_expired_deleted_or_unverified'})
        return result

    def mark_used(self, conn, owner, run_id, memories):
        for m in memories:
            row = conn.execute("SELECT * FROM memories WHERE id=? AND session_id=?", (m['memory_id'], owner)).fetchone()
            if not row or row['status'] != 'active' or row['version'] != m['memory_version'] or expired(json.loads(row['content_json'])) or row['content_sha256'] != m['content_sha256']:
                raise ServiceError(409, '记忆在模型派发前已变化，请恢复研究重新读取')
            conn.execute("UPDATE run_memory_usages SET status='used',last_used_at=? WHERE run_id=? AND memory_id=?", (now(), run_id, m['memory_id']))
        if memories:
            self.db.event(conn, owner, run_id, 'memory.used', {'memory_ids': [m['memory_id'] for m in memories]})
