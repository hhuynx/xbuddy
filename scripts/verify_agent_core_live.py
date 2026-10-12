"""Real-model/Fuyao verification in isolated storage. Prints metadata only."""
from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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
    cache=ROOT/'.cache/verification'/('agent-core-live-'+tag)
    cache.mkdir(parents=True)
    settings=replace(settings,data_dir=cache/'data')
    result={'scope':'agent_core_real_model_fuyao','checked_at':datetime.now(timezone.utc).isoformat(),'checks':{},'runs':[]}
    app=create_api(settings)
    def check(value,name):
        if not value:
            raise AssertionError(name)
        result['checks'][name]='passed'
        print(json.dumps({'check':name,'status':'passed'}),flush=True)
    def get(client,path):
        response=client.get(path)
        response.raise_for_status()
        return response.json()
    def post(client,path,body):
        response=client.post(path,json=body)
        response.raise_for_status()
        return response.json()
    def wait(client,rid):
        limit=time.monotonic()+240
        while time.monotonic()<limit:
            state=get(client,'/api/runs/'+rid)
            if state['status'] in {'completed','partial','failed','blocked','recoverable_failed','awaiting_input','awaiting_approval'}:
                return state
            time.sleep(.2)
        raise TimeoutError('real_agent_run_timeout')
    def start(client,thread,goal):
        return post(client,f'/api/threads/{thread}/runs',{'goal':goal,'mode':'agent','budget':{
            'context_tokens':32000,'model_calls':8,'model_tokens':250000,'tool_attempts':16,'duration_seconds':900}})['id']
    try:
        with TestClient(app) as client:
            client.headers['X-CSRF-Token']=get(client,'/api/session')['csrf_token']
            thread=post(client,'/api/threads',{'title':'Agent 基础能力真实验证'})['id']
            rid=start(client,thread,'怎么研究股票')
            state=wait(client,rid)
            check(state['status']=='completed' and not state['tasks'] and not state['tool_calls'],'methodology_completed_without_tools')
            report=get(client,f'/api/runs/{rid}/report')['content']
            check(report.get('answer_mode')=='knowledge' and not report['gaps'] and bool(report.get('narrative')),'methodology_has_useful_answer')
            result['runs'].append({'case':'methodology','id':rid,'status':state['status'],'models':state['runtime']['model_calls'],'tools':state['attempts_used']})
            rid=start(client,thread,'请帮我研究一只股票')
            state=wait(client,rid)
            check(state['status']=='awaiting_input' and bool(state['pending_input']) and not state['tool_calls'],'missing_stock_requests_clarification')
            post(client,f'/api/runs/{rid}/input',{'question_id':state['pending_input']['id'],'answer':'贵州茅台 600519.SH；研究最近60个交易日历史价格变化、波动率和最大回撤，以及最近已披露年报营业收入和归母净利润。'})
            state=wait(client,rid)
            check(state['status']=='completed','single_stock_continues_after_clarification')
            artifact=get(client,f'/api/runs/{rid}/report')
            report=artifact['content']
            check(report['status']=='complete' and report['validation_status']=='passed' and bool(report.get('narrative')),'single_stock_evidence_grounded_answer')
            check(report.get('goal_coverage') and all(c['status']=='satisfied' for c in report['goal_coverage']),'single_stock_objectives_satisfied')
            check(any(c.get('field')=='historical_price_change_pct' for c in report['claims']) and any(c.get('field')=='operating_income' for c in report['claims']),'single_stock_price_and_financial_facts')
            result['runs'].append({'case':'clarified_single_stock','id':rid,'status':state['status'],'models':state['runtime']['model_calls'],'tools':state['attempts_used'],'facts':sum(c['kind']=='fact' for c in report['claims'])})
            for format_ in ('markdown','html','csv','evidence'):
                response=client.get(f"/api/artifacts/{artifact['id']}/export",params={'format':format_})
                check(response.status_code==200 and bool(response.content),'grounded_export_'+format_)
            rid=start(client,thread,'它最近30个交易日的历史价格变化、波动率和最大回撤怎样？只比较这个较短窗口，说明口径。')
            state=wait(client,rid)
            check(state['status']=='completed','pronoun_followup_and_changed_window_completed')
            report=get(client,f'/api/runs/{rid}/report')['content']
            checks=[c for c in state['tool_calls'] if c['tool']=='calculate_price_comparison']
            check(checks and checks[-1]['parameters']['trading_days']==30,'followup_uses_new_window')
            evidence=get(client,f'/api/runs/{rid}/evidence')['evidence']
            check(evidence and all(e['run_id']==rid for e in evidence),'followup_fetches_fresh_evidence')
            check(report.get('narrative') and report.get('goal_coverage'),'followup_explains_results_with_coverage')
            result['runs'].append({'case':'pronoun_followup','id':rid,'status':state['status'],'models':state['runtime']['model_calls'],'tools':state['attempts_used']})
            owner=app.state.research.db.one('SELECT session_id FROM runs WHERE id=?',(rid,))['session_id']
            result['storage']=str(cache.relative_to(ROOT))
            (cache/'browser-handoff.local.json').write_text(json.dumps({'cookies':dict(client.cookies),'thread':thread,'run':rid,'owner':owner}),encoding='utf-8')
            check(len(get(client,f'/api/threads/{thread}/messages')['messages'])>=8,'thread_messages_persisted')
        result['status']='passed'
    except Exception as error:
        result['status']='failed'
        result['error_type']=type(error).__name__
        # Assertion labels are authored by this script; do not print provider bodies.
        if isinstance(error,AssertionError):
            result['failed_check']=str(error)
    out=ROOT/'docs/verification/agent-core-live.local.json'
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':result['status'],'checks':len(result['checks']),'report':str(out.relative_to(ROOT))}),flush=True)
    return 0 if result['status']=='passed' else 1


if __name__=='__main__':
    raise SystemExit(main())
