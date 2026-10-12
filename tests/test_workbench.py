from __future__ import annotations

import asyncio
import copy
import json
import time

import pytest
from fastapi.testclient import TestClient

from server.events import latest_cursor, workspace_batches, workspace_snapshot
from server.storage.database import encode, fingerprint
from server.ui.presentation import financial_rows, number, price_charts, raw_value
from test_harness import agent, prepare, submit, wait


def editable(client, app, run_id):
    state = wait(client, run_id, {"awaiting_approval", "paused"})
    deadline = time.monotonic() + 3
    while app.state.research.db.one("SELECT lease_until FROM run_runtime WHERE run_id=?", (run_id,))["lease_until"] > time.time():
        assert time.monotonic() < deadline
        time.sleep(.02)
    return state


def update_payload(state):
    return {"plan_version": state["plan_version"], "plan_fingerprint": fingerprint(state["runtime"]["plan"]),
            "plan": copy.deepcopy(state["runtime"]["plan"])}


def approve(client, state):
    pending = next(a for a in state["approvals"] if a["status"] == "pending")
    content = pending["content"]
    return client.post(f"/api/approvals/{pending['id']}/decision", json={"decision": "approve",
        "plan_version": content["plan_version"], "parameters_fingerprint": content["parameters_fingerprint"]})


def test_plan_edit_changes_real_execution_and_expires_old_approval(agent):
    client, thread, app, model, requests, _ = agent
    run_id = submit(client, thread, {"tool_attempts": 1})
    state = editable(client, app, run_id)
    assert len(requests) == 1
    first_call = state["tool_calls"][0]["id"]
    payload = update_payload(state)
    payload["plan"]["tasks"][-1]["arguments"]["trading_days"] = 3
    path = f"/api/runs/{run_id}/plan"
    result = client.put(path, json=payload)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "paused" and result.json()["plan_version"] == 2
    assert approve(client, state).status_code == 409
    assert client.put(path, json=payload).status_code == 409
    assert client.post(f"/api/runs/{run_id}/control/resume").status_code == 200
    state2 = editable(client, app, run_id)
    assert state2["approvals"][-1]["content"]["plan_version"] == 2
    assert approve(client, state2).status_code == 200
    done = wait(client, run_id)
    assert done["status"] == "completed", encode({"gaps": client.get(f"/api/runs/{run_id}/report").json()["content"]["gaps"], "calls": done["tool_calls"]})
    assert len(model.calls) == 2 and model.calls[-1]['stage']=='review' and len(requests) == 3
    assert done["tool_calls"][0]["id"] == first_call
    calc = next(c for c in done["tool_calls"] if c["tool"] == "calculate_price_comparison")
    assert calc["parameters"]["trading_days"] == 3
    report = client.get(f"/api/runs/{run_id}/report").json()["content"]
    change = next(c for c in report["claims"] if c.get("field") == "historical_price_change_pct")
    assert change["value"] == pytest.approx(20)  # last three closes: 90,108,108


def test_plan_edit_preserves_dispatched_work_and_enforces_dependencies(agent):
    client, thread, app, *_ = agent
    run_id = submit(client, thread, {"tool_attempts": 1})
    state = editable(client, app, run_id)
    payload = update_payload(state)
    payload["plan"]["tasks"][0]["title"] = "修改已经执行的任务"
    assert client.put(f"/api/runs/{run_id}/plan", json=payload).status_code == 409
    payload = update_payload(state)
    payload["plan"]["tasks"][1]["depends_on"] = ["compare"]
    assert client.put(f"/api/runs/{run_id}/plan", json=payload).status_code == 422
    payload = update_payload(state)
    payload["plan"]["tasks"] = [payload["plan"]["tasks"][0]]
    assert client.put(f"/api/runs/{run_id}/plan", json=payload).status_code == 200
    assert client.post(f"/api/runs/{run_id}/control/resume").status_code == 200
    assert wait(client, run_id)["status"] == "completed"
    assert len(client.get(f"/api/runs/{run_id}/snapshot").json()["run"]["tasks"]) == 1


def test_plan_edit_refuses_active_lease_and_foreign_session(agent):
    client, thread, app, *_ = agent
    run_id = submit(client, thread, {"tool_attempts": 0})
    state = editable(client, app, run_id)
    payload = update_payload(state)
    with app.state.research.db.connect() as conn:
        conn.execute("UPDATE run_runtime SET lease_until=? WHERE run_id=?", (time.time()+2, run_id))
    assert client.put(f"/api/runs/{run_id}/plan", json=payload).status_code == 409
    with TestClient(app) as stranger:
        prepare(stranger)
        assert stranger.put(f"/api/runs/{run_id}/plan", json=payload).status_code == 404
        assert stranger.get(f"/api/runs/{run_id}/snapshot").status_code == 404
        assert stranger.get(f"/api/runs/{run_id}/events/stream").status_code == 404
    client.headers.pop("X-CSRF-Token")
    assert client.put(f"/api/runs/{run_id}/plan", json=payload).status_code == 403


def test_edited_plan_survives_worker_restart_before_continue(agent):
    client, thread, app, model, requests, _ = agent
    run_id = submit(client, thread, {"tool_attempts": 1})
    state = editable(client, app, run_id)
    payload = update_payload(state)
    payload["plan"]["tasks"][-1]["arguments"]["trading_days"] = 4
    assert client.put(f"/api/runs/{run_id}/plan", json=payload).status_code == 200
    client.portal.call(app.state.research.harness.close)
    client.portal.call(app.state.research.harness.start)
    assert client.post(f"/api/runs/{run_id}/control/resume").status_code == 200
    state = editable(client, app, run_id)
    assert approve(client, state).status_code == 200
    done = wait(client, run_id)
    assert done["status"] == "completed" and len(requests) == 3 and len(model.calls) == 2 and model.calls[-1]['stage']=='review'
    calc = next(c for c in done["tool_calls"] if c["tool"] == "calculate_price_comparison")
    assert calc["parameters"]["trading_days"] == 4


def frames(text):
    return [dict(line.split(": ", 1) for line in part.splitlines() if ": " in line)
            for part in text.split("\n\n") if part.strip()]


def test_sse_replays_persisted_ids_with_last_event_id_and_snapshot(agent):
    client, thread, app, *_ = agent
    run_id = submit(client, thread)
    wait(client, run_id)
    events = client.get(f"/api/runs/{run_id}/events?limit=500").json()["events"]
    path = f"/api/runs/{run_id}/events/stream"
    response = client.get(path)
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    data = frames(response.text)
    assert data[0]["event"] == "snapshot" and "id" not in data[0]
    assert [int(x["id"]) for x in data if "id" in x] == [e["id"] for e in events]
    assert data[-1]["event"] == "complete"
    after = events[len(events)//2]["id"]
    resumed = frames(client.get(path + "?after=1", headers={"Last-Event-ID": str(after)}).text)
    assert [int(x["id"]) for x in resumed if "id" in x] == [e["id"] for e in events if e["id"] > after]
    assert client.get(path, headers={"Last-Event-ID": "invalid"}).status_code == 422
    assert client.get(path + "?after=-1").status_code == 422
    reset = frames(client.get(path + "?after=9223372036854775807").text)
    assert reset[0]["event"] == "reset"
    state = client.get(f"/api/runs/{run_id}/snapshot").json()
    assert state["event_cursor"] >= events[-1]["id"]
    assert state["run"]["model_usage"][0]["usage"]["token_status"] == "reported"
    assert "synthetic-secret" not in response.text + encode(state)


def test_workspace_subscription_replays_only_owned_events(agent):
    client, thread, app, *_ = agent
    run_id = submit(client, thread)
    wait(client, run_id)
    service = app.state.research
    owner = service.db.one("SELECT session_id FROM runs WHERE id=?", (run_id,))["session_id"]
    snapshot = workspace_snapshot(service, owner, run_id)
    cursor = snapshot["event_cursor"]
    with TestClient(app) as stranger:
        prepare(stranger)
    with service.db.connect() as conn:
        service.db.event(conn, owner, run_id, "test.persisted", {"real_event": True})
    async def read():
        feed = workspace_batches(service, owner, cursor)
        try:
            batch = await asyncio.wait_for(anext(feed), 1)
            assert len(batch) == 1 and batch[0]["event_type"] == "test.persisted"
            assert batch[0]["id"] == latest_cursor(service, owner)
        finally:
            await feed.aclose()
    asyncio.run(read())


def test_price_chart_points_link_to_verified_raw_fields(agent):
    client, thread, app, *_ = agent
    run_id = submit(client, thread)
    wait(client, run_id)
    service = app.state.research
    owner = service.db.one("SELECT session_id FROM runs WHERE id=?", (run_id,))["session_id"]
    evidence = service.collection(owner, run_id, "evidence")
    charts, issues = price_charts(service, owner, run_id, evidence)
    assert not issues and len(charts) == 1
    chart = charts[0]
    assert chart["options"]["series"][0]["data"] == [100,120,90,108,108]
    assert chart["options"]["series"][0]["connectNulls"] is False
    ref = chart["point_refs"][0][2]
    record = service.resource("evidence", ref["evidence_id"], owner)
    raw = service.resource("raw_records", record["raw_id"], owner)
    assert raw_value(raw["body"], ref["raw_path"]) == 90
    with service.db.connect() as conn:
        conn.execute("UPDATE raw_records SET body_json='{}' WHERE id=?", (raw["id"],))
    charts, issues = price_charts(service, owner, run_id, evidence)
    assert len(charts[0]["sources"]) == 1 and issues[0]["reason"] == "conflict"


def test_financial_table_retains_null_units_and_refuses_blocked_report():
    report = {"validation_status": "passed", "claims": [{"kind": "fact", "field": "operating_income", "symbol": "600519.SH",
        "as_of": "2024-12-31T00:00:00+08:00", "value": 1000000000, "unit": "CNY", "evidence_ids": ["source"]}]}
    rows = financial_rows(report)
    assert rows[0]["operating_income"] == "10.00 亿元"
    assert rows[0]["parent_holder_net_profit"] == "缺失"
    assert rows[0]["evidence_ids"] == ["source"] and rows[0]["claim_indices"] == [0]
    assert financial_rows(report | {"validation_status": "blocked"}) == []
    assert number(None, "CNY") == "缺失" and number(0, "%") == "0.00%"
    assert raw_value({"data": {"item": [{"close_price": None}]}}, "$.data.item[0].close_price") is None
    assert raw_value({}, "__import__('os')") is None
    assert raw_value({"data": [1]}, "$.data.item[0].price") is None


def test_chart_unions_observations_without_filling_gaps_or_mixing_adjustments(monkeypatch):
    from server.ui import presentation
    from test_data_foundation import DAYS
    def record(identifier, adjust, rows, quality="available"):
        return {"id": identifier, "content": {"tool": "get_daily_bars", "quality": quality,
            "method": {"adjust": adjust, "interval": "1d"}, "parameters": {"thscode": identifier},
            "records": rows, "as_of": "2025-01-10", "fetched_at": "2025-01-11"}}
    monkeypatch.setattr(presentation, "checked_evidence", lambda service, owner, run, record: record["content"])
    charts, issues = price_charts(None, "owner", "run", [
        record("one", "forward", [{"date_ms": DAYS[1], "close_price": 20, "source_index": 7}, {"date_ms": DAYS[0], "close_price": 10, "source_index": 4}]),
        record("two", "forward", [{"date_ms": DAYS[1], "close_price": 30, "source_index": 2}]),
        record("three", "none", [{"date_ms": DAYS[0], "close_price": 40, "source_index": 0}]),
        record("bad", "forward", [], "stale")])
    assert len(charts) == 2 and issues == [{"evidence_id": "bad", "reason": "stale"}]
    assert charts[0]["options"]["series"][1]["data"] == [None, 30]
    assert charts[0]["point_refs"][1][0] is None
    assert charts[0]["point_refs"][0][0]["raw_path"] == "$.data.item[4].close_price"
