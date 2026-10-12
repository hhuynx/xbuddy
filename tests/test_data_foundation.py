from __future__ import annotations

import asyncio
import json
import math
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from server.api.routes import create_api
from server.config import Settings
from server.storage.database import fingerprint

DAYS = [1736121600000, 1736208000000, 1736294400000, 1736380800000, 1736467200000]
PRICES = [100, 120, 90, 108, 108]
SYMBOLS = ["600519.SH", "000858.SZ"]
PERIOD = 1735574400000


def envelope(items, timestamp=None):
    return {"code": 0, "request_id": "synthetic-request", "data": {"timestamp": timestamp or DAYS[-1], "item": items}}


def response_for(request):
    path = request.url.path
    if path.endswith("trading-days"):
        return envelope([{"date_ms": day, "date": f"202501{6+i:02}"} for i, day in enumerate(DAYS)])
    if path.endswith("historical"):
        return envelope([{"date_ms": day, "close_price": price, "open_price": price, "volume": None}
                         for day, price in zip(DAYS, PRICES)])
    if path.endswith("income-statements"):
        first = request.url.params["thscode"] == SYMBOLS[0]
        return envelope([{"thscode": request.url.params["thscode"], "period": request.url.params["period"],
                          "fiscal_year": 2024, "fiscal_period": "FY", "period_end_ms": PERIOD,
                          "report_date_ms": 1743465600000, "currency": "CNY",
                          "operating_income": 200 if first else 150, "net_profit": -10 if first else 20,
                          "parent_holder_net_profit": 30 if first else None, "basic_eps": 2}], PERIOD)
    if path.endswith("snapshot"):
        return envelope([{"thscode": s, "last_price": 100} for s in request.url.params["thscodes"].split(",")])
    return envelope([{"thscode": SYMBOLS[0], "name": "构造公司", "currency": "CNY"}])


@pytest.fixture
def kit(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response_for(request))

    settings = Settings(data_dir=tmp_path, fuyao_api_key="synthetic-key")
    app = create_api(settings, httpx.MockTransport(handler))
    with TestClient(app) as client:
        csrf = client.get("/api/session").json()["csrf_token"]
        client.headers["X-CSRF-Token"] = csrf
        thread = client.post("/api/threads", json={"title": "构造研究", "goal": "验证数据与计算"}).json()
        run = client.post(f"/api/threads/{thread['id']}/runs", json={"goal": "验证数据与计算"}).json()
        yield client, run["id"], app, requests, settings


def execute(kit, tool, arguments=None, key=None):
    client, run_id, *_ = kit
    body = {"arguments": arguments or {}}
    if key:
        body["idempotency_key"] = key
    response = client.post(f"/api/runs/{run_id}/tools/{tool}", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def evidence_id(call):
    return call["result"]["evidence_ids"][0]


def bars(kit, symbol=SYMBOLS[0]):
    return execute(kit, "get_daily_bars", {"thscode": symbol, "start": DAYS[0], "end": DAYS[-1]})


def test_source_values_paths_units_and_fingerprints_are_preserved(kit):
    client, _, _, requests, _ = kit
    call = bars(kit)
    record = client.get("/api/evidence/" + evidence_id(call)).json()
    raw = client.get(record["raw_url"]).json()
    assert raw["body"]["data"]["item"][0]["close_price"] == 100
    assert raw["body"]["data"]["item"][0]["volume"] is None
    assert raw["content_sha256"] == fingerprint(raw["body"])
    assert record["content_sha256"] == fingerprint(record["content"])
    assert record["content"]["units"]["close_price"] == "CNY/share"
    assert record["content"]["field_map"]["close_price"]["path_template"] == "$.data.item[{source_index}].close_price"
    assert record["content"]["method"]["adjust"] == "forward"
    assert record["content"]["request_id"] == "synthetic-request"
    assert requests[0].headers["X-api-key"] == "synthetic-key"
    assert "synthetic-key" not in json.dumps(record) + json.dumps(raw) + json.dumps(call)


def test_price_metrics_use_exact_calendar_and_known_formulas(kit):
    client, *_ = kit
    calendar = execute(kit, "get_trading_calendar")
    sources = [bars(kit, s) for s in SYMBOLS]
    call = execute(kit, "calculate_price_comparison", {
        "evidence_ids": [evidence_id(x) for x in sources], "calendar_evidence_id": evidence_id(calendar), "trading_days": 5})
    metric = client.get("/api/metrics/" + call["result"]["derived_metric_ids"][0]).json()
    values = metric["content"]["results"][0]["metrics"]
    assert values["historical_price_change_pct"] == pytest.approx(8)
    assert values["maximum_drawdown_pct"] == pytest.approx(-25)
    assert values["annualized_volatility_pct"] == pytest.approx(math.sqrt(0.045625 * 252) * 100)
    assert metric["content"]["results"][0]["return_observations"] == 4
    assert len(metric["input_refs"]) == 3 and len(metric["input_refs"][1]["field_paths"]) == 10
    assert metric["content"]["method"]["performance"] == "historical_price_change_not_total_return"


def test_missing_bar_is_not_filled_and_adjustments_must_match(kit):
    client, _, app, _, _ = kit
    original_fetch = app.state.research.fuyao.fetch

    async def missing(path, params):
        result = await original_fetch(path, params)
        if path.endswith("historical") and params["thscode"] == SYMBOLS[1]:
            result.body["data"]["item"].pop()
        return result

    app.state.research.fuyao.fetch = missing
    calendar = execute(kit, "get_trading_calendar")
    source = bars(kit)
    incomplete = bars(kit, SYMBOLS[1])
    call = execute(kit, "calculate_price_comparison", {
        "evidence_ids": [evidence_id(source), evidence_id(incomplete)], "calendar_evidence_id": evidence_id(calendar), "trading_days": 5})
    assert call["quality"] == "failed" and call["result"]["derived_metric_ids"] == []
    app.state.research.fuyao.fetch = original_fetch
    other = execute(kit, "get_daily_bars", {"thscode": SYMBOLS[1], "start": DAYS[0], "end": DAYS[-1], "adjust": "none"})
    call = execute(kit, "calculate_price_comparison", {
        "evidence_ids": [evidence_id(source), evidence_id(other)], "calendar_evidence_id": evidence_id(calendar), "trading_days": 5})
    assert call["quality"] == "failed"


def test_financial_comparison_handles_null_negative_and_disclosure_cutoff(kit):
    client, *_ = kit
    source = [execute(kit, "get_financial_metrics", {"thscode": s}) for s in SYMBOLS]
    args = {"evidence_ids": [evidence_id(x) for x in source], "fields": ["operating_income", "net_profit", "parent_holder_net_profit"]}
    call = execute(kit, "calculate_financial_comparison", args)
    metric = client.get("/api/metrics/" + call["result"]["derived_metric_ids"][0]).json()["content"]
    assert metric["results"][0]["comparisons"][0]["relative_difference_pct"] == -25
    negative = metric["results"][1]["comparisons"][0]
    assert negative["difference_from_first"] == 30 and negative["relative_difference_pct"] is None
    assert negative["relative_difference_quality"] == "undefined_nonpositive_baseline"
    assert metric["results"][2]["quality"] == "missing" and call["quality"] == "missing"
    blocked = execute(kit, "calculate_financial_comparison", {**args, "known_at_ms": PERIOD})
    assert blocked["quality"] == "failed"
    quarterly = [execute(kit, "get_financial_metrics", {"thscode": s, "period": "quarterly"}) for s in SYMBOLS]
    blocked = execute(kit, "calculate_financial_comparison", {"evidence_ids": [evidence_id(x) for x in quarterly]})
    assert blocked["quality"] == "failed"


def test_idempotency_reuses_saved_call_and_rejects_changed_arguments(kit):
    client, run_id, _, requests, _ = kit
    first = execute(kit, "resolve_security", {"q": SYMBOLS[0]}, "same-key")
    second = execute(kit, "resolve_security", {"q": SYMBOLS[0]}, "same-key")
    assert first["id"] == second["id"] and second["reused"] is True and len(requests) == 1
    changed = client.post(f"/api/runs/{run_id}/tools/resolve_security", json={"arguments": {"q": SYMBOLS[1]}, "idempotency_key": "same-key"})
    assert changed.status_code == 409 and len(requests) == 1
    events = client.get(f"/api/runs/{run_id}/events").json()
    ids = [x["id"] for x in events["events"]]
    assert ids == sorted(set(ids))
    assert client.get(f"/api/runs/{run_id}/events", params={"after": events["next_cursor"]}).json()["events"] == []


def test_state_and_cookie_survive_application_recreation(kit):
    client, run_id, app, _, settings = kit
    call = bars(kit)
    cookies = dict(client.cookies)
    with TestClient(create_api(settings)) as restarted:
        restarted.cookies.update(cookies)
        assert restarted.get(f"/api/runs/{run_id}").json()["tool_calls"][0]["id"] == call["id"]
        assert restarted.get("/api/evidence/" + evidence_id(call)).status_code == 200
        rows = app.state.research.db.all("SELECT version FROM schema_migrations")
    assert rows == [{"version": 1}, {"version": 2}, {"version": 3}, {"version": 4}]


def test_session_isolation_and_csrf_protect_all_resources(kit):
    client, run_id, app, _, _ = kit
    call = bars(kit)
    evidence = client.get("/api/evidence/" + evidence_id(call)).json()
    with TestClient(app) as stranger:
        assert stranger.get("/api/tools").status_code == 401
        token = stranger.get("/api/session").json()["csrf_token"]
        stranger.headers["X-CSRF-Token"] = token
        for path in [f"/api/runs/{run_id}", f"/api/runs/{run_id}/events", f"/api/calls/{call['id']}",
                     f"/api/evidence/{evidence['id']}", evidence["raw_url"], f"/api/runs/{run_id}/evidence"]:
            assert stranger.get(path).status_code == 404
        assert stranger.post(f"/api/runs/{run_id}/tools/get_trading_calendar", json={}).status_code == 404
    token = client.headers.pop("X-CSRF-Token")
    assert client.post("/api/threads", json={"title": "x"}).status_code == 403
    client.headers["X-CSRF-Token"] = token
    assert client.post("/api/threads", headers={"Origin": "https://foreign.example"}, json={"title": "x"}).status_code == 403
    assert client.get("/api/tools", headers={"Host": "foreign.example"}).status_code == 400


@pytest.mark.parametrize("body,quality", [({"code": 2003, "data": None}, "unauthorized"),
    (envelope([]), "missing"), ({"code": 0, "data": {"item": [None]}}, "failed"),
    ({"code": "0", "data": {"item": []}}, "failed")])
def test_business_failure_empty_and_malformed_data_are_explicit(tmp_path, body, quality):
    app = create_api(Settings(data_dir=tmp_path, fuyao_api_key="synthetic-key"), httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    with TestClient(app) as client:
        client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf_token"]
        thread = client.post("/api/threads", json={"title": "x"}).json()
        run = client.post(f"/api/threads/{thread['id']}/runs", json={"goal": "x"}).json()
        call = client.post(f"/api/runs/{run['id']}/tools/get_trading_calendar", json={}).json()
        assert call["quality"] == quality and call["attempts"] == 1


def test_retries_are_bounded_and_each_attempt_is_audited(kit):
    client, run_id, app, requests, _ = kit
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(503, json={"code": 5001}) if count == 1 else httpx.Response(200, json=response_for(request))

    app.state.research.fuyao.transport = httpx.MockTransport(handler)
    call = execute(kit, "get_trading_calendar")
    assert call["quality"] == "available" and call["attempts"] == 2
    audit = client.get("/api/calls/" + call["id"]).json()["attempt_log"]
    assert [a["http_status"] for a in audit] == [503, 200]
    assert client.get(f"/api/runs/{run_id}").json()["attempts_used"] == 2


def test_missing_credentials_and_budget_never_dispatch_external_request(kit):
    _, _, app, requests, settings = kit
    app.state.research.fuyao.settings = replace(settings, fuyao_api_key="")
    call = execute(kit, "get_trading_calendar")
    assert call["quality"] == "unauthorized" and requests == []
    app.state.research.settings = replace(settings, max_tool_attempts=1)
    blocked = execute(kit, "resolve_security", {"q": SYMBOLS[0]})
    assert blocked["attempts"] == 0 and blocked["quality"] == "failed" and requests == []


def test_tool_allowlist_and_financial_parameter_modes_are_enforced(kit):
    client, run_id, _, requests, _ = kit
    for name, arguments, status in [("place_order", {}, 404),
        ("get_daily_bars", {"thscode": SYMBOLS[0], "start": 1, "end": 2}, 422),
        ("get_financial_metrics", {"thscode": SYMBOLS[0], "start": DAYS[0], "end": DAYS[-1], "limit": 4}, 422),
        ("get_price_snapshot", {"symbols": [SYMBOLS[0], SYMBOLS[0]]}, 422)]:
        assert client.post(f"/api/runs/{run_id}/tools/{name}", json={"arguments": arguments}).status_code == status
    assert requests == []


def test_snapshot_ready_time_is_not_presented_as_symbol_trade_time(kit):
    client, *_ = kit
    call = execute(kit, "get_price_snapshot", {"symbols": SYMBOLS})
    content = client.get("/api/evidence/" + evidence_id(call)).json()["content"]
    assert call["quality"] == "unverified"
    assert "upstream_timestamp_is_not_each_symbol_trade_time" in content["issues"]


def test_ifind_requires_discovery_readonly_hint_and_explicit_allowlist(tmp_path):
    class FakeMCP:
        calls = 0

        async def discover(self):
            return [{"name": "market_width", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}, "annotations": {"readOnlyHint": True}},
                    {"name": "trade", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": False}},
                    {"name": "unknown", "inputSchema": {"type": "object"}, "annotations": None}]

        async def call(self, name, arguments):
            self.calls += 1
            return {"content": [{"type": "text", "text": "构造 MCP 输出"}], "isError": False}

    fake = FakeMCP()
    settings = Settings(data_dir=tmp_path, ifind_mcp_url="https://synthetic.example/mcp", ifind_allowlist=("market_width", "trade", "unknown"))
    with TestClient(create_api(settings, ifind_client=fake)) as client:
        client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf_token"]
        assert client.post("/api/tools/ifind/discover").json()["status"] == "discovered"
        thread = client.post("/api/threads", json={"title": "MCP"}).json()
        run = client.post(f"/api/threads/{thread['id']}/runs", json={"goal": "MCP"}).json()
        for name in ("trade", "unknown"):
            assert client.post(f"/api/runs/{run['id']}/tools/ifind.{name}", json={}).status_code == 403
        allowed = client.post(f"/api/runs/{run['id']}/tools/ifind.market_width", json={}).json()
        assert allowed["quality"] == "unverified" and fake.calls == 1


def test_stale_calendar_blocks_a_normal_recent_period_result(kit):
    calendar = execute(kit, "get_trading_calendar")
    source = execute(kit, "get_daily_bars", {"thscode": SYMBOLS[0], "start": DAYS[0], "end": DAYS[-1] + 10 * 86400000})
    result = execute(kit, "calculate_price_comparison", {
        "evidence_ids": [evidence_id(source)], "calendar_evidence_id": evidence_id(calendar), "trading_days": 5})
    assert result["quality"] == "stale" and result["result"]["derived_metric_ids"] == []


@pytest.mark.parametrize("prices", [[100, 0, 90, 108, 108], [100, None, 90, 108, 108], [1e-300, 1e300, 90, 108, 108]])
def test_zero_missing_and_extreme_prices_cannot_produce_normal_metrics(kit, prices):
    _, _, app, *_ = kit
    original = app.state.research.fuyao.fetch

    async def changed(path, params):
        result = await original(path, params)
        if path.endswith("historical"):
            for record, price in zip(result.body["data"]["item"], prices):
                record["close_price"] = price
        return result

    app.state.research.fuyao.fetch = changed
    calendar, source = execute(kit, "get_trading_calendar"), bars(kit)
    metric = execute(kit, "calculate_price_comparison", {
        "evidence_ids": [evidence_id(source)], "calendar_evidence_id": evidence_id(calendar), "trading_days": 5})
    assert metric["quality"] == "failed" and metric["result"]["derived_metric_ids"] == []


def test_concurrent_identical_calls_dispatch_once(kit):
    _, run_id, app, _, _ = kit
    service = app.state.research
    owner = service.db.one("SELECT session_id FROM runs WHERE id=?", (run_id,))["session_id"]

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def handler(request):
            calls.append(request)
            started.set()
            await release.wait()
            return httpx.Response(200, json=response_for(request))

        service.fuyao.transport = httpx.MockTransport(handler)
        first = asyncio.create_task(service.execute(owner, run_id, "get_trading_calendar", {}))
        await started.wait()
        from server.services import ServiceError
        with pytest.raises(ServiceError) as error:
            await service.execute(owner, run_id, "get_trading_calendar", {})
        assert error.value.status == 409
        release.set()
        result = await first
        assert result["quality"] == "available" and len(calls) == 1

    asyncio.run(scenario())


def test_declared_task_dependencies_are_enforced(kit):
    client, run_id, _, requests, _ = kit
    first = client.post(f"/api/runs/{run_id}/tasks", json={"title": "日历", "tool": "get_trading_calendar"}).json()
    second = client.post(f"/api/runs/{run_id}/tasks", json={"title": "解析", "tool": "resolve_security",
                          "arguments": {"q": SYMBOLS[0]}, "depends_on": [first["id"]]}).json()
    payload = {"arguments": {"q": SYMBOLS[0]}, "task_id": second["id"]}
    assert client.post(f"/api/runs/{run_id}/tools/resolve_security", json=payload).status_code == 409
    assert requests == []
    assert client.post(f"/api/runs/{run_id}/tools/get_trading_calendar", json={"task_id": first["id"]}).json()["quality"] == "available"
    assert client.post(f"/api/runs/{run_id}/tools/resolve_security", json=payload).json()["quality"] == "available"


def test_modified_evidence_fingerprint_blocks_calculation(kit):
    _, _, app, *_ = kit
    calendar, source = execute(kit, "get_trading_calendar"), bars(kit)
    record_id = evidence_id(source)
    row = app.state.research.db.one("SELECT content_json FROM evidence WHERE id=?", (record_id,))
    content = json.loads(row["content_json"])
    content["records"][0]["close_price"] = 900
    with app.state.research.db.connect() as connection:
        connection.execute("UPDATE evidence SET content_json=? WHERE id=?", (json.dumps(content), record_id))
    result = execute(kit, "calculate_price_comparison", {
        "evidence_ids": [record_id], "calendar_evidence_id": evidence_id(calendar), "trading_days": 5})
    assert result["quality"] == "failed"
    assert "指纹" in result["result"]["issues"][0]
