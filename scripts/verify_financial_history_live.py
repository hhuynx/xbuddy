"""Verify multi-year research with actual providers, isolated data and metadata output."""
from __future__ import annotations

import csv
import io
import json
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from fastapi.testclient import TestClient
from server.api.routes import create_api
from server.config import Settings
from server.harness.model import JsonModel
from server.ui.presentation import financial_rows, raw_value


def main():
    settings=Settings.load()
    if not JsonModel(settings).configured or not settings.fuyao_api_key:
        print(json.dumps({'status':'blocked','reason':'missing_model_or_fuyao_configuration'}))
        return 2
    cache=ROOT/'.cache/verification'/('financial-history-live-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f'))
    settings=replace(settings,data_dir=cache/'data')
    result={'scope':'real_model_fuyao_multi_annual_research','checked_at':datetime.now(timezone.utc).isoformat(),'checks':{},'storage':str(cache.relative_to(ROOT))}
    app=create_api(settings)
    def check(value,name):
        if not value:raise AssertionError(name)
        result['checks'][name]='passed'
        print(json.dumps({'check':name,'status':'passed'}),flush=True)
    def request(client,method,path,**kwargs):
        response=client.request(method,path,**kwargs)
        response.raise_for_status()
        return response.json()
    try:
        with TestClient(app) as client:
            client.headers['X-CSRF-Token']=request(client,'GET','/api/session')['csrf_token']
            thread=request(client,'POST','/api/threads',json={'title':'多年度财务研究验证'})['id']
            run=request(client,'POST',f'/api/threads/{thread}/runs',json={'mode':'agent',
                'goal':'研究贵州茅台600519.SH最近4个已披露会计年度的同口径合并年报营业收入和归母净利润，观察多期变化方向；同时统计最近60个交易日的前复权历史价格变化、波动率和最大回撤。列出各年度数据、来源、报告期和披露时点，不编造没有原文支持的业务原因。',
                'budget':{'model_calls':8,'model_tokens':250000,'context_tokens':48000,'tool_attempts':16,'duration_seconds':900}})['id']
            deadline=time.monotonic()+240
            while time.monotonic()<deadline:
                state=request(client,'GET','/api/runs/'+run)
                if state['status'] in {'completed','partial','failed','blocked','awaiting_input','awaiting_approval','recoverable_failed'}:break
                time.sleep(.2)
            else:raise TimeoutError('multi_annual_research_timeout')
            result['run']={'id':run,'status':state['status'],'models':state['runtime']['model_calls'],'tools':state['attempts_used']}
            check(state['status']=='completed','four_year_research_completed')
            artifact=request(client,'GET',f'/api/runs/{run}/report')
            report=artifact['content']
            check(report['validation_status']=='passed' and report['status']=='complete' and not report['gaps'],'verified_without_false_data_gap')
            facts=[f for f in report['claims'] if f['kind']=='fact' and f.get('field') in {'operating_income','parent_holder_net_profit'}]
            years=sorted({f['fiscal_year'] for f in facts})
            check(len(years)>=4,'four_fiscal_years_visible')
            for year in years:
                check({f['field'] for f in facts if f['fiscal_year']==year}=={'operating_income','parent_holder_net_profit'},'both_fields_'+str(year))
            check(all(f"报告期末 {f['fiscal_year']}-12-31" in f['text'] for f in facts),'period_text_uses_shanghai_calendar_date')
            check(len(financial_rows(report))>=4,'table_preserves_historical_rows')
            check(all(c['status']=='satisfied' for c in report['goal_coverage']) and bool(report.get('narrative')),'annual_goal_coverage_and_synthesis')
            calls=[c for c in state['tool_calls'] if c['tool']=='get_financial_metrics']
            check(len(calls)==1 and calls[0]['parameters']['period']=='annual','one_financial_request_returns_history')
            for fact in facts:
                raw=request(client,'GET',f"/api/evidence/{fact['evidence_ids'][0]}/raw")
                check(raw_value(raw['body'],fact['reference']['raw_path'])==fact['value'],'source_'+str(fact['fiscal_year'])+'_'+fact['field'])
            for format_ in ('markdown','html','csv','evidence'):
                response=client.get(f"/api/artifacts/{artifact['id']}/export",params={'format':format_})
                check(response.status_code==200 and bool(response.content),'export_'+format_)
                if format_ in {'markdown','html'}:check(all(str(y) in response.text for y in years),'years_in_'+format_)
                if format_=='csv':
                    rows=list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
                    check(len([r for r in rows if r['field']=='operating_income'])>=4,'all_years_in_csv')
            (cache/'browser-handoff.local.json').write_text(json.dumps({'cookies':dict(client.cookies),'thread':thread,'run':run}),encoding='utf-8')
            result['run'].update(fiscal_years=years,financial_facts=len(facts))
        result['status']='passed'
    except Exception as error:
        result.update(status='failed',error_type=type(error).__name__)
        if isinstance(error,AssertionError):result['failed_check']=str(error)
    out=ROOT/'docs/verification/financial-history-live.local.json'
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':result['status'],'checks':len(result['checks']),'report':str(out.relative_to(ROOT))}),flush=True)
    return 0 if result['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
