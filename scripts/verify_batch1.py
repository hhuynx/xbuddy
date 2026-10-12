"""Readiness checks. Reports contain metadata only, never credentials or market rows."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[1]
SHANGHAI = timezone(timedelta(hours=8))
PACKAGES = ("fastapi", "nicegui", "uvicorn", "pydantic", "httpx", "langgraph",
            "langgraph-checkpoint-sqlite", "mcp", "python-dotenv", "pytest")
TOOLS = ("resolve_security", "get_trading_calendar", "get_price_snapshot",
         "get_daily_bars", "get_financial_metrics")


class PlanTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    tool: Literal["resolve_security", "get_trading_calendar", "get_price_snapshot",
                  "get_daily_bars", "get_financial_metrics"]
    depends_on: list[str] = Field(default_factory=list)


class ProbePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1)
    tasks: list[PlanTask] = Field(min_length=1, max_length=10)


def validate_plan(content: str) -> ProbePlan:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    plan = ProbePlan.model_validate_json(content)
    seen: set[str] = set()
    for task in plan.tasks:
        if task.id in seen or any(item not in seen for item in task.depends_on):
            raise ValueError("Task IDs must be unique and dependencies must precede tasks")
        seen.add(task.id)
    return plan


def read_config(path: Path) -> dict[str, str]:
    config = {key: value for key, value in dotenv_values(path).items() if value is not None}
    for key in (*config, "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "FUYAO_API_KEY",
                "IFIND_MCP_URL", "IFIND_API_KEY"):
        if key in os.environ:
            config[key] = os.environ[key]
    return config


def redact(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {redact(key, secrets): redact(item, secrets) for key, item in value.items()}
    return value


def valid_url(url: str) -> bool:
    parsed = urlparse(url)
    local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    return bool(parsed.hostname and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment
                and (parsed.scheme == "https" or (local and parsed.scheme == "http")))


def summarize_envelope(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict) or type(body.get("code")) is not int:
        return {"status": "failed", "reason": "invalid_envelope"}
    code = body["code"]
    if code != 0:
        return {"status": "unauthorized" if code in (2001, 2003) else "failed",
                "business_code": code}
    data = body.get("data")
    if not isinstance(data, dict):
        return {"status": "failed", "reason": "missing_data_object", "business_code": 0}
    records = data.get("item", data.get("abilities"))
    if not isinstance(records, list):
        return {"status": "failed", "reason": "unknown_data_shape", "business_code": 0}
    if any(not isinstance(item, dict) or not item for item in records):
        return {"status": "failed", "reason": "invalid_record_shape", "business_code": 0}
    fields = sorted(set().union(*(item.keys() for item in records[:3] if isinstance(item, dict))))
    return {"status": "passed" if records else "empty", "business_code": 0,
            "row_count": len(records), "field_names": fields,
            "timestamp_present": type(data.get("timestamp")) in (int, float),
            "data_timestamp_ms": data.get("timestamp") if type(data.get("timestamp")) in (int, float) else None}


def command_version(args: list[str]) -> dict[str, Any]:
    if not shutil.which(args[0]):
        return {"status": "missing"}
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=15)
        return {"status": "passed" if result.returncode == 0 else "failed",
                "version": result.stdout.strip()[:120] if result.returncode == 0 else None,
                "exit_code": result.returncode}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"status": "failed", "reason": type(error).__name__}


def probe_environment() -> dict[str, Any]:
    packages: dict[str, Any] = {}
    for name in PACKAGES:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    import sqlite3
    return {"python": platform.python_version(), "python_compatible": sys.version_info >= (3, 11),
            "sqlite": sqlite3.sqlite_version, "packages": packages,
            "git": command_version(["git", "--version"])}


def probe_model(config: dict[str, str], offline: bool, timeout: float) -> dict[str, Any]:
    missing = [key for key in ("LLM_BASE_URL", "LLM_MODEL") if not config.get(key)]
    no_key = config.get("LLM_ALLOW_NO_KEY", "false").lower() == "true"
    if not config.get("LLM_API_KEY") and not no_key:
        missing.append("LLM_API_KEY")
    if missing:
        return {"status": "blocked", "missing": missing}
    base = config["LLM_BASE_URL"].rstrip("/")
    if not valid_url(base):
        return {"status": "failed", "reason": "invalid_or_insecure_base_url"}
    if offline:
        return {"status": "not_tested", "reason": "offline_mode"}
    headers = {"Authorization": "Bearer " + config["LLM_API_KEY"]} if config.get("LLM_API_KEY") else {}
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            response = client.post(base + "/chat/completions", headers=headers, json={
                "model": config["LLM_MODEL"], "max_tokens": 800,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": (
                    "你在执行接口格式验证。只输出 JSON，不输出行情或买卖建议。"
                    '格式为 {"goal":"...","tasks":[{"id":"t1","tool":"...","depends_on":[]}]}。'
                    "工具名只能来自：" + ",".join(TOOLS) + "。任务依赖必须引用前面的任务。"
                )}, {"role": "user", "content": "规划两步：先查询交易日历，再查询行情快照。"}],
            })
            response.raise_for_status()
            body = response.json()
            plan = validate_plan(body["choices"][0]["message"]["content"])
            if ([task.tool for task in plan.tasks] != ["get_trading_calendar", "get_price_snapshot"]
                    or plan.tasks[1].depends_on != [plan.tasks[0].id]):
                raise ValueError("Plan did not follow the two-step probe goal")
            usage = {key: value for key, value in body.get("usage", {}).items()
                     if key in {"prompt_tokens", "completion_tokens", "total_tokens"} and type(value) is int}
            return {"status": "passed", "structured_plan_valid": True, "task_count": len(plan.tasks),
                    "elapsed_ms": round((time.perf_counter() - started) * 1000), "usage": usage}
    except httpx.HTTPStatusError as error:
        return {"status": "failed", "http_status": error.response.status_code}
    except Exception as error:
        return {"status": "failed", "reason": type(error).__name__}


def probe_fuyao(config: dict[str, str], offline: bool, timeout: float) -> dict[str, Any]:
    base = config.get("FUYAO_BASE_URL", "https://fuyao.aicubes.cn").rstrip("/")
    key = config.get("FUYAO_API_KEY", "")
    if not valid_url(base):
        return {"status": "failed", "reason": "invalid_or_insecure_base_url"}
    if offline:
        return {"status": "not_tested" if key else "blocked", "reason": "offline_mode" if key else "missing_api_key"}
    if not key:
        # A single unauthenticated request checks reachability and the real auth boundary.
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                response = client.get(base + "/api/a-share/calendar/trading-days")
                summary = summarize_envelope(response.json())
                return {"status": "blocked", "reason": "missing_api_key",
                        "unauthenticated_probe": {"http_status": response.status_code, **summary}}
        except Exception as error:
            return {"status": "blocked", "reason": "missing_api_key", "reachability_error": type(error).__name__}
    symbols = config.get("PROBE_SYMBOLS", "600519.SH,000858.SZ").split(",")
    if not 1 <= len(symbols) <= 3 or any(not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol) for symbol in symbols):
        return {"status": "failed", "reason": "invalid_probe_symbols"}
    today = datetime.now(SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0)
    params_window = {"start": int((today - timedelta(days=120)).timestamp() * 1000),
                     "end": int(today.timestamp() * 1000), "interval": "1d", "adjust": "forward"}
    calls = [("resolve_security", "/api/meta/tickers/search", {"q": symbols[0]}),
             ("get_trading_calendar", "/api/a-share/calendar/trading-days", {}),
             ("get_price_snapshot", "/api/a-share/prices/snapshot", {"thscodes": ",".join(symbols)})]
    for symbol in symbols:
        calls.append(("get_daily_bars", "/api/a-share/prices/historical", {"thscode": symbol, **params_window}))
        calls.append(("get_financial_metrics", "/api/a-share/financials/income-statements",
                      {"thscode": symbol, "period": "quarterly", "limit": 4}))
    results = []
    with httpx.Client(timeout=timeout, follow_redirects=False, headers={"X-api-key": key}) as client:
        for name, path, params in calls:
            started = time.perf_counter()
            try:
                response = client.get(base + path, params=params)
                if response.status_code != 200:
                    summary = {"status": "failed", "http_status": response.status_code}
                else:
                    summary = {"http_status": response.status_code, **summarize_envelope(response.json())}
            except Exception as error:
                summary = {"status": "failed", "reason": type(error).__name__}
            results.append({"tool": name, "endpoint": path, "symbol": params.get("thscode"),
                            "elapsed_ms": round((time.perf_counter() - started) * 1000), **summary})
            if summary.get("business_code") == 2001:
                break
            time.sleep(0.25)
    passed = sum(item["status"] == "passed" for item in results)
    return {"status": "passed" if passed == len(calls) else "partial" if passed else "failed",
            "successful_calls": passed, "required_calls": len(calls), "tools": results}


def probe_docker() -> dict[str, Any]:
    docker = shutil.which("docker")
    if not docker:
        return {"status": "missing"}
    config_dir = ROOT / ".docker-probe"
    config_dir.mkdir(exist_ok=True)
    try:
        result = subprocess.run([docker, "--config", str(config_dir), "version", "--format",
                                 "{{.Client.Version}}|{{.Server.Version}}"],
                                capture_output=True, text=True, timeout=15)
        versions = result.stdout.strip().split("|")
        return {"status": "passed" if result.returncode == 0 else "blocked",
                "client_version": versions[0] if versions else None,
                "server_version": versions[1] if len(versions) > 1 and versions[1] else None,
                "reason": None if result.returncode == 0 else "docker_daemon_unavailable"}
    except Exception as error:
        return {"status": "blocked", "reason": type(error).__name__}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--report", type=Path, default=ROOT / "docs/verification/batch1.local.json")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    config = read_config(args.env_file)
    try:
        timeout = min(120, max(1, float(config.get("PROBE_TIMEOUT_SECONDS", "20"))))
    except ValueError:
        timeout = 20
    report = {"checked_at": datetime.now(SHANGHAI).isoformat(), "scope": "batch1_readiness",
              "environment": probe_environment(), "model": probe_model(config, args.offline, timeout),
              "fuyao": probe_fuyao(config, args.offline, timeout),
              "ifind": {"status": "not_tested" if config.get("IFIND_MCP_URL") else "not_configured",
                        "reason": "actual_transport_and_authorization_require_verification"},
              "docker": probe_docker(),
              "deployment": {"status": "not_verified", "strategy": "python_container_with_persistent_volume",
                             "provider_configured": bool(config.get("DEPLOYMENT_PROVIDER"))}}
    report["live_prerequisites_passed"] = report["model"]["status"] == "passed" and report["fuyao"]["status"] == "passed"
    secrets = [value for key, value in config.items() if value and any(word in key for word in ("KEY", "TOKEN", "SECRET"))]
    report = redact(report, secrets)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["live_prerequisites_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
