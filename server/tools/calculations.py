"""Deterministic metrics; invalid or incomparable inputs produce explicit gaps."""
from __future__ import annotations

import math
import statistics

from server.evidence.normalize import numeric
from server.storage.database import fingerprint

CODE_VERSION = "metrics-1.0"


class CalculationError(ValueError):
    def __init__(self, message: str, quality: str = "failed"):
        super().__init__(message)
        self.quality = quality


def require(evidence: dict, tool: str):
    content = evidence["content"]
    if fingerprint(content) != evidence["content_sha256"]:
        raise CalculationError("输入证据指纹与已保存内容不一致")
    if content["tool"] != tool or content["quality"] != "available":
        raise CalculationError("输入证据类型或质量不满足计算条件")
    return content


def reference(evidence: dict, fields: list[str], indices: list[int]) -> dict:
    return {"evidence_id": evidence["id"], "content_sha256": evidence["content_sha256"],
            "field_paths": [f"$.data.item[{i}].{field}" for i in indices for field in fields]}


def price_comparison(evidence: list[dict], calendar: dict, trading_days: int) -> tuple[dict, list[dict]]:
    cal = require(calendar, "get_trading_calendar")
    series = [require(item, "get_daily_bars") for item in evidence]
    if len({x["parameters"]["thscode"] for x in series}) != len(series):
        raise CalculationError("同一标的不能重复比较")
    if len({(x["method"]["adjust"], x["method"]["interval"]) for x in series}) != 1:
        raise CalculationError("复权方式或周期不一致")
    # Use the common requested end, not the last row; a missing last bar must remain missing.
    end = min(x["parameters"]["end"] for x in series)
    loaded_at = cal["method"].get("upstream_ready_ms")
    if end > max(x["date_ms"] for x in cal["records"]) and (loaded_at is None or end > loaded_at):
        raise CalculationError("交易日历已过期或无法覆盖研究截止时点", "stale")
    days = sorted(x["date_ms"] for x in cal["records"] if x["date_ms"] <= end)
    if len(days) < trading_days:
        raise CalculationError("交易日历覆盖不足")
    days = days[-trading_days:]
    if any(x["parameters"]["start"] > days[0] for x in series):
        raise CalculationError("历史取数窗口覆盖不足")
    results, refs = [], [reference(calendar, ["date_ms"], [x["source_index"] for x in cal["records"] if x["date_ms"] in days])]
    for item, content in zip(evidence, series):
        by_date = {row["date_ms"]: row for row in content["records"]}
        if any(day not in by_date for day in days):
            raise CalculationError("交易日不齐全；不填零或静默补齐停牌数据")
        rows = [by_date[day] for day in days]
        prices = [row.get("close_price") for row in rows]
        if any(not numeric(p) or p <= 0 for p in prices):
            raise CalculationError("价格缺失、非有限数值或不为正")
        if content["units"].get("close_price") != "CNY/share":
            raise CalculationError("价格单位未验证")
        changes = [prices[i] / prices[i - 1] - 1 for i in range(1, len(prices))]
        peak, drawdown = prices[0], 0.0
        for price in prices:
            peak = max(peak, price)
            drawdown = min(drawdown, price / peak - 1)
        metrics = {"historical_price_change_pct": (prices[-1] / prices[0] - 1) * 100,
                   "annualized_volatility_pct": statistics.stdev(changes) * math.sqrt(252) * 100,
                   "maximum_drawdown_pct": drawdown * 100}
        if any(not math.isfinite(value) for value in metrics.values()):
            raise CalculationError("计算结果超过有限数值范围")
        results.append({"symbol": content["parameters"]["thscode"], "metrics": metrics,
                        "unit": "percent", "start_ms": days[0], "end_ms": days[-1],
                        "price_points": len(prices), "return_observations": len(changes),
                        "adjust": content["method"]["adjust"], "quality": "available"})
        refs.append(reference(item, ["date_ms", "close_price"], [row["source_index"] for row in rows]))
    return {"quality": "available", "code_version": CODE_VERSION, "results": results,
            "method": {"performance": "historical_price_change_not_total_return",
                       "price_change_formula": "(last_close / first_close - 1) * 100",
                       "volatility_formula": "sample_stdev(simple_daily_returns) * sqrt(252) * 100",
                       "drawdown_formula": "min(close / running_max_close - 1) * 100",
                       "annualization_days": 252, "window": "N closing observations give N-1 returns",
                       "alignment": "exact_trading_calendar_dates", "rounding": "raw values retained; display only"}}, refs


def financial_comparison(evidence: list[dict], fields: list[str], period_end_ms: int | None,
                         known_at_ms: int | None) -> tuple[dict, list[dict]]:
    contents = [require(item, "get_financial_metrics") for item in evidence]
    if len({x["parameters"]["thscode"] for x in contents}) != len(contents):
        raise CalculationError("同一标的不能重复比较")
    if any(x["method"].get("statement") != "income-statements" or x["method"].get("basis") != "annual" for x in contents):
        raise CalculationError("仅比较已明确年报口径的利润表；累计/单季未知时拒绝计算")
    eligible = [{r["period_end_ms"]: r for r in c["records"]
                 if known_at_ms is None or r["report_date_ms"] <= known_at_ms} for c in contents]
    common = set.intersection(*(set(rows) for rows in eligible))
    chosen = period_end_ms if period_end_ms is not None else max(common) if common else None
    if chosen not in common:
        raise CalculationError("没有该截止时点已披露的共同报告期")
    rows = [records[chosen] for records in eligible]
    if len({(r["currency"], r["fiscal_year"], r["fiscal_period"], r["basis"]) for r in rows}) != 1:
        raise CalculationError("报告期、币种或统计口径不一致")
    results, issues = [], []
    for field in fields:
        unit = contents[0]["units"].get(field)
        if not unit or any(c["units"].get(field) != unit for c in contents):
            raise CalculationError("财务字段单位未知或不一致")
        values = [row.get(field) for row in rows]
        gaps = [contents[i]["parameters"]["thscode"] for i, value in enumerate(values) if not numeric(value)]
        if gaps:
            issues.append("字段缺失：" + field)
            results.append({"field": field, "quality": "missing", "missing_symbols": gaps, "unit": unit})
            continue
        baseline = values[0]
        if any(not math.isfinite(value - baseline) or (baseline > 0 and not math.isfinite(value / baseline - 1)) for value in values[1:]):
            raise CalculationError("财务计算结果超过有限数值范围")
        comparisons = []
        for content, value in zip(contents[1:], values[1:]):
            comparisons.append({"symbol": content["parameters"]["thscode"], "value": value,
                                "difference_from_first": value - baseline,
                                "relative_difference_pct": (value / baseline - 1) * 100 if baseline > 0 else None,
                                "relative_difference_quality": "available" if baseline > 0 else "undefined_nonpositive_baseline"})
        results.append({"field": field, "quality": "available", "unit": unit,
                        "first_symbol": contents[0]["parameters"]["thscode"], "first_value": baseline,
                        "comparisons": comparisons})
    refs = [reference(item, fields + ["period_end_ms", "report_date_ms", "currency", "fiscal_period"], [row["source_index"]])
            for item, row in zip(evidence, rows)]
    return {"quality": "missing" if issues else "available", "code_version": CODE_VERSION, "results": results,
            "period_end_ms": chosen, "known_at_ms": known_at_ms, "issues": issues,
            "method": {"basis": "annual_consolidated_income_statement", "difference_formula": "value - first_value",
                       "relative_formula": "(value / first_value - 1) * 100, only when first_value > 0",
                       "historical_knowledge": "disclosure_cutoff_applied" if known_at_ms else "latest_available_reports_not_historical_backtest"}}, refs
