"""User-facing agent behaviours, with bounded synthetic model and provider responses."""
from __future__ import annotations

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from server.api.routes import create_api
from server.config import Settings
from server.storage.database import encode
from test_data_foundation import response_for
from test_harness import FakeModel, prepare, price_plan, wait


class ScriptModel(FakeModel):
    def __init__(self, script):
        super().__init__()
        self.script = script

    async def complete(self, messages):
        context = json.loads(messages[-1]['content'])
        self.calls.append(context)
        return self.script(context, len(self.calls)), {'total_tokens':100,'token_status':'reported','cost_status':'unknown'}


def entry(intent, **values):
    return {'intent':intent,'tasks':[],'limitations':[],'unresolved_goals':[],**values}


def finish(context, *, missing=False):
    refs = list(context['facts'])
    return {'action':'finish','coverage':[{'objective_id':o['id'],'status':'missing' if missing or not refs else 'satisfied',
        'fact_refs':refs,'reason':'证据没有覆盖所需指标。' if missing or not refs else ''} for o in context['objectives']],
        'sections':[{'title':'观察与研究边界','paragraphs':[{'kind':'inference' if refs else 'uncertain',
            'text':'这些观察来自已取得的数据，仍需注意样本窗口及统计口径。' if refs else '当前数据不足，尚需补充可核验资料。',
            'fact_refs':refs[:12]}]}], 'tasks':[], 'limitations':[],'unresolved_goals':[],'question':None}


def answer_text(report):
    return '\n'.join([c['text'] for c in report['claims']] + [p['text'] for s in report.get('narrative',[]) for p in s['paragraphs']])


def start(client, thread, goal, **budget):
    response = client.post(f'/api/threads/{thread}/runs',json={'mode':'agent','goal':goal,'budget':{'context_tokens':24000,**budget}})
    assert response.status_code == 201,response.text
    return response.json()['id']


@pytest.fixture
def core(tmp_path):
    requests = []
    def transport(request):
        requests.append(request)
        return httpx.Response(200,json=response_for(request))
    def script(c,n):
        return finish(c) if c.get('stage') == 'review' else price_plan()
    model = ScriptModel(script)
    settings = Settings(data_dir=tmp_path,fuyao_api_key='synthetic-secret')
    app = create_api(settings,httpx.MockTransport(transport),model=model)
    with TestClient(app) as client:
        yield client,prepare(client),app,model,requests,settings


def test_methodology_answer_uses_no_financial_tool_or_numeric_gap(core):
    client,thread,app,model,requests,_ = core
    model.script = lambda c,n:entry('answer',answer='研究股票可以从商业模式、行业竞争、财务质量、估值和风险开始，并记录资料来源和待验证问题。')
    run = start(client,thread,'怎么研究股票')
    state = wait(client,run)
    assert state['status'] == 'completed' and state['attempts_used'] == 0 and not requests
    assert state['runtime']['model_calls'] == 1 and state['runtime']['model_tokens'] == 100
    report = client.get(f'/api/runs/{run}/report').json()['content']
    assert report['answer_mode'] == 'knowledge' and report['gaps'] == [] and report['claims'] == []
    assert '商业模式' in report['narrative'][0]['paragraphs'][0]['text']
    messages = client.get(f'/api/threads/{thread}/messages').json()['messages']
    assert [m['role'] for m in messages] == ['user','assistant']
    assert client.get('/api/artifacts/'+state['artifacts'][0]['id']+'/export?format=html').status_code == 200


def test_clarification_response_is_bound_persistent_and_continues_same_run(core):
    client,thread,app,model,requests,_ = core
    model.script = lambda c,n: finish(c) if c.get('stage')=='review' else (price_plan() if any(m['text']=='600519.SH' for m in c['conversation']) else entry('clarify',question={'text':'想研究哪只股票？','options':['600519.SH','000858.SZ']}))
    run = start(client,thread,'帮我研究一只股票')
    waiting = wait(client,run,{'awaiting_input'})
    assert waiting['pending_input'] and not requests and not waiting['tasks']
    question = waiting['pending_input']['id']
    assert client.post(f'/api/runs/{run}/input',json={'question_id':'wrong','answer':'600519.SH'}).status_code == 404
    assert client.post(f'/api/runs/{run}/input',json={'question_id':question,'answer':'600519.SH'}).status_code == 200
    done = wait(client,run)
    assert done['status'] == 'completed' and done['runtime']['model_calls'] == 3
    assert client.post(f'/api/runs/{run}/input',json={'question_id':question,'answer':'000858.SZ'}).status_code == 409
    messages = client.get(f'/api/threads/{thread}/messages').json()['messages']
    assert [m['role'] for m in messages] == ['user','assistant','user','assistant']


def test_followup_conversation_keeps_previous_object_and_new_constraint(core):
    client,thread,_,model,requests,_ = core
    model.script = lambda c,n:entry('answer',answer='已记录研究框架，后续可以按新窗口核验。')
    run = start(client,thread,'研究贵州茅台')
    assert wait(client,run)['status'] == 'completed'
    followup = start(client,thread,'只看最近三十个交易日')
    assert wait(client,followup)['status'] == 'completed'
    texts = [m['text'] for m in model.calls[-1]['conversation']]
    assert '研究贵州茅台' in texts and texts[-1]=='只看最近三十个交易日'
    assert not requests


def test_successful_tool_with_incomplete_goal_triggers_bounded_extension(core):
    client,thread,_,model,requests,_ = core
    full = price_plan()
    def script(c,n):
        if c.get('stage') != 'review':
            return entry('research',tasks=[full['tasks'][0]],objectives=[{'id':'price','text':'比较历史价格','required_fields':['historical_price_change_pct']}])
        if c['can_extend']:
            result = finish(c,missing=True)
            result.update(action='extend',sections=[],tasks=full['tasks'][1:])
            return result
        result = finish(c)
        result['coverage'][0]['fact_refs'] = [k for k,v in c['facts'].items() if v.get('field')=='historical_price_change_pct']
        return result
    model.script = script
    run = start(client,thread,'比较两家公司历史价格')
    done = wait(client,run)
    assert done['status']=='completed' and done['runtime']['replan_count']==1
    assert len(requests)==3 and len(model.calls)==3
    report = client.get(f'/api/runs/{run}/report').json()['content']
    assert report['goal_coverage'][0]['status']=='satisfied' and report['narrative']


@pytest.mark.parametrize('fault',['number','reference','coverage'])
def test_model_fabrication_is_repaired_or_excluded(core,fault):
    client,thread,_,model,requests,_ = core
    def script(c,n):
        if c.get('stage') != 'review':
            return price_plan()
        result = finish(c)
        if n==2:
            if fault=='number':
                result['sections'][0]['paragraphs'][0]['text']='历史收益达到999%。'
            elif fault=='reference':
                result['sections'][0]['paragraphs'][0]['fact_refs']=['F999']
            else:
                result['coverage']=[]
        return result
    model.script = script
    run = start(client,thread,'比较股票历史价格')
    done = wait(client,run)
    assert done['status']=='completed' and len(model.calls)==3 and len(requests)==3
    report = client.get(f'/api/runs/{run}/report').json()['content']
    assert '999' not in answer_text(report) and report['narrative']
    assert client.get('/api/artifacts/'+done['artifacts'][0]['id']).status_code==200


def test_no_verified_data_reports_real_failure_not_normal_answer(core):
    client,thread,app,model,requests,_ = core
    model.script=lambda c,n: entry('research',tasks=[price_plan()['tasks'][1]]) if c.get('stage')!='review' else finish(c)
    app.state.research.fuyao.transport=httpx.MockTransport(lambda r:httpx.Response(401,json={'message':'synthetic-secret'}))
    run=start(client,thread,'查看股票历史数据')
    state=wait(client,run)
    assert state['status']=='failed'
    report=client.get(f'/api/runs/{run}/report').json()['content']
    assert not [c for c in report['claims'] if c['kind']=='fact']
    assert report['goal_coverage'][0]['status']=='missing'
    assert 'synthetic-secret' not in encode(state)+encode(report)


def test_clarification_survives_new_process_objects_and_reuses_initial_model_call(tmp_path):
    settings=Settings(data_dir=tmp_path,fuyao_api_key='synthetic-secret')
    initial=ScriptModel(lambda c,n:entry('clarify',question={'text':'请给出具体标的。','options':[]}))
    app=create_api(settings,httpx.MockTransport(lambda r:httpx.Response(200,json=response_for(r))),model=initial)
    with TestClient(app) as client:
        thread=prepare(client)
        run=start(client,thread,'研究一家公司')
        pending=wait(client,run,{'awaiting_input'})['pending_input']
        cookies=dict(client.cookies)
    model=ScriptModel(lambda c,n:finish(c) if c.get('stage')=='review' else price_plan())
    restored=create_api(settings,httpx.MockTransport(lambda r:httpx.Response(200,json=response_for(r))),model=model)
    with TestClient(restored) as client:
        client.cookies.update(cookies)
        client.headers['X-CSRF-Token']=client.get('/api/session').json()['csrf_token']
        assert client.get(f'/api/runs/{run}').json()['status']=='awaiting_input'
        assert client.post(f'/api/runs/{run}/input',json={'question_id':pending['id'],'answer':'600519.SH'}).status_code==200
        done=wait(client,run)
        assert done['status']=='completed' and done['runtime']['model_calls']==3
        assert len(initial.calls)==1 and len(model.calls)==2
        assert '600519.SH' in [m['text'] for m in model.calls[0]['conversation']]


def test_ambiguous_entity_waits_for_verified_choice_and_does_not_repeat_lookup(core):
    from test_data_foundation import DAYS,SYMBOLS,envelope
    client,thread,app,model,requests,_=core
    plan=entry('research',tasks=[{'id':'resolve','title':'解析标的','tool':'resolve_security','arguments':{'q':'构造公司','limit':5},'depends_on':[]},
        {'id':'bars','title':'取得历史价格','tool':'get_daily_bars','arguments':{'thscode':'@resolve.thscode','start':DAYS[0],'end':DAYS[-1]},'depends_on':['resolve']}])
    model.script=lambda c,n:finish(c) if c.get('stage')=='review' else plan
    def transport(r):
        requests.append(r)
        if r.url.path.endswith('/search'):
            return httpx.Response(200,json=envelope([{'thscode':s,'name':'构造公司','currency':'CNY'} for s in SYMBOLS]))
        return httpx.Response(200,json=response_for(r))
    app.state.research.fuyao.transport=httpx.MockTransport(transport)
    run=start(client,thread,'研究构造公司历史价格')
    state=wait(client,run,{'awaiting_input'})
    pending=state['pending_input']
    assert pending['question']['kind']=='entity' and len(requests)==1
    assert client.post(f'/api/runs/{run}/input',json={'question_id':pending['id'],'answer':'arbitrary'}).status_code==422
    assert client.post(f'/api/runs/{run}/input',json={'question_id':pending['id'],'answer':SYMBOLS[1]}).status_code==200
    done=wait(client,run)
    assert done['status']=='completed' and len(requests)==2
    assert requests[-1].url.params['thscode']==SYMBOLS[1]
    assert sum(c['tool']=='resolve_security' for c in done['tool_calls'])==1


def test_review_budget_blocks_dispatch_and_rejected_expansion_preserves_verified_facts(core):
    client,thread,_,model,requests,_=core
    run=start(client,thread,'比较历史价格',model_calls=1)
    state=wait(client,run,{'awaiting_approval'})
    assert len(model.calls)==1 and len(requests)==3
    approval=state['approvals'][-1]
    assert approval['content']['parameters']['operation']['phase'].startswith('review_')
    assert client.post('/api/approvals/'+approval['id']+'/decision',json={'decision':'reject',
        'plan_version':approval['content']['plan_version'],'parameters_fingerprint':approval['content']['parameters_fingerprint']}).status_code==200
    done=wait(client,run)
    assert done['status']=='partial' and len(model.calls)==1
    report=client.get(f'/api/runs/{run}/report').json()['content']
    assert any(c['kind']=='fact' for c in report['claims']) and not report.get('narrative')
    assert report['execution_failures'] and not report['gaps']


def test_unrepairable_model_answer_preserves_facts_but_never_publishes_invented_numbers(core):
    client,thread,_,model,requests,_=core
    def script(c,n):
        if c.get('stage')!='review':
            return price_plan()
        result=finish(c)
        result['sections'][0]['paragraphs'][0]['text']='收益为999%。'
        return result
    model.script=script
    run=start(client,thread,'比较股票历史表现')
    done=wait(client,run)
    assert done['status']=='partial' and len(model.calls)==3 and len(requests)==3
    report=client.get(f'/api/runs/{run}/report').json()['content']
    assert '999' not in answer_text(report) and not report.get('narrative')
    assert any(g['reason'].startswith('model_output_validation_failed') for g in report['execution_failures'])
    assert not any(g['reason'].startswith('model_output_validation_failed') for g in report['gaps'])


def test_stop_waiting_input_expires_question_and_blocks_future_work(core):
    client,thread,_,model,requests,_=core
    model.script=lambda c,n:entry('clarify',question={'text':'研究哪个标的？','options':[]})
    run=start(client,thread,'研究股票')
    pending=wait(client,run,{'awaiting_input'})['pending_input']
    assert client.post(f'/api/runs/{run}/control/stop').status_code==200
    assert client.post(f'/api/runs/{run}/input',json={'question_id':pending['id'],'answer':'600519.SH'}).status_code==409
    assert client.get(f'/api/runs/{run}').json()['status']=='stopped' and len(model.calls)==1 and not requests


def test_foreign_owner_cannot_read_dialogue_or_answer_question(core):
    client,thread,app,model,requests,_=core
    model.script=lambda c,n:entry('clarify',question={'text':'研究哪个标的？','options':[]})
    run=start(client,thread,'研究股票')
    pending=wait(client,run,{'awaiting_input'})['pending_input']
    with TestClient(app) as other:
        other.headers['X-CSRF-Token']=other.get('/api/session').json()['csrf_token']
        assert other.get(f'/api/threads/{thread}/messages').status_code==404
        assert other.post(f'/api/runs/{run}/input',json={'question_id':pending['id'],'answer':'600519.SH'}).status_code==404
    assert client.get(f'/api/runs/{run}').json()['status']=='awaiting_input' and not requests


def test_comparison_cannot_claim_coverage_using_only_one_security(core):
    client,thread,_,model,requests,_=core
    def script(c,n):
        if c.get('stage')!='review':
            return price_plan() | {'objectives':[{'id':'comparison','text':'比较两只股票历史价格变化','required_fields':['historical_price_change_pct']}]}
        result=finish(c)
        refs=[r for r,f in c['facts'].items() if f.get('field')=='historical_price_change_pct']
        result['coverage'][0]['fact_refs']=refs[:1] if n==2 else refs
        return result
    model.script=script
    run=start(client,thread,'比较两只股票历史价格变化')
    state=wait(client,run)
    assert state['status']=='completed' and len(model.calls)==3
    assert state['agent']['phase_errors']['review_0_0'].endswith('security_coverage_incomplete')


def test_goal_completion_keeps_resolved_quality_issues_in_audit_history(core):
    client,thread,_,model,requests,_=core
    plan=price_plan() | {'objectives':[{'id':'price','text':'比较历史价格变化','required_fields':['historical_price_change_pct']}],
        'unresolved_goals':['初始计划曾认为历史价格尚待补充。']}
    plan['tasks'].append({'id':'snapshot','title':'附加快照','tool':'get_price_snapshot',
        'arguments':{'symbols':['600519.SH']},'depends_on':[]})
    model.script=lambda c,n:finish(c) if c.get('stage')=='review' else plan
    run=start(client,thread,'比较历史价格变化')
    state=wait(client,run)
    assert state['status']=='completed' and len(requests)==4
    artifact=client.get(f'/api/runs/{run}/report').json()
    report=artifact['content']
    assert not report['gaps'] and report['execution_issues']
    assert any(i['reason']=='unsupported_goal' for i in report['execution_issues'])
    assert any(i['reason']=='unverified' for i in report['execution_issues'])
    assert all(c['status']=='satisfied' for c in report['goal_coverage'])
    for format_ in ('markdown','html'):
        response=client.get(f"/api/artifacts/{artifact['id']}/export?format={format_}")
        assert response.status_code==200 and '调用与数据质量记录' in response.text


def test_repeated_clarifications_stop_at_three_questions(core):
    client,thread,_,model,requests,_=core
    model.script=lambda c,n:entry('clarify',question={'text':'请进一步明确研究对象。','options':[]})
    run=start(client,thread,'研究股票',model_calls=6)
    for number in range(3):
        state=wait(client,run,{'awaiting_input'})
        assert state['runtime']['model_calls']==number+1
        assert client.post(f'/api/runs/{run}/input',json={'question_id':state['pending_input']['id'],
            'answer':'尚需进一步明确'}).status_code==200
    state=wait(client,run)
    assert state['status']=='failed' and len(model.calls)==4 and not requests
    assert not state['pending_input']
    report=client.get(f'/api/runs/{run}/report').json()['content']
    assert any(g['reason']=='clarification_limit' for g in report['execution_failures'])
    assert not report['gaps']


def test_clarification_deadline_expires_input_without_more_model_calls(core):
    client,thread,app,model,requests,_=core
    model.script=lambda c,n:entry('clarify',question={'text':'研究哪个标的？','options':[]})
    run=start(client,thread,'研究股票')
    pending=wait(client,run,{'awaiting_input'})['pending_input']
    with app.state.research.db.connect() as conn:
        conn.execute('UPDATE run_runtime SET deadline=? WHERE run_id=?',(time.time()-.01,run))
    state=wait(client,run)
    assert state['status']=='failed' and len(model.calls)==1 and not requests
    assert not state['pending_input']
    assert client.post(f'/api/runs/{run}/input',json={'question_id':pending['id'],'answer':'600519.SH'}).status_code==409
    report=client.get(f'/api/runs/{run}/report').json()['content']
    assert any(g['reason']=='input_expired_or_stopped' for g in report['execution_failures'])
    assert not report['gaps']


def test_chinese_percentage_cannot_bypass_numerical_answer_validation(core):
    client,thread,_,model,requests,_=core
    def script(c,n):
        if c.get('stage')!='review':
            return price_plan()
        result=finish(c)
        result['sections'][0]['paragraphs'][0]['text']='历史收益达到百分之九十九。'
        return result
    model.script=script
    run=start(client,thread,'比较历史价格')
    state=wait(client,run)
    assert state['status']=='partial' and len(model.calls)==3 and len(requests)==3
    report=client.get(f'/api/runs/{run}/report').json()['content']
    assert '百分之九十九' not in answer_text(report) and not report.get('narrative')
    assert state['agent']['phase_errors']['review_0_0'].endswith('model_authored_number')


def test_impossible_identity_requirement_is_repaired_before_financial_dispatch(core):
    client,thread,_,model,requests,_=core
    def script(c,n):
        if c.get('stage')=='review':
            return finish(c)
        fields=['historical_price_change_pct'] + (['name'] if n==1 else [])
        return price_plan() | {'objectives':[{'id':'comparison','text':'比较历史价格','required_fields':fields}]}
    model.script=script
    run=start(client,thread,'比较两个明确代码的历史价格')
    state=wait(client,run)
    assert state['status']=='completed' and len(model.calls)==3 and len(requests)==3
    assert state['agent']['phase_errors']['plan'].endswith('identity_requirement_without_resolution_source')
    assert model.calls[1]['repair_errors']['plan'].endswith('identity_requirement_without_resolution_source')
    assert model.calls[1]['rejected_draft']['objectives'][0]['required_fields'][-1]=='name'
