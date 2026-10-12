"""Real model + Fuyao + approval + process restart + validated research report.

Public output contains metadata only. Market records and checkpoints remain local.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
GOAL = "比较贵州茅台600519.SH与五粮液000858.SZ最近60个交易日的历史价格表现、波动率和最大回撤（前复权），并比较最近共同年报营业收入和归母净利润。仅描述历史及已披露财报，列出证据、单位、时点和数据缺口。"


def main():
    config = {**dotenv_values(ROOT / ".env"), **os.environ}
    if not all(config.get(k) for k in ("FUYAO_API_KEY", "LLM_BASE_URL", "LLM_MODEL")) or not (config.get("LLM_API_KEY") or config.get("LLM_ALLOW_NO_KEY") == "true"):
        print(json.dumps({"status": "blocked", "reason": "missing_model_or_fuyao_configuration"}))
        return 2
    cache = ROOT / ".cache/verification/batch3-live"
    cache.mkdir(parents=True, exist_ok=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "XBUDDY_HOST": "127.0.0.1", "XBUDDY_PORT": str(port),
           "XBUDDY_DATA_DIR": str(cache / "data"), "XBUDDY_COOKIE_SECURE": "false"}
    base = f"http://127.0.0.1:{port}"
    report = {"scope": "batch3_real_model_fuyao", "checked_at": datetime.now(timezone.utc).isoformat()}
    process = None
    log = (cache / "server.log").open("w", encoding="utf-8")

    def stop(child):
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)

    def start():
        child = subprocess.Popen([sys.executable, "-m", "server.main"], cwd=ROOT, env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 30
            with httpx.Client(timeout=2) as probe:
                while time.monotonic() < deadline:
                    if child.poll() is not None:
                        raise RuntimeError("owned_server_exited")
                    try:
                        if probe.get(base + "/health").status_code == 200:
                            return child
                    except httpx.TransportError:
                        pass
                    time.sleep(.2)
            raise TimeoutError("owned_server_start_timeout")
        except BaseException:
            stop(child)
            raise

    def wait(client, run_id, statuses, seconds=150):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            response = client.get(f"/api/runs/{run_id}")
            response.raise_for_status()
            state = response.json()
            if state["status"] in statuses:
                return state
            if state["status"] in {"recoverable_failed", "blocked", "failed", "stopped"}:
                raise AssertionError("run_failed")
            time.sleep(.25)
        raise TimeoutError("research_timeout")

    try:
        process = start()
        with httpx.Client(base_url=base, timeout=10) as client:
            client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf_token"]
            thread = client.post("/api/threads", json={"title": "第三批真实 Agent 验收", "goal": GOAL})
            thread.raise_for_status()
            created = client.post(f"/api/threads/{thread.json()['id']}/runs", json={"goal": GOAL, "mode": "agent", "budget": {"tool_attempts": 1}})
            created.raise_for_status()
            run_id = created.json()["id"]
            state = wait(client, run_id, {"awaiting_approval"})
            assert state["attempts_used"] == 1 and len(state["tool_calls"]) == 1
            first = state["tool_calls"][0]
            assert first["status"] == "completed"
            report["approval_blocks_next_dispatch"] = "passed"
            report["planned_tasks"] = len(state["runtime"]["plan"]["tasks"])
            report["planner_model_calls_before_restart"] = state["runtime"]["model_calls"]
            first_pid = process.pid
            stop(process)
            process = start()
            assert first_pid != process.pid
            loaded = client.get(f"/api/runs/{run_id}").json()
            assert loaded["status"] == "awaiting_approval" and loaded["tool_calls"][0]["id"] == first["id"]
            approval = loaded["approvals"][-1]
            response = client.post(f"/api/approvals/{approval['id']}/decision", json={"decision": "approve",
                "plan_version": approval["content"]["plan_version"], "parameters_fingerprint": approval["content"]["parameters_fingerprint"]})
            response.raise_for_status()
            finished = wait(client, run_id, {"completed", "partial"})
            assert not any(a["status"] == "pending" for a in finished["approvals"])
            assert finished["runtime"]["model_calls"] == state["runtime"]["model_calls"], "unexpected_replan_or_repeat"
            assert sum(c["id"] == first["id"] for c in finished["tool_calls"]) == 1
            result = client.get(f"/api/runs/{run_id}/report")
            result.raise_for_status()
            artifact = result.json()["content"]
            assert artifact["validation_status"] == "passed"
            assert {"calculate_price_comparison", "calculate_financial_comparison"} <= {c["tool"] for c in finished["tool_calls"] if c["quality"] == "available"}
            assert any(c["kind"] == "fact" and c.get("derived_metric_ids") for c in artifact["claims"])
            for source in artifact["sources"]:
                assert client.get("/api/evidence/" + source["evidence_id"] + "/raw").status_code == 200
            assert finished["attempts_used"] == len(finished["tool_calls"]), "unexpected_duplicate_attempt"
            report.update(status="passed", report_status=artifact["status"], validation="passed", process_restart="passed",
                approval_checkpoint_resume="passed", completed_call_reuse="passed", raw_provenance="passed",
                tool_calls=len(finished["tool_calls"]), tool_attempts=finished["attempts_used"],
                source_count=len(artifact["sources"]), computation_count=len(artifact["computations"]),
                claim_count=len(artifact["claims"]), gap_count=len(artifact["gaps"]),
                model_calls=finished["runtime"]["model_calls"], model_tokens=finished["runtime"]["model_tokens"], cost_status="unknown")
    except Exception as error:
        report.update(status="failed", reason=type(error).__name__)
    finally:
        stop(process)
        log.close()
    target = ROOT / "docs/verification/batch3-live.local.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
