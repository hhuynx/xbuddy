"""Real comparison/synthesis probe, with private storage and metadata-only output."""
from __future__ import annotations

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


def main():
    settings=Settings.load()
    if not JsonModel(settings).configured or not settings.fuyao_api_key:
        print(json.dumps({'status':'blocked','reason':'missing_model_or_fuyao_configuration'}))
        return 2
    tag=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    cache=ROOT/'.cache/verification'/('agent-core-comparison-'+tag)
    settings=replace(settings,data_dir=cache/'data')
    result={'scope':'agent_core_real_comparison_model_fuyao','checked_at':datetime.now(timezone.utc).isoformat(),'checks':{}}
    app=create_api(settings)
    def check(value,name):
        if not value:
            raise AssertionError(name)
        result['checks'][name]='passed'
        print(json.dumps({'check':name,'status':'passed'}),flush=True)
    def request(client,method,path,**kwargs):
        response=client.request(method,path,**kwargs)
        response.raise_for_status()
        return response.json()
    try:
        with TestClient(app) as client:
            client.headers['X-CSRF-Token']=request(client,'GET','/api/session')['csrf_token']
            thread=request(client,'POST','/api/threads',json={'title':'比较研究真实验证'})['id']
            run=request(client,'POST',f'/api/threads/{thread}/runs',json={
                'mode':'agent','goal':'比较贵州茅台600519.SH和五粮液000858.SZ最近60个交易日的前复权历史价格变化、年化波动率、最大回撤，以及最近共同已披露年报的营业收入和归母净利润。解释可观察的差异，列出来源、时点、单位和口径；不要假定未来表现或没有原文证据的行业原因。',
                'budget':{'model_calls':8,'model_tokens':250000,'context_tokens':32000,'tool_attempts':16,'duration_seconds':900}})['id']
            deadline=time.monotonic()+240
            while time.monotonic()<deadline:
                state=request(client,'GET','/api/runs/'+run)
                if state['status'] in {'completed','partial','failed','blocked','recoverable_failed','awaiting_input','awaiting_approval'}:
                    break
                time.sleep(.2)
            else:
                raise TimeoutError('comparison_timeout')
            check(state['status']=='completed','explicit_comparison_completed_without_clarification')
            artifact=request(client,'GET',f'/api/runs/{run}/report')
            report=artifact['content']
            check(report['status']=='complete' and report['validation_status']=='passed','report_complete_and_verified')
            check(bool(report.get('narrative')) and all(c['status']=='satisfied' for c in report['goal_coverage']),'synthesis_and_goal_coverage')
            fields={'historical_price_change_pct','annualized_volatility_pct','maximum_drawdown_pct','operating_income','parent_holder_net_profit'}
            for symbol in ('600519.SH','000858.SZ'):
                observed={c.get('field') for c in report['claims'] if c['kind']=='fact' and c.get('symbol')==symbol}
                check(fields<=observed,'verified_price_finance_fields_'+symbol)
            check(all(p.get('fact_refs') for s in report['narrative'] for p in s['paragraphs'] if p['kind']!='uncertain'),'synthesis_cites_facts')
            check(any(p.get('evidence_ids') or p.get('derived_metric_ids') for s in report['narrative'] for p in s['paragraphs']),'synthesis_has_source_links')
            for format_ in ('markdown','html','csv','evidence'):
                response=client.get(f"/api/artifacts/{artifact['id']}/export",params={'format':format_})
                check(response.status_code==200 and bool(response.content),'export_'+format_)
            result.update(run={'id':run,'status':state['status'],'models':state['runtime']['model_calls'],
                'tools':state['attempts_used'],'facts':sum(c['kind']=='fact' for c in report['claims'])},storage=str(cache.relative_to(ROOT)))
        result['status']='passed'
    except Exception as error:
        result.update(status='failed',error_type=type(error).__name__,storage=str(cache.relative_to(ROOT)))
        if isinstance(error,AssertionError):
            result['failed_check']=str(error)
    out=ROOT/'docs/verification/agent-core-comparison.local.json'
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':result['status'],'checks':len(result['checks']),'report':str(out.relative_to(ROOT))}),flush=True)
    return 0 if result['status']=='passed' else 1


if __name__=='__main__':
    raise SystemExit(main())
