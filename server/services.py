from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from typing import Any

import jsonschema
from pydantic import ValidationError

from server.config import Settings
from server.evidence.normalize import normalize
from server.models import EvidenceContract
from server.storage.database import Database, encode, fingerprint, now, uid
from server.tools.calculations import CalculationError, financial_comparison, price_comparison
from server.tools.fuyao import FetchResult, FuyaoClient, redact
from server.tools.ifind import IfindClient
from server.tools.specs import SPECS, request_parameters
from server.tracing import traced


class ServiceError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message


def view(row: dict) -> dict:
    result = {}
    for key, value in row.items():
        if key == "session_id":
            continue
        result[key[:-5] if key.endswith("_json") else key] = json.loads(value) if key.endswith("_json") and value is not None else value
    return result


class ResearchService:
    def __init__(self, settings: Settings, db: Database | None = None, transport=None, ifind_client=None):
        self.settings = settings
        self.db = db or Database(settings.db_path, settings.database_url)
        self.fuyao = FuyaoClient(settings, transport)
        self.ifind = ifind_client or IfindClient(settings)
        from server.documents import Documents
        self.documents = Documents(self)

    def ensure_owner(self, owner: str) -> None:
        # NiceGUI callbacks retain an owner ID after the HTTP page request ends.
        # Revalidate it at the service boundary, including callbacks over a live socket.
        if not self.db.one('SELECT id FROM sessions WHERE id=? AND expires_at>?', (owner, int(time.time()))):
            raise ServiceError(401, '会话已过期，请刷新工作台重新连接')

    def resource(self, table: str, record_id: str, owner: str) -> dict:
        self.ensure_owner(owner)
        row = self.db.owned(table, record_id, owner)
        if not row:
            raise ServiceError(404, "资源不存在或不属于当前会话")
        return view(row)

    def threads(self, owner: str) -> list[dict]:
        self.ensure_owner(owner)
        return [view(row) for row in self.db.all("SELECT * FROM threads WHERE session_id=? AND NOT EXISTS (SELECT 1 FROM thread_deletions WHERE thread_id=threads.id) ORDER BY updated_at DESC LIMIT 100", (owner,))]

    def delete_thread(self, owner: str, thread_id: str) -> dict:
        """Remove a conversation from the product; retain private audit/checkpoint records."""
        self.ensure_owner(owner)
        with self.db.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            thread = connection.execute('SELECT id FROM threads WHERE id=? AND session_id=?', (thread_id, owner)).fetchone()
            if not thread:
                raise ServiceError(404, '对话不存在或不属于当前会话')
            if connection.execute('SELECT thread_id FROM thread_deletions WHERE thread_id=?', (thread_id,)).fetchone():
                return {'id': thread_id, 'deleted': True}
            timestamp = now()
            connection.execute('INSERT INTO thread_deletions VALUES(?,?,?)', (thread_id, owner, timestamp))
            runs = connection.execute('SELECT id,status FROM runs WHERE thread_id=? AND session_id=?', (thread_id, owner)).fetchall()
            for run in runs:
                if run['status'] not in {'completed', 'partial', 'blocked', 'stopped', 'failed'}:
                    connection.execute("UPDATE runs SET status='stopped',updated_at=? WHERE id=?", (timestamp, run['id']))
                connection.execute("UPDATE approvals SET status='expired' WHERE run_id=? AND status='pending'", (run['id'],))
                connection.execute("UPDATE run_inputs SET status='expired' WHERE run_id=? AND status='pending'", (run['id'],))
                self.db.event(connection, owner, run['id'], 'thread.deleted', {'thread_id': thread_id})
        return {'id': thread_id, 'deleted': True}

    def create_thread(self, owner: str, title: str, goal: str) -> dict:
        self.ensure_owner(owner)
        record_id, timestamp = uid(), now()
        with self.db.connect() as connection:
            connection.execute("INSERT INTO threads VALUES(?,?,?,?,?,?)", (record_id, owner, title, goal, timestamp, timestamp))
        return self.resource("threads", record_id, owner)

    def create_run(self, owner: str, thread_id: str, goal: str, mode: str = "manual") -> dict:
        self.resource("threads", thread_id, owner)
        record_id, timestamp = uid(), now()
        with self.db.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute('SELECT thread_id FROM thread_deletions WHERE thread_id=?', (thread_id,)).fetchone():
                raise ServiceError(404, '对话已删除')
            connection.execute("INSERT INTO runs(id,session_id,thread_id,goal,mode,status,created_at,updated_at) VALUES(?,?,?,?,?,'data_collection',?,?)",
                               (record_id, owner, thread_id, goal, mode, timestamp, timestamp))
            self.db.event(connection, owner, record_id, "run.created", {"mode": mode})
            connection.execute("UPDATE threads SET updated_at=? WHERE id=?", (timestamp, thread_id))
        return self.resource("runs", record_id, owner)

    def collection(self, owner: str, run_id: str, table: str) -> list[dict]:
        self.resource("runs", run_id, owner)
        if table not in {"tasks", "tool_calls", "evidence", "derived_metrics"}:
            raise ValueError("Unsupported collection")
        return [view(row) for row in self.db.all(f"SELECT * FROM {table} WHERE run_id=? AND session_id=? ORDER BY created_at LIMIT 500", (run_id, owner))]

    def events(self, owner: str, run_id: str, after: int = 0, limit: int = 100) -> list[dict]:
        self.resource("runs", run_id, owner)
        return [view(row) for row in self.db.all("SELECT * FROM run_events WHERE run_id=? AND session_id=? AND id>? ORDER BY id LIMIT ?",
                                                (run_id, owner, after, limit))]

    def mcp_specs(self) -> dict[str, dict]:
        rows = self.db.all("SELECT * FROM mcp_tools WHERE provider_fingerprint=?", (self.settings.provider_fingerprint("ifind"),))
        return {row["name"]: json.loads(row["spec_json"]) for row in rows}

    def tools(self) -> list[dict]:
        rows = {row["name"]: row for row in self.db.all("SELECT * FROM tool_capabilities")}
        tools = []
        for name, spec in SPECS.items():
            configured = spec.provider in {"local", "documents"} or bool(self.settings.fuyao_api_key)
            row = rows.get(name)
            verified = row and row["provider_fingerprint"] == self.settings.provider_fingerprint(spec.provider)
            tools.append({"name": name, "description": spec.description, "version": spec.version, "provider": spec.provider,
                          "permission": spec.permission, "read_only": True, "enabled": configured,
                          "status": "available" if spec.provider in {"local", "documents"} else row["status"] if verified else "configured_unverified" if configured else "not_configured",
                          "last_verified_at": row["checked_at"] if verified else None,
                          "last_data_quality": json.loads(row["details_json"]).get("quality") if verified else None,
                          "verified_scope": json.loads(row["details_json"]).get("scope") if verified else None,
                          "timeout_seconds": self.settings.timeout_seconds, "max_retries": spec.max_retries,
                          "cost_status": "not_charged" if spec.provider in {"local", "documents"} else "unknown",
                          "input_schema": spec.arguments_model.model_json_schema(),
                          "output_schema": EvidenceContract.model_json_schema() if spec.provider in {"fuyao", "documents"} else {"type": "object", "required": ["quality", "results", "method"]}})
        for name, spec in self.mcp_specs().items():
            enabled = (spec.get("annotations") or {}).get("readOnlyHint") is True and name[6:] in self.settings.ifind_allowlist
            tools.append({"name": name, "description": spec.get("description", "")[:500], "version": "mcp-1.0",
                          "provider": "ifind", "permission": "ifind.read", "read_only": (spec.get("annotations") or {}).get("readOnlyHint") is True,
                          "enabled": enabled, "status": "discovered_allowlisted" if enabled else "discovered_disabled",
                          "input_schema": spec.get("inputSchema", {}), "output_schema": spec.get("outputSchema", {"type": "object"}),
                          "cost_status": "unknown", "timeout_seconds": self.settings.timeout_seconds, "max_retries": 0})
        return tools

    async def discover_ifind(self) -> dict:
        if not self.settings.ifind_mcp_url:
            return {"status": "not_configured", "tools": []}
        try:
            specs = await self.ifind.discover()
            for spec in specs:
                if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", spec["name"]):
                    raise ValueError("invalid_tool_name")
                jsonschema.Draft202012Validator.check_schema(spec["inputSchema"])
            with self.db.connect() as connection:
                connection.execute("DELETE FROM mcp_tools")
                for spec in specs:
                    connection.execute("INSERT INTO mcp_tools VALUES(?,?,?,?)", (
                        "ifind." + spec["name"], self.settings.provider_fingerprint("ifind"), encode(spec), now()))
            return {"status": "discovered", "tools": [x for x in self.tools() if x["provider"] == "ifind"]}
        except Exception:
            return {"status": "failed", "reason": "mcp_discovery_failed", "tools": []}

    def arguments(self, name: str, arguments: dict) -> dict:
        if name in SPECS:
            try:
                return SPECS[name].arguments_model.model_validate(arguments).model_dump(mode="json")
            except ValidationError:
                raise ServiceError(422, "工具参数不符合已注册契约") from None
        spec = self.mcp_specs().get(name)
        if not spec:
            raise ServiceError(404, "工具未注册")
        if name[6:] not in self.settings.ifind_allowlist or (spec.get("annotations") or {}).get("readOnlyHint") is not True:
            raise ServiceError(403, "该 MCP 工具未通过只读白名单许可")
        try:
            jsonschema.validate(arguments, spec["inputSchema"])
            encode(arguments)
        except (jsonschema.ValidationError, ValueError):
            raise ServiceError(422, "MCP 参数不符合工具契约") from None
        return arguments

    def create_task(self, owner: str, run_id: str, title: str, tool: str, arguments: dict, depends_on: list[str]) -> dict:
        self.resource("runs", run_id, owner)
        arguments = self.arguments(tool, arguments)
        for dependency in depends_on:
            task = self.resource("tasks", dependency, owner)
            if task["run_id"] != run_id:
                raise ServiceError(422, "任务依赖必须属于同一研究运行")
        record_id, timestamp = uid(), now()
        with self.db.connect() as connection:
            connection.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?)", (
                record_id, owner, run_id, title, tool, encode(arguments), encode(depends_on), "pending", timestamp, timestamp))
            self.db.event(connection, owner, run_id, "task.created", {"task_id": record_id, "tool": tool})
        return self.resource("tasks", record_id, owner)

    @traced('tool')
    async def execute(self, owner: str, run_id: str, name: str, arguments: dict,
                      idempotency_key: str | None = None, task_id: str | None = None, lease_owner: str | None = None) -> dict:
        run_record = self.resource("runs", run_id, owner)
        if run_record["mode"] == "agent" and lease_owner is None:
            raise ServiceError(409, "自动研究由后台调度器派发，不能手动绕过计划和审批")
        arguments = self.arguments(name, arguments)
        spec = SPECS.get(name)
        provider, version, retries = (spec.provider, spec.version, spec.max_retries) if spec else ("ifind", "mcp-1.0", 0)
        signature = fingerprint({"tool": name, "version": version, "arguments": arguments,
                                 "task_id": task_id,
                                 "provider_context": self.settings.provider_fingerprint(provider) if provider != "local" else "local"})
        key = idempotency_key or signature
        if task_id:
            task = self.resource("tasks", task_id, owner)
            if task["run_id"] != run_id or task["tool"] != name or task["arguments"] != arguments:
                raise ServiceError(422, "任务和本次操作不匹配")
            for dependency in task["depends_on"]:
                if self.resource("tasks", dependency, owner)["status"] != "completed":
                    raise ServiceError(409, "前置任务尚未成功完成")
        call_id, started, previous_attempts = uid(), time.perf_counter(), 0
        summary: dict[str, Any] = {"quality": "failed", "issues": ["not_executed"], "evidence_ids": [], "derived_metric_ids": []}
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM tool_calls WHERE run_id=? AND idempotency_key=?", (run_id, key)).fetchone()
            if existing:
                if existing["fingerprint"] != signature:
                    raise ServiceError(409, "相同幂等键绑定了不同参数")
                if existing["status"] == "interrupted" and lease_owner:
                    call_id, previous_attempts = existing["id"], existing["attempts"]
                    summary = json.loads(existing["result_json"]) if existing["result_json"] else summary
                    connection.execute("UPDATE tool_calls SET status='running' WHERE id=?", (call_id,))
                elif existing["status"] == "running":
                    raise ServiceError(409, "该调用正在执行或需要恢复，请勿重复派发")
                else:
                    return {**view(dict(existing)), "reused": True}
            if not task_id:
                task_id, timestamp = uid(), now()
                connection.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?)", (
                    task_id, owner, run_id, spec.description if spec else name, name, encode(arguments), "[]", "pending", timestamp, timestamp))
                self.db.event(connection, owner, run_id, "task.created", {"task_id": task_id, "tool": name})
            if not existing:
                connection.execute("INSERT INTO tool_calls(id,session_id,run_id,task_id,tool,version,parameters_json,fingerprint,idempotency_key,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,'running',?)",
                                   (call_id, owner, run_id, task_id, name, version, encode(arguments), signature, key, now()))
            self.db.event(connection, owner, run_id, "tool.started", {"call_id": call_id, "task_id": task_id, "tool": name})
            connection.execute("UPDATE tasks SET status='running',updated_at=? WHERE id=?", (now(), task_id))
        interrupted = False
        for attempt in range(previous_attempts + 1, retries + 2):
            attempt_started, attempt_id = time.perf_counter(), uid()
            with self.db.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                run = connection.execute("SELECT * FROM runs WHERE id=? AND session_id=?", (run_id, owner)).fetchone()
                config = json.loads(run["config_json"])
                limit = config.get("budget", {}).get("tool_attempts", self.settings.max_tool_attempts)
                runtime = connection.execute("SELECT * FROM run_runtime WHERE run_id=?", (run_id,)).fetchone() if run["mode"] == "agent" else None
                permission = not runtime or (lease_owner == runtime["lease_owner"] and runtime["lease_until"] > time.time()
                                              and name in config.get("allowed_tools", [])
                                              and any(t["name"] == name and t["enabled"] and t["read_only"] for t in self.tools()))
                if (run["status"] in {"stopped", "paused", "awaiting_approval"} or run["attempts_used"] >= limit
                    or not permission or (runtime and runtime["deadline"] <= time.time())):
                    interrupted = run["status"] == "paused" and bool(runtime)
                    summary.update(quality="failed", issues=["run_control_or_attempt_budget_limit"])
                    break
                connection.execute("UPDATE runs SET attempts_used=attempts_used+1,updated_at=? WHERE id=?", (now(), run_id))
                connection.execute("UPDATE tool_calls SET attempts=? WHERE id=?", (attempt, call_id))
                connection.execute("INSERT INTO tool_attempts(id,call_id,attempt,started_at,cost_status) VALUES(?,?,?,?,?)", (
                    attempt_id, call_id, attempt, now(), "not_charged" if provider in {"local", "documents"} else "unknown"))
            result, content, refs = FetchResult(), None, []
            failure_quality, failure_details = "failed", None
            fetched_at = now()
            try:
                if provider == "fuyao":
                    path, params = request_parameters(name, arguments)
                    timeout = min(self.settings.timeout_seconds, max(0.01, runtime["deadline"] - time.time())) if runtime else self.settings.timeout_seconds
                    result = await asyncio.wait_for(self.fuyao.fetch(path, params), timeout)
                    fetched_at = now()
                    content = normalize(name, path, params, fetched_at, result).model_dump(mode="json")
                elif provider == 'documents':
                    excerpts = self.documents.search(owner, **arguments)
                    result.body = {'excerpts':excerpts}
                    content = EvidenceContract(provider='documents', tool=name, endpoint='local:documents', parameters=arguments,
                        request_id=None, fetched_at=fetched_at, as_of=None, quality='available' if excerpts else 'missing',
                        records=excerpts, field_map={'text':'$.excerpts[*].text'}, units={'text':'原文'},
                        method={'retrieval':'keyword_bigrams','authenticity':'user_supplied_unverified','numeric_extraction':'none'},
                        issues=[] if excerpts else ['document_search_no_match']).model_dump(mode='json')
                elif provider == "ifind":
                    timeout = min(self.settings.timeout_seconds, max(0.01, runtime["deadline"] - time.time())) if runtime else self.settings.timeout_seconds
                    body = await asyncio.wait_for(self.ifind.call(name[6:], arguments), timeout)
                    if len(encode(body)) > 4 * 1024 * 1024:
                        raise ValueError("mcp_result_limit")
                    result = FetchResult(body=body, error="mcp_tool_error" if body.get("isError") else None)
                    content = EvidenceContract(provider="ifind", tool=name, endpoint=self.settings.ifind_mcp_url,
                        parameters=arguments, request_id=None, fetched_at=now(), as_of=None,
                        quality="failed" if result.error else "unverified", records=[] if result.error else [{"content": body, "source_index": 0}],
                        field_map={}, units={}, method={"field_mapping": "pending"},
                        issues=[result.error or "ifind_time_unit_field_mapping_pending"]).model_dump(mode="json")
                else:
                    inputs = [self.resource("evidence", record_id, owner) for record_id in arguments["evidence_ids"]]
                    if name == "calculate_price_comparison":
                        calendar = self.resource("evidence", arguments["calendar_evidence_id"], owner)
                        content, refs = price_comparison(inputs, calendar, arguments["trading_days"])
                    else:
                        content, refs = financial_comparison(inputs, arguments["fields"], arguments["period_end_ms"], arguments["known_at_ms"])
                    result.body = content
            except ServiceError as error:
                result = FetchResult(error="evidence_not_accessible")
            except CalculationError as error:
                result = FetchResult(error="incompatible_or_incomplete_evidence")
                failure_quality, failure_details = error.quality, str(error)
            except asyncio.TimeoutError:
                result = FetchResult(error="tool_timeout", retryable=True)
            except Exception:
                result.error = "execution_failed"
                result.retryable = False
            elapsed = round((time.perf_counter() - attempt_started) * 1000)
            safe_body = redact(result.body if result.body is not None else {"error": result.error}, self.settings.secrets)
            raw_id = uid()
            with self.db.connect() as connection:
                connection.execute("UPDATE tool_attempts SET finished_at=?,elapsed_ms=?,http_status=?,business_code=?,request_id=?,error=? WHERE id=?",
                                   (now(), elapsed, result.http_status, result.business_code, result.request_id, result.error, attempt_id))
                connection.execute("INSERT INTO raw_records VALUES(?,?,?,?,?,?,?,?)", (
                    raw_id, owner, call_id, attempt, provider, encode(safe_body), fingerprint(safe_body), fetched_at))
                summary = {"quality": content["quality"] if content else failure_quality, "issues": content.get("issues", []) if content else [failure_details or result.error],
                           "retryable": result.retryable,
                           "provider": provider, "http_status": result.http_status, "business_code": result.business_code,
                           "request_id": result.request_id, "evidence_ids": [], "derived_metric_ids": []}
                if content and provider in {"fuyao", "ifind", "documents"}:
                    evidence_id = uid()
                    connection.execute("INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?)", (
                        evidence_id, owner, run_id, call_id, raw_id, encode(content), fingerprint(content), now()))
                    summary.update(evidence_ids=[evidence_id], record_count=len(content["records"]))
                    self.db.event(connection, owner, run_id, "evidence.saved", {"evidence_id": evidence_id, "quality": content["quality"]})
                if content and provider == "local":
                    metric_id = uid()
                    connection.execute("INSERT INTO derived_metrics VALUES(?,?,?,?,?,?,?,?)", (
                        metric_id, owner, run_id, call_id, encode(content), encode(refs), fingerprint(content), now()))
                    summary["derived_metric_ids"] = [metric_id]
                    self.db.event(connection, owner, run_id, "metric.saved", {"metric_id": metric_id, "quality": content["quality"]})
                if provider not in {"local", "documents"}:
                    state = "verified" if not result.error and content and content["quality"] in {"available", "unverified"} else summary["quality"]
                    connection.execute("INSERT OR REPLACE INTO tool_capabilities VALUES(?,?,?,?,?,?)", (
                        name, provider, self.settings.provider_fingerprint(provider), state,
                        encode({"http_status": result.http_status, "business_code": result.business_code, "quality": summary["quality"],
                                "scope": {"endpoint": content.get("endpoint") if content else None,
                                          **{k: arguments[k] for k in ("statement", "period", "interval", "adjust") if k in arguments}}}), now()))
                # Commit result and evidence together before the next checkpoint. Recovery can
                # finalize this call without repeating a completed remote request.
                connection.execute("UPDATE tool_calls SET result_json=?,quality=? WHERE id=?", (encode(summary), summary["quality"], call_id))
            if not result.retryable or attempt > retries:
                break
            await asyncio.sleep(min(2, 0.5 * (2 ** (attempt - 1))))
        status = "interrupted" if interrupted else "completed" if summary["quality"] in {"available", "unverified"} else "failed"
        with self.db.connect() as connection:
            connection.execute("UPDATE tool_calls SET status=?,quality=?,elapsed_ms=?,result_json=?,finished_at=? WHERE id=?", (
                status, summary["quality"], round((time.perf_counter() - started) * 1000), encode(summary), now(), call_id))
            connection.execute("UPDATE tasks SET status=?,updated_at=? WHERE id=?", ("pending" if interrupted else status, now(), task_id))
            self.db.event(connection, owner, run_id, "tool.finished", {"call_id": call_id, "task_id": task_id, "status": status, "quality": summary["quality"]})
        return {**self.resource("tool_calls", call_id, owner), "reused": False}
