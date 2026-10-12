"""Financial history must reach the observer, user tables and exact source references."""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone, timedelta

import httpx
import pytest
from server.harness.dialogue import AUTHORED_NUMBER, required_periods
from server.storage.database import encode
from server.ui.presentation import financial_rows, raw_value
from test_agent_core import core, entry, finish, start
from test_data_foundation import envelope
from test_harness import wait

SHANGHAI=timezone(timedelta(hours=8))


def annual_records(years=(2023,2025,2022,2024)):
    return [{'thscode':'600519.SH','period':'annual','fiscal_year':year,'fiscal_period':'FY','currency':'CNY',
        'period_end_ms':int(datetime(year,12,31,tzinfo=SHANGHAI).timestamp()*1000),
        'report_date_ms':int(datetime(year+1,4,1,tzinfo=SHANGHAI).timestamp()*1000),
        'operating_income':(year-2020)*100,'parent_holder_net_profit':(year-2020)*10,
        'net_profit':(year-2020)*11,'basic_eps':year-2020} for year in years]


def plan(limit=4):
    return entry('research',tasks=[{'id':'annual','title':'取得多年度年报','tool':'get_financial_metrics',
        'arguments':{'thscode':'600519.SH','statement':'income','period':'annual','limit':limit},'depends_on':[]}],
        objectives=[{'id':'income_trend','text':'整理最近四个会计年度营业收入和归母净利润，观察趋势',
                     'required_fields':['operating_income','parent_holder_net_profit']}])


def configure(core,*,years=(2023,2025,2022,2024)):
    client,thread,app,model,requests,_=core
    def transport(request):
        requests.append(request)
        limit=int(request.url.params.get('limit',4))
        rows=annual_records(years)
        return httpx.Response(200,json=envelope(rows[:limit]))
    app.state.research.fuyao.transport=httpx.MockTransport(transport)
    model.script=lambda c,n:finish(c) if c.get('stage')=='review' else plan()
    return client,thread,app,model,requests


def test_four_year_response_reaches_observer_table_and_all_exports_without_refetch(core):
    client,thread,app,model,requests=configure(core)
    run=start(client,thread,'研究600519.SH最近四个年度营业收入和归母净利润趋势')
    state=wait(client,run)
    assert state['status']=='completed' and len(requests)==1 and state['runtime']['replan_count']==0
    assert state['runtime']['model_calls']==2
    artifact=client.get(f'/api/runs/{run}/report').json()
    report=artifact['content']
    assert report['schema_version']==3 and not report['gaps']
    assert state['runtime']['plan']['objectives'][0]['required_periods']==4
    assert {f['fiscal_year'] for f in model.calls[-1]['facts'].values()}=={2022,2023,2024,2025}
    rows=financial_rows(report)
    assert len(rows)==4 and [r['period'][:4] for r in rows]==['2025','2024','2023','2022']
    facts=[c for c in report['claims'] if c['kind']=='fact']
    assert len(facts)==16
    for fact in facts:
        raw=client.get(f"/api/evidence/{fact['evidence_ids'][0]}/raw").json()
        assert raw_value(raw['body'],fact['reference']['raw_path'])==fact['value']
        assert str(fact['fiscal_year']) in fact['text'] and fact['report_date_ms']>fact['period_end_ms']
        assert f"报告期末 {fact['fiscal_year']}-12-31" in fact['text']
    prefix=f"/api/artifacts/{artifact['id']}/export"
    for format_ in ('markdown','html'):
        response=client.get(prefix,params={'format':format_})
        assert response.status_code==200
        assert all(str(y) in response.text for y in (2022,2023,2024,2025))
    response=client.get(prefix,params={'format':'csv'})
    rows=list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
    assert len([r for r in rows if r['field']=='operating_income'])==4
    assert client.get(prefix,params={'format':'evidence'}).status_code==200


def test_annual_goal_cannot_claim_four_year_coverage_from_latest_year_alone(core):
    client,thread,app,model,requests=configure(core)
    def script(c,n):
        if c.get('stage')!='review':return plan()
        result=finish(c)
        if n==2:
            result['coverage'][0]['fact_refs']=[k for k,f in c['facts'].items() if f['fiscal_year']==2025]
        return result
    model.script=script
    run=start(client,thread,'整理四年营收和利润')
    state=wait(client,run)
    assert state['status']=='completed' and len(requests)==1 and len(model.calls)==3
    assert state['agent']['phase_errors']['review_0_0'].endswith('financial_period_coverage_incomplete')


def test_disclosure_metadata_prose_does_not_trigger_false_amount_violation(core):
    client,thread,app,model,requests=configure(core)
    def script(c,n):
        if c.get('stage')!='review':return plan()
        result=finish(c)
        result['sections'][0]['paragraphs'][0]['text']='披露时点元数据需要与公告原文核对；历史财务观察仍须结合具体口径。'
        return result
    model.script=script
    run=start(client,thread,'整理四年营收和归母净利润')
    state=wait(client,run)
    assert state['status']=='completed' and len(model.calls)==2 and len(requests)==1
    report=client.get(f'/api/runs/{run}/report').json()['content']
    assert report['narrative'] and not report['gaps']


@pytest.mark.parametrize('text',['三点五亿元','百分之九点五','九十九成','五倍','999%'])
def test_chinese_decimal_and_other_unverified_numbers_remain_rejected(text):
    assert AUTHORED_NUMBER.search(text)


def test_calendar_year_is_not_mistaken_for_required_period_count():
    assert required_periods({'text':'2020年度营业收入','required_fields':['operating_income']})==1


def test_insufficient_annual_response_can_extend_limit_and_reuse_completed_request(core):
    client,thread,app,model,requests=configure(core,years=(2025,2024,2023,2022))
    def script(c,n):
        if c.get('stage')!='review':return plan(limit=1)
        if c['can_extend']:
            result=finish(c,missing=True)
            result.update(action='extend',sections=[],tasks=[{**plan()['tasks'][0],'id':'annual_more'}])
            return result
        return finish(c)
    model.script=script
    run=start(client,thread,'整理最近四个年度营收和利润')
    state=wait(client,run)
    assert state['status']=='completed' and state['runtime']['replan_count']==1 and len(requests)==2
    assert [int(r.url.params['limit']) for r in requests]==[1,4]
    assert len(state['tool_calls'])==2 and all(c['attempts']==1 for c in state['tool_calls'])


def test_missing_field_in_one_annual_period_is_not_complete_trend_coverage(core):
    client,thread,app,model,requests=configure(core)
    records=annual_records()
    records[2]['parent_holder_net_profit']=None
    def transport(request):
        requests.append(request)
        return httpx.Response(200,json=envelope(records))
    app.state.research.fuyao.transport=httpx.MockTransport(transport)
    run=start(client,thread,'整理四年营收和归母净利润')
    state=wait(client,run)
    assert state['status']=='partial' and len(model.calls)==3 and len(requests)==1
    report=client.get(f'/api/runs/{run}/report').json()['content']
    assert not report.get('narrative')
    assert any(g['reason'].endswith('financial_period_coverage_incomplete') for g in report['execution_failures'])
    assert any(g.get('period_end_ms')==records[2]['period_end_ms'] for g in report['gaps'])


def test_saved_legacy_report_stays_verifiable_after_history_upgrade(core):
    client,thread,app,model,requests=configure(core)
    # A bounded approval provides a stable pre-planning legacy checkpoint.
    run=start(client,thread,'查看最新年报',model_calls=0)
    state=wait(client,run,{'awaiting_approval'})
    with app.state.research.db.connect() as conn:
        config=json.loads(conn.execute('SELECT config_json FROM runs WHERE id=?',(run,)).fetchone()['config_json'])
        config.pop('financial_history_version')
        conn.execute('UPDATE runs SET config_json=? WHERE id=?',(encode(config),run))
    model.script=lambda c,n:finish(c) if c.get('stage')=='review' else (plan() | {'objectives':[]})
    approval=state['approvals'][-1]
    assert client.post('/api/approvals/'+approval['id']+'/decision',json={'decision':'approve',
        'plan_version':approval['content']['plan_version'],'parameters_fingerprint':approval['content']['parameters_fingerprint']}).status_code==200
    state=wait(client,run)
    assert state['status']=='completed'
    artifact=client.get(f'/api/runs/{run}/report').json()
    assert artifact['content']['schema_version']==2
    assert len(financial_rows(artifact['content']))==1
    assert client.get(f"/api/artifacts/{artifact['id']}/export?format=html").status_code==200
