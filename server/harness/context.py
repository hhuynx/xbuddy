"""Auditable structured compaction. Full market responses remain in evidence storage."""
from __future__ import annotations

import json
import math

from server.harness.reporting import checked_evidence
from server.models import RunBudget
from server.services import ServiceError, view
from server.storage.database import encode, fingerprint, now, uid
from server.tools.fuyao import redact


def estimate_tokens(value):
    text = value if isinstance(value, str) else encode(value)
    cjk = sum('\u2e80' <= char <= '\u9fff' for char in text)
    return math.ceil(cjk + (len(text)-cjk)/3) + 32


class ContextManager:
    def __init__(self, service):
        self.service, self.db = service, service.db

    def capture(self, owner, run_id):
        self.service.resource('runs', run_id, owner)
        link = self.db.one('SELECT parent_run_id FROM run_links WHERE run_id=? AND session_id=?', (run_id, owner))
        sources = [run_id] + ([link['parent_run_id']] if link else [])
        with self.db.connect() as conn:
            for source in sources:
                rows = conn.execute("SELECT * FROM run_events WHERE run_id=? AND session_id=? AND event_type NOT LIKE 'context.%' AND id NOT IN (SELECT source_event_id FROM context_entries WHERE run_id=?) ORDER BY id", (source, owner, run_id)).fetchall()
                for row in rows:
                    content = redact({'source_run_id': source, 'event_id': row['id'], 'event_type': row['event_type'],
                        'at': row['created_at'], 'payload': json.loads(row['payload_json'])}, self.service.settings.secrets)
                    conn.execute('INSERT OR IGNORE INTO context_entries(session_id,run_id,source_run_id,source_event_id,content_json,content_sha256,created_at) VALUES(?,?,?,?,?,?,?)',
                                 (owner, run_id, source, row['id'], encode(content), fingerprint(content), now()))

    def entries(self, owner, run_id, after=0, limit=200):
        self.service.resource('runs', run_id, owner)
        return [view(r) for r in self.db.all('SELECT * FROM context_entries WHERE run_id=? AND session_id=? AND id>? ORDER BY id LIMIT ?', (run_id, owner, after, limit))]

    def state(self, owner, run_id, memories):
        run = self.service.harness.snapshot(owner, run_id)
        runtime = run.get('runtime') or {}
        evidence = []
        for item in self.service.collection(owner, run_id, 'evidence'):
            c = item['content']
            evidence.append({'evidence_id': item['id'], 'sha256': item['content_sha256'], 'raw_record_id': item['raw_id'],
                **{k: c[k] for k in ('provider', 'tool', 'quality', 'parameters', 'as_of', 'fetched_at', 'units', 'method', 'issues')},
                'records': len(c['records']), 'read_url': f"/api/evidence/{item['id']}/slice"})
        link = self.db.one('SELECT * FROM run_links WHERE run_id=? AND session_id=?', (run_id, owner))
        tasks = self.service.collection(owner, run_id, 'tasks')
        return {'goal': run['goal'], 'run_id': run_id, 'plan_version': run['plan_version'],
            'constraints': {'budget': RunBudget(**run['config']['budget']).model_dump(), 'allowed_tools': run['config']['allowed_tools'],
                'investment_expression': 'facts_inference_uncertainty_separate; no_trade_advice_or_promises',
                'source_policy': 'new_run_data_for_current_claims; old_memory_is_historical_context'},
            'plan': runtime.get('plan'), 'task_state': [{k: t[k] for k in ('id','title','tool','arguments','depends_on','status')} for t in tasks],
            'approvals': [{'id': a['id'], 'status': a['status'], 'content': a['content']} for a in run.get('approvals', [])],
            'evidence_catalog': evidence,
            'calculation_refs': [{'id': m['id'], 'sha256': m['content_sha256'], 'input_refs': m['input_refs'], 'method': m['content'].get('method')} for m in self.service.collection(owner, run_id, 'derived_metrics')],
            'unresolved': {'goal_gaps': (runtime.get('plan') or {}).get('unresolved_goals', []),
                'tasks': [t['id'] for t in tasks if t['status'] in {'failed', 'blocked'}],
                'tool_issues': [{'call_id': c['id'], 'quality': c['quality'], 'issues': (c.get('result') or {}).get('issues', [])} for c in self.service.collection(owner, run_id, 'tool_calls') if c['quality'] not in {'available', None}]},
            'memories': [{'id': m['memory_id'], 'version': m['memory_version'], 'content': m['content'], 'trust': m['content']['trust']} for m in memories],
            'followup': view(link)['content'] if link else None,
            'agent_decision': self.service.dialogue.state(run_id).get('entry') if hasattr(self.service,'dialogue') else None,
            'pending_input': run.get('pending_input')}

    def prepare(self, owner, run_id, phase, base, system):
        memories = self.service.memories.valid_for_context(owner, run_id)
        self.capture(owner, run_id)
        entries = [view(r) for r in self.db.all('SELECT * FROM context_entries WHERE run_id=? AND session_id=? ORDER BY id', (run_id, owner))]
        for item in entries:
            if fingerprint(item['content']) != item['content_sha256']:
                raise ServiceError(409, '上下文原文校验失败，请检查运行记录')
        protected = self.state(owner, run_id, memories)
        identifier = uid()
        original = redact({**base, 'protected_state': protected, 'history': [x['content'] for x in entries]}, self.service.settings.secrets)
        # The protected state is retained byte for byte; only redundant chronological history is replaced.
        counts, ranges = {}, []
        for item in entries:
            kind = item['content']['event_type']
            counts[kind] = counts.get(kind, 0) + 1
        for source in dict.fromkeys(x['source_run_id'] for x in entries):
            items = [x for x in entries if x['source_run_id'] == source]
            ranges.append({'source_run_id': source, 'first_entry': items[0]['id'], 'last_entry': items[-1]['id'],
                'event_ids': [x['source_event_id'] for x in items], 'count': len(items),
                'sha256': fingerprint([x['content_sha256'] for x in items])})
        summary = {'method': 'structured_state_compaction_v1', 'event_counts': counts, 'entry_count': len(entries),
            'original_history_hash': fingerprint(original['history']), 'protected_state_hash': fingerprint(original['protected_state']),
            'meaning': '目标、约束、完整计划、任务、审批、口径、证据引用及缺口保留在 protected_state；原文按范围可读取。'}
        with_pointer = {**original, 'context_snapshot_id': identifier}
        messages = lambda data: [{'role': 'system', 'content': system}, {'role': 'user', 'content': encode(data)}]
        before = estimate_tokens(messages(with_pointer))
        budget = RunBudget(**self.service.resource('runs', run_id, owner)['config']['budget']).context_tokens
        candidate = {k: v for k, v in with_pointer.items() if k != 'history'} | {'history_summary': summary}
        compact_tokens = estimate_tokens(messages(candidate))
        compressed = bool(entries and before > budget and compact_tokens < before)
        sent = candidate if compressed else with_pointer
        after = estimate_tokens(messages(sent))
        stored_summary = {**summary, 'original_sha256': fingerprint(with_pointer), 'sent_sha256': fingerprint(sent)}
        stored_summary['snapshot_sha256'] = fingerprint({'original': with_pointer, 'sent': sent,
            'summary': stored_summary, 'ranges': ranges, 'before_tokens': before, 'after_tokens': after,
            'compressed': int(compressed)})
        digest = fingerprint({'original': original, 'budget': budget, 'system': system})
        with self.db.connect() as conn:
            existing = conn.execute('SELECT * FROM context_snapshots WHERE run_id=? AND phase=? AND input_hash=?', (run_id, phase, digest)).fetchone()
            if existing:
                identifier = existing['id']
                sent = json.loads(existing['sent_json'])
                if existing['after_tokens'] != estimate_tokens(messages(sent)) or existing['before_tokens'] != estimate_tokens(messages(json.loads(existing['original_json']))):
                    raise ServiceError(409, '上下文预算估算与原文不一致')
                after = existing['after_tokens']
            else:
                conn.execute('INSERT INTO context_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', (identifier, owner, run_id, phase, digest,
                    encode(with_pointer), encode(sent), encode(stored_summary), encode(ranges), before, after, int(compressed), now()))
                self.db.event(conn, owner, run_id, 'context.compressed' if compressed else 'context.prepared', {
                    'snapshot_id': identifier, 'phase': phase, 'trigger': 'input_context_budget' if compressed else 'within_budget',
                    'before_tokens_estimated': before, 'after_tokens_estimated': after, 'context_budget': budget,
                    'entry_count': len(entries), 'protected_state_hash': summary['protected_state_hash']})
        verified = self.read(owner, identifier)
        return {'snapshot_id': identifier, 'messages': messages(verified['sent']), 'memories': memories, 'after_tokens': after, 'before_tokens': before}

    def snapshots(self, owner, run_id):
        self.service.resource('runs', run_id, owner)
        return [view(r) for r in self.db.all('SELECT id,phase,before_tokens,after_tokens,compressed,summary_json,ranges_json,created_at FROM context_snapshots WHERE run_id=? AND session_id=? ORDER BY created_at', (run_id, owner))]

    def read(self, owner, snapshot_id):
        record = self.service.resource('context_snapshots', snapshot_id, owner)
        try:
            summary, original, sent = record['summary'], record['original'], record['sent']
            if summary.get('snapshot_sha256') and summary['snapshot_sha256'] != fingerprint({
                'original': original, 'sent': sent, 'summary': {k: v for k, v in summary.items() if k != 'snapshot_sha256'},
                'ranges': record['ranges'], 'before_tokens': record['before_tokens'],
                'after_tokens': record['after_tokens'], 'compressed': record['compressed']}):
                raise ValueError('snapshot')
            if summary['method'] != 'structured_state_compaction_v1':
                raise ValueError('method')
            if fingerprint(original) != summary['original_sha256'] or fingerprint(sent) != summary['sent_sha256']:
                raise ValueError('body')
            if any(fingerprint(body['protected_state']) != summary['protected_state_hash'] for body in (original, sent)):
                raise ValueError('protected_state')
            history = original['history']
            if summary['entry_count'] != len(history) or summary['original_history_hash'] != fingerprint(history):
                raise ValueError('history')
            counts, entries = {}, []
            # Fetch provenance in one read, so large histories do not open two
            # SQLite connections per event or observe different database snapshots.
            provenance = {row['source_event_id']: row for row in self.db.all('''
                SELECT e.*, v.run_id AS event_run_id, v.event_type AS event_kind,
                    v.created_at AS event_created_at, v.payload_json AS event_payload
                FROM context_entries e JOIN run_events v ON v.id=e.source_event_id
                    AND v.run_id=e.source_run_id AND v.session_id=e.session_id
                WHERE e.run_id=? AND e.session_id=?''', (record['run_id'], owner))}
            for content in history:
                row = provenance.get(content['event_id'])
                if not row or row['source_run_id'] != content['source_run_id'] or fingerprint(content) != row['content_sha256'] or json.loads(row['content_json']) != content:
                    raise ValueError('original_entry')
                if redact({'source_run_id': row['event_run_id'], 'event_id': row['source_event_id'],
                    'event_type': row['event_kind'], 'at': row['event_created_at'],
                    'payload': json.loads(row['event_payload'])}, self.service.settings.secrets) != content:
                    raise ValueError('source_event')
                entries.append(row)
                kind = content['event_type']
                counts[kind] = counts.get(kind, 0) + 1
            ranges = []
            for source in dict.fromkeys(x['source_run_id'] for x in entries):
                items = [x for x in entries if x['source_run_id'] == source]
                ranges.append({'source_run_id': source, 'first_entry': items[0]['id'], 'last_entry': items[-1]['id'],
                    'event_ids': [x['source_event_id'] for x in items], 'count': len(items),
                    'sha256': fingerprint([x['content_sha256'] for x in items])})
            if record['ranges'] != ranges or summary['event_counts'] != counts:
                raise ValueError('provenance')
            if record['compressed']:
                if sent['history_summary'] != {k: v for k, v in summary.items() if k not in {'original_sha256', 'sent_sha256', 'snapshot_sha256'}}:
                    raise ValueError('summary')
            elif sent['history'] != history:
                raise ValueError('sent_history')
        except (KeyError, TypeError, ValueError):
            raise ServiceError(409, '上下文内容、摘要或原文范围校验失败') from None
        return record

    def evidence_slice(self, owner, evidence_id, offset=0, limit=20, fields=None):
        record = self.service.resource('evidence', evidence_id, owner)
        try:
            c = checked_evidence(self.service, owner, record['run_id'], record)
        except Exception:
            raise ServiceError(409, '原始证据校验失败') from None
        if c['quality'] != 'available':
            raise ServiceError(422, '该证据尚未通过质量与口径验证')
        available = set(k for row in c['records'] for k in row)
        if fields and (len(fields) > 16 or any(f not in available for f in fields)):
            raise ServiceError(422, '字段不在证据契约中')
        selected = c['records'][offset:offset+limit]
        rows = [{k: r[k] for k in dict.fromkeys(fields + ['source_index']) if k in r} for r in selected] if fields else selected
        return {'evidence_id': evidence_id, 'quality': c['quality'], 'as_of': c['as_of'], 'fetched_at': c['fetched_at'],
            'units': c['units'], 'method': c['method'], 'offset': offset, 'records': rows,
            'total': len(c['records']), 'next_offset': offset + len(rows), 'sha256': record['content_sha256'],
            'raw_field_paths': c['field_map'], 'trust': 'external_untrusted'}
