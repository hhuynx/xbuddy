"""Immutable report versions, provenance validation and linked follow-up research."""
from __future__ import annotations

from server.harness.reporting import build_report, safe_text, save_report
from server.models import FollowUpCreate, ReportRevision
from server.services import ServiceError, view
from server.storage.database import encode, fingerprint, now
from server.publication import artifact_view, publication_errors


class ArtifactService:
    def __init__(self, service):
        self.service, self.db = service, service.db

    def versions(self, owner, run_id):
        self.service.resource('runs', run_id, owner)
        return [artifact_view(view(r)) for r in self.db.all("SELECT * FROM artifacts WHERE run_id=? AND session_id=? AND kind='research_report' ORDER BY version", (run_id, owner))]

    def latest(self, owner, run_id):
        items = self.versions(owner, run_id)
        if not items:
            raise ServiceError(404, '当前运行尚无报告')
        return items[-1]

    def verify(self, owner, artifact_id):
        artifact = self.service.resource('artifacts', artifact_id, owner)
        if artifact['kind'] != 'research_report':
            raise ServiceError(422, '该产物不是研究报告')
        report = artifact['content']
        checksum = self.db.one('SELECT * FROM artifact_checksums WHERE artifact_id=?', (artifact_id,))
        if checksum and fingerprint(report) != checksum['content_sha256']:
            raise ServiceError(409, '报告内容指纹校验失败')
        if publication_errors(report):
            raise ServiceError(409, '报告未通过当前发布校验，请保存新的校验版本')
        if report['validation_status'] == 'passed':
            current = build_report(self.service, owner, artifact['run_id'])
            keys = ('goal','plan_version','claims','sources','computations','gaps','limitations','narrative','goal_coverage','answer_mode','execution_issues','execution_failures')
            if current['validation_status'] != 'passed' or any(fingerprint(report.get(k)) != fingerprint(current.get(k)) for k in keys if k in report):
                raise ServiceError(409, '报告依据已变化或无法重新核验，请保存新的校验版本')
        elif report.get('claims'):
            raise ServiceError(409, '未通过验证的报告包含核心结论')
        if not checksum:
            with self.db.connect() as conn:
                conn.execute('INSERT OR IGNORE INTO artifact_checksums VALUES(?,?,?)', (artifact_id, fingerprint(report), now()))
        return artifact

    def revise(self, owner, run_id, payload: ReportRevision):
        run = self.service.resource('runs', run_id, owner)
        if run['mode'] != 'agent' or run['status'] not in {'completed','partial','blocked','failed'}:
            raise ServiceError(409, '请等待研究结束后重新核验报告')
        if not safe_text(payload.note):
            raise ServiceError(422, '版本说明包含不支持的投资表达')
        report = build_report(self.service, owner, run_id)
        artifact_id = save_report(self.service, owner, run_id, report, new_version=True, expected_version=payload.expected_version, note=payload.note)
        return self.service.resource('artifacts', artifact_id, owner)

    def followup(self, owner, run_id, payload: FollowUpCreate):
        parent = self.service.resource('runs', run_id, owner)
        artifact = self.verify(owner, payload.parent_artifact_id) if payload.parent_artifact_id else self.verify(owner, self.latest(owner, run_id)['id'])
        if artifact['run_id'] != run_id or parent['status'] not in {'completed','partial','blocked','stopped'}:
            raise ServiceError(409, '追问必须关联本次已结束运行的报告')
        previous = self.db.one('SELECT content_json FROM run_links WHERE run_id=?', (run_id,))
        import json
        basis_goal = json.loads(previous['content_json']).get('basis_goal', parent['goal']) if previous else parent['goal']
        report = artifact['content']
        context = {'parent_run_id': run_id, 'parent_artifact_id': artifact['id'], 'parent_version': artifact['version'],
            'parent_goal': parent['goal'], 'basis_goal': basis_goal, 'parent_report_hash': fingerprint(report),
            'previous_status': report['status'], 'previous_claims': report['claims'], 'previous_gaps': report['gaps'],
            'previous_limitations': report['limitations'], 'data_policy': 'historical_reference_only; fetch_new_data_in_new_run'}
        run = self.service.harness.submit(owner, parent['thread_id'], payload.goal, payload.budget, followup=context)
        return run
