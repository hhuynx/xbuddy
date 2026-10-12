from __future__ import annotations

import math
from datetime import datetime, timezone

from server.models import EvidenceContract, Quality
from server.tools.fuyao import FetchResult
from server.tools.specs import MAX_MS, MIN_MS

PRICE_FIELDS = {"last_price", "open_price", "high_price", "low_price", "prev_price", "close_price", "price_change"}
FINANCIAL_FIELDS = {"operating_income", "operating_costs", "operating_expenses", "sales_fee", "manage_fee",
                    "research_and_development_expenses", "operating_profit", "interest_expenses", "profit_total",
                    "income_tax_expense", "net_profit", "parent_holder_net_profit", "basic_eps"}


def epoch_ms(value) -> int | None:
    return value if type(value) is int and MIN_MS <= value <= MAX_MS else None


def iso_ms(value) -> str | None:
    value = epoch_ms(value)
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat() if value is not None else None


def numeric(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def worse(current: Quality, proposed: Quality) -> Quality:
    order = {Quality.AVAILABLE: 0, Quality.UNVERIFIED: 1, Quality.MISSING: 2,
             Quality.STALE: 3, Quality.CONFLICT: 4, Quality.FAILED: 5, Quality.UNAUTHORIZED: 6}
    return proposed if order[proposed] > order[current] else current


def normalize(name: str, endpoint: str, parameters: dict, fetched_at: str, result: FetchResult) -> EvidenceContract:
    body = result.body
    base = dict(provider="fuyao", tool=name, endpoint=endpoint, parameters=parameters,
                request_id=result.request_id, fetched_at=fetched_at, as_of=None, quality=Quality.FAILED,
                records=[], field_map={}, units={}, method={}, issues=[])
    if result.error:
        base["quality"] = Quality.UNAUTHORIZED if result.business_code in (2001, 2003) or result.http_status in (401, 403) \
            or result.error == "missing_credentials" else Quality.FAILED
        base["issues"] = [result.error]
        return EvidenceContract(**base)
    data = body.get("data") if isinstance(body, dict) else None
    items = data.get("item") if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) > 4000 or any(not isinstance(item, dict) or not item for item in items):
        base["issues"] = ["invalid_record_shape"]
        return EvidenceContract(**base)
    if not items:
        base["quality"], base["issues"] = Quality.MISSING, ["empty_records"]
        return EvidenceContract(**base)
    issues, units, mapping, records = [], {}, {}, []
    quality = Quality.AVAILABLE
    required = {"resolve_security": {"thscode", "name", "currency"},
                "get_trading_calendar": {"date_ms", "date"},
                "get_price_snapshot": {"thscode", "last_price"},
                "get_daily_bars": {"date_ms", "close_price"},
                "get_financial_metrics": {"thscode", "period", "period_end_ms", "report_date_ms", "currency", "fiscal_year", "fiscal_period"}}[name]
    for index, item in enumerate(items):
        record = {**item, "source_index": index}
        if required - item.keys():
            issues.append("required_fields_missing")
            quality = worse(quality, Quality.MISSING)
        for key, value in item.items():
            mapping[key] = {"path_template": "$.data.item[{source_index}]." + key,
                            "transformation": "identity"}
            if key in PRICE_FIELDS:
                units[key] = "CNY/share"
            elif key == "turnover":
                units[key] = "CNY"
            elif key == "volume":
                units[key] = "share"
            elif key == "price_change_ratio_pct":
                units[key] = "percent"
            elif name == "get_financial_metrics" and key not in required | {"ticker"}:
                # Only mapped numeric amount fields are considered verified units.
                if key in FINANCIAL_FIELDS or key in {"total_assets", "total_liabilities", "total_equity",
                         "net_cash_flows_oper_act", "net_cash_flows_inv_act", "net_cash_flows_fnc_act"}:
                    units[key] = "CNY/share" if key == "basic_eps" else "CNY"
            if key in PRICE_FIELDS | FINANCIAL_FIELDS | {"turnover", "volume", "price_change_ratio_pct"}:
                if value is None:
                    issues.append("null_field:" + key)
                elif not numeric(value):
                    issues.append("invalid_numeric_field:" + key)
                    quality = worse(quality, Quality.FAILED)
        if name in {"get_daily_bars", "get_trading_calendar"}:
            date = epoch_ms(item.get("date_ms"))
            if date is None:
                issues.append("invalid_date_ms")
                quality = worse(quality, Quality.FAILED)
            if name == "get_daily_bars":
                record["thscode"] = parameters["thscode"]
                if date is not None and not parameters["start"] <= date <= parameters["end"]:
                    issues.append("record_outside_requested_window")
                    quality = worse(quality, Quality.CONFLICT)
                if not numeric(item.get("close_price")) or item["close_price"] <= 0:
                    quality = worse(quality, Quality.MISSING if item.get("close_price") is None else Quality.FAILED)
                    issues.append("invalid_close_price")
        if name == "get_financial_metrics":
            if item.get("thscode") != parameters["thscode"] or item.get("period") != parameters["period"]:
                quality = worse(quality, Quality.CONFLICT)
                issues.append("financial_scope_mismatch")
            if item.get("currency") != "CNY":
                quality = worse(quality, Quality.UNVERIFIED)
                issues.append("unknown_currency")
            if epoch_ms(item.get("period_end_ms")) is None or epoch_ms(item.get("report_date_ms")) is None:
                quality = worse(quality, Quality.UNVERIFIED)
                issues.append("unknown_report_or_disclosure_time")
            record["basis"] = "annual" if item.get("period") == "annual" and item.get("fiscal_period") in {"FY", "Q4"} else "unknown"
        records.append(record)
    if name == "get_price_snapshot":
        requested = set(parameters["thscodes"].split(","))
        returned = {item.get("thscode") for item in items}
        if returned != requested:
            quality = worse(quality, Quality.MISSING if returned <= requested else Quality.CONFLICT)
            issues.append("snapshot_symbol_coverage_mismatch")
        if quality == Quality.AVAILABLE:
            quality = Quality.UNVERIFIED
        issues.append("upstream_timestamp_is_not_each_symbol_trade_time")
    date_field = "period_end_ms" if name == "get_financial_metrics" else "date_ms"
    valid_dates = [epoch_ms(x.get(date_field)) for x in records]
    valid_dates = [x for x in valid_dates if x is not None]
    as_of = iso_ms(max(valid_dates)) if valid_dates else iso_ms(data.get("timestamp"))
    if as_of is None and quality == Quality.AVAILABLE:
        quality = Quality.UNVERIFIED
        issues.append("unknown_as_of")
    if name in {"get_daily_bars", "get_trading_calendar"} and len(valid_dates) != len(set(valid_dates)):
        quality = worse(quality, Quality.CONFLICT)
        issues.append("duplicate_dates")
    if name == "get_financial_metrics" and len(valid_dates) != len(set(valid_dates)):
        quality = worse(quality, Quality.CONFLICT)
        issues.append("duplicate_report_periods")
    method = {"currency_source": "provider_contract:A-share=CNY", "timestamp_semantics":
              "report_period_end" if name == "get_financial_metrics" else
              "bar_date" if name == "get_daily_bars" else "calendar_date" if name == "get_trading_calendar" else
              "upstream_ready_time", "unit_source": "provider_contract_and_response_fields"}
    if name == "get_trading_calendar":
        method["upstream_ready_ms"] = epoch_ms(data.get("timestamp"))
    if name == "get_daily_bars":
        method.update(adjust=parameters["adjust"], interval=parameters["interval"],
                      performance_kind="price_change_not_total_return")
    if name == "get_financial_metrics":
        method.update(period=parameters["period"], statement=endpoint.rsplit("/", 1)[-1],
                      basis="annual" if all(x["basis"] == "annual" for x in records) else "unknown",
                      timestamp_semantics="period_end_is_not_disclosure_or_fetch_time")
    base.update(quality=quality, records=records, units=units, field_map=mapping, method=method,
                as_of=as_of, issues=sorted(set(issues)))
    return EvidenceContract(**base)
