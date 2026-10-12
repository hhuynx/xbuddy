"""Local publication policy shared by generation, persistence and delivery.

This is a conservative expression check, not a semantic guarantee. User questions
and raw evidence are retained as inputs; they are never treated as authored advice.
"""
from __future__ import annotations

import re
import unicodedata
from copy import deepcopy

PARAGRAPH_LABELS = {'fact': '事实', 'inference': '推断', 'uncertain': '不确定信息', 'knowledge': '一般知识'}
BLOCKED_MESSAGE = '这份回答未通过当前发布校验，内容已停止展示。请重新发起研究或重新核验报告。'


def normalized(text: str) -> str:
    value = unicodedata.normalize('NFKC', text).casefold()
    value = ''.join(c for c in value if unicodedata.category(c) != 'Cf')
    value = re.sub(r'[`*_~]', '', value)
    return re.sub(r'(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])', '', value)


NEGATIVE = re.compile(r'(?:不(?:能|会|应|要|宜|可|必|建议|推荐)?|无法|不得|禁止|避免|拒绝|未必|没有|并非|勿)(?:直接|立即|再|继续|能够|提供|给出|做出|作出|作|做|确定|任何|承诺|向用户|向你|关于|给你|对外|进行|保证|绝对|总是|一定|必然|肯定|将会|将要|将|这只|这支|该|此|股价|股票|价格|收益|回报|未来|明天|明日|下周|下月|的|可以|也|能|会|应|就|再|是|地|有|说|其)*$')
ACTION = re.compile(r'买入|卖出|加仓|减仓|抄底|建仓|清仓|补仓|做多|做空|买进|卖空|购入|购进|卖掉|减持|持有|购买|出售')
REFUSED_ACTIONS = re.compile(r'(?:不能|无法|不得|不会|不)(?:向用户|向你)?(?:直接)?(?:提供|产出|输出|给出|作出|做出)(?:任何|直接)?(?:' + ACTION.pattern + r')(?:\s*[/、或和及]\s*(?:' + ACTION.pattern + r'))*(?:结论|建议|指令|判断|决策)?')
DIRECTIVE = re.compile(r'建议|推荐|应当|应该|应立即|务必|必须|赶紧|立即|现在|请|最好|值得|可以考虑')
CERTAINTY = re.compile(r'毫无悬念|毫无疑问|确定|肯定|一定|必然|必定|必将|必涨|必跌|准涨|准跌|绝对|稳涨|稳跌|铁定|百分之百|板上钉钉|注定')
DIRECTION = re.compile(r'涨|跌|走高|走低|上行|下行|翻倍|突破|创新高|创新低')
FUTURE = re.compile(r'明天|明日|下周|下月|未来|接下来|将|必然|一定|必定|肯定|必涨|必跌|准涨|准跌|铁定|注定')
SECURITY = re.compile(r'股|证券|大盘|指数|基金|债券|币价|行情|价格')
PROMISE = re.compile(r'(?:保证|确保|保障|承诺|锁定).{0,12}(?:收益|回报|盈利|利润|赚钱)|(?:收益|回报).{0,12}(?:保证|保障)|稳赚|保本|无风险获利|零风险赚钱|不会亏|绝不亏')
ENGLISH = re.compile(r'\b(?:buy|sell|short|purchase|accumulate)\s+(?:now|immediately|this\s+(?:stock|share)|the\s+(?:stock|share))\b|\b(?:should|must|recommend(?:ed)?\s+to)\s+(?:buy|sell|purchase|hold)\b|\b(?:guarantee\w*|ensure\w*|assure\w*)\s+(?:\w+\s+){0,3}(?:profit\w*|return\w*)\b|\b(?:risk[ -]?free|certain|guaranteed)\s+(?:profit\w*|return\w*)\b|\b(?:will|definitely|certainly|surely)\s+(?:\w+\s+){0,3}(?:rise|fall|rally|drop|double)\b')


def safe_text(text: str) -> bool:
    value = normalized(text)
    for clause in re.split(r'[。！？!?;；，,\n]|但是|不过|然而', value):
        # Keep English word boundaries; compact Chinese formatting separately.
        for match in ENGLISH.finditer(clause):
            if not re.search(r'\b(?:not|never|cannot|no)\s+(?:\w+\s+){0,3}$', clause[:match.start()]):
                return False
        compact = re.sub(r'\s+', '', clause)
        refusals = [(m.start(), m.end()) for m in REFUSED_ACTIONS.finditer(compact)]
        for match in ACTION.finditer(compact):
            if any(start <= match.start() and match.end() <= end for start, end in refusals):
                continue
            before, after = compact[:match.start()], compact[match.end():]
            if NEGATIVE.search(before):
                continue
            traditional = match[0] in {'买入','卖出','加仓','减仓','抄底','建仓','清仓','补仓','做多','做空','买进','卖空'}
            directed = DIRECTIVE.search(before[-18:]) or re.match(r'(?:这只|该|此|这支|上述|这些)?(?:股票|证券|基金|债券)', after)
            if traditional or directed:
                return False
        for match in PROMISE.finditer(compact):
            if not NEGATIVE.search(compact[:match.start()]):
                return False
        # Historical observations and explicitly uncertain scenarios are allowed.
        if DIRECTION.search(compact) and FUTURE.search(compact) and (SECURITY.search(compact) or CERTAINTY.search(compact)):
            forecasts = list(CERTAINTY.finditer(compact)) + list(re.finditer(r'将会|将要|将', compact))
            uncertain = re.search(r'可能|或许|也许|未必|不一定|无法判断|不能确定|无法确定|难以判断|无法预测|不能预测|不预测|无法保证|不能保证|不保证|取决于|假设|如果|情景|情境|是否|不代表|不意味着', compact)
            if any(not NEGATIVE.search(compact[:m.start()]) and not (uncertain and m[0].startswith('将')) for m in forecasts):
                return False
            if SECURITY.search(compact) and not forecasts and not uncertain:
                return False
    return True


def safe_fragments(texts) -> bool:
    """Check individual fields and their joined form to catch split expressions."""
    items = [t for t in texts if isinstance(t, str) and t]
    return all(safe_text(t) for t in items) and safe_text(' '.join(items))


def publication_errors(report: dict) -> list[str]:
    if report.get('validation_status') == 'failed' and (report.get('claims') or report.get('narrative')):
        return ['publication_unverified_conclusion']
    texts = list(report.get('limitations', []))
    texts.append(report.get('revision_note', ''))
    for claim in report.get('claims', []):
        if claim.get('kind') not in {'fact','inference','uncertain'}:
            return ['publication_kind_invalid']
        texts += [claim.get('text', ''), *claim.get('limitations', [])]
    for section in report.get('narrative', []):
        texts.append(section.get('title', ''))
        for paragraph in section.get('paragraphs', []):
            if paragraph.get('kind') not in PARAGRAPH_LABELS:
                return ['publication_kind_invalid']
            texts.append(paragraph.get('text', ''))
    for collection in ('gaps', 'execution_issues', 'execution_failures', 'goal_coverage'):
        for item in report.get(collection, []):
            texts += [item.get('detail', ''), item.get('reason', '')]
    for note in report.get('knowledge_context', {}).get('user_notes', []):
        texts.append(note.get('text', ''))
    return [] if safe_fragments(texts) else ['publication_prohibited_expression']


def enforce_report(report: dict) -> dict:
    errors = publication_errors(report)
    if errors:
        # Keep provenance, but remove every authored conclusion on a failed gate.
        report.update(status='blocked', validation_status='failed', claims=[], narrative=[],
                      limitations=['回答内容未通过发布校验，无法展示研究结论。'],
                      gaps=[{'reason':errors[0], 'detail':'请重新研究或保存新的校验版本。'}])
        report['validation_errors'] = list(dict.fromkeys(report.get('validation_errors', []) + errors))
        report.pop('execution_issues', None)
        report.pop('execution_failures', None)
        report.pop('goal_coverage', None)
        report.pop('revision_note', None)
        report.get('knowledge_context', {}).pop('user_notes', None)
    return report


def artifact_view(artifact: dict) -> dict:
    """Mask old prohibited content in run/version lists; preserve stored history."""
    if artifact.get('kind') == 'research_report' and publication_errors(artifact['content']):
        artifact = deepcopy(artifact)
        enforce_report(artifact['content'])
        artifact['publication_status'] = 'blocked'
    return artifact


def agent_view(state: dict) -> dict:
    """Do not deliver a legacy draft as an accepted Agent answer or analysis."""
    entry = state.get('entry') or {}
    review = state.get('review') or {}
    bad_entry = not safe_text(entry.get('answer', ''))
    bad_review = publication_errors({'narrative':review.get('sections', []),
                                     'goal_coverage':review.get('coverage', []),
                                     'limitations':review.get('limitations', [])})
    recovery = state.get('recovery_analysis') or {}
    bad_recovery = publication_errors({'narrative':recovery.get('sections',[]),'limitations':recovery.get('limitations',[])})
    if bad_entry or bad_review or bad_recovery:
        state = deepcopy(state)
        if bad_entry:
            state['entry']['answer'] = BLOCKED_MESSAGE
        if bad_review:
            state.pop('review', None)
        if bad_recovery:
            state.pop('recovery_analysis',None)
        state['publication_status'] = 'blocked'
    return state
