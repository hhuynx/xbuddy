"""Conversation removal must stop execution, isolate owners and preserve audit data."""
from fastapi.testclient import TestClient
from test_agent_core import core, entry, start
from test_harness import wait


def test_delete_completed_conversation_hides_resources_and_survives_reload(core):
    client, thread, app, model, *_ = core
    model.script = lambda c, n: entry('answer', answer='研究可从商业模式、财务质量及风险开始。')
    run = start(client, thread, '研究方法')
    done = wait(client, run)
    artifact = done['artifacts'][0]['id']
    other = client.post('/api/threads', json={'title': '保留对话', 'goal': ''}).json()['id']
    assert client.delete(f'/api/threads/{thread}').status_code == 200
    assert [t['id'] for t in client.get('/api/threads').json()['threads']] == [other]
    for path in (f'/api/threads/{thread}', f'/api/threads/{thread}/messages', f'/api/runs/{run}', f'/api/artifacts/{artifact}'):
        assert client.get(path).status_code == 404
    assert client.post(f'/api/threads/{thread}/runs', json={'mode': 'manual', 'goal': '新任务'}).status_code == 404
    assert client.delete(f'/api/threads/{thread}').status_code == 200
    assert app.state.research.db.one('SELECT id FROM artifacts WHERE id=?', (artifact,))
    assert app.state.research.db.one('SELECT status FROM runs WHERE id=?', (run,))['status'] == 'completed'


def test_delete_waiting_conversation_stops_run_and_expires_pending_input(core):
    client, thread, app, model, requests, _ = core
    model.script = lambda c, n: entry('clarify', question={'text': '研究哪只股票？', 'options': ['600519.SH']})
    run = start(client, thread, '研究股票')
    waiting = wait(client, run, {'awaiting_input'})
    assert client.delete(f'/api/threads/{thread}').status_code == 200
    db = app.state.research.db
    assert db.one('SELECT status FROM runs WHERE id=?', (run,))['status'] == 'stopped'
    assert db.one('SELECT status FROM run_inputs WHERE id=?', (waiting['pending_input']['id'],))['status'] == 'expired'
    assert client.post(f'/api/runs/{run}/control/resume').status_code in {404, 409}
    assert client.post(f'/api/runs/{run}/input', json={'question_id': waiting['pending_input']['id'], 'answer': '600519.SH'}).status_code in {404, 409}
    assert not requests


def test_delete_requires_owner_and_csrf(core):
    client, thread, app, *_ = core
    with TestClient(app) as foreign:
        csrf = foreign.get('/api/session').json()['csrf_token']
        assert foreign.delete(f'/api/threads/{thread}', headers={'X-CSRF-Token': csrf}).status_code == 404
    assert client.delete(f'/api/threads/{thread}', headers={'X-CSRF-Token': 'invalid'}).status_code == 403
    assert client.get(f'/api/threads/{thread}').status_code == 200
