"""History pagination must retain owner isolation, integrity and model context bounds."""
from fastapi.testclient import TestClient
from test_agent_core import core


def seed(core,count=75):
    client,thread,app,*_=core
    run=client.post(f'/api/threads/{thread}/runs',json={'goal':'分页研究','mode':'manual'}).json()['id']
    service=app.state.research
    record=service.db.one('SELECT * FROM runs WHERE id=?',(run,))
    with service.db.connect() as conn:
        for i in range(count):service.dialogue.add_message(conn,record,'user' if i%2==0 else 'assistant',f'seed:{i}',f'历史消息 {i}')
    return client,thread,service,record


def test_history_pages_cover_all_messages_without_overlap_and_preserve_model_limit(core):
    client,thread,service,run=seed(core)
    first=client.get(f'/api/threads/{thread}/messages?limit=20').json()
    assert len(first['messages'])==20 and first['has_more']
    collected=first['messages']
    page=first
    while page['has_more']:
        page=client.get(f'/api/threads/{thread}/messages',params={'limit':20,'before':page['before']}).json()
        assert all(m['id']<collected[0]['id'] for m in page['messages'])
        collected=page['messages']+collected
    assert len(collected)==75 and len({m['id'] for m in collected})==75
    assert [m['content']['text'] for m in collected]==[f'历史消息 {i}' for i in range(75)]
    assert len(service.dialogue.messages(run['session_id'],thread,run['id']))==60


def test_history_pagination_rejects_foreign_owner_invalid_bounds_and_corrupt_message(core):
    client,thread,service,run=seed(core,3)
    with TestClient(core[2]) as other:
        other.get('/api/session')
        assert other.get(f'/api/threads/{thread}/messages?limit=1').status_code==404
    for query in ('limit=0','limit=61','before=0','before=-1'):
        assert client.get(f'/api/threads/{thread}/messages?{query}').status_code==422
    with service.db.connect() as conn:
        conn.execute("UPDATE thread_messages SET content_json='{}' WHERE thread_id=?",(thread,))
    assert client.get(f'/api/threads/{thread}/messages').status_code==409
