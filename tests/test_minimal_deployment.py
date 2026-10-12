"""Backend contracts run locally and, when configured, against isolated PostgreSQL schemas."""
import asyncio
import os
import time
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from server.api.routes import create_api
from server.config import Settings
from server.dispatch import Dispatch
from server.storage.database import encode, fingerprint
from server.storage.postgres import translate
from test_agent_core import ScriptModel, entry, finish, start
from test_data_foundation import response_for
from test_harness import prepare, wait, price_plan


@pytest.fixture(params=['sqlite','postgres'])
def stack(request,tmp_path):
    url = ''
    schema = 'xbuddy_qa_'+uuid4().hex
    base = os.getenv('XBUDDY_TEST_DATABASE_URL','')
    if request.param == 'postgres':
        if not base:
            pytest.skip('Set XBUDDY_TEST_DATABASE_URL for real PostgreSQL contract checks')
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo
        with psycopg.connect(base,autocommit=True) as conn:
            conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        url = make_conninfo(base,options='-csearch_path='+schema)
    model = ScriptModel(lambda c,n:finish(c) if c.get('stage')=='review' else price_plan())
    overlap = {'active':0,'max':0,'requests':0}
    async def transport(request):
        overlap['active'] += 1
        overlap['max'] = max(overlap['max'],overlap['active'])
        overlap['requests'] += 1
        await asyncio.sleep(0.1)
        overlap['active'] -= 1
        return httpx.Response(200,json=response_for(request))
    settings = Settings(data_dir=tmp_path,database_url=url,embedded_worker=False,fuyao_api_key='synthetic-secret')
    web = create_api(settings,httpx.MockTransport(transport),model=model)
    worker = create_api(replace(settings,embedded_worker=True),httpx.MockTransport(transport),model=model)
    try:
        with TestClient(web) as client:
            thread = prepare(client)
            yield client,thread,web,worker,model,overlap
    finally:
        if url:
            with psycopg.connect(base,autocommit=True) as conn:
                conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_deleted_conversation_never_dispatches_after_worker_restart(stack):
    client, thread, web, worker, model, overlap = stack
    run = start(client, thread, '待执行研究')
    assert client.get('/api/runs/'+run).json()['status'] == 'queued'
    assert client.delete('/api/threads/'+thread).status_code == 200
    assert client.get('/api/threads').json()['threads'] == []
    assert client.get('/api/runs/'+run).status_code == 404
    assert web.state.research.db.one('SELECT status FROM runs WHERE id=?', (run,))['status'] == 'stopped'
    with TestClient(worker):
        time.sleep(0.6)
        assert not model.calls and overlap['requests'] == 0
    assert client.delete('/api/threads/'+thread).status_code == 200


def payload(text='公司面临行业竞争风险，拟加强研发和渠道建设。'):
    return {'title':'合成测试年报','source_url':'https://example.org/annual-report',
        'published_at':'2026-03-31T09:00:00+08:00','text':text}


def test_split_worker_parallel_trace_and_idempotence(stack):
    client,thread,web,worker,model,overlap = stack
    run = start(client,thread,'比较历史价格')
    time.sleep(0.2)
    assert client.get('/api/runs/'+run).json()['status']=='queued'
    assert web.state.harness.scheduler is None and not model.calls
    with TestClient(worker):
        state = wait(client,run,timeout=40)
        assert state['status']=='completed', state
        assert overlap['max']==2 and overlap['requests']==3
        assert state['attempts_used']==4
        trace = client.get(f'/api/runs/{run}/trace').json()
        assert {'model','tool'} <= {s['name'] for s in trace['spans']}
        assert len({s['trace_id'] for s in trace['spans']})==1
        assert trace['model_usage'] and 'synthetic-secret' not in encode(trace)
        assert all(s['elapsed_ms'] >= 0 for s in trace['spans'])
        before = overlap['requests']
    with TestClient(worker):
        time.sleep(0.3)
        assert overlap['requests']==before
    outbox = web.state.research.db.all('SELECT * FROM dispatch_outbox WHERE run_id=?',(run,))
    assert outbox


def test_document_rag_report_and_source_integrity(stack):
    client,thread,web,worker,model,_ = stack
    imported = client.post('/api/documents',json=payload())
    assert imported.status_code==201, imported.text
    assert client.post('/api/documents',json=payload()).json()['reused']
    found = client.get('/api/documents/search',params={'query':'行业竞争风险'}).json()['excerpts']
    assert found[0]['text']==payload()['text'] and found[0]['page']==1
    def script(c,n):
        if c.get('stage')=='review':return finish(c)
        return entry('research',tasks=[{'id':'original','title':'检索风险原文','tool':'search_documents',
            'arguments':{'query':'行业竞争风险'},'depends_on':[]}],
            objectives=[{'id':'risk','text':'查阅风险原文','required_fields':['document_excerpt']}])
    model.script = script
    run = start(client,thread,'基于导入年报整理行业竞争风险，保留原文出处')
    with TestClient(worker):
        assert wait(client,run,timeout=40)['status']=='completed'
    artifact = client.get(f'/api/runs/{run}/report').json()
    report = artifact['content']
    claim = next(c for c in report['claims'] if c.get('field')=='document_excerpt')
    assert claim['reference']['source_url']==payload()['source_url'] and claim['unit']=='原文'
    assert claim['value']==payload()['text'] and claim['limitations']
    owner = web.state.research.db.one('SELECT session_id FROM runs WHERE id=?',(run,))['session_id']
    with TestClient(create_api(Settings(data_dir=web.state.research.settings.data_dir,database_url=web.state.research.settings.database_url,embedded_worker=False))) as stranger:
        prepare(stranger)
        assert not stranger.get('/api/documents').json()['documents']
        assert stranger.get(f'/api/runs/{run}/trace').status_code==404
    with web.state.research.db.connect() as conn:
        conn.execute('UPDATE document_chunks SET text=? WHERE id=?',('被篡改',found[0]['chunk_id']))
    assert client.get('/api/documents/search',params={'query':'被篡改'}).status_code==409
    # Existing report cannot silently remain verified after its source was altered.
    from server.services import ServiceError
    with pytest.raises(ServiceError) as error:
        web.state.research.artifacts.verify(owner,artifact['id'])
    assert error.value.status==409


def test_document_no_match_is_gap_and_requires_csrf(stack):
    client,thread,web,worker,model,_ = stack
    with TestClient(web) as stranger:
        stranger.get('/api/session')
        assert stranger.post('/api/documents',json=payload()).status_code==403
    client.post('/api/documents',json=payload())
    model.script=lambda c,n:finish(c,missing=True) if c.get('stage')=='review' else entry('research',
        tasks=[{'id':'missing','title':'查阅原文','tool':'search_documents','arguments':{'query':'不存在的商誉减值信息'},'depends_on':[]}],
        objectives=[{'id':'risk','text':'商誉减值','required_fields':['document_excerpt']}])
    run = start(client,thread,'查阅商誉减值原文')
    with TestClient(worker):
        state = wait(client,run,timeout=40)
    assert state['status'] in {'partial','failed'}
    report = client.get(f'/api/runs/{run}/report').json()['content']
    assert report['gaps'] and not any(c.get('field')=='document_excerpt' for c in report['claims'])


def test_parallel_budget_approval_precedes_dispatch(stack):
    client,thread,web,worker,model,overlap = stack
    run = start(client,thread,'比较历史价格',tool_attempts=1)
    with TestClient(worker):
        state = wait(client,run,{'awaiting_approval'},timeout=40)
        assert overlap['requests']==0 and state['attempts_used']==0
        pending = next(a for a in state['approvals'] if a['status']=='pending')
        response=client.post(f"/api/approvals/{pending['id']}/decision",json={'decision':'reject','plan_version':pending['content']['plan_version'],
            'parameters_fingerprint':pending['content']['parameters_fingerprint']})
        assert response.status_code==200,response.text
        assert wait(client,run,timeout=40)['status'] in {'partial','failed'}
    assert overlap['requests']==0


def test_redis_failure_preserves_outbox(stack):
    client,thread,web,worker,model,_ = stack
    run=start(client,thread,'比较历史价格')
    async def check():
        dispatch=Dispatch(web.state.research.db,'redis://127.0.0.1:1/0','qa')
        await dispatch.tick()
        assert dispatch.status=='degraded_sql_polling'
        await dispatch.close()
    asyncio.run(check())
    assert web.state.research.db.one('SELECT * FROM dispatch_outbox WHERE run_id=?',(run,))['sent_at'] is None
    with TestClient(worker):
        assert wait(client,run,timeout=40)['status']=='completed'


def test_dialect_literals_and_conflict_forms():
    assert translate("SELECT * FROM t WHERE x=? AND y LIKE 'context.%'")=="SELECT * FROM t WHERE x=%s AND y LIKE 'context.%%'"
    assert "'?'" in translate("SELECT '?' WHERE x=?")
    assert translate('INSERT OR IGNORE INTO t VALUES(?)').endswith('ON CONFLICT DO NOTHING')
    assert 'pg_advisory_xact_lock' in translate('BEGIN IMMEDIATE')


def test_real_redis_outbox_delivery_and_ack(stack):
    url = os.getenv('XBUDDY_TEST_REDIS_URL','')
    if not url:pytest.skip('Set XBUDDY_TEST_REDIS_URL for real Redis integration')
    client,thread,web,worker,model,_ = stack
    run = start(client,thread,'比较历史价格')
    async def check():
        dispatch = Dispatch(web.state.research.db,url,'qa')
        dispatch.stream = 'xbuddy:qa:'+uuid4().hex
        try:
            await dispatch.tick()
            assert dispatch.status=='available'
            assert await dispatch.redis.xlen(dispatch.stream)==1
            assert (await dispatch.redis.xpending(dispatch.stream,dispatch.group))['pending']==0
            assert web.state.research.db.one('SELECT sent_at FROM dispatch_outbox WHERE run_id=?',(run,))['sent_at']
            await dispatch.tick()
            assert await dispatch.redis.xlen(dispatch.stream)==1
        finally:
            await dispatch.redis.delete(dispatch.stream)
            await dispatch.close()
    asyncio.run(check())
    with TestClient(worker):
        assert wait(client,run,timeout=40)['status']=='completed'


def test_actual_worker_process_approval_and_restart(stack):
    client,thread,web,_,model,_ = stack
    client.post('/api/documents',json=payload())
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            context = json.loads(body['messages'][-1]['content'])
            calls.append(context.get('stage','plan'))
            response = finish(context) if context.get('stage')=='review' else entry('research',
                tasks=[{'id':'original','title':'查阅风险原文','tool':'search_documents','arguments':{'query':'行业竞争风险'},'depends_on':[]}],
                objectives=[{'id':'risk','text':'查阅风险','required_fields':['document_excerpt']}])
            encoded = encode({'choices':[{'message':{'content':encode(response)}}],'usage':{'total_tokens':100}}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(encoded)))
            self.end_headers();self.wfile.write(encoded)
    mock = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server_thread=threading.Thread(target=mock.serve_forever,daemon=True);server_thread.start()
    settings=web.state.research.settings
    env={**os.environ,'XBUDDY_DATA_DIR':str(settings.data_dir),'XBUDDY_DATABASE_URL':settings.database_url,
        'XBUDDY_REDIS_URL':'','LLM_BASE_URL':f'http://127.0.0.1:{mock.server_port}/v1','LLM_MODEL':'synthetic',
        'LLM_API_KEY':'','LLM_ALLOW_NO_KEY':'true','FUYAO_API_KEY':'','IFIND_MCP_URL':'','IFIND_API_KEY':''}
    process=None
    def launch():
        return subprocess.Popen([sys.executable,'-m','server.worker'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    def stop():
        if process and process.poll() is None:
            process.terminate()
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
    try:
        run=start(client,thread,'基于年报查阅风险原文',tool_attempts=0)
        process=launch()
        state=wait(client,run,{'awaiting_approval'},timeout=50)
        approval=next(a for a in state['approvals'] if a['status']=='pending')
        stop()
        process=launch()
        content=approval['content']
        decision=client.post(f"/api/approvals/{approval['id']}/decision",json={'decision':'approve',
            'plan_version':content['plan_version'],'parameters_fingerprint':content['parameters_fingerprint']})
        assert decision.status_code==200,decision.text
        state=wait(client,run,timeout=50)
        assert state['status']=='completed',state
        assert calls==['plan','review'] and state['attempts_used']==1
        assert len(state['tool_calls'])==1 and client.get(f'/api/runs/{run}/report').status_code==200
        assert not model.calls  # The Web process never ran a model.
    finally:
        stop();mock.shutdown();mock.server_close();server_thread.join(timeout=5)


@pytest.mark.parametrize('action',['pause','stop'])
def test_parallel_inflight_control_blocks_next_batch(stack,action):
    client,thread,web,worker,model,overlap=stack
    started,release=threading.Event(),threading.Event()
    dispatches=[]
    original=worker.state.research.fuyao.fetch
    async def held(path,parameters):
        dispatches.append(path)
        if len(dispatches)==2:started.set()
        while not release.is_set():await asyncio.sleep(0.01)
        return await original(path,parameters)
    worker.state.research.fuyao.fetch=held
    run=start(client,thread,'比较历史价格')
    with TestClient(worker):
        try:
            assert started.wait(15)
            assert client.post(f'/api/runs/{run}/control/{action}').status_code==200
        finally:
            release.set()
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            state=client.get('/api/runs/'+run).json()
            if all(c['status']=='completed' for c in state['tool_calls']):break
            time.sleep(0.03)
        assert len(dispatches)==2 and state['attempts_used']==2
        assert state['status']==('paused' if action=='pause' else 'stopped')
        if action=='pause':
            assert client.post(f'/api/runs/{run}/control/resume').status_code==200
            assert wait(client,run,timeout=40)['status']=='completed'
            assert overlap['requests']==3
        else:
            assert client.get(f'/api/runs/{run}/report').status_code==404
