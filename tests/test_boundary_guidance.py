"""Policy explanations remain useful while trade advice and bad output stay blocked."""
import httpx
import pytest
from fastapi.testclient import TestClient
from server.api.routes import create_api
from server.config import Settings
from server.harness.boundaries import trading_question
from server.publication import safe_text, publication_errors
from test_agent_core import core, entry, finish, start, ScriptModel
from test_data_foundation import response_for
from test_harness import prepare, wait, price_plan


@pytest.mark.parametrize('text', [
    '本助手不产出买入/卖出结论，目标价与仓位建议无法作为可验证目标。',
    '我不能提供买入或卖出建议。可以核对已披露资料。',
    '不提供加仓、减仓、建仓或清仓建议。',
])
def test_explicit_refusal_lists_are_allowed(text):
    assert safe_text(text)


@pytest.mark.parametrize('text', [
    '我不能提供买入或卖出建议，但建议现在买入。',
    '不产出买入/卖出结论，不过应该加仓。',
    '不能提供买入建议，立即卖出。',
    '我不能提供买入建议但是未来股价必涨。',
])
def test_disclaimer_does_not_authorize_later_advice(text):
    assert not safe_text(text)


def test_bare_decision_redirects_without_calls_then_continues_same_run(core):
    client, thread, app, model, requests, _ = core
    model.script = lambda c,n: finish(c) if c.get('stage')=='review' else price_plan() | {'resolved_goal':'核对茅台历史行情表现'}
    run = start(client, thread, '查看茅台股票能不能买入')
    pending = wait(client, run, {'awaiting_input'})
    question = pending['pending_input']
    assert '不能提供' in (question.get('question') or question)['text']
    assert not model.calls and not requests and pending['runtime']['model_calls'] == 0
    assert not pending['artifacts']
    reply = client.post(f'/api/runs/{run}/input', json={'question_id':question['id'], 'answer':'查看历史行情表现'})
    assert reply.status_code == 200
    done = wait(client, run)
    assert done['status'] == 'completed'
    assert any(m['text']=='查看历史行情表现' for m in model.calls[0]['conversation'])
    assert '茅台' in model.calls[0]['goal']
    assert model.calls[0]['selected_research_direction']=='查看历史行情表现'
    report = client.get(f'/api/runs/{run}/report').json()['content']
    assert not report.get('execution_failures') and not report['gaps'] and not publication_errors(report)


def test_mixed_research_and_methodology_are_not_intercepted():
    assert trading_question('查看茅台财务和风险，再判断能不能买') is None
    assert trading_question('如何判断一只股票能不能买，介绍研究方法') is None
    assert trading_question('茅台该不该卖')


def test_unknown_field_is_repaired_under_strict_contract(core):
    client, thread, _, model, requests, _ = core
    def script(c,n):
        result = entry('answer', answer='可以先研究商业模式和财务质量，记录来源与研究边界。')
        if n == 1:
            result['intent_note'] = 'synthetic extra field'
        else:
            assert c['repair_errors']['plan'].endswith(':schema_extra_field')
        return result
    model.script = script
    done = wait(client, start(client, thread, '介绍研究框架'))
    assert done['status'] == 'completed' and len(model.calls)==2 and not requests


def test_unrepairable_schema_is_execution_failure_not_data_gap(core):
    client, thread, _, model, requests, _ = core
    model.script = lambda c,n: entry('answer', answer='研究框架。') | {'intent_note':'extra'}
    run = start(client, thread, '介绍研究框架')
    done = wait(client, run)
    assert done['status']=='failed' and len(model.calls)==2 and not requests
    report = client.get(f'/api/runs/{run}/report').json()['content']
    assert report['answer_mode']=='error' and report['claims']==[] and report['gaps']==[]
    assert report['execution_failures'][0]['reason'].endswith(':schema_extra_field')
    messages = client.get(f'/api/threads/{thread}/messages').json()['messages']
    assert '模型回复包含不支持的字段' in messages[-1]['content']['text']
    assert '尚无可验证结论' not in messages[-1]['content']['text']
    artifact = done['artifacts'][0]['id']
    for fmt in ('markdown','html'):
        exported=client.get(f'/api/artifacts/{artifact}/export?format={fmt}')
        assert exported.status_code == 200 and '执行问题' in exported.text
        assert 'schema_extra_field' not in exported.text  # Present friendly text in exported explanations.


def test_boundary_question_survives_worker_restart(tmp_path):
    settings = Settings(data_dir=tmp_path,fuyao_api_key='synthetic-secret')
    first = ScriptModel(lambda c,n: entry('answer',answer='研究说明。'))
    app = create_api(settings,httpx.MockTransport(lambda r:httpx.Response(200,json=response_for(r))),model=first)
    with TestClient(app) as client:
        thread = prepare(client)
        run = start(client,thread,'茅台能买吗')
        question = wait(client,run,{'awaiting_input'})['pending_input']
        cookies = dict(client.cookies)
        csrf = client.get('/api/session').json()['csrf_token']
        assert not first.calls
    restored = create_api(settings,httpx.MockTransport(lambda r:httpx.Response(200,json=response_for(r))),model=ScriptModel(lambda c,n:entry('answer',answer='可以先梳理报告期、披露口径与数据可得性。')))
    with TestClient(restored) as client:
        client.cookies.update(cookies)
        assert client.get(f'/api/runs/{run}').json()['pending_input']['id'] == question['id']
        assert client.post(f'/api/runs/{run}/input',json={'question_id':question['id'],'answer':'梳理研究风险与数据边界'},headers={'X-CSRF-Token':csrf}).status_code == 200
        assert wait(client,run)['status']=='completed'
