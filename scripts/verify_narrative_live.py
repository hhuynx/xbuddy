"""Configured model + synthetic financial responses in fresh, isolated storage.

Never reads historical conversations or research databases. No external financial calls.
"""
import json
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]

import httpx
from fastapi.testclient import TestClient
from server.api.routes import create_api
from server.config import Settings
from server.harness.model import JsonModel
from server.publication import publication_errors
from test_data_foundation import response_for


def main():
    settings=Settings.load()
    if not JsonModel(settings).configured:
        print(json.dumps({'status':'unavailable','reason':'model_not_configured'}))
        return 2
    cache=ROOT/'.cache/verification'/('narrative-synthetic-model-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f'))
    cache.mkdir(parents=True)
    settings=replace(settings,data_dir=cache,database_url='',redis_url='',embedded_worker=True,
        fuyao_api_key='synthetic-secret',ifind_mcp_url='',ifind_api_key='')
    requests=[]
    def synthetic(request):
        requests.append(request.url.path)
        return httpx.Response(200,json=response_for(request))
    app=create_api(settings,httpx.MockTransport(synthetic))
    result={'scope':'configured_model_with_synthetic_financial_samples','model':settings.llm_model,
        'storage':str(cache.relative_to(ROOT)),'external_financial_calls':0,'historical_data_read':False}
    try:
        with TestClient(app) as client:
            client.headers['X-CSRF-Token']=client.get('/api/session').json()['csrf_token']
            thread=client.post('/api/threads',json={'title':'合成样本：真实模型文字分析验证'}).json()['id']
            response=client.post(f'/api/threads/{thread}/runs',json={'mode':'agent',
                'goal':'合成数据测试：仅使用工具返回的构造样本，核对600519.SH的最近一个同口径合并年报营业收入和归母净利润，说明主要观察、解释与不足。不获取行情，不推断经营事件，不提供交易建议。',
                'budget':{'model_calls':4,'model_tokens':200000,'context_tokens':48000,'duration_seconds':360,'tool_attempts':8}})
            response.raise_for_status()
            run=response.json()['id']
            end=time.monotonic()+300
            while time.monotonic()<end:
                state=client.get('/api/runs/'+run).json()
                if state['status'] in {'completed','partial','failed','blocked','awaiting_approval','awaiting_input','recoverable_failed'}:break
                time.sleep(.2)
            else:raise AssertionError('run_timeout')
            result.update(run_status=state['status'],model_calls=state['runtime']['model_calls'],
                model_tokens=state['runtime']['model_tokens'],synthetic_tool_calls=len(requests))
            response=client.get(f'/api/runs/{run}/report')
            response.raise_for_status()
            report=response.json()['content']
            paragraphs=[p for s in report.get('narrative',[]) for p in s['paragraphs']]
            if not any(p['kind']=='inference' for p in paragraphs):raise AssertionError('no_validated_analysis')
            if report['validation_status']!='passed' or publication_errors(report):raise AssertionError('analysis_validation_failed')
            artifact=state['artifacts'][-1]['id']
            if client.get('/api/artifacts/'+artifact).status_code!=200:raise AssertionError('artifact_verification_failed')
            if client.get('/api/artifacts/'+artifact+'/export?format=html').status_code!=200:raise AssertionError('export_verification_failed')
            result.update(status='passed',report_status=report['status'],sections=len(report['narrative']),
                paragraphs=len(paragraphs),analysis_characters=sum(len(p['text']) for p in paragraphs if p['kind']!='fact'),
                facts=sum(c['kind']=='fact' for c in report['claims']),authoring=report['method']['authoring'])
    except Exception as exc:
        result.update(status='failed',error_type=type(exc).__name__)
        if isinstance(exc,AssertionError):result['failed_check']=str(exc)
        if 'run' in locals():
            metadata=app.state.research.dialogue.state(run)
            result['planning_error']=metadata.get('planning_error')
            result['review_error']=metadata.get('review_error')
            result['recovery_error']=metadata.get('recovery_error')
    (cache/'result.local.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False),flush=True)
    return 0 if result['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
