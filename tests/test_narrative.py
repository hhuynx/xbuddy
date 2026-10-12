"""Complete analysis, bounded recovery, provenance and provider output accounting."""
import json
import asyncio
from dataclasses import replace

import httpx
import pytest

from server.config import Settings
from server.harness.dialogue import validate_analysis
from server.harness.model import JsonModel, ModelFailure
from server.publication import publication_errors
from test_agent_core import core, finish, start
from test_harness import price_plan, wait


def rich_sections(context):
    refs = list(context['facts'])[:2]
    return [
        {'title':'主要观察','paragraphs':[
            {'kind':'fact','text':'{{'+refs[0]+'}}','fact_refs':[refs[0]]},
            {'kind':'inference','text':'这些历史观察为比较样本期内表现提供了依据，解释时应保持相同统计窗口。','fact_refs':refs}]},
        {'title':'数据的研究含义','paragraphs':[
            {'kind':'inference','text':'价格变化反映了样本区间内的观察，不能单凭它判断经营质量。','fact_refs':refs},
            {'kind':'uncertain','text':'现有价格资料不包含经营原因，解释原因仍需核对经营公告。','fact_refs':[]}]},
        {'title':'后续验证方向','paragraphs':[
            {'kind':'uncertain','text':'完整分析末段：后续应核对财务披露与风险原文，再比较不同来源的口径。','fact_refs':[]}]}]


def test_complete_sections_persist_export_and_cite_full_facts(core):
    client,thread,app,model,requests,_ = core
    def script(c,n):
        if c.get('stage')!='review':
            return price_plan()
        assert all('evidence_ids' not in f and 'unit' in f for f in c['facts'].values())
        result=finish(c)
        result['sections']=rich_sections(c)
        return result
    model.script=script
    run=start(client,thread,'比较历史价格并解释研究含义')
    state=wait(client,run)
    assert state['status']=='completed' and len(model.calls)==2 and len(requests)==3
    response=client.get(f'/api/runs/{run}/report')
    assert response.status_code==200
    report=response.json()['content']
    assert sum(len(s['paragraphs']) for s in report['narrative'])==5
    first=report['narrative'][0]['paragraphs'][0]
    assert first['text']==report['claims'][int(first['fact_refs'][0][1:])]['text']
    assert first['evidence_ids']
    assert first['derived_metric_ids']==report['claims'][int(first['fact_refs'][0][1:])]['derived_metric_ids']
    text=client.get(f'/api/threads/{thread}/messages').json()['messages'][-1]['content']['text']
    assert '### 数据的研究含义' in text and '完整分析末段' in text
    artifact=state['artifacts'][0]['id']
    assert '完整分析末段' in client.get(f'/api/artifacts/{artifact}/export?format=html').text
    assert not publication_errors(report)


def recovery_script(c,n):
    if c.get('stage')=='analysis_recovery':
        return {'sections':rich_sections(c),'limitations':['本段仅解释已核验样本，尚未确认研究目标完整覆盖。']}
    if c.get('stage')=='review':
        raise ModelFailure('model_output_incomplete',{'total_tokens':777,'token_status':'reported','cost_status':'unknown'})
    return price_plan()


def test_truncated_review_recovers_analysis_without_claiming_complete(core):
    client,thread,app,model,requests,_=core
    model.script=recovery_script
    run=start(client,thread,'比较历史行情并给出分析描述')
    state=wait(client,run)
    assert state['status']=='partial' and len(model.calls)==4 and len(requests)==3
    assert state['runtime']['model_tokens']==100+777*2+100
    report=client.get(f'/api/runs/{run}/report').json()['content']
    assert report['validation_status']=='passed' and report['status']=='partial'
    assert report['method']['authoring']=='model_partial_analysis_recovery'
    assert report['narrative'] and all(c['kind']=='fact' for c in report['claims'])
    assert report['execution_failures'][0]['reason']=='model_output_incomplete'
    assert '尚未确认' in report['execution_failures'][0]['detail']
    assert not report.get('goal_coverage') and not publication_errors(report)
    # Every render rechecks the source and the stored model draft, including the extra phase.
    artifact=state['artifacts'][0]['id']
    assert client.get('/api/artifacts/'+artifact).status_code==200
    app.state.research.dialogue.put(run,recovery_facts_hash='tampered')
    assert client.get('/api/artifacts/'+artifact).status_code==409


@pytest.mark.parametrize('fault',['number','advice','reference'])
def test_invalid_recovery_prose_is_not_published(core,fault):
    client,thread,app,model,requests,_=core
    def script(c,n):
        result=recovery_script(c,n)
        if c.get('stage')=='analysis_recovery':
            paragraph=result['sections'][0]['paragraphs'][1]
            if fault=='number': paragraph['text']='历史涨幅为999%。'
            if fault=='advice': paragraph['text']='建议立即买入。'
            if fault=='reference': paragraph['fact_refs']=['F999']
        return result
    model.script=script
    run=start(client,thread,'解释已核验的历史数据')
    state=wait(client,run)
    report=client.get(f'/api/runs/{run}/report').json()['content']
    assert state['status']=='partial' and len(model.calls)==4 and len(requests)==3
    assert not report.get('narrative') and not publication_errors(report)
    assert '历史涨幅为999%' not in json.dumps(report,ensure_ascii=False)
    assert app.state.research.dialogue.state(run)['recovery_error'].startswith('model_output_validation_failed')


def test_recovery_obeys_call_budget_and_keeps_facts(core):
    client,thread,app,model,requests,_=core
    model.script=recovery_script
    run=start(client,thread,'解释历史行情',model_calls=3)
    state=wait(client,run)
    report=client.get(f'/api/runs/{run}/report').json()['content']
    assert state['status']=='partial' and len(model.calls)==3 and len(requests)==3
    assert not report.get('narrative') and any(c['kind']=='fact' for c in report['claims'])


def test_fact_only_response_gets_bounded_prose_repair(core):
    client,thread,app,model,requests,_=core
    def script(c,n):
        if c.get('stage')!='review':return price_plan()
        result=finish(c)
        if n==2:
            ref=next(iter(c['facts']))
            result['sections']=[{'title':'数据','paragraphs':[{'kind':'fact','text':'','fact_refs':[ref]}]}]
        else:
            assert c['repair_error'].endswith(':analysis_description_required')
        return result
    model.script=script
    state=wait(client,start(client,thread,'解释历史数据'))
    assert state['status']=='completed' and len(model.calls)==3


def test_stopping_after_failed_review_prevents_recovery_dispatch(core):
    client,thread,app,model,requests,_=core
    def script(c,n):
        if c.get('stage')=='review' and n==3:
            run=c['protected_state']['run_id']
            owner=app.state.research.db.one('SELECT session_id FROM runs WHERE id=?',(run,))['session_id']
            app.state.harness.control(owner,run,'stop')
        return recovery_script(c,n)
    model.script=script
    run=start(client,thread,'解释历史数据')
    state=wait(client,run,{'stopped'})
    assert len(model.calls)==3 and not state['artifacts']
    assert not any(c.get('stage')=='analysis_recovery' for c in model.calls)


def test_unsafe_recovery_is_masked_in_agent_snapshot():
    from server.publication import agent_view
    state={'recovery_analysis':{'sections':[{'title':'观察','paragraphs':[
        {'kind':'inference','text':'建议立即买入。','fact_refs':['F0']}]}],'limitations':[]}}
    published=agent_view(state)
    assert published['publication_status']=='blocked' and 'recovery_analysis' not in published
    assert 'recovery_analysis' in state


def test_truncation_usage_and_adaptive_output_limit():
    bodies=[]
    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'finish_reason':'length','message':{'content':'{"broken"'}}],
            'usage':{'prompt_tokens':450,'completion_tokens':12000,'total_tokens':12450,
                'completion_tokens_details':{'reasoning_tokens':9000}}})
    model=JsonModel(Settings(llm_base_url='https://api.deepseek.com',llm_model='deepseek-flash',llm_api_key='synthetic-secret'),httpx.MockTransport(handler))
    for error,limit in [('',12000),('model_output_incomplete',24000)]:
        with pytest.raises(ModelFailure) as exc:
            asyncio.run(model.complete([{'role':'user','content':json.dumps({'repair_error':error})}]))
        assert str(exc.value)=='model_output_incomplete' and exc.value.usage['total_tokens']==12450
        assert exc.value.usage['reasoning_tokens']==9000
        assert bodies[-1]['max_tokens']==limit and bodies[-1]['reasoning_effort']=='low'
    # Automatic provider-specific options never leak into arbitrary compatible providers.
    assert JsonModel(replace(model.settings,llm_base_url='https://other.example/v1')).request_options()=={}
    assert JsonModel(replace(model.settings,llm_reasoning_effort='none')).request_options()=={'thinking':{'type':'disabled'}}


def test_invalid_json_still_accounts_reported_tokens():
    response={'choices':[{'finish_reason':'stop','message':{'content':'invalid json'}}],
        'usage':{'total_tokens':654}}
    model=JsonModel(Settings(llm_base_url='https://model.example/v1',llm_model='test',llm_api_key='synthetic-secret'),
        httpx.MockTransport(lambda r:httpx.Response(200,json=response)))
    with pytest.raises(ModelFailure) as exc:
        asyncio.run(model.complete([{'role':'user','content':'{}'}]))
    assert str(exc.value)=='model_invalid_json' and exc.value.usage['total_tokens']==654
