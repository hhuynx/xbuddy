"""Isolated browser QA server. Synthetic model/data; never loads .env credentials.

This entry point is for verification only. Production uses server.main.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

import httpx
from nicegui import app, ui

from server.api.routes import install_api
from server.config import Settings
from server.harness import runtime
from server.ui.workbench import register_workbench
from test_data_foundation import response_for
from test_harness import FakeModel, price_plan

data = Path(os.environ['XBUDDY_QA_DATA_DIR']).resolve()
if not data.is_relative_to((ROOT / '.cache/verification').resolve()):
    raise ValueError('QA data must stay in the private verification directory')


class FaultModel(FakeModel):
    async def complete(self, messages):
        context = json.loads(messages[-1]['content'])
        goal = context.get('goal', '')
        if '构造模型结构测试' in goal:
            self.calls.append(context)
            return {'tasks': [{'invented': True}]}, {'total_tokens': 80, 'token_status': 'reported'}
        if '构造限流测试' in goal and 'previous_plan' not in context:
            self.calls.append(context)
            task = price_plan()['tasks'][1] | {'arguments': price_plan()['tasks'][1]['arguments'] | {'thscode': '000001.SZ'}}
            return {'tasks': [task], 'limitations': []}, {'total_tokens': 80, 'token_status': 'reported'}
        return await super().complete(messages)


def transport(request):
    if request.url.params.get('thscode') == '000001.SZ':
        return httpx.Response(429, json={'message': 'synthetic-secret'})
    return httpx.Response(200, json=response_for(request))


service = install_api(app, Settings(data_dir=data, fuyao_api_key='synthetic-secret'),
                      httpx.MockTransport(transport), model=FaultModel())
original_report, failed = runtime.save_report, set()


def fail_once(*args, **kwargs):
    result = original_report(*args, **kwargs)
    artifact = service.resource('artifacts', result, args[1])
    if '构造报告恢复测试' in artifact['content']['goal'] and artifact['run_id'] not in failed:
        failed.add(artifact['run_id'])
        raise RuntimeError('synthetic-secret report save fault')
    return result


runtime.save_report = fail_once
original_fetch = service.fuyao.fetch


async def slow_fetch(path, params):
    await asyncio.sleep(.5)
    return await original_fetch(path, params)


service.fuyao.fetch = slow_fetch
register_workbench(service)
ui.run(host='127.0.0.1', port=int(os.environ.get('XBUDDY_QA_PORT', '8097')), show=False, reload=False)
