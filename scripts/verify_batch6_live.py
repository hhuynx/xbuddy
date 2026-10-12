"""Real model/Fuyao acceptance with two process restarts and fresh-data follow-up.

Uses a unique private data directory. Only metadata is printed; source responses,
exports, credentials and the owned server's logs stay in ignored local folders.
"""
from __future__ import annotations

import csv
import io
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.storage.database import fingerprint

GOAL = ('比较贵州茅台600519.SH与五粮液000858.SZ最近60个交易日的前复权历史价格变化、'
        '年化波动率、最大回撤，并比较最近共同年报营业收入与归母净利润。'
        '仅描述历史与已披露财务，列出来源、时点、单位、口径和数据缺口。')
FOLLOWUP = ('重新获取600519.SH与000858.SZ最近30个交易日的前复权价格，'
            '比较历史价格变化、年化波动率和最大回撤，保留证据与缺口。')


def main():
    config = {**dotenv_values(ROOT / '.env'), **os.environ}
    if not all(config.get(k) for k in ('FUYAO_API_KEY', 'LLM_BASE_URL', 'LLM_MODEL')) or not (
            config.get('LLM_API_KEY') or config.get('LLM_ALLOW_NO_KEY') == 'true'):
        print(json.dumps({'status': 'blocked', 'reason': 'missing_model_or_fuyao_configuration'}))
        return 2
    tag = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    cache = ROOT / '.cache/verification' / ('batch6-live-' + tag)
    cache.mkdir(parents=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = {**os.environ, 'XBUDDY_HOST': '127.0.0.1', 'XBUDDY_PORT': str(port),
           'XBUDDY_DATA_DIR': str(cache / 'data'), 'XBUDDY_COOKIE_SECURE': 'false'}
    base = f'http://127.0.0.1:{port}'
    result = {'scope': 'batch6_real_model_fuyao', 'checked_at': datetime.now(timezone.utc).isoformat(), 'checks': {}}
    process = None
    log = (cache / 'server.log').open('w', encoding='utf-8')

    def check(value, name):
        if not value:
            raise AssertionError(name)
        if name not in result['checks']:
            print(json.dumps({'check': name, 'status': 'passed'}), flush=True)
        result['checks'][name] = 'passed'

    def stop(child):
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)

    def start():
        child = subprocess.Popen([sys.executable, '-m', 'server.main'], cwd=ROOT, env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 30
            with httpx.Client(timeout=2) as probe:
                while time.monotonic() < deadline:
                    if child.poll() is not None:
                        raise RuntimeError('owned_server_exited')
                    try:
                        if probe.get(base + '/health').status_code == 200:
                            return child
                    except httpx.TransportError:
                        pass
                    time.sleep(.2)
            raise TimeoutError('owned_server_start_timeout')
        except BaseException:
            stop(child)
            raise

    def get(client, path):
        response = client.get(path)
        response.raise_for_status()
        return response.json()

    def post(client, path, data=None):
        response = client.post(path, json=data)
        response.raise_for_status()
        return response.json()

    def wait(client, run_id, seconds=180):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            state = get(client, '/api/runs/' + run_id)
            if state['status'] in {'completed', 'partial', 'failed', 'blocked', 'recoverable_failed', 'awaiting_approval', 'awaiting_input', 'stopped'}:
                return state
            time.sleep(.25)
        raise TimeoutError('research_timeout')

    def approve(client, state):
        item = next(a for a in state['approvals'] if a['status'] == 'pending')
        content = item['content']
        body = {'decision': 'approve', 'plan_version': content['plan_version'],
                'parameters_fingerprint': content['parameters_fingerprint']}
        post(client, '/api/approvals/' + item['id'] + '/decision', body)
        check(client.post('/api/approvals/' + item['id'] + '/decision', json=body).status_code == 409,
              'approval_replay_rejected')

    def finish(client, run_id):
        for _ in range(8):
            state = wait(client, run_id)
            if state['status'] != 'awaiting_approval':
                check(state['status'] == 'completed', 'run_completed_' + ('followup' if state.get('parent') else 'parent'))
                return state
            approve(client, state)
        raise AssertionError('approval_cycle_limit')

    def validate_report(client, run_id):
        artifact = get(client, f'/api/runs/{run_id}/report')
        content = artifact['content']
        check(content['validation_status'] == 'passed' and content['status'] == 'complete', 'report_validation_' + str(artifact['version']))
        for source in content['sources']:
            evidence = get(client, '/api/evidence/' + source['evidence_id'])
            raw = get(client, '/api/evidence/' + source['evidence_id'] + '/raw')
            check(fingerprint(raw['body']) == raw['content_sha256'] and fingerprint(evidence['content']) == evidence['content_sha256'], 'raw_and_evidence_checksums')
        for claim in content['claims']:
            if claim['kind'] == 'fact':
                check(bool(claim['evidence_ids'] and claim.get('as_of') and claim.get('unit')), 'facts_have_sources_units_and_time')
        return artifact

    try:
        process = start()
        with httpx.Client(base_url=base, timeout=12) as client:
            client.headers['X-CSRF-Token'] = get(client, '/api/session')['csrf_token']
            thread = post(client, '/api/threads', {'title': '第六批真实整体验收', 'goal': GOAL})
            memory = post(client, '/api/memories', {'content': '优先前复权历史价格；清楚列出单位与缺口'})
            parent = post(client, f"/api/threads/{thread['id']}/runs", {'goal': GOAL, 'mode': 'agent', 'budget': {'tool_attempts': 1}})['id']
            state = wait(client, parent)
            check(state['status'] == 'awaiting_approval' and state['attempts_used'] == 1, 'budget_blocks_next_dispatch')
            first_call, first_pid = state['tool_calls'][0]['id'], process.pid
            models_before = state['runtime']['model_calls']
            stop(process)
            process = start()
            restored = get(client, '/api/runs/' + parent)
            check(process.pid != first_pid and restored['status'] == 'awaiting_approval', 'approval_survives_process_restart')
            approve(client, restored)
            finished = finish(client, parent)
            check(finished['runtime']['model_calls'] == models_before + 1 and sum(c['id'] == first_call for c in finished['tool_calls']) == 1,
                  'finished_calls_and_model_ledger_reused')
            artifact = validate_report(client, parent)
            check({'calculate_price_comparison', 'calculate_financial_comparison'} <= {c['tool'] for c in finished['tool_calls'] if c['quality'] == 'available'}, 'both_financial_calculations')
            index = next(i for i, c in enumerate(artifact['content']['claims']) if c['kind'] == 'fact')
            fact = post(client, '/api/memories', {'kind': 'research_fact', 'source_artifact_id': artifact['id'], 'claim_index': index})
            version2 = post(client, f'/api/runs/{parent}/reports', {'expected_version': 1, 'note': '第六批重新校验来源'})
            check(version2['version'] == 2 and version2['content']['previous_artifact_id'] == artifact['id'], 'immutable_report_versions')
            for format_, extension in [('markdown', 'md'), ('html', 'html'), ('csv', 'csv'), ('evidence', 'json')]:
                response = client.get(f"/api/artifacts/{version2['id']}/export", params={'format': format_})
                response.raise_for_status()
                (cache / ('report.' + extension)).write_bytes(response.content)
                check(bool(response.content), 'export_' + extension)
                if format_ == 'csv':
                    rows = list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
                    check(len(rows) == len(version2['content']['claims']) and all(
                        row['value'] == ('' if claim.get('value') is None else str(claim['value']))
                        and json.loads(row['evidence_ids']) == claim['evidence_ids']
                        for row, claim in zip(rows, version2['content']['claims'])), 'csv_exact_values_and_refs')
                if format_ == 'evidence':
                    check(response.json()['report_sha256'] == fingerprint(version2['content']), 'export_binds_report_checksum')
            child = post(client, f'/api/runs/{parent}/followups', {'goal': FOLLOWUP, 'parent_artifact_id': version2['id'],
                         'budget': {'context_tokens': 2000}})['id']
            awaiting = wait(client, child)
            check(awaiting['status'] == 'awaiting_approval' and awaiting['runtime']['model_calls'] == 0, 'context_budget_blocks_model_dispatch')
            deleted = client.delete('/api/memories/' + memory['id'], params={'expected_version': 1})
            deleted.raise_for_status()
            first_pid = process.pid
            stop(process)
            process = start()
            awaiting = get(client, '/api/runs/' + child)
            check(process.pid != first_pid and awaiting['status'] == 'awaiting_approval', 'context_approval_survives_restart')
            approve(client, awaiting)
            followed = finish(client, child)
            followup = validate_report(client, child)
            old_ids = {e['id'] for e in get(client, f'/api/runs/{parent}/evidence')['evidence']}
            new_ids = {e['id'] for e in get(client, f'/api/runs/{child}/evidence')['evidence']}
            check(bool(new_ids) and not old_ids.intersection(new_ids), 'followup_fetches_new_evidence')
            context = get(client, f'/api/runs/{child}/context')
            check(any(m['memory_id'] == memory['id'] and m['status'] == 'withdrawn' for m in context['memories']), 'deleted_memory_withdrawn_after_restart')
            check(any(m['memory_id'] == fact['id'] and m['status'] == 'used' for m in context['memories']), 'valid_historical_fact_used')
            compressed = next(s for s in context['snapshots'] if s['compressed'])
            snapshot = get(client, '/api/context/' + compressed['id'])
            check(compressed['before_tokens'] > compressed['after_tokens'] and snapshot['ranges'] and
                  fingerprint(snapshot['original']['protected_state']) == fingerprint(snapshot['sent']['protected_state']), 'audited_compaction_preserves_state')
            events = get(client, f'/api/runs/{child}/events?limit=500')['events']
            after = events[len(events)//2]['id']
            streamed = client.get(f'/api/runs/{child}/events/stream', headers={'Last-Event-ID': str(after)})
            streamed.raise_for_status()
            ids = [int(line[4:]) for line in streamed.text.splitlines() if line.startswith('id: ')]
            check(ids == [e['id'] for e in events if e['id'] > after], 'sse_replays_exact_stored_events')
            # Persist only private browser handoff IDs/cookie state; never put tokens in public metadata.
            (cache / 'browser-handoff.json').write_text(json.dumps({'parent': parent, 'child': child, 'thread': thread['id'],
                'artifact': version2['id'], 'cookies': dict(client.cookies)}), encoding='utf-8')
            result.update(status='passed', parent_tool_attempts=finished['attempts_used'], parent_model_calls=finished['runtime']['model_calls'],
                followup_tool_attempts=followed['attempts_used'], followup_model_calls=followed['runtime']['model_calls'],
                parent_sources=len(artifact['content']['sources']), parent_claims=len(artifact['content']['claims']),
                followup_sources=len(followup['content']['sources']), followup_claims=len(followup['content']['claims']),
                model_tokens=finished['runtime']['model_tokens'] + followed['runtime']['model_tokens'], cost_status='unknown')
    except Exception as error:
        result.update(status='failed', reason=type(error).__name__)
    finally:
        stop(process)
        log.close()
    target = ROOT / 'docs/verification/batch6-live.local.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'passed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
