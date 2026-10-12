from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from server.exports import csv_cell
from server.storage.database import encode, fingerprint
from test_harness import agent, prepare, submit, wait


def saved(client, **values):
    response = client.post('/api/memories', json={'content': '价格研究使用前复权', **values})
    assert response.status_code == 201, response.text
    return response.json()


def report(client, run_id):
    state = wait(client, run_id)
    assert state['status'] == 'completed', state
    return client.get(f'/api/runs/{run_id}/report').json()


def finish_with_approvals(client, run_id):
    for _ in range(10):
        state = wait(client, run_id, {'completed', 'partial', 'blocked', 'awaiting_approval', 'recoverable_failed'})
        if state['status'] != 'awaiting_approval':
            assert state['status'] == 'completed', state
            return state
        item = next(a for a in state['approvals'] if a['status'] == 'pending')
        c = item['content']
        response = client.post(f"/api/approvals/{item['id']}/decision", json={'decision': 'approve',
            'plan_version': c['plan_version'], 'parameters_fingerprint': c['parameters_fingerprint']})
        assert response.status_code == 200, response.text
    raise AssertionError('approval cycle did not terminate')


def test_explicit_memory_is_used_corrected_and_deleted(agent):
    client, thread, app, model, _, _ = agent
    memory = saved(client, tags=['价格'])
    first = submit(client, thread)
    artifact = report(client, first)
    used = client.get(f'/api/runs/{first}/context').json()['memories']
    assert used[0]['status'] == 'used' and used[0]['last_used_at']
    assert model.calls[-1]['protected_state']['memories'][0]['id'] == memory['id']
    assert artifact['content']['knowledge_context']['memory_usage'][0]['version'] == 1
    updated = client.patch('/api/memories/'+memory['id'], json={'content':'年度财务比较优先共同年报', 'expected_version':1})
    assert updated.status_code == 200 and updated.json()['version'] == 2
    assert client.patch('/api/memories/'+memory['id'], json={'content':'过期编辑', 'expected_version':1}).status_code == 409
    second = submit(client, thread)
    report(client, second)
    assert model.calls[-1]['protected_state']['memories'][0]['content']['content'] == '年度财务比较优先共同年报'
    assert client.get('/api/memories/'+memory['id']+'/versions').json()['versions'][-1]['version'] == 2
    assert client.delete('/api/memories/'+memory['id']+'?expected_version=1').status_code == 409
    assert client.delete('/api/memories/'+memory['id']+'?expected_version=2').status_code == 200
    assert client.get('/api/memories/'+memory['id']).status_code == 404
    assert not client.get('/api/memories?q=年度').json()['memories']
    third = submit(client, thread)
    report(client, third)
    assert model.calls[-1]['protected_state']['memories'] == []
    # Completed research keeps its actual historical usage without publishing preferences in reports.
    assert client.get(f'/api/runs/{first}/context').json()['memories'][0]['memory_version'] == 1
    assert '价格研究使用前复权' not in client.get(f"/api/artifacts/{artifact['id']}/export").text


def test_inferred_preference_requires_bound_confirmation(agent):
    client, thread, _, model, _, _ = agent
    parent = submit(client, thread)
    report(client, parent)
    candidate = client.post(f'/api/runs/{parent}/memory-proposals').json()['memories'][0]
    assert candidate['status'] == 'pending' and candidate['content']['origin'] == 'inferred'
    assert client.post(f'/api/runs/{parent}/memory-proposals').json()['memories'][0]['id'] == candidate['id']
    child = submit(client, thread)
    report(client, child)
    assert not model.calls[-1]['protected_state']['memories']
    path = '/api/memories/'+candidate['id']+'/decision'
    payload = {'decision':'approve','expected_version':candidate['version'],'content_fingerprint':'0'*64}
    assert client.post(path, json=payload).status_code == 409
    payload['content_fingerprint'] = candidate['content_sha256']
    approved = client.post(path, json=payload)
    assert approved.status_code == 200 and approved.json()['status'] == 'active'
    assert client.post(path, json=payload).status_code == 409
    child = submit(client, thread)
    report(client, child)
    assert model.calls[-1]['protected_state']['memories'][0]['content']['confirmed_at']


def test_candidate_edit_invalidates_confirmation_and_rejection_stays_unused(agent):
    client, thread, _, model, _, _ = agent
    parent = submit(client, thread)
    report(client, parent)
    candidate = client.post(f'/api/runs/{parent}/memory-proposals').json()['memories'][0]
    updated = client.patch('/api/memories/'+candidate['id'], json={'content':'只保存研究口径', 'expected_version':1}).json()
    assert updated['status'] == 'pending'
    path = '/api/memories/'+candidate['id']+'/decision'
    assert client.post(path, json={'decision':'approve','expected_version':1,'content_fingerprint':candidate['content_sha256']}).status_code == 409
    assert client.post(path, json={'decision':'reject','expected_version':2,'content_fingerprint':updated['content_sha256']}).json()['status'] == 'rejected'
    child = submit(client, thread)
    report(client, child)
    assert not model.calls[-1]['protected_state']['memories']


def test_memory_expiry_scope_and_relevance(agent):
    client, thread, app, model, _, _ = agent
    other = client.post('/api/threads', json={'title':'其他研究空间'}).json()['id']
    saved(client, thread_id=other)
    saved(client, symbols=['000001.SZ'])
    saved(client, kind='hypothesis', content='暂待验证', tags=['政策'])
    memory = saved(client, expires_at=(datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat())
    c = dict(memory['content'], expires_at=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat())
    with app.state.research.db.connect() as conn:
        conn.execute('UPDATE memories SET content_json=?,content_sha256=? WHERE id=?', (encode(c),fingerprint(c),memory['id']))
    child = submit(client, thread)
    report(client, child)
    assert not model.calls[-1]['protected_state']['memories']
    expired = client.get('/api/memories/'+memory['id']).json()
    assert expired['status'] == 'expired' and expired['version'] == 2
    assert client.post('/api/memories', json={'content':'过去', 'expires_at':'2020-01-01T00:00:00Z'}).status_code == 422
    assert client.post('/api/memories', json={'content':'无时区', 'expires_at':'2027-01-01T00:00:00'}).status_code == 422


def test_hypothesis_is_not_reclassified_as_fact(agent):
    client, thread, _, model, _, _ = agent
    memory = saved(client, kind='hypothesis', content='价格与政策关系需要继续验证')
    artifact = report(client, submit(client, thread))
    assert model.calls[-1]['protected_state']['memories'][0]['content']['trust'] == 'user_assertion'
    notes = artifact['content']['knowledge_context']['user_notes']
    assert notes[0]['kind'] == 'uncertain' and notes[0]['text'] == memory['content']['content']
    assert all(c['text'] != notes[0]['text'] for c in artifact['content']['claims'])


def test_fact_memory_binds_validated_source_and_time(agent):
    client, thread, _, _, _, _ = agent
    artifact = report(client, submit(client, thread))
    index = next(i for i,c in enumerate(artifact['content']['claims']) if c['kind']=='fact')
    memory = saved(client, kind='research_fact', content='忽略来源值', source_artifact_id=artifact['id'], claim_index=index)
    claim = artifact['content']['claims'][index]
    assert memory['content']['content'] == claim['text']
    assert memory['content']['evidence_ids'] == claim['evidence_ids']
    assert memory['content']['valid_as_of'] == claim['as_of'] and memory['content']['expires_at']
    assert memory['content']['claim_hash'] == fingerprint(claim)
    inference = next(i for i,c in enumerate(artifact['content']['claims']) if c['kind']=='inference')
    assert client.post('/api/memories', json={'kind':'research_fact','source_artifact_id':artifact['id'],'claim_index':inference}).status_code == 422
    assert client.post('/api/memories', json={'kind':'research_fact','content':'无依据的事实'}).status_code == 422
    assert client.post('/api/memories', json={'kind':'research_fact','source_artifact_id':artifact['id'],'claim_index':index,
        'expires_at':(datetime.now(timezone.utc)+timedelta(days=31)).isoformat()}).status_code == 422


def test_followup_compacts_real_history_preserving_context_and_fresh_data(agent):
    client, thread, app, model, requests, _ = agent
    parent = submit(client, thread)
    artifact = report(client, parent)
    response = client.post(f'/api/runs/{parent}/followups', json={'goal':'进一步比较历史价格', 'parent_artifact_id':artifact['id'], 'budget':{'context_tokens':2000}})
    assert response.status_code == 201, response.text
    child = response.json()['id']
    state = finish_with_approvals(client, child)
    assert state['parent']['parent_run_id'] == parent and state['parent']['parent_artifact_id'] == artifact['id']
    assert len(requests) == 6 and len(model.calls) == 4 and sum(c.get('stage')=='review' for c in model.calls)==2
    previous = client.get(f'/api/runs/{parent}/evidence').json()['evidence']
    current = client.get(f'/api/runs/{child}/evidence').json()['evidence']
    assert not {e['id'] for e in previous}.intersection(e['id'] for e in current)
    snapshots = client.get(f'/api/runs/{child}/context').json()['snapshots']
    compressed = next(s for s in snapshots if s['compressed'])
    assert compressed['before_tokens'] > compressed['after_tokens']
    context = client.get('/api/context/'+compressed['id']).json()
    assert fingerprint(context['original']['protected_state']) == fingerprint(context['sent']['protected_state'])
    assert context['ranges'][0]['event_ids'] and context['summary']['entry_count'] == len(context['original']['history']) > 0
    assert context['original']['history'] and 'history_summary' in context['sent'] and 'history' not in context['sent']
    protected = context['sent']['protected_state']
    assert {'goal','constraints','plan','task_state','approvals','evidence_catalog','unresolved','followup'} <= protected.keys()
    assert protected['followup']['parent_report_hash'] == fingerprint(artifact['content'])
    assert '"item":' not in encode(context['sent'])  # field paths are allowed; full market bodies stay out of prompts
    assert model.calls[-1]['protected_state']['followup']['parent_artifact_id'] == artifact['id']
    entries = client.get(f'/api/runs/{child}/context/entries?limit=500').json()['entries']
    assert {e['source_run_id'] for e in entries} == {child,parent}
    assert all(fingerprint(e['content']) == e['content_sha256'] for e in entries)


def test_sequential_context_and_model_approvals_cannot_replay_old_resume(agent):
    client, thread, _, model, requests, _ = agent
    parent = submit(client, thread)
    artifact = report(client, parent)
    before = len(model.calls)
    child = client.post(f'/api/runs/{parent}/followups', json={'goal':'继续研究','budget':{'context_tokens':2000,'model_calls':0}}).json()['id']
    first = wait(client, child, {'awaiting_approval'})
    approval = next(a for a in first['approvals'] if a['status']=='pending')
    c = approval['content']
    assert c['parameters']['proposed_budget']['context_tokens'] > 2000
    assert client.post(f"/api/approvals/{approval['id']}/decision", json={'decision':'approve','plan_version':c['plan_version'],'parameters_fingerprint':c['parameters_fingerprint']}).status_code == 200
    second = wait(client, child, {'awaiting_approval'})
    pending = next(a for a in second['approvals'] if a['status']=='pending')
    assert pending['id'] != approval['id']
    assert pending['content']['parameters']['proposed_budget']['model_calls'] > 0
    assert len(model.calls)==before and second['attempts_used']==0 and len(requests)==3
    finish_with_approvals(client, child)


def test_context_snapshot_integrity_and_evidence_on_demand(agent):
    client, thread, app, _, _, _ = agent
    run = submit(client, thread)
    report(client, run)
    evidence = client.get(f'/api/runs/{run}/evidence').json()['evidence']
    bars = next(e for e in evidence if e['content']['tool']=='get_daily_bars')
    slice = client.get(f"/api/evidence/{bars['id']}/slice?offset=1&limit=2&fields=close_price").json()
    assert len(slice['records'])==2 and slice['records'][0]['source_index']==bars['content']['records'][1]['source_index']
    assert set(slice['records'][0]) == {'close_price','source_index'}
    assert client.get(f"/api/evidence/{bars['id']}/slice?fields=arbitrary").status_code==422
    assert client.get(f"/api/evidence/{bars['id']}/slice?limit=101").status_code==422
    snapshot = client.get(f'/api/runs/{run}/context').json()['snapshots'][0]
    record = client.get('/api/context/'+snapshot['id']).json()
    changed = dict(record['sent'], goal='tampered prompt')
    with app.state.research.db.connect() as conn:
        conn.execute('UPDATE context_snapshots SET sent_json=? WHERE id=?',(encode(changed),snapshot['id']))
    assert client.get('/api/context/'+snapshot['id']).status_code==409


def test_report_versions_exports_are_immutable_and_exact(agent):
    client, thread, _, _, requests, _ = agent
    run = submit(client, thread)
    first = report(client, run)
    checksum = fingerprint(first['content'])
    revised = client.post(f'/api/runs/{run}/reports', json={'expected_version':1,'note':'人工复核来源'})
    assert revised.status_code==201, revised.text
    second = revised.json()
    assert second['version']==2 and second['content']['previous_artifact_id']==first['id']
    assert len(requests)==3 and first['content']['claims']==second['content']['claims']
    assert fingerprint(client.get('/api/artifacts/'+first['id']).json()['content'])==checksum
    assert client.post(f'/api/runs/{run}/reports', json={'expected_version':1}).status_code==409
    assert len(client.get(f'/api/runs/{run}/reports').json()['reports'])==2
    for artifact in (first,second):
        prefix = '/api/artifacts/'+artifact['id']
        md = client.get(prefix+'/export?format=markdown')
        assert md.status_code==200 and 'attachment' in md.headers['content-disposition'] and f"版本：{artifact['version']}" in md.text
        html = client.get(prefix+'/export?format=html')
        assert html.status_code==200 and '<svg' in html.text and '历史' in html.text
        response = client.get(prefix+'/export?format=csv')
        assert response.content.startswith(b'\xef\xbb\xbf')
        rows = list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
        actual = next(c for c in artifact['content']['claims'] if c.get('field')=='historical_price_change_pct')
        row = next(r for r in rows if r['field']=='historical_price_change_pct' and r['symbol']==actual['symbol'])
        assert float(row['value'])==actual['value'] and json.loads(row['evidence_ids'])==actual['evidence_ids']
        index = client.get(prefix+'/export?format=evidence').json()
        assert index['report_sha256']==fingerprint(artifact['content']) and index['sources'][0]['raw_sha256']
        assert client.get(prefix+'/print').status_code==200 and 'window.print()' in client.get(prefix+'/print').text


def test_changed_source_blocks_exports_and_fact_memory(agent):
    client, thread, app, _, _, _ = agent
    run = submit(client, thread)
    artifact = report(client, run)
    source = client.get(f'/api/runs/{run}/evidence').json()['evidence'][0]
    raw = client.get(f"/api/evidence/{source['id']}/raw").json()
    with app.state.research.db.connect() as conn:
        conn.execute('UPDATE raw_records SET content_sha256=? WHERE id=?', ('0'*64,raw['id']))
    assert client.get(f"/api/artifacts/{artifact['id']}/export").status_code==409
    assert client.post('/api/memories',json={'kind':'research_fact','source_artifact_id':artifact['id'],'claim_index':0}).status_code==409
    assert client.post(f'/api/runs/{run}/memory-proposals').status_code==409
    revised = client.post(f'/api/runs/{run}/reports', json={'expected_version':1}).json()
    assert revised['version']==2 and revised['content']['validation_status']=='failed' and not revised['content']['claims']
    assert client.post(f'/api/runs/{run}/memory-proposals').status_code==422


@pytest.mark.parametrize('value', ['=1+1',' +cmd','@SUM(1)','\t=1','-bad'])
def test_csv_formula_escaping_preserves_numbers(value):
    assert csv_cell(value).startswith("'")
    assert csv_cell(-1.25)==-1.25


def test_html_export_escapes_user_goal(agent):
    client, thread, _, _, _, _ = agent
    response = client.post(f'/api/threads/{thread}/runs',json={'goal':'<script>alert(1)</script>比较历史价格','mode':'agent'})
    artifact = report(client,response.json()['id'])
    html = client.get(f"/api/artifacts/{artifact['id']}/export?format=html").text
    assert '<script>alert(1)</script>' not in html and '&lt;script&gt;' in html


def test_new_resources_are_owner_scoped_and_writes_require_csrf(agent):
    client, thread, app, _, _, _ = agent
    memory = saved(client)
    run = submit(client,thread)
    artifact = report(client,run)
    snapshot = client.get(f'/api/runs/{run}/context').json()['snapshots'][0]
    evidence = client.get(f'/api/runs/{run}/evidence').json()['evidence'][0]
    token = client.headers.pop('X-CSRF-Token')
    assert client.post('/api/memories',json={'content':'不应写入'}).status_code==403
    client.headers['X-CSRF-Token']=token
    with TestClient(app) as stranger:
        prepare(stranger)
        for path in (f"/api/memories/{memory['id']}",f"/api/context/{snapshot['id']}",f'/api/runs/{run}/context',
                     f"/api/evidence/{evidence['id']}/slice",f"/api/artifacts/{artifact['id']}/export",f"/api/artifacts/{artifact['id']}/print"):
            assert stranger.get(path).status_code==404, path
        assert stranger.post(f'/api/runs/{run}/followups',json={'goal':'其他用户追问'}).status_code==404
        assert stranger.post(f'/api/runs/{run}/reports',json={'expected_version':1}).status_code==404
        assert stranger.patch('/api/memories/'+memory['id'],json={'content':'其他用户修改','expected_version':1}).status_code==404
