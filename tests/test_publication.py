"""Publication policy at model repair, storage, legacy reads and export boundaries."""
from __future__ import annotations

import json

import pytest

from server.harness.dialogue import validate_entry
from server.harness.reporting import save_report
from server.publication import BLOCKED_MESSAGE, publication_errors, safe_text
from server.storage.database import encode, fingerprint
from test_agent_core import core, entry, finish, start
from test_harness import price_plan, wait

UNSAFE = [
    '这只股票明天上涨毫无悬念。', '现在应当购入这只股票。', '这笔投资能够确保回报。',
    '这只股票明天上涨。', '该股下周必定走高。', '价格将下跌。', '这只股票明天肯定不会上涨。',
    '建议继续持有这只股票。', '请立即卖掉这只股票。', '不要犹豫，立即买入。',
    '不保证收益，但现在应当购入这只股票。', '回报得到保障。',
    '现 在 应 当 购\u200b入 这只股票。', '现在应当购\n入这只股票。', '确保**回报**。',
    'You should buy this stock.', 'This stock will definitely rise tomorrow.',
    'We ensure investment returns.', 'ＢＵＹ ＮＯＷ',
]


@pytest.mark.parametrize('text', UNSAFE)
def test_unsupported_expressions_rejected_before_model_publication(text):
    assert not safe_text(text)
    with pytest.raises(ValueError, match='prohibited_expression'):
        validate_entry(None, entry('answer', answer=text), [], '投资研究问题')


@pytest.mark.parametrize('text', [
    '不能给出直接买入建议。可以研究已披露的财务数据。',
    '不保证收益，也不建议加仓。', '股票明天可能上涨，也可能下跌。',
    '不能保证未来股价上涨。', '不能保证这只股票未来一定上涨。',
    '股价不一定上涨。', '历史股价上涨，不代表未来涨跌。',
    '样本期股价下跌，营业收入有所增长。', '不含分红等持有回报。',
    '先核对报告期、披露时点和合并利润表统计口径。',
    'Do not buy now. Study historical disclosures.',
    'We cannot ensure returns. Future performance remains uncertain.',
])
def test_historical_observations_uncertainty_and_refusal_remain_available(text):
    assert safe_text(text)
    assert validate_entry(None, entry('answer', answer=text), [], '投资研究问题')['answer'] == text


@pytest.mark.parametrize('bad', UNSAFE[:3])
def test_answer_expression_failure_uses_existing_bounded_repair(core, bad):
    client, thread, _, model, requests, _ = core
    model.script = lambda c,n:entry('answer', answer=bad if n==1 else '先研究商业模式，再核对财务资料的来源与口径。')
    state = wait(client, start(client, thread, '怎么研究股票'))
    assert state['status']=='completed' and state['runtime']['model_calls']==2
    assert not requests
    messages = client.get(f'/api/threads/{thread}/messages').json()['messages']
    assert bad not in encode(messages)
    assert '商业模式' in messages[-1]['content']['text']


@pytest.mark.parametrize('bad', UNSAFE[:3])
def test_research_expression_failure_repairs_without_repeating_financial_tools(core, bad):
    client, thread, _, model, requests, _ = core
    def script(c,n):
        if c.get('stage')!='review':
            return price_plan()
        result = finish(c)
        if n==2:
            result['sections'][0]['paragraphs'][0]['text']=bad
        return result
    model.script=script
    state=wait(client,start(client,thread,'比较股票历史价格'))
    assert state['status']=='completed' and state['runtime']['model_calls']==3
    assert len(requests)==3
    artifact=client.get('/api/artifacts/'+state['artifacts'][0]['id']).json()
    assert not publication_errors(artifact['content']) and bad not in encode(artifact)


@pytest.mark.parametrize('mode', ['answer','research'])
def test_persistent_rejection_terminates_with_visible_execution_failure(core, mode):
    client, thread, _, model, requests, _ = core
    def script(c,n):
        if mode=='answer':
            return entry('answer',answer=UNSAFE[0])
        if c.get('stage')!='review':
            return price_plan()
        result=finish(c)
        result['sections'][0]['paragraphs'][0]['text']=UNSAFE[0]
        return result
    model.script=script
    state=wait(client,start(client,thread,'投资研究方法与历史价格'))
    assert state['status'] in {'failed','partial','blocked'}
    assert state['runtime']['model_calls']==(2 if mode=='answer' else 3)
    artifact=client.get('/api/artifacts/'+state['artifacts'][0]['id']).json()
    assert artifact['content']['execution_failures'] and not artifact['content'].get('narrative')
    assert not any(g['reason'].startswith('model_output_validation_failed') for g in artifact['content']['gaps'])
    assert UNSAFE[0] not in encode(artifact)
    assert len(requests)==(0 if mode=='answer' else 3)


def test_legacy_valid_fingerprints_do_not_bypass_delivery_policy(core):
    client,thread,app,model,_,_=core
    model.script=lambda c,n:entry('answer',answer='先核对财务数据来源，再形成研究问题。')
    state=wait(client,start(client,thread,'怎么研究股票'))
    aid=state['artifacts'][0]['id']
    service=app.state.research
    artifact=client.get('/api/artifacts/'+aid).json()
    report=artifact['content']
    report['narrative'][0]['paragraphs'][0]['text']=UNSAFE[0]
    row=service.db.one("SELECT * FROM thread_messages WHERE run_id=? AND role='assistant'",(state['id'],))
    content=json.loads(row['content_json']) | {'text':UNSAFE[0]}
    service.dialogue.put(state['id'], entry=entry('answer',answer=UNSAFE[0]))
    # Simulate an old policy publishing the content legitimately: fingerprints
    # agree, so denial must come from the publication gate, not hash failure.
    with service.db.connect() as conn:
        conn.execute('UPDATE artifacts SET content_json=? WHERE id=?',(encode(report),aid))
        conn.execute('UPDATE artifact_checksums SET content_sha256=? WHERE artifact_id=?',(fingerprint(report),aid))
        conn.execute('UPDATE thread_messages SET content_json=?,content_sha256=? WHERE id=?',(encode(content),fingerprint(content),row['id']))
    messages=client.get(f'/api/threads/{thread}/messages').json()['messages']
    assert messages[-1]['content']['text']==BLOCKED_MESSAGE
    assert messages[-1]['content']['publication_status']=='blocked'
    assert UNSAFE[0] not in encode(messages)
    assert json.loads(service.db.one('SELECT content_json FROM thread_messages WHERE id=?',(row['id'],))['content_json'])['text']==UNSAFE[0]
    assert client.get('/api/runs/'+state['id']+'/report').status_code==409
    for url in ('/api/runs/'+state['id'], '/api/runs/'+state['id']+'/reports'):
        response=client.get(url)
        assert response.status_code==200 and UNSAFE[0] not in response.text
    for suffix in ('','/print','/export?format=markdown','/export?format=html','/export?format=csv','/export?format=evidence'):
        response=client.get('/api/artifacts/'+aid+suffix)
        assert response.status_code==409,(suffix,response.text)
        assert UNSAFE[0] not in response.text
    assert len(model.calls)==1


def test_final_storage_gate_blocks_authored_text_without_rewriting_user_request(core):
    client,thread,app,model,_,_=core
    model.script=lambda c,n:entry('answer',answer='可以研究已披露的数据，并记录来源。')
    state=wait(client,start(client,thread,UNSAFE[0]))
    service=app.state.research
    report=client.get('/api/artifacts/'+state['artifacts'][0]['id']).json()['content']
    report['narrative'][0]['paragraphs'][0]['text']=UNSAFE[1]
    aid=save_report(service,service.db.one('SELECT session_id FROM runs WHERE id=?',(state['id'],))['session_id'],state['id'],report,new_version=True,expected_version=1)
    result=client.get('/api/artifacts/'+aid).json()['content']
    assert result['status']=='blocked' and result['validation_status']=='failed'
    assert not result['claims'] and not result['narrative'] and result['gaps']
    assert result['goal']==UNSAFE[0] and UNSAFE[1] not in encode(result)


def test_exports_distinguish_narrative_categories_and_keep_references(core):
    client,thread,_,model,_,_=core
    def script(c,n):
        if c.get('stage')!='review':return price_plan()
        result=finish(c)
        refs=result['sections'][0]['paragraphs'][0]['fact_refs']
        result['sections'][0]['paragraphs'] += [
            {'kind':'fact','text':'{{'+refs[0]+'}}','fact_refs':[refs[0]]},
            {'kind':'uncertain','text':'历史样本不足以判断未来表现。','fact_refs':[]},
        ]
        return result
    model.script=script
    state=wait(client,start(client,thread,'比较股票历史价格'))
    assert state['status']=='completed'
    aid=state['artifacts'][0]['id']
    report=client.get('/api/artifacts/'+aid).json()['content']
    for suffix in ('/export?format=markdown','/export?format=html','/print'):
        response=client.get('/api/artifacts/'+aid+suffix)
        assert response.status_code==200
        for label in ('事实','推断','不确定信息'):
            assert label in response.text
        assert report['narrative'][0]['paragraphs'][0]['fact_refs'][0] in response.text
    assert report['claims'][0]['reference'] and report['claims'][0]['unit'] and report['claims'][0]['as_of']


def test_failed_integrity_cannot_publish_safe_sounding_narrative(core):
    client,thread,app,model,_,_=core
    model.script=lambda c,n:entry('answer',answer='这段知识解释仅用于构造完整性失败样本。')
    state=wait(client,start(client,thread,'研究方法'))
    service=app.state.research
    artifact=client.get('/api/artifacts/'+state['artifacts'][0]['id']).json()
    report=artifact['content'] | {'validation_status':'failed','validation_errors':['narrative_evidence_changed']}
    aid=save_report(service,service.db.one('SELECT session_id FROM runs WHERE id=?',(state['id'],))['session_id'],state['id'],report,new_version=True,expected_version=1)
    result=client.get('/api/artifacts/'+aid).json()['content']
    assert result['status']=='blocked' and not result['narrative'] and not result['claims']
    for suffix in ('/export?format=markdown','/export?format=html','/print'):
        response=client.get('/api/artifacts/'+aid+suffix)
        assert response.status_code==200 and '完整性失败样本' not in response.text
        assert '未通过' in response.text


def test_knowledge_export_is_explicitly_unverified_general_knowledge(core):
    client,thread,_,model,_,_=core
    model.script=lambda c,n:entry('answer',answer='先研究商业模式，并记录待验证问题。')
    state=wait(client,start(client,thread,'怎么研究股票'))
    aid=state['artifacts'][0]['id']
    for fmt in ('markdown','html'):
        response=client.get('/api/artifacts/'+aid+'/export?format='+fmt)
        assert response.status_code==200 and '一般知识' in response.text
        assert '未作为本次金融数据核验结论' in response.text
