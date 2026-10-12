"""Narrow, local redirection for bare trading decisions; no financial judgment."""
import re

DECISION = re.compile(r'能不能\s*(?:买|卖|加仓|减仓)|可不可以\s*(?:买|卖)|(?:能|可以)(?:买入|卖出|购买|买|卖)(?:吗|么)|(?:该不该|要不要|值不值得|是否应该|是否适合|值得|应该)\s*(?:现在|直接)?\s*(?:买|卖|加仓|减仓)|(?:买还是卖|买入还是卖出)')
SCOPE_NOTE = '当前仅研究用户确认的事实与风险范围，原问题中的交易决策部分超出产品支持范围。'
RESEARCH_SCOPE = re.compile(r'财务|利润|营收|营业收入|估值|年报|现金流|市盈率|市净率|基本面|历史|波动|回撤|风险|行业|比较|研究方法|判断方法|如何判断|怎么判断')


def trading_question(goal: str) -> dict | None:
    if not DECISION.search(goal) or RESEARCH_SCOPE.search(goal):
        return None
    return {'intent':'clarify', 'answer':'', 'tasks':[], 'objectives':[], 'limitations':[], 'unresolved_goals':[],
        'question':{'text':'我不能提供买入或卖出建议，也不能承诺收益或预测确定的涨跌。可以帮助你核对这只股票的已披露数据和研究风险。你希望先研究哪一方面？',
            'options':['查看已披露财务', '查看历史行情表现', '梳理研究风险与数据边界']}}
