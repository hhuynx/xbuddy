"""Isolated NiceGUI verification server; synthetic model/data, no .env secrets."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]

import httpx
from nicegui import app,ui
from server.api.routes import install_api
from server.config import Settings
from fastapi import Depends, Query
from server.security import write_owner
from server.ui.workbench import register_workbench
from server.storage.database import encode,fingerprint
from test_agent_core import ScriptModel,entry,finish
from test_harness import price_plan
from test_data_foundation import response_for
from test_narrative import rich_sections, recovery_script

data=Path(os.environ['XBUDDY_QA_DATA_DIR']).resolve()
if not data.is_relative_to((ROOT/'.cache/verification').resolve()):
    raise ValueError('Verification data must stay in the private workspace cache')


def script(context,count):
    goal=context['goal']
    if '分析恢复验证' in goal:
        return recovery_script(context,count)
    if context.get('stage')=='review':
        result=finish(context)
        if '完整文字分析验证' in goal:
            result['sections']=rich_sections(context)
        if '分类展示验证' in goal:
            refs=result['sections'][0]['paragraphs'][0]['fact_refs']
            result['sections'][0]['paragraphs'] += [
                {'kind':'fact','text':'{{'+refs[0]+'}}','fact_refs':[refs[0]]},
                {'kind':'uncertain','text':'历史样本不足以判断未来表现。','fact_refs':[]},
            ]
        if '补充任务验证' in goal and context['can_extend']:
            result=finish(context,missing=True)
            result.update(action='extend',sections=[],tasks=price_plan()['tasks'][1:])
        if '回答校验验证' in goal:
            result['sections'][0]['paragraphs'][0]['text']='历史收益为999%。'
        return result
    if '原文检索验证' in goal:
        return entry('research',tasks=[{'id':'source','title':'检索年报风险原文','tool':'search_documents',
            'arguments':{'query':'行业竞争风险'},'depends_on':[]}],
            objectives=[{'id':'risk','text':'核对风险原文','required_fields':['document_excerpt']}])
    if '结构错误验证' in goal:
        return entry('answer',answer='可以先研究商业模式与财务资料。') | {'intent_note':'synthetic extra field'}
    if context.get('selected_research_direction') == '查看已披露财务':
        return entry('research',resolved_goal='核对茅台已披露年报营业收入',
            tasks=[{'id':'financial','title':'核对已披露年报','tool':'get_financial_metrics',
                'arguments':{'thscode':'600519.SH','period':'annual','limit':1},'depends_on':[]}],
            objectives=[{'id':'revenue','text':'核对年报营业收入','required_fields':['operating_income']}])
    if '发布审核验证' in goal:
        return entry('answer',answer='先研究商业模式，再核对财务资料的来源与口径。' if context.get('repair_errors') else '这只股票明天上涨毫无悬念。')
    if '怎么研究股票' in goal:
        return entry('answer',answer='研究股票可以从商业模式、行业竞争、财务质量、估值和风险开始。先确定研究问题，再收集有来源的资料，最后记录结论与待验证问题。')
    if '布局问答' in goal:
        return entry('answer',answer='### 研究框架\n\n先理解**商业模式**，再检查财务质量。\n\n- 收集原始资料\n- 保留不确定性\n\n这是布局验收的构造回答。')
    if '研究一只股票' in goal and not any(m['role']=='user' and m['text']=='600519.SH' for m in context['conversation']):
        return entry('clarify',question={'text':'想研究哪只股票？','options':['600519.SH','000858.SZ']})
    if '补充任务验证' in goal:
        return price_plan() | {'tasks':[price_plan()['tasks'][0]],'objectives':[{'id':'comparison','text':'比较历史价格','required_fields':['historical_price_change_pct']}]}
    return price_plan()


service=install_api(app,Settings(data_dir=data,fuyao_api_key='synthetic-secret'),
    httpx.MockTransport(lambda r:httpx.Response(200,json=response_for(r))),model=ScriptModel(script))
register_workbench(service)


@app.post('/_qa/history')
def history(pairs:int=Query(30,ge=1,le=60),owner:str=Depends(write_owner)):
    thread=service.create_thread(owner,'长对话布局验证','')
    run=service.create_run(owner,thread['id'],'历史布局样本','manual')
    record=service.db.one('SELECT * FROM runs WHERE id=?',(run['id'],))
    with service.db.connect() as conn:
        for i in range(pairs):
            service.dialogue.add_message(conn,record,'user',f'qa-user:{i}',f'历史提问 {i}：希望了解公司财务数据的研究方法，以及如何核对不同会计年度的信息。')
            service.dialogue.add_message(conn,record,'assistant',f'qa-answer:{i}',f'历史回答 {i}：先核对报告期、披露时点与统计口径，再形成有依据的观察。\n\n**研究方法**\n\n- 保留资料来源\n- 区分事实与推断\n- 记录待验证问题',answer_mode='knowledge')
        service.db.event(conn,owner,run['id'],'qa.history_seeded',{'messages':pairs*2})
    return {'thread':thread['id'],'run':run['id']}


@app.post('/_qa/legacy-publication/{artifact_id}')
def legacy_publication(artifact_id:str,owner:str=Depends(write_owner)):
    artifact=service.artifacts.verify(owner,artifact_id)
    if artifact['content'].get('answer_mode')!='knowledge':
        raise ValueError('This fixture requires an existing general knowledge answer')
    report=artifact['content']
    bad='这只股票明天上涨毫无悬念。'
    report['narrative'][0]['paragraphs'][0]['text']=bad
    row=service.db.one("SELECT * FROM thread_messages WHERE run_id=? AND role='assistant'",(artifact['run_id'],))
    content=json.loads(row['content_json']) | {'text':bad}
    with service.db.connect() as conn:
        conn.execute('UPDATE artifacts SET content_json=? WHERE id=?',(encode(report),artifact_id))
        conn.execute('UPDATE artifact_checksums SET content_sha256=? WHERE artifact_id=?',(fingerprint(report),artifact_id))
        conn.execute('UPDATE thread_messages SET content_json=?,content_sha256=? WHERE id=?',(encode(content),fingerprint(content),row['id']))
    return {'status':'seeded_legacy_published_content'}

ui.run(host='127.0.0.1',port=int(os.environ.get('XBUDDY_QA_PORT','8107')),show=False,reload=False)
