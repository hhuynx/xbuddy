"""Live API -> raw evidence -> calculations -> real process restart verification.

Only metadata is printed. Authorized market responses stay in ignored local storage.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
SHANGHAI = timezone(timedelta(hours=8))


def main() -> int:
    config = {**dotenv_values(ROOT / ".env"), **os.environ}
    if not config.get("FUYAO_API_KEY"):
        print(json.dumps({"status": "blocked", "reason": "missing_fuyao_credentials"}))
        return 2
    cache = ROOT / ".cache/verification/batch2-live"
    cache.mkdir(parents=True, exist_ok=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = {**os.environ, "XBUDDY_HOST": "127.0.0.1", "XBUDDY_PORT": str(port),
           "XBUDDY_DATA_DIR": str(cache / "data"), "XBUDDY_COOKIE_SECURE": "false"}
    base = f"http://127.0.0.1:{port}"
    report = {"checked_at": datetime.now(SHANGHAI).isoformat(), "scope": "batch2_live_api", "checks": []}
    process = None
    log = (cache / "server.log").open("w", encoding="utf-8")

    def start():
        child = subprocess.Popen([sys.executable, "-m", "server.main"], cwd=ROOT, env=env, stdout=log, stderr=log)
        deadline = time.monotonic() + 30
        try:
            with httpx.Client(timeout=2) as probe:
                while time.monotonic() < deadline:
                    if child.poll() is not None:
                        raise RuntimeError("server_exited")
                    try:
                        if probe.get(base + "/health").status_code == 200:
                            return child
                    except httpx.TransportError:
                        pass
                    time.sleep(0.2)
            raise TimeoutError("server_start_timeout")
        except BaseException:
            stop(child)
            raise

    def stop(child):
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)

    try:
        process = start()
        with httpx.Client(base_url=base, timeout=90) as client:
            token = client.get("/api/session").json()["csrf_token"]
            client.headers["X-CSRF-Token"] = token
            thread = client.post("/api/threads", json={"title": "第二批真实接入验收", "goal": "验证数据、证据、计算和重启持久化"})
            thread.raise_for_status()
            run = client.post(f"/api/threads/{thread.json()['id']}/runs", json={"goal": "第二批真实工具验收"}).json()
            run_id = run["id"]
            today = datetime.now(SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0)
            window = {"start": int((today - timedelta(days=160)).timestamp() * 1000), "end": int(today.timestamp() * 1000)}

            def call(name, arguments, key=None):
                payload = {"arguments": arguments}
                if key:
                    payload["idempotency_key"] = key
                response = client.post(f"/api/runs/{run_id}/tools/{name}", json=payload)
                response.raise_for_status()
                result = response.json()
                report["checks"].append({"tool": name, "quality": result["quality"], "attempts": result["attempts"],
                                         "reused": result["reused"], "record_count": result["result"].get("record_count"),
                                         "elapsed_ms": result["elapsed_ms"]})
                return result

            calls = [call("resolve_security", {"q": "600519.SH"}), call("get_trading_calendar", {}),
                     call("get_price_snapshot", {"symbols": ["600519.SH", "000858.SZ"]})]
            bar_calls, financial_calls = [], []
            for symbol in ("600519.SH", "000858.SZ"):
                bar_calls.append(call("get_daily_bars", {"thscode": symbol, **window}, "bars-" + symbol))
                financial_calls.append(call("get_financial_metrics", {"thscode": symbol, "period": "annual", "limit": 4}))
            all_sources = calls + bar_calls + financial_calls
            assert all(x["quality"] == "available" for x in all_sources if x["tool"] != "get_price_snapshot"), "source_quality_failure"
            assert calls[2]["quality"] == "unverified", "snapshot_semantics_not_preserved"
            source_ids = [x["result"]["evidence_ids"][0] for x in bar_calls]
            prices = call("calculate_price_comparison", {"evidence_ids": source_ids,
                          "calendar_evidence_id": calls[1]["result"]["evidence_ids"][0], "trading_days": 60})
            financials = call("calculate_financial_comparison", {"evidence_ids": [x["result"]["evidence_ids"][0] for x in financial_calls]})
            assert prices["quality"] == "available" and financials["quality"] == "available", "calculation_quality_failure"
            evidence = client.get("/api/evidence/" + source_ids[0]).json()
            raw = client.get(evidence["raw_url"])
            assert raw.status_code == 200 and raw.json()["body"]["code"] == 0
            report["raw_response_access"] = "passed"
            first_pid = process.pid
            stop(process)
            process = start()
            assert process.pid != first_pid
            persisted = client.get("/api/evidence/" + source_ids[0]).json()
            assert persisted["content_sha256"] == evidence["content_sha256"]
            for item in (prices, financials):
                assert client.get("/api/metrics/" + item["result"]["derived_metric_ids"][0]).status_code == 200
            before = client.get(f"/api/runs/{run_id}").json()["attempts_used"]
            reused = call("get_daily_bars", {"thscode": "600519.SH", **window}, "bars-600519.SH")
            after = client.get(f"/api/runs/{run_id}").json()["attempts_used"]
            assert reused["reused"] and reused["id"] == bar_calls[0]["id"] and before == after
            report.update(process_restart="passed", evidence_persistence="passed", metric_persistence="passed",
                          idempotency_after_restart="passed", successful_external_calls=7,
                          status="passed", ifind="not_configured")
    except Exception as error:
        # Error types only: do not print server response bodies or configuration.
        report.update(status="failed", reason=type(error).__name__)
    finally:
        stop(process)
        log.close()
    output = ROOT / "docs/verification/batch2-live.local.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
