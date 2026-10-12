from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone

from server.evidence.normalize import iso_ms, normalize
from server.storage.database import encode, fingerprint, now, uid
from server.tools.calculations import financial_comparison, price_comparison
from server.tools.fuyao import FetchResult
from server.tools.specs import request_parameters
from server.publication import enforce_report, safe_text


def checked_evidence(service, owner, run_id, record):
    c = record["content"]
    if record["run_id"] != run_id or fingerprint(c) != record["content_sha256"]:
        raise ValueError("evidence_hash_or_scope")
    raw = service.resource("raw_records", record["raw_id"], owner)
    if fingerprint(raw["body"]) != raw["content_sha256"] or raw["call_id"] != record["call_id"]:
        raise ValueError("raw_hash_or_scope")
    if c['provider'] == 'documents':
        call = service.resource('tool_calls',record['call_id'],owner)
        if (call['run_id'] != run_id or call['tool'] != 'search_documents' or c['parameters'] != call['parameters'] or
            raw['provider'] != 'documents' or raw['body'].get('excerpts') != c['records']):
            raise ValueError('document_request_provenance')
        for excerpt in c['records']:
            service.documents.verify_excerpt(owner,excerpt)
    if c["provider"] == "fuyao" and c["quality"] == "available":
        call = service.resource("tool_calls", record["call_id"], owner)
        endpoint, params = request_parameters(call["tool"], call["parameters"])
        attempt = service.db.one("SELECT * FROM tool_attempts WHERE call_id=? AND attempt=?", (call["id"], raw["attempt"]))
        if (call["run_id"] != run_id or call["tool"] != c["tool"] or endpoint != c["endpoint"] or params != c["parameters"]
            or raw["provider"] != "fuyao" or raw["fetched_at"] != c["fetched_at"] or raw["body"].get("code") != 0
            or not attempt or attempt["http_status"] != 200 or attempt["business_code"] != 0 or attempt["request_id"] != c["request_id"]):
            raise ValueError("request_provenance_mismatch")
        expected = normalize(c["tool"], c["endpoint"], c["parameters"], c["fetched_at"],
                             FetchResult(body=raw["body"], request_id=c["request_id"])).model_dump(mode="json")
        if fingerprint(expected) != record["content_sha256"]:
            raise ValueError("raw_field_mapping_mismatch")
    return c


def checked_metric(service, owner, run_id, record):
    c = record["content"]
    if record["run_id"] != run_id or fingerprint(c) != record["content_sha256"]:
        raise ValueError("metric_hash_or_scope")
    call = service.resource("tool_calls", record["call_id"], owner)
    args = call["parameters"]
    evidence = [service.resource("evidence", eid, owner) for eid in args["evidence_ids"]]
    for item in evidence:
        checked_evidence(service, owner, run_id, item)
    if call["tool"] == "calculate_price_comparison":
        cal = service.resource("evidence", args["calendar_evidence_id"], owner)
        checked_evidence(service, owner, run_id, cal)
        expected, refs = price_comparison(evidence, cal, args["trading_days"])
    elif call["tool"] == "calculate_financial_comparison":
        expected, refs = financial_comparison(evidence, args["fields"], args["period_end_ms"], args["known_at_ms"])
    else:
        raise ValueError("unsupported_formula")
    if fingerprint(expected) != record["content_sha256"] or refs != record["input_refs"]:
        raise ValueError("formula_or_input_reference_mismatch")
    return c, evidence


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value)


def build_report(service, owner: str, run_id: str, *, include_agent=True) -> dict:
    """Render quantitative claims from verified fields; no model-authored numbers or recommendations."""
    run = service.resource("runs", run_id, owner)
    financial_history = run['config'].get('financial_history_version') == 1
    runtime = service.db.one("SELECT * FROM run_runtime WHERE run_id=?", (run_id,))
    import json
    plan = json.loads(runtime["plan_json"] or "{}")
    claims, gaps, validation_errors, sources, computations = [], [], [], [], []
    if not safe_text(run['goal']) and not (hasattr(service, 'dialogue') and service.dialogue.state(run_id).get('policy_redirected')):
        gaps.append({'reason': 'unsupported_goal',
            'detail': '仅提供可验证的历史与已披露数据；请求中的预测、收益承诺或交易建议无法作为研究结论。'})
    if any(not safe_text(x) for x in plan.get("limitations", [])):
        validation_errors.append("prohibited_plan_expression")
    tasks = service.collection(owner, run_id, "tasks")
    for task in tasks:
        if task["status"] != "completed":
            gaps.append({"task_id": task["id"], "tool": task["tool"], "reason": task["status"]})
    for text in plan.get("unresolved_goals", []):
        if safe_text(text):
            gaps.append({"reason": "unsupported_goal", "detail": text})
        else:
            validation_errors.append("prohibited_plan_expression")

    def add_fact(symbol, field, value, unit, as_of, evidence_ids, metric_ids, reference, method, *, period=None):
        if not numeric(value) or not unit or not as_of:
            gaps.append({"reason": "value_unit_or_time_missing", "field": field})
            return
        labels = {"close_price": "收盘价", "operating_income": "营业收入", "parent_holder_net_profit": "归母净利润",
                  "net_profit": "净利润", "basic_eps": "基本每股收益", "historical_price_change_pct": "历史价格变化",
                  "annualized_volatility_pct": "年化波动率", "maximum_drawdown_pct": "最大回撤"}
        name = labels.get(field, field)
        text = f"{symbol} · {name} = {value:.8g} {unit}"
        if period:
            period_date=datetime.fromtimestamp(period['period_end_ms']/1000,timezone(timedelta(hours=8))).date().isoformat()
            text = f"{symbol} · 报告期末 {period_date} · {name} = {value:.8g} {unit}"
        if field == "latest_covered_trade_date_ms":
            date = datetime.fromtimestamp(value/1000, timezone(timedelta(hours=8))).date().isoformat()
            text = "A 股交易日历最后覆盖交易日：" + date + "（上海时间）"
            reference["display_transformation"] = "unix_ms_to_Asia_Shanghai_date"
        claims.append({"kind": "fact", "text": text,
                       "value": value, "unit": unit, "field": field, "symbol": symbol,
                       "as_of": as_of, "evidence_ids": evidence_ids, "derived_metric_ids": metric_ids,
                       "reference": reference, "method": method, "limitations": []})
        if period:
            claims[-1].update(period)

    for record in service.collection(owner, run_id, "evidence"):
        try:
            c = checked_evidence(service, owner, run_id, record)
            sources.append({"evidence_id": record["id"], "raw_id": record["raw_id"], "provider": c["provider"],
                            "tool": c["tool"], "as_of": c["as_of"], "fetched_at": c["fetched_at"],
                            "quality": c["quality"], "hash": record["content_sha256"]})
            if c["quality"] != "available":
                gaps.append({"reason": c["quality"], "evidence_id": record["id"], "issues": c["issues"]})
                continue
            if c['tool'] == 'search_documents':
                for index, row in enumerate(c['records']):
                    if not safe_text(row['text']) or not safe_text(row['title']):
                        gaps.append({'reason':'document_excerpt_publication_blocked','evidence_id':record['id']})
                        continue
                    claims.append({'kind':'fact','text':f"原文摘录 · {row['title']} · 第 {row['page']} 页：{row['text']}",
                        'value':row['text'],'field':'document_excerpt','unit':'原文','as_of':row['published_at'],
                        'evidence_ids':[record['id']],'derived_metric_ids':[],
                        'reference':{'evidence_id':record['id'],'raw_path':f'$.excerpts[{index}].text',
                            **{k:row[k] for k in ('document_id','chunk_id','source_url','page','offset_start','offset_end','document_sha256')}},
                        'method':c['method'],'limitations':['仅核验摘录与导入原文一致；来源真实性与原文陈述尚需外部核验']})
                continue
            if c["tool"] == "get_trading_calendar":
                row = max(c["records"], key=lambda x: x["date_ms"])
                add_fact("A 股交易日历", "latest_covered_trade_date_ms", row["date_ms"], "Unix ms", iso_ms(row["date_ms"]),
                         [record["id"]], [], {"evidence_id": record["id"], "raw_path": f"$.data.item[{row['source_index']}].date_ms"}, c["method"])
                continue
            if c["tool"] == "resolve_security":
                for row in c["records"]:
                    for field in ("thscode", "name", "currency"):
                        value = row.get(field)
                        if isinstance(value, str) and safe_text(value) and c["as_of"]:
                            claims.append({"kind": "fact", "text": f"标的解析 · {field} = {value}", "value": value,
                                "unit": "identifier", "field": field, "as_of": c["as_of"], "evidence_ids": [record["id"]],
                                "derived_metric_ids": [], "limitations": ["仅为查询匹配，非投资筛选"],
                                "reference": {"evidence_id": record["id"], "raw_path": f"$.data.item[{row['source_index']}].{field}"}, "method": c["method"]})
                continue
            if c["tool"] not in {"get_daily_bars", "get_financial_metrics"}:
                continue
            # A financial response already contains its historical periods. Do not
            # discard those rows before the research observer can inspect them.
            rows = (sorted(c['records'],key=lambda x:x['period_end_ms'],reverse=True)
                    if financial_history and c['tool']=='get_financial_metrics' else
                    [max(c["records"], key=lambda x: x.get("date_ms", x.get("period_end_ms", 0)))])
            fields = ["close_price"] if c["tool"] == "get_daily_bars" else ["operating_income", "parent_holder_net_profit", "net_profit", "basic_eps"]
            for row in rows:
                period = ({**{k:row.get(k) for k in ('period_end_ms','fiscal_year','fiscal_period','report_date_ms','currency','basis')},
                           'statement':c['method'].get('statement')}
                          if financial_history and c['tool']=='get_financial_metrics' else None)
                for field in fields:
                    value = row.get(field)
                    if value is None:
                        gap = {"reason": "null_field", "evidence_id": record["id"], "field": field}
                        if period:
                            gap['period_end_ms'] = period['period_end_ms']
                        gaps.append(gap)
                        continue
                    mapping = c["field_map"].get(field)
                    if not mapping:
                        raise ValueError("field_mapping_missing")
                    add_fact(row["thscode"], field, value, c["units"].get(field),
                             iso_ms(row.get("date_ms", row.get("period_end_ms"))), [record["id"]], [],
                             {"evidence_id": record["id"], "raw_path": mapping["path_template"].replace("{source_index}", str(row["source_index"])),
                              "report_date": iso_ms(row.get("report_date_ms"))}, c["method"],period=period)
        except Exception:
            validation_errors.append("evidence_integrity_failed:" + record["id"])

    for record in service.collection(owner, run_id, "derived_metrics"):
        try:
            c, evidence = checked_metric(service, owner, run_id, record)
            ids = [x["id"] for x in evidence]
            computations.append({"metric_id": record["id"], "content": c, "input_refs": record["input_refs"], "hash": record["content_sha256"]})
            if c["quality"] != "available":
                gaps.append({"reason": c["quality"], "metric_id": record["id"], "issues": c.get("issues", [])})
                continue
            if "period_end_ms" not in c:
                for result in c["results"]:
                    for field, value in result["metrics"].items():
                        add_fact(result["symbol"], field, value, "%", iso_ms(result["end_ms"]), ids, [record["id"]],
                                 {"metric_id": record["id"], "path": ["results", c["results"].index(result), "metrics", field]}, c["method"])
            else:
                period=None
                if financial_history:
                    row=next(r for r in evidence[0]['content']['records'] if r['period_end_ms']==c['period_end_ms'])
                    period={**{k:row.get(k) for k in ('period_end_ms','fiscal_year','fiscal_period','report_date_ms','currency','basis')},
                            'statement':'income-statements'}
                for index, result in enumerate(c["results"]):
                    for j, comparison in enumerate(result.get("comparisons", [])):
                        for field, unit in (("difference_from_first", result["unit"]), ("relative_difference_pct", "%")):
                            value = comparison.get(field)
                            if value is None:
                                gaps.append({"reason": "undefined_nonpositive_baseline", "field": field, "metric_id": record["id"]})
                                continue
                            add_fact(comparison["symbol"] + " 相对 " + result["first_symbol"], result["field"] + "." + field,
                                     value, unit, iso_ms(c["period_end_ms"]), ids, [record["id"]],
                                     {"metric_id": record["id"], "path": ["results", index, "comparisons", j, field]}, c["method"],period=period)
        except Exception:
            validation_errors.append("metric_integrity_failed:" + record["id"])

    # Keep fact / inference / uncertain explicit. Interpretation is deliberately bounded.
    if any(x["derived_metric_ids"] for x in claims):
        claims.append({"kind": "inference", "text": "历史价格及同口径财务差异只描述已观测样本，不能据此确定未来市场表现。",
                       "evidence_ids": list(dict.fromkeys(e for c in claims for e in c["evidence_ids"])),
                       "derived_metric_ids": list(dict.fromkeys(m for c in claims for m in c["derived_metric_ids"])),
                       "limitations": ["未建立因果识别或预测模型"], "as_of": None})
    if not claims:
        gaps.append({"reason": "no_verified_quantitative_findings"})
    if gaps:
        claims.append({"kind": "uncertain", "text": "本次研究存在未解决的数据或执行缺口，相关部分无法形成已验证结论。",
                       "evidence_ids": [], "derived_metric_ids": [], "limitations": [x["reason"] for x in gaps], "as_of": None})
    if any(not safe_text(c["text"]) for c in claims):
        validation_errors.append("prohibited_expression")
    if validation_errors:
        claims = []  # Integrity failure blocks every core conclusion in this artifact.
    knowledge = {'memory_usage': [], 'context_snapshots': [], 'followup': None, 'user_notes': []}
    if hasattr(service, 'memories'):
        for item in service.memories.usages(owner, run_id):
            if item['last_used_at']:
                c = item['content']
                knowledge['memory_usage'].append({'memory_id': item['memory_id'], 'version': item['memory_version'],
                    'kind': c['kind'], 'valid_as_of': c.get('valid_as_of'), 'expires_at': c.get('expires_at'),
                    'evidence_ids': c.get('evidence_ids', []), 'source_artifact_id': c.get('source_artifact_id'),
                    'last_used_at': item['last_used_at'], 'role': 'historical_context_or_user_preference'})
                if c['kind'] in {'hypothesis', 'open_question'} and safe_text(c['content']):
                    knowledge['user_notes'].append({'kind': 'uncertain', 'label': '研究假设' if c['kind'] == 'hypothesis' else '待解决问题',
                        'text': c['content'], 'memory_id': item['memory_id'], 'validation': 'user_assertion_unverified'})
        knowledge['context_snapshots'] = [{'id': s['id'], 'compressed': bool(s['compressed']), 'before_tokens': s['before_tokens'], 'after_tokens': s['after_tokens']} for s in service.contexts.snapshots(owner, run_id)]
        link = service.db.one('SELECT parent_run_id,parent_artifact_id FROM run_links WHERE run_id=? AND session_id=?', (run_id, owner))
        if link:
            knowledge['followup'] = {**link, 'data_policy': 'new_run_fresh_queries; parent_is_historical_context'}
    report = {"schema_version": 3 if financial_history else 2, "run_id": run_id, "goal": run["goal"], "plan_version": run["plan_version"],
            "created_at": now(), "status": "blocked" if validation_errors else "partial" if gaps else "complete",
            "validation_status": "failed" if validation_errors else "passed", "validation_errors": validation_errors,
            "claims": claims, "gaps": gaps, "limitations": plan.get("limitations", []), "sources": sources, "computations": computations,
            "knowledge_context": knowledge,
            "method": {"authoring": "goal_driven_plan_and_deterministic_evidence_rendering",
                       "numeric_validation": "raw_field_mapping_and_formula_recomputation",
                       "fact_time": "trade_date_or_report_period; disclosure_time_recorded_separately",
                       "cost_status": "unknown", "future_performance": "not_estimated"}}
    if financial_history:
        report['method']['financial_history'] = 'all_verified_periods'
    if include_agent:
        from server.harness.dialogue import enrich_report
        report = enrich_report(service, owner, run_id, report)
    return enforce_report(report)


def save_report(service, owner, run_id, report, *, new_version=False, expected_version=None, note=''):
    report['revision_note'] = note
    enforce_report(report)
    with service.db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        run = conn.execute("SELECT status FROM runs WHERE id=? AND session_id=?", (run_id, owner)).fetchone()
        if not run:
            from server.services import ServiceError
            raise ServiceError(404, '研究不存在')
        if run["status"] in {"stopped", "paused", "awaiting_approval", "awaiting_input"}:
            return None
        existing = conn.execute("SELECT * FROM artifacts WHERE run_id=? AND kind='research_report' ORDER BY version DESC LIMIT 1", (run_id,)).fetchone()
        if existing and not new_version:
            return existing["id"]
        if new_version and (not existing or existing['version'] != expected_version or run['status'] not in {'completed','partial','blocked'}):
            from server.services import ServiceError
            raise ServiceError(409, '报告版本或运行状态已变化，请刷新后重试')
        version = existing['version']+1 if existing else 1
        report.update(report_version=version, previous_artifact_id=existing['id'] if existing else None)
        ids = []
        for claim in report["claims"]:
            record_id = uid()
            conn.execute("INSERT INTO claims VALUES(?,?,?,?,?)", (record_id, owner, run_id, encode(claim), now()))
            ids.append(record_id)
        report["claim_ids"] = ids
        artifact_id = uid()
        conn.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)", (artifact_id, owner, run_id, "research_report", version, encode(report), now()))
        conn.execute('INSERT INTO artifact_checksums VALUES(?,?,?)', (artifact_id, fingerprint(report), now()))
        if new_version:
            status = 'blocked' if report['validation_status'] == 'failed' else 'failed' if report['status'] == 'failed' else 'partial' if report['status'] == 'partial' else 'completed'
            conn.execute('UPDATE runs SET status=?,updated_at=? WHERE id=?', (status, now(), run_id))
        service.db.event(conn, owner, run_id, "report.saved", {"artifact_id": artifact_id, 'version': version, "validation_status": report["validation_status"], "status": report["status"]})
        return artifact_id
