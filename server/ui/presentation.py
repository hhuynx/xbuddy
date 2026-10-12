"""Research views use stored evidence, original units and explicit quality states."""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone

from server.harness.reporting import checked_evidence

SHANGHAI = timezone(timedelta(hours=8))
LABELS = {"available": "可用", "unverified": "口径待复核", "missing": "数据缺失", "stale": "数据过期",
          "awaiting_input": "等待补充信息",
          "conflict": "数据冲突", "failed": "失败", "unauthorized": "无权限", "completed": "已完成",
          "queued": "排队中", "running": "执行中", "awaiting_approval": "等待确认", "paused": "已暂停",
          "stopped": "已停止", "partial": "部分完成", "blocked": "验证受阻", "pending": "待执行",
          "recoverable_failed": "可恢复失败", "interrupted": "已中断", "data_collection": "手动查询",
          "complete": "完整报告", "passed": "校验通过"}
FIELDS = {"operating_income": "营业收入", "parent_holder_net_profit": "归母净利润", "net_profit": "净利润",
          "basic_eps": "基本每股收益", "close_price": "收盘价", "historical_price_change_pct": "历史价格变化",
          "annualized_volatility_pct": "年化波动率", "maximum_drawdown_pct": "最大回撤"}
REASONS = {"unsupported_goal": "研究需求尚未完成或超出支持范围", "null_field": "原始字段为空",
           "publication_prohibited_expression": "回答内容未通过发布校验", "publication_kind_invalid": "回答分类未通过发布校验",
           "publication_unverified_conclusion": "未通过验证的报告包含结论",
           "goal_not_covered":"研究目标尚未覆盖", "budget_or_time_limit":"预算或时限不足以完成综合回答",
           "model_timeout":"模型请求超时", "model_invalid_json":"模型未返回合法 JSON",
           "model_output_incomplete":"模型回答被截断，未形成完整正文",
           "model_response_structure_invalid":"模型响应协议不匹配", "model_processing_failed":"模型处理失败",
           "model_request_or_json_failed":"模型请求失败", "replan_limit":"补充研究达到次数上限",
           "input_expired_or_stopped":"补充信息等待已结束",
           "no_verified_quantitative_findings": "尚无可验证结论", "undefined_nonpositive_baseline": "基数非正，无法计算常规相对差异",
           "value_unit_or_time_missing": "数值、单位或时点不完整", "budget_limit_or_rejected": "预算不足或扩展被拒绝",
           "budget_or_control_limit": "预算或运行控制阻止执行", "time_limit": "运行时限已到",
           "dependency_failed": "前置任务未完成", "argument_dependency_or_dispatch_validation_failed": "参数或前置证据未通过验证",
           "missing_credentials": "数据凭据未配置", "empty_records": "供应商返回空数据",
           "upstream_unavailable": "供应商暂不可用", "tool_timeout": "数据请求超时"}
EVENTS = {"analysis.recovered":"根据已核验数据恢复文字分析", "tasks.parallel":"并行执行独立任务", "run.created": "创建运行", "run.queued": "进入研究队列", "run.leased": "后台接管运行",
          "input.requested":"等待补充研究信息", "input.answered":"补充信息已保存", "research.observed":"检查研究目标与证据覆盖",
          "model.started": "模型开始规划", "model.finished": "模型调用结束", "plan.validated": "研究计划通过验证",
          "plan.edited": "研究计划已修改", "plan.replanned": "追加替代研究任务", "plan.failed": "规划未通过验证",
          "plan.replan_failed": "重规划未通过验证", "task.created": "创建研究任务", "tool.started": "开始工具调用",
          "tool.finished": "工具调用结束", "evidence.saved": "保存来源证据", "metric.saved": "保存计算结果",
          "approval.requested": "等待预算确认", "approval.decided": "预算决定已保存", "report.saved": "研究报告已保存",
          "run.pause": "研究已暂停", "run.resume": "研究继续", "run.stop": "研究已停止",
          "run.finished": "本次研究结束", "tool.recovered": "恢复调用账本", "run.deadline_reached": "运行时限已到",
          "run.recoverable_failed": "运行等待恢复", "task.failed": "任务失败", "task.blocked": "任务无法执行",
          "context.prepared": "模型上下文已保存", "context.compressed": "按预算压缩上下文",
          "memory.selected": "选择有效长期记忆", "memory.used": "模型实际使用记忆", "memory.withdrawn": "撤回失效记忆",
          "memory.decided": "候选记忆确认结果已保存", "memory.expired": "长期记忆已过期", "memory.conflict": "研究事实来源失效，等待复核", "run.linked": "关联前次研究"}


def stamp(value, short=False):
    if not value:
        return "时点待确认"
    try:
        date = datetime.fromtimestamp(value/1000, SHANGHAI) if isinstance(value, (int, float)) else datetime.fromisoformat(value).astimezone(SHANGHAI)
        return date.strftime("%m-%d %H:%M" if short else "%Y-%m-%d %H:%M")
    except (ValueError, TypeError, OSError):
        return "时点待确认"


def number(value, unit=""):
    if type(value) not in (int, float) or not math.isfinite(value):
        return "缺失"
    if unit == "CNY" and abs(value) >= 100000000:
        return f"{value/100000000:,.2f} 亿元"
    suffix = {"CNY": " 元", "CNY/share": " 元/股", "%": "%", "percent": "%"}.get(unit, " " + unit if unit else "")
    return f"{value:,.2f}" + suffix


def reason(value):
    if value.endswith(':schema_extra_field'):
        return '模型回复包含不支持的字段'
    if value.endswith(':schema_required_field'):
        return '模型回复缺少必要字段'
    if value.endswith(':schema_invalid'):
        return '模型回复的字段结构不符合要求'
    if value.endswith(':prohibited_expression'):
        return '模型回复未通过投资表达边界校验'
    if value.startswith('model_output_validation_failed'):
        return '模型回答的结构、证据引用或数值未通过验证'
    if value.startswith('model_http_'):
        return '模型接口返回 HTTP '+value.removeprefix('model_http_')
    return REASONS.get(value, LABELS.get(value, "该项资料或执行结果尚不可确认"))


def claim_text(claim):
    if claim["kind"] != "fact" or not isinstance(claim.get("value"), (int, float)) or claim.get("unit") == "Unix ms":
        return claim["text"]
    field = claim.get("field", "")
    base, _, derived = field.partition(".")
    label = FIELDS.get(base, base)
    if derived:
        label += " · " + {"difference_from_first": "绝对差异", "relative_difference_pct": "相对差异"}.get(derived, derived)
    return f"{claim.get('symbol', '')} · {label}：{number(claim['value'], claim.get('unit', ''))}"


def narrative_text(paragraph, report):
    """Format only exact cited fact text; leave the persisted verified report intact."""
    replacements = {}
    for ref in paragraph.get('fact_refs', []):
        try:
            index = int(ref[1:])
            claim = report['claims'][index]
            if ref != 'F'+str(index) or index < 0 or claim['kind'] != 'fact':
                continue
        except (ValueError, IndexError, KeyError):
            continue
        if type(claim.get('value')) not in (int, float) or claim.get('unit') == 'Unix ms' or not claim['text']:
            continue
        display = claim_text(claim)
        symbol, year = claim.get('symbol'), claim.get('fiscal_year')
        if symbol and year:
            display = display.replace(symbol+' · ', f'{symbol}（{year} 年） · ', 1)
        replacements[claim['text']] = display
    if not replacements:
        return paragraph['text']
    pattern = '|'.join(re.escape(key) for key in sorted(replacements, key=len, reverse=True))
    return re.sub(pattern, lambda match: replacements[match[0]], paragraph['text'])


def raw_value(body, path):
    match = re.fullmatch(r"\$\.data\.item\[(\d+)\]\.([A-Za-z0-9_]+)", path or "")
    document_match = re.fullmatch(r'\$\.excerpts\[(\d+)\]\.([A-Za-z0-9_]+)',path or '')
    if document_match:
        items = body.get('excerpts',[]) if isinstance(body,dict) else []
        index = int(document_match[1])
        return items[index].get(document_match[2]) if isinstance(items,list) and index < len(items) and isinstance(items[index],dict) else None
    if not match:
        return None
    data = body.get("data") if isinstance(body, dict) else None
    items = data.get("item", []) if isinstance(data, dict) else []
    if not isinstance(items, list):
        return None
    index = int(match[1])
    return items[index].get(match[2]) if index < len(items) and isinstance(items[index], dict) else None


def price_charts(service, owner, run_id, evidence):
    groups, issues = {}, []
    for record in evidence:
        content = record["content"]
        if content["tool"] != "get_daily_bars":
            continue
        if content["quality"] != "available":
            issues.append({"evidence_id": record["id"], "reason": content["quality"]})
            continue
        try:
            c = checked_evidence(service, owner, run_id, record)
        except Exception:
            issues.append({"evidence_id": record["id"], "reason": "conflict"})
            continue
        key = (c["method"]["adjust"], c["method"]["interval"])
        rows = sorted(c["records"], key=lambda r: r["date_ms"])
        groups.setdefault(key, []).append({"symbol": c["parameters"]["thscode"], "evidence_id": record["id"],
            "rows": rows, "as_of": c["as_of"], "fetched_at": c["fetched_at"]})
    charts = []
    for (adjust, interval), sources in groups.items():
        days = sorted(set(row["date_ms"] for source in sources for row in source["rows"]))
        dates = [datetime.fromtimestamp(day/1000, SHANGHAI).strftime("%Y-%m-%d") for day in days]
        series, refs = [], []
        for source in sources:
            rows = {row["date_ms"]: row for row in source["rows"]}
            series.append({"name": source["symbol"], "type": "line", "showSymbol": False, "connectNulls": False,
                "data": [rows[d].get("close_price") if d in rows else None for d in days], "lineStyle": {"width": 2}})
            refs.append([{"evidence_id": source["evidence_id"],
                          "raw_path": f"$.data.item[{rows[d]['source_index']}].close_price"} if d in rows else None for d in days])
        charts.append({"adjust": adjust, "interval": interval, "sources": sources, "point_refs": refs,
            "options": {"animation": False, "color": ["#0f766e", "#5469ad", "#b58743"],
                "tooltip": {"trigger": "axis", "renderMode": "richText"}, "legend": {"bottom": 0},
                "grid": {"left": 64, "right": 16, "top": 24, "bottom": 78},
                "xAxis": {"type": "category", "data": dates, "boundaryGap": False, "axisLabel": {"hideOverlap": True}},
                "yAxis": {"type": "value", "scale": True, "name": "元/股"},
                "dataZoom": [{"type": "inside"}, {"type": "slider", "bottom": 30, "height": 16}], "series": series}})
    return charts, issues


def financial_rows(report):
    rows = {}
    if report.get("validation_status") != "passed":
        return []
    for index, claim in enumerate(report.get("claims", [])):
        if claim["kind"] != "fact" or claim.get("field") not in {"operating_income", "parent_holder_net_profit", "net_profit", "basic_eps"}:
            continue
        key = (claim["symbol"], claim["as_of"])
        row = rows.setdefault(key, {"id": str(len(rows)), "symbol": claim["symbol"], "period": stamp(claim["as_of"])[:10],
                                   "claim_indices": [], "evidence_ids": [], **{f: "缺失" for f in ("operating_income", "parent_holder_net_profit", "net_profit", "basic_eps")}})
        row[claim["field"]] = number(claim["value"], claim["unit"])
        row["claim_indices"].append(index)
        row["evidence_ids"] += [e for e in claim["evidence_ids"] if e not in row["evidence_ids"]]
    return list(rows.values())
