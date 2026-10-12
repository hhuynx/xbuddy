from __future__ import annotations

import asyncio
import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from server.api.routes import create_api
from server.config import Settings
from server.harness.planning import validate_plan
from server.harness.reporting import build_report, safe_text
from server.storage.database import encode, fingerprint
from test_data_foundation import DAYS, SYMBOLS, response_for


def price_plan():
    return {"tasks": [
        {"id": "calendar", "title": "交易日历", "tool": "get_trading_calendar", "arguments": {}, "depends_on": []},
        *[{"id": "bars_" + str(i), "title": "历史价格", "tool": "get_daily_bars", "arguments": {
            "thscode": s, "start": DAYS[0], "end": DAYS[-1]}, "depends_on": []} for i, s in enumerate(SYMBOLS)],
        {"id": "compare", "title": "比较历史表现", "tool": "calculate_price_comparison", "arguments": {
            "evidence_ids": ["@bars_0.evidence", "@bars_1.evidence"], "calendar_evidence_id": "@calendar.evidence",
            "trading_days": 5}, "depends_on": ["calendar", "bars_0", "bars_1"]}], "limitations": []}


class FakeModel:
    configured = True

    def __init__(self, plan=None):
        self.plan = plan or price_plan()
        self.calls = []

    async def complete(self, messages):
        context = json.loads(messages[-1]["content"])
        self.calls.append(context)
        if context.get('stage') == 'review':
            refs = list(context['facts'])
            return {'action':'finish','coverage':[{'objective_id':o['id'],'status':'satisfied' if refs else 'missing',
                'fact_refs':refs,'reason':'' if refs else '供应商没有可用数据，无法核验该项研究。'} for o in context['objectives']],
                'sections':[{'title':'研究解释','paragraphs':[{'kind':'inference' if refs else 'uncertain',
                    'text':'历史样本仅反映已观察的表现，分析需要结合统计口径与数据覆盖。' if refs else '供应商数据不可用，本次无法核验研究目标。',
                    'fact_refs':refs[:12]}]}], 'tasks':[], 'limitations':[],'unresolved_goals':context['previous_plan'].get('unresolved_goals',[]),'question':None}, {'total_tokens':80,'token_status':'reported','cost_status':'unknown'}
        if "previous_plan" in context:
            return {"tasks": [], "limitations": ["无法恢复的供应商缺口"]}, {"total_tokens": 80, "token_status": "reported", "cost_status": "unknown"}
        return self.plan, {"total_tokens": 100, "token_status": "reported", "cost_status": "unknown"}


def prepare(client):
    client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf_token"]
    return client.post("/api/threads", json={"title": "构造 Agent 研究"}).json()["id"]


def submit(client, thread, budget=None):
    response = client.post(f"/api/threads/{thread}/runs", json={"goal": "比较两个标的历史价格", "mode": "agent", **({"budget": budget} if budget else {})})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def wait(client, run_id, statuses=None, timeout=20):
    statuses = statuses or {"completed", "partial", "blocked", "failed", "recoverable_failed"}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get("/api/runs/" + run_id).json()
        if state["status"] in statuses:
            return state
        time.sleep(0.03)
    raise AssertionError({"status": state["status"], "events": client.get(f"/api/runs/{run_id}/events").json()})


@pytest.fixture
def agent(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response_for(request))
    model = FakeModel()
    # These cases synchronize on an individual dispatch. Parallel batch contracts
    # are exercised separately in test_minimal_deployment with concurrency=2.
    settings = Settings(data_dir=tmp_path, fuyao_api_key="synthetic-secret", tool_concurrency=1)
    app = create_api(settings, httpx.MockTransport(handler), model=model)
    with TestClient(app) as client:
        thread = prepare(client)
        yield client, thread, app, model, requests, settings


def test_goal_to_valid_report_and_numeric_provenance(agent):
    client, thread, app, model, requests, _ = agent
    run_id = submit(client, thread)
    state = wait(client, run_id)
    assert state["status"] == "completed", client.get(f"/api/runs/{run_id}/events?limit=500").json()["events"][-1]["payload"]
    assert len(requests) == 3 and len(model.calls) == 2
    assert state["attempts_used"] == 4 and state["runtime"]["model_tokens"] == 180
    artifact = client.get(f"/api/runs/{run_id}/report").json()
    report = artifact["content"]
    assert report["validation_status"] == "passed" and report["status"] == "complete"
    returns = [x for x in report["claims"] if x.get("field") == "historical_price_change_pct"]
    assert len(returns) == 2 and returns[0]["value"] == pytest.approx(8)
    assert returns[0]["derived_metric_ids"] and returns[0]["evidence_ids"] and returns[0]["unit"] == "%"
    assert {c["kind"] for c in report["claims"]} == {"fact", "inference"}
    events = client.get(f"/api/runs/{run_id}/events?limit=500").json()["events"]
    assert {"plan.validated", "model.finished", "metric.saved", "report.saved", "run.finished"} <= {e["event_type"] for e in events}
    assert "synthetic-secret" not in encode(state) + encode(report) + encode(events)
    # Public task/tool APIs cannot bypass the agent's plan and controls.
    assert client.post(f"/api/runs/{run_id}/tools/get_trading_calendar", json={}).status_code == 409
    assert client.post(f"/api/runs/{run_id}/tasks", json={"title": "绕过", "tool": "get_trading_calendar"}).status_code == 409


@pytest.mark.parametrize("decision", ["approve", "reject", "modify"])
def test_budget_interrupt_is_bound_and_decisions_respected(agent, decision):
    client, thread, _, _, requests, _ = agent
    run_id = submit(client, thread, {"tool_attempts": 0})
    state = wait(client, run_id, {"awaiting_approval"})
    assert not requests and state["attempts_used"] == 0
    approval = state["approvals"][-1]
    content = approval["content"]
    path = f"/api/approvals/{approval['id']}/decision"
    payload = {"decision": decision, "plan_version": content["plan_version"], "parameters_fingerprint": "0" * 64}
    assert client.post(path, json=payload).status_code == 409
    payload["parameters_fingerprint"] = content["parameters_fingerprint"]
    if decision == "modify":
        payload["budget"] = content["parameters"]["proposed_budget"] | {"tool_attempts": 0}
    assert client.post(path, json=payload).status_code == 200
    result = wait(client, run_id)
    assert result["status"] == ("completed" if decision == "approve" else "failed")
    assert len(requests) == (3 if decision == "approve" else 0)
    assert client.post(path, json=payload).status_code == 409


def test_model_budget_interrupt_happens_before_model_dispatch(agent):
    client, thread, _, model, requests, _ = agent
    run_id = submit(client, thread, {"model_calls": 0})
    state = wait(client, run_id, {"awaiting_approval"})
    assert not model.calls and not requests
    approval = state["approvals"][-1]
    payload = {"decision": "approve", "plan_version": approval["content"]["plan_version"],
               "parameters_fingerprint": approval["content"]["parameters_fingerprint"]}
    assert client.post(f"/api/approvals/{approval['id']}/decision", json=payload).status_code == 200
    assert wait(client, run_id)["status"] == "completed"


@pytest.mark.parametrize("action", ["pause", "stop"])
def test_control_during_tool_call_blocks_next_dispatch(agent, action):
    client, thread, app, _, requests, _ = agent
    started, release = threading.Event(), threading.Event()
    original = app.state.research.fuyao.fetch
    async def slow(path, params):
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return await original(path, params)
    app.state.research.fuyao.fetch = slow
    run_id = submit(client, thread)
    assert started.wait(3)
    assert client.post(f"/api/runs/{run_id}/control/{action}").status_code == 200
    release.set()
    wait(client, run_id, {"paused" if action == "pause" else "stopped"})
    time.sleep(0.25)
    assert len(requests) == 1
    if action == "pause":
        assert client.post(f"/api/runs/{run_id}/control/resume").status_code == 200
        assert wait(client, run_id)["status"] == "completed"
        assert len(requests) == 3
    else:
        assert client.get(f"/api/runs/{run_id}/report").status_code == 404


def test_retry_attempts_consume_budget_and_missing_data_stays_partial(agent):
    client, thread, app, model, requests, _ = agent
    model.plan = {"tasks": [price_plan()["tasks"][1]], "limitations": []}
    async def failure(path, params):
        from server.tools.fuyao import FetchResult
        requests.append(path)
        return FetchResult(http_status=503, error="upstream_unavailable", retryable=True)
    app.state.research.fuyao.fetch = failure
    run_id = submit(client, thread, {"tool_attempts": 2})
    state = wait(client, run_id)
    assert state["status"] == "failed" and state["attempts_used"] == 2
    assert len(requests) == 2 and state["tool_calls"][0]["attempts"] == 2
    assert state["runtime"]["replan_count"] == 0 and len(model.calls) == 2 and model.calls[-1]['stage'] == 'review'
    report = client.get(f"/api/runs/{run_id}/report").json()["content"]
    assert report["gaps"] and all(c["kind"] == "uncertain" for c in report["claims"])


def test_changed_raw_data_or_formula_blocks_core_claims(agent):
    client, thread, app, *_ = agent
    run_id = submit(client, thread)
    state = wait(client, run_id)
    owner = app.state.research.db.one("SELECT session_id FROM runs WHERE id=?", (run_id,))["session_id"]
    metric = app.state.research.collection(owner, run_id, "derived_metrics")[0]
    changed = metric["content"]
    changed["results"][0]["metrics"]["historical_price_change_pct"] = 999
    with app.state.research.db.connect() as conn:
        conn.execute("UPDATE derived_metrics SET content_json=?,content_sha256=? WHERE id=?", (encode(changed), fingerprint(changed), metric["id"]))
    report = build_report(app.state.research, owner, run_id)
    assert report["validation_status"] == "failed" and not report["claims"]
    assert any(x.startswith("metric_integrity_failed") for x in report["validation_errors"])


def test_approval_and_checkpoints_survive_restart_without_repeating_results(agent):
    client, thread, app, model, requests, settings = agent
    run_id = submit(client, thread, {"tool_attempts": 1})
    state = wait(client, run_id, {"awaiting_approval"})
    assert len(requests) == 1
    cookie = dict(client.cookies)
    # Close the old worker explicitly, reopen both application and SQLite checkpointer.
    client.portal.call(app.state.harness.close)
    model2 = FakeModel()
    app2 = create_api(settings, httpx.MockTransport(lambda r: (requests.append(r) or httpx.Response(200, json=response_for(r)))), model=model2)
    with TestClient(app2) as new:
        new.cookies.update(cookie)
        new.headers["X-CSRF-Token"] = new.get("/api/session").json()["csrf_token"]
        loaded = new.get(f"/api/runs/{run_id}").json()
        assert loaded["status"] == "awaiting_approval"
        approval = loaded["approvals"][-1]
        assert new.post(f"/api/approvals/{approval['id']}/decision", json={"decision": "approve", "plan_version": approval["content"]["plan_version"], "parameters_fingerprint": approval["content"]["parameters_fingerprint"]}).status_code == 200
        finished = wait(new, run_id)
        assert finished["status"] == "completed" and len(requests) == 3
        assert len(finished["tool_calls"]) == 4 and len(model2.calls)==1 and model2.calls[0]['stage']=='review'


@pytest.mark.parametrize("bad", ["cycle", "tool", "literal_evidence", "bad_parameter", "recommendation"])
def test_bad_model_plan_never_reaches_tools(agent, bad):
    client, thread, _, model, requests, _ = agent
    plan = price_plan()
    if bad == "cycle":
        plan["tasks"][0]["depends_on"] = ["compare"]
    elif bad == "tool":
        plan["tasks"][0]["tool"] = "arbitrary_http"
    elif bad == "literal_evidence":
        plan["tasks"][-1]["arguments"]["evidence_ids"] = ["foreign-evidence"]
    elif bad == "bad_parameter":
        plan["tasks"][1]["arguments"]["interval"] = "1m"
    else:
        plan["tasks"][0]["title"] = "建议买入并保证收益"
    model.plan = plan
    run_id = submit(client, thread)
    state = wait(client, run_id)
    assert state["status"] == "failed" and not requests and len(model.calls) == 2
    assert not safe_text("一定上涨且保证收益")


def test_foreign_session_cannot_approve_control_or_read_report(agent):
    client, thread, app, *_ = agent
    run_id = submit(client, thread, {"tool_attempts": 0})
    state = wait(client, run_id, {"awaiting_approval"})
    a = state["approvals"][0]
    with TestClient(app) as other:
        prepare(other)
        assert other.get(f"/api/runs/{run_id}/report").status_code == 404
        assert other.post(f"/api/runs/{run_id}/control/stop").status_code == 404
        assert other.post(f"/api/approvals/{a['id']}/decision", json={"decision": "approve", "plan_version": a["content"]["plan_version"], "parameters_fingerprint": a["content"]["parameters_fingerprint"]}).status_code == 404


def test_two_workers_cannot_lease_or_dispatch_the_same_run(agent):
    client, thread, app, _, requests, settings = agent
    started, release = threading.Event(), threading.Event()
    original = app.state.research.fuyao.fetch
    async def slow(path, params):
        started.set()
        while not release.is_set():
            await asyncio.sleep(.01)
        return await original(path, params)
    app.state.research.fuyao.fetch = slow
    run_id = submit(client, thread)
    assert started.wait(3)
    second_model = FakeModel()
    app2 = create_api(settings, httpx.MockTransport(lambda r: (requests.append(r) or httpx.Response(200, json=response_for(r)))), model=second_model)
    with TestClient(app2):
        time.sleep(.3)
        assert not second_model.calls
        release.set()
        assert wait(client, run_id)["status"] == "completed"
        assert len(requests) == 3 and not second_model.calls


def test_inflight_restart_recovers_checkpoint_and_reuses_finished_task(agent):
    client, thread, app, _, requests, settings = agent
    started = threading.Event()
    original = app.state.research.fuyao.fetch
    async def slow_second(path, params):
        if path.endswith("historical"):
            started.set()
            await asyncio.sleep(60)
        return await original(path, params)
    app.state.research.fuyao.fetch = slow_second
    run_id = submit(client, thread)
    # Synchronize on the actual second dispatch; a three-second startup SLA
    # is unrelated to checkpoint recovery and flakes during browser QA load.
    assert started.wait(10)
    before = client.get(f"/api/runs/{run_id}").json()
    assert len(requests) == 1 and before["attempts_used"] == 2
    finished_call = next(c for c in before["tool_calls"] if c["status"] == "completed")["id"]
    client.portal.call(app.state.harness.close)
    model2 = FakeModel()
    app2 = create_api(settings, httpx.MockTransport(lambda r: (requests.append(r) or httpx.Response(200, json=response_for(r)))), model=model2)
    with TestClient(app2) as new:
        new.cookies.update(dict(client.cookies))
        new.headers["X-CSRF-Token"] = new.get("/api/session").json()["csrf_token"]
        recovered = wait(new, run_id)
        assert recovered["status"] == "completed" and len(model2.calls)==1 and model2.calls[0]['stage']=='review'
        assert len(requests) == 3 and len(recovered["tool_calls"]) == 4
        assert recovered["attempts_used"] == 5
        assert sum(c["id"] == finished_call for c in recovered["tool_calls"]) == 1
        events = new.get(f"/api/runs/{run_id}/events?limit=500").json()["events"]
        assert any(e["event_type"] == "tool.recovered" and e["payload"]["uncertain_remote_dispatch"] for e in events)


def test_deadline_limits_inflight_timeout_and_blocks_next_tool(agent):
    client, thread, app, _, requests, _ = agent
    started = threading.Event()
    async def timeout(path, params):
        started.set()
        await asyncio.sleep(2)
        raise AssertionError("should_have_timed_out")
    app.state.research.fuyao.fetch = timeout
    run_id = submit(client, thread)
    assert started.wait(3)
    with app.state.research.db.connect() as conn:
        conn.execute("UPDATE run_runtime SET deadline=? WHERE run_id=?", (time.time()-.01, run_id))
    state = wait(client, run_id)
    assert state["status"] == "failed" and len(state["tool_calls"]) == 1 and state["attempts_used"] == 1
    assert not requests and len(state["runtime"]["plan"]["tasks"]) == 4


def test_uncovered_goal_is_partial_but_method_limitations_are_not_data_gaps(agent):
    client, thread, _, model, *_ = agent
    model.plan = {"tasks": [price_plan()["tasks"][0]], "limitations": ["日历仅描述供应商覆盖范围"], "unresolved_goals": ["未启用估值工具"]}
    run_id = submit(client, thread)
    state = wait(client, run_id)
    report = client.get(f"/api/runs/{run_id}/report").json()["content"]
    assert state["status"] == "partial"
    assert report["limitations"] and len(report["gaps"]) == 1 and report["gaps"][0]["reason"] == "unsupported_goal"
    assert any(c["kind"] == "fact" and c["field"] == "latest_covered_trade_date_ms" for c in report["claims"])


def test_name_resolution_cannot_hide_ambiguity_by_requesting_one_match(agent):
    _, _, app, *_ = agent
    plan = {"tasks": [
        {"id": "resolve", "title": "检索名称", "tool": "resolve_security", "arguments": {"q": "构造名称", "limit": 1}, "depends_on": []},
        {"id": "bars", "title": "查询", "tool": "get_daily_bars", "arguments": {"thscode": "@resolve.thscode", "start": DAYS[0], "end": DAYS[-1]}, "depends_on": ["resolve"]}]}
    with pytest.raises(ValueError, match="name_search_must_detect_ambiguity"):
        validate_plan(app.state.research, plan, ["resolve_security", "get_daily_bars"])


@pytest.mark.parametrize("variant", ["valid", "bad_json", "truncated", "http_failure", "too_big"])
def test_real_model_adapter_checks_json_finish_and_http_without_leaking(variant):
    from server.harness.model import JsonModel, ModelFailure
    calls = []
    def handler(request):
        calls.append(request)
        body = {"choices": [{"finish_reason": "stop", "message": {"content": encode({"tasks": [], "limitations": []})}}], "usage": {"total_tokens": 45}}
        if variant == "bad_json":
            body["choices"][0]["message"]["content"] = "not JSON synthetic-secret"
        if variant == "truncated":
            body["choices"][0]["finish_reason"] = "length"
        if variant == "too_big":
            return httpx.Response(200, content=b"x" * (1024*1024+1))
        return httpx.Response(401 if variant == "http_failure" else 200, json=body)
    model = JsonModel(Settings(llm_base_url="https://synthetic.example/v1", llm_model="synthetic-model", llm_api_key="synthetic-secret"), httpx.MockTransport(handler))
    if variant == "valid":
        response, usage = asyncio.run(model.complete([{"role": "user", "content": "构造计划"}]))
        assert response["tasks"] == [] and usage["total_tokens"] == 45 and usage["cost_status"] == "unknown"
    else:
        with pytest.raises(ModelFailure) as error:
            asyncio.run(model.complete([{"role": "user", "content": "构造计划"}]))
        assert "synthetic-secret" not in str(error.value)
    assert calls[0].url.path == "/v1/chat/completions" and calls[0].headers["Authorization"] == "Bearer synthetic-secret"
