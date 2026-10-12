from __future__ import annotations

import asyncio
import json
import math
import time
import traceback
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import TypedDict
from pydantic import ValidationError
from uuid import NAMESPACE_URL, uuid5

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from server.harness.model import JsonModel, ModelFailure
from server.harness.dialogue import (Dialogue, Entry, Review, AnalysisDraft, ENTRY_RULES, REVIEW_RULES,
    ANALYSIS_RULES, facts_for_model, compact_facts, validate_entry, validate_review, validate_analysis)
from server.harness.planning import PLANNER_RULES, Plan, materialize, validate_plan
from server.harness.reporting import build_report, safe_text, save_report
from server.models import ApprovalDecision, PlanUpdate, RunBudget
from server.services import ServiceError, view
from server.storage.database import encode, fingerprint, now, uid
from server.dispatch import Dispatch
from server.tracing import traced

TERMINAL = {"completed", "partial", "blocked", "stopped", "failed"}
SHANGHAI = timezone(timedelta(hours=8))


class State(TypedDict, total=False):
    run_id: str
    owner: str
    plan_version: int
    route: str
    task: dict
    batch: list[dict]


class Harness:
    lease_seconds = 15
    poll_seconds = 0.2

    def __init__(self, service, model=None):
        self.service, self.db, self.settings = service, service.db, service.settings
        self.model = model or JsonModel(self.settings)
        self.service.dialogue = Dialogue(service)
        self.instance = uid()
        self.scheduler = None
        self.active = {}
        self.graph = None
        self.saver_context = None
        self.lifecycle_users = 0
        self.dispatch = Dispatch(self.db, self.settings.redis_url, self.instance)

    async def start(self):
        self.lifecycle_users += 1
        if not self.settings.embedded_worker:
            return
        if self.scheduler:
            return
        if self.settings.database_url:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            self.saver_context = AsyncPostgresSaver.from_conn_string(self.settings.database_url)
        else:
            self.saver_context = AsyncSqliteSaver.from_conn_string(str(self.settings.data_dir / "checkpoints.sqlite3"))
        saver = await self.saver_context.__aenter__()
        if not self.settings.database_url:
            await saver.conn.execute("PRAGMA journal_mode=WAL")
            await saver.conn.execute("PRAGMA busy_timeout=10000")
        await saver.setup()
        graph = StateGraph(State)
        graph.add_node("plan", self.plan)
        graph.add_node("gate", self.gate)
        graph.add_node("execute", self.execute)
        graph.add_node("report", self.report)
        graph.add_node("review", self.review)
        graph.add_edge(START, "plan")
        graph.add_edge("plan", "gate")
        graph.add_conditional_edges("gate", lambda s: s["route"], {"execute": "execute", "review": "review", "report": "report", "end": END})
        graph.add_conditional_edges("review", lambda s: s["route"], {"gate": "gate", "report": "report", "end": END})
        graph.add_edge("execute", "gate")
        graph.add_edge("report", END)
        self.graph = graph.compile(checkpointer=saver, name="xbuddy_research")
        self.scheduler = asyncio.create_task(self.schedule(), name="xbuddy-scheduler")

    async def close(self):
        self.lifecycle_users = max(0, self.lifecycle_users - 1)
        if self.lifecycle_users:
            return
        if not self.scheduler:
            return
        self.scheduler.cancel()
        with suppress(asyncio.CancelledError):
            await self.scheduler
        self.scheduler = None
        for task in list(self.active.values()):
            task.cancel()
        await asyncio.gather(*self.active.values(), return_exceptions=True)
        self.active.clear()
        with self.db.connect() as conn:
            conn.execute("UPDATE run_runtime SET lease_owner=NULL,lease_until=0 WHERE lease_owner=?", (self.instance,))
        await self.saver_context.__aexit__(None, None, None)
        await self.dispatch.close()
        self.graph = None

    def submit(self, owner: str, thread_id: str, goal: str, budget: RunBudget, *, followup=None) -> dict:
        if not self.model.configured:
            raise ServiceError(503, "请在本地 .env 配置可用的模型服务后启动自动研究")
        allowed = [x["name"] for x in self.service.tools() if x["enabled"] and x["read_only"]]
        run = self.service.create_run(owner, thread_id, goal, "agent")
        with self.db.connect() as conn:
            conn.execute("UPDATE runs SET schema_version=4,config_json=? WHERE id=?",
                         (encode({"budget": budget.model_dump(), "allowed_tools": allowed, "max_replans": 1, "agent_protocol": 1,
                                  "financial_history_version": 1, "response_policy_version": 1, "narrative_version": 1}), run["id"]))
            conn.execute("INSERT INTO run_runtime(run_id,started_at,deadline) VALUES(?,?,?)",
                         (run["id"], now(), time.time() + budget.duration_seconds))
            self.service.dialogue.add_message(conn, run, 'user', 'user_goal', goal, owner=owner)
            if followup:
                conn.execute('INSERT INTO run_links VALUES(?,?,?,?,?,?)', (run['id'], owner, followup['parent_run_id'], followup['parent_artifact_id'], encode(followup), now()))
                self.db.event(conn, owner, run['id'], 'run.followup', {'parent_run_id': followup['parent_run_id'], 'parent_artifact_id': followup['parent_artifact_id'], 'fresh_data': True})
        self.service.memories.select(owner, run['id'])
        with self.db.connect() as conn:
            conn.execute("UPDATE runs SET status='queued' WHERE id=?", (run['id'],))
            self.db.event(conn, owner, run["id"], "run.queued", {"budget": budget.model_dump(), "cost_status": "unknown"})
        return self.service.resource("runs", run["id"], owner)

    def runtime(self, run_id):
        row = self.db.one("SELECT * FROM run_runtime WHERE run_id=?", (run_id,))
        return view(row) if row else None

    def snapshot(self, owner, run_id):
        from server.publication import agent_view, artifact_view
        run = self.service.resource("runs", run_id, owner)
        runtime = self.runtime(run_id)
        if runtime:
            run["runtime"] = {k: runtime[k] for k in ("heartbeat_at", "started_at", "deadline", "model_calls", "model_tokens", "plan", "replan_count", "last_error", "elapsed_ms")}
        run["approvals"] = [view(x) for x in self.db.all("SELECT * FROM approvals WHERE run_id=? AND session_id=? ORDER BY created_at", (run_id, owner))]
        run["artifacts"] = [artifact_view(view(x)) for x in self.db.all("SELECT * FROM artifacts WHERE run_id=? AND session_id=? ORDER BY version", (run_id, owner))]
        run['memory_usage'] = self.service.memories.usages(owner, run_id)
        run['context_snapshots'] = self.service.contexts.snapshots(owner, run_id)
        run['parent'] = self.db.one('SELECT parent_run_id,parent_artifact_id FROM run_links WHERE run_id=? AND session_id=?', (run_id, owner))
        run['pending_input'] = self.service.dialogue.pending(owner, run_id)
        run['agent'] = agent_view(self.service.dialogue.state(run_id))
        return run

    def control(self, owner, run_id, action):
        self.service.ensure_owner(owner)
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM runs WHERE id=? AND session_id=?", (run_id, owner)).fetchone()
            if not row:
                raise ServiceError(404, "研究不存在")
            if row["mode"] != "agent":
                raise ServiceError(409, "此控制只适用于自动研究")
            status = row["status"]
            if status in TERMINAL:
                if action == "stop" and status == "stopped":
                    return self.service.resource("runs", run_id, owner)
                raise ServiceError(409, "研究已结束")
            if action == "pause":
                if status not in {"queued", "running", "paused"}:
                    raise ServiceError(409, "请先处理当前审批")
                target = "paused"
            elif action == "resume":
                if status not in {"paused", "recoverable_failed"}:
                    raise ServiceError(409, "当前运行不能继续")
                runtime = conn.execute("SELECT deadline FROM run_runtime WHERE run_id=?", (run_id,)).fetchone()
                if runtime["deadline"] <= time.time():
                    raise ServiceError(409, "运行时限已到，请新建研究")
                target = "queued"
            elif action == "stop":
                target = "stopped"
                conn.execute("UPDATE approvals SET status='expired' WHERE run_id=? AND status='pending'", (run_id,))
                conn.execute("UPDATE run_inputs SET status='expired' WHERE run_id=? AND status='pending'", (run_id,))
            else:
                raise ServiceError(422, "不支持的运行控制")
            conn.execute("UPDATE runs SET status=?,updated_at=? WHERE id=?", (target, now(), run_id))
            self.db.event(conn, owner, run_id, "run." + action, {"in_flight": "already_dispatched_requests_may_finish", "new_dispatch": "disabled" if action != "resume" else "enabled"})
        return self.service.resource("runs", run_id, owner)

    def decide(self, owner, approval_id, payload: ApprovalDecision):
        self.service.ensure_owner(owner)
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM approvals WHERE id=? AND session_id=?", (approval_id, owner)).fetchone()
            if not row:
                raise ServiceError(404, "审批不存在")
            content = json.loads(row["content_json"])
            run = conn.execute("SELECT * FROM runs WHERE id=?", (row["run_id"],)).fetchone()
            runtime = conn.execute("SELECT * FROM run_runtime WHERE run_id=?", (row["run_id"],)).fetchone()
            params = content["parameters"]
            if (row["status"] != "pending" or run["status"] != "awaiting_approval" or runtime["deadline"] <= time.time()
                or payload.plan_version != run["plan_version"] or payload.plan_version != content["plan_version"]
                or payload.parameters_fingerprint != content["parameters_fingerprint"] or fingerprint(params) != payload.parameters_fingerprint):
                raise ServiceError(409, "审批已失效，或与具体计划/参数不匹配")
            if payload.decision != "modify" and payload.budget is not None:
                raise ServiceError(422, "只有修改审批可以设置预算")
            config = json.loads(run["config_json"])
            budget = params["proposed_budget"] if payload.decision == "approve" else payload.budget.model_dump() if payload.budget else None
            if payload.decision == "modify" and budget is None:
                raise ServiceError(422, "修改审批需要提供完整预算")
            if budget:
                budget = RunBudget.model_validate(budget).model_dump()
                old = RunBudget(**config["budget"]).model_dump()
                proposed = RunBudget(**params['proposed_budget']).model_dump()
                if (any(budget[k] > proposed[k] for k in budget)
                    or budget["tool_attempts"] < run["attempts_used"] or budget["model_calls"] < runtime["model_calls"]
                    or budget["model_tokens"] < runtime["model_tokens"] or budget["duration_seconds"] != old["duration_seconds"]):
                    raise ServiceError(422, "修改预算超出审批范围、低于已消耗量或改变运行时限")
                config["budget"] = budget
            denied = payload.decision == "reject" or any(config["budget"][k] < v for k, v in params["minimum_budget"].items())
            if denied:
                config["budget_denied"] = True
            result_status = "rejected" if payload.decision == "reject" else "approved"
            content.update(status=result_status, decision=payload.decision, decided_at=now(), accepted_budget=budget)
            conn.execute("UPDATE approvals SET status=?,content_json=? WHERE id=?", (result_status, encode(content), approval_id))
            conn.execute("UPDATE runs SET status='queued',config_json=?,updated_at=? WHERE id=?", (encode(config), now(), row["run_id"]))
            self.db.event(conn, owner, row["run_id"], "approval.decided", {"approval_id": approval_id, "decision": payload.decision, "plan_version": payload.plan_version, "budget_denied": denied})
        return self.service.resource("approvals", approval_id, owner)

    def physical_ids(self, run_id, plan):
        return {task["id"]: str(uuid5(NAMESPACE_URL, run_id + ":" + task["id"])) for task in plan["tasks"]}

    def edit_plan(self, owner: str, run_id: str, payload: PlanUpdate):
        self.service.ensure_owner(owner)
        # Keep the transaction open through validation so pause/resume/approval cannot race it.
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute("SELECT * FROM runs WHERE id=? AND session_id=?", (run_id, owner)).fetchone()
            if not run:
                raise ServiceError(404, "研究不存在")
            runtime = conn.execute("SELECT * FROM run_runtime WHERE run_id=?", (run_id,)).fetchone()
            if (run["mode"] != "agent" or run["status"] not in {"paused", "awaiting_approval"} or not runtime
                or runtime["lease_until"] > time.time() or runtime["deadline"] <= time.time()):
                raise ServiceError(409, "请先暂停并等待当前操作结束，再修改计划")
            old = json.loads(runtime["plan_json"] or "{}")
            if not old.get("tasks"):
                raise ServiceError(409, "规划尚未完成")
            if payload.plan_version != run["plan_version"] or payload.plan_fingerprint != fingerprint(old):
                raise ServiceError(409, "计划已经变化，请重新打开编辑器")
            config = json.loads(run["config_json"])
            try:
                plan = validate_plan(self.service, payload.plan, config["allowed_tools"])
                plan.update({k:v for k,v in old.items() if k not in plan})
                if any(not safe_text(t["title"]) for t in plan["tasks"]) or any(not safe_text(x) for x in plan["limitations"] + plan["unresolved_goals"]):
                    raise ValueError("prohibited_expression")
            except (ValueError, ServiceError):
                raise ServiceError(422, "计划中的工具、参数、依赖、引用或表达未通过验证") from None
            old_ids, ids = self.physical_ids(run_id, old), self.physical_ids(run_id, plan)
            incoming = {t["id"]: t for t in plan["tasks"]}
            locked = {row["task_id"] for row in conn.execute("SELECT DISTINCT task_id FROM tool_calls WHERE run_id=?", (run_id,))}
            for task in old["tasks"]:
                if old_ids[task["id"]] in locked and incoming.get(task["id"]) != task:
                    raise ServiceError(409, "已经派发过的任务不能修改或移除，结果和证据必须保留")
            for logical_id, task_id in old_ids.items():
                if logical_id not in ids:
                    conn.execute("DELETE FROM tasks WHERE id=? AND run_id=?", (task_id, run_id))
            for task in plan["tasks"]:
                record_id = ids[task["id"]]
                if record_id in locked:
                    continue
                conn.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,tool=excluded.tool,arguments_json=excluded.arguments_json,depends_on_json=excluded.depends_on_json,status='pending',updated_at=excluded.updated_at",
                    (record_id, owner, run_id, task["title"], task["tool"], encode(task["arguments"]),
                     encode([ids[x] for x in task["depends_on"]]), "pending", now(), now()))
            conn.execute("UPDATE approvals SET status='expired' WHERE run_id=? AND status='pending'", (run_id,))
            conn.execute("UPDATE runs SET status='paused',plan_version=plan_version+1,updated_at=? WHERE id=?", (now(), run_id))
            conn.execute("UPDATE run_runtime SET plan_json=? WHERE run_id=?", (encode(plan), run_id))
            metadata = self.service.dialogue.state(run_id) | {'entry':plan,'review':None,'review_error':None,'review_facts_hash':None,
                'recovery_analysis':None,'recovery_facts_hash':None,'recovery_error':None}
            conn.execute('UPDATE agent_state SET content_json=?,content_sha256=? WHERE run_id=?',
                         (encode(metadata),fingerprint(metadata),run_id))
            self.db.event(conn, owner, run_id, "plan.edited", {"plan_version": run["plan_version"]+1, "plan_hash": fingerprint(plan),
                "task_count": len(plan["tasks"]), "completed_work_preserved": True, "pending_approvals_expired": True})
        return self.snapshot(owner, run_id)

    def persist_plan(self, state, plan, replan=False):
        run_id, owner = state["run_id"], state["owner"]
        ids = self.physical_ids(run_id, plan)
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            for task in plan["tasks"]:
                cursor = conn.execute("INSERT OR IGNORE INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (ids[task["id"]], owner, run_id, task["title"], task["tool"], encode(task["arguments"]),
                     encode([ids[x] for x in task["depends_on"]]), "pending", now(), now()))
                if cursor.rowcount:
                    self.db.event(conn, owner, run_id, "task.created", {"task_id": ids[task["id"]], "logical_id": task["id"], "tool": task["tool"]})
            conn.execute("UPDATE run_runtime SET plan_json=?,replan_count=replan_count+? WHERE run_id=?", (encode(plan), int(replan), run_id))
            if replan:
                conn.execute("UPDATE runs SET plan_version=plan_version+1 WHERE id=?", (run_id,))
                conn.execute("UPDATE approvals SET status='expired' WHERE run_id=? AND status='pending'", (run_id,))
            self.db.event(conn, owner, run_id, "plan.replanned" if replan else "plan.validated", {"plan_version": run["plan_version"] + int(replan), "task_count": len(plan["tasks"]), "plan_hash": fingerprint(plan)})

    def alive(self, state):
        run = self.service.resource("runs", state["run_id"], state["owner"])
        runtime = self.runtime(state["run_id"])
        if not runtime:
            return False
        lease = self.db.one("SELECT lease_owner,lease_until FROM run_runtime WHERE run_id=?", (state["run_id"],))
        return run["status"] not in TERMINAL and lease["lease_owner"] == self.instance and lease["lease_until"] > time.time()

    def controlled(self, state):
        if self.db.one('SELECT d.thread_id FROM thread_deletions d JOIN runs r ON r.thread_id=d.thread_id WHERE r.id=?', (state['run_id'],)):
            return False
        run = self.service.resource("runs", state["run_id"], state["owner"])
        if run["status"] == "paused":
            interrupt({"kind": "paused", "run_id": run["id"]})
            run = self.service.resource("runs", state["run_id"], state["owner"])
        return self.alive(state) and run["status"] not in {"paused", "awaiting_approval", "awaiting_input"}

    def ensure_budget(self, state, operation, minimum, recommendation=None):
        if not self.controlled(state):
            return False
        run = self.service.resource("runs", state["run_id"], state["owner"])
        runtime = self.runtime(run["id"])
        if runtime["deadline"] <= time.time():
            return False
        config = run["config"]
        budget = RunBudget(**config["budget"]).model_dump()
        if all(budget[k] >= v for k, v in minimum.items()):
            return True
        if config.get("budget_denied"):
            return False
        proposed = dict(budget)
        for k, value in minimum.items():
            proposed[k] = max(proposed[k], value)
        for k, value in (recommendation or {}).items():
            proposed[k] = max(proposed[k], value)
        try:
            RunBudget.model_validate(proposed)
        except ValueError:
            return False
        parameters = {"operation": operation, "plan_hash": fingerprint(runtime["plan"] or {}),
                      "minimum_budget": minimum, "proposed_budget": proposed}
        digest = fingerprint(parameters)
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            fresh = conn.execute("SELECT status FROM runs WHERE id=?", (run["id"],)).fetchone()
            if fresh["status"] not in {"running", "queued", "awaiting_approval"}:
                return False
            existing = conn.execute("SELECT * FROM approvals WHERE run_id=? AND status='pending'", (run["id"],)).fetchone()
            if existing:
                approval_id = existing["id"]
            else:
                approval_id = uid()
                content = {"plan_version": run["plan_version"], "action": "expand_budget", "parameters": parameters,
                           "parameters_fingerprint": digest, "reason": "下一操作需要扩大已授权预算；供应商实际费用未知", "status": "pending"}
                conn.execute("INSERT INTO approvals VALUES(?,?,?,?,?,?)", (approval_id, state["owner"], run["id"], "pending", encode(content), now()))
                self.db.event(conn, state["owner"], run["id"], "approval.requested", {"approval_id": approval_id, "plan_version": run["plan_version"], "parameters_fingerprint": digest})
            conn.execute("UPDATE runs SET status='awaiting_approval',updated_at=? WHERE id=?", (now(), run["id"]))
        while True:
            interrupt({"kind": "approval", "approval_id": approval_id, "parameters_fingerprint": digest})
            # An earlier interrupt's resume value cannot consume a newly created approval.
            fresh = self.service.resource("runs", run["id"], state["owner"])
            if fresh['status'] != 'awaiting_approval':
                break
        # Resume values are never authority. Only the persisted, validated decision is.
        return self.alive(state) and fresh["status"] != "awaiting_approval" and all(fresh["config"]["budget"][k] >= v for k, v in minimum.items())

    @traced('model')
    async def model_request(self, state, phase, context, rules, validator):
        run = self.service.resource("runs", state["run_id"], state["owner"])
        existing = self.db.one("SELECT * FROM model_calls WHERE run_id=? AND phase=?", (run["id"], phase))
        if existing:
            if existing["status"] == "completed":
                return validator(json.loads(existing["response_json"]))
            errors = self.service.dialogue.state(run['id']).get('phase_errors', {})
            raise ModelFailure(errors.get(phase, "model_phase_previously_failed_or_unconfirmed"))
        prepared = self.service.contexts.prepare(state['owner'], run['id'], phase, context, rules)
        needed = prepared['after_tokens']
        current_budget = RunBudget(**run['config']['budget'])
        if needed > current_budget.context_tokens:
            if not self.ensure_budget(state, {'phase': phase, 'context_snapshot_id': prepared['snapshot_id'], 'reason': 'protected_context_exceeds_budget'},
                {'context_tokens': needed}, {'context_tokens': min(100000, math.ceil((needed+2048)/1000)*1000)}):
                raise ModelFailure('context_budget_limit')
        messages = prepared['messages']
        output_limit = self.model.output_token_limit(messages) if hasattr(self.model, 'output_token_limit') else 6000
        estimated = math.ceil(len(encode(messages)) / 2) + output_limit
        runtime = self.runtime(run["id"])
        if not self.ensure_budget(state, {"phase": phase, "request_hash": fingerprint(messages)},
                                  {"model_calls": runtime["model_calls"] + 1, "model_tokens": runtime["model_tokens"] + estimated},
                                  {'model_calls':runtime['model_calls']+2} if phase == 'plan' else None):
            raise ModelFailure("model_budget_or_control_limit")
        started = time.perf_counter()
        record_id = uid()
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            live = conn.execute("SELECT status FROM runs WHERE id=?", (run["id"],)).fetchone()
            lease = conn.execute("SELECT * FROM run_runtime WHERE run_id=?", (run["id"],)).fetchone()
            if live["status"] not in {"queued", "running"} or lease["lease_owner"] != self.instance or lease["lease_until"] <= time.time() or lease["deadline"] <= time.time():
                raise ModelFailure("model_dispatch_control_limit")
            self.service.memories.mark_used(conn, state['owner'], run['id'], prepared['memories'])
            conn.execute("UPDATE run_runtime SET model_calls=model_calls+1,model_tokens=model_tokens+? WHERE run_id=?", (estimated, run["id"]))
            conn.execute("INSERT INTO model_calls(id,session_id,run_id,phase,status,request_hash,started_at) VALUES(?,?,?,?,'running',?,?)", (record_id, state["owner"], run["id"], phase, fingerprint(messages), now()))
            self.db.event(conn, state["owner"], run["id"], "model.started", {"phase": phase, 'context_snapshot_id': prepared['snapshot_id'], "estimated_token_reservation": estimated, "cost_status": "unknown"})
        response, usage, error = None, {"token_status": "estimated", "total_tokens": estimated, "cost_status": "unknown"}, None
        try:
            timeout = min(self.settings.model_timeout_seconds, max(0.01, self.runtime(run["id"])["deadline"] - time.time()))
            response, reported = await asyncio.wait_for(self.model.complete(messages), timeout)
            usage.update(reported)
            result = validator(response)
        except asyncio.TimeoutError:
            result, error = None, 'model_timeout'
        except ModelFailure as exc:
            usage.update(exc.usage)
            import re
            result, error = None, str(exc)
            if not re.fullmatch(r'[a-z][a-z0-9_]{0,80}',error):
                error = 'model_request_or_json_failed'
        except ValidationError as exc:
            kinds = {e['type'] for e in exc.errors(include_input=False)}
            code = 'schema_extra_field' if 'extra_forbidden' in kinds else 'schema_required_field' if 'missing' in kinds else 'schema_invalid'
            result, error = None, 'model_output_validation_failed:' + code
        except (ValueError, ServiceError) as exc:
            import re
            code = str(exc)
            result, error = None, 'model_output_validation_failed'
            if re.fullmatch(r'[a-z_]{1,80}', code):
                error += ':' + code
        except Exception:
            result, error = None, 'model_processing_failed'
        elapsed = round((time.perf_counter() - started) * 1000)
        with self.db.connect() as conn:
            total = usage.get("total_tokens", estimated)
            if type(total) is not int or total < 0:
                total = estimated
            conn.execute("UPDATE run_runtime SET model_tokens=model_tokens+? WHERE run_id=?", (total-estimated, run["id"]))
            conn.execute("UPDATE model_calls SET status=?,response_json=?,usage_json=?,elapsed_ms=?,finished_at=? WHERE id=?",
                         ("failed" if error else "completed", encode(response) if response is not None else None, encode(usage), elapsed, now(), record_id))
            self.db.event(conn, state["owner"], run["id"], "model.finished", {"phase": phase, "elapsed_ms": elapsed, "usage": usage, "error": error})
        if error:
            errors = self.service.dialogue.state(run['id']).get('phase_errors', {}) | {phase: error}
            self.service.dialogue.put(run['id'], phase_errors=errors)
            raise ModelFailure(error)
        return result

    def model_context(self, state):
        run = self.service.resource('runs', state['run_id'], state['owner'])
        anchor = self.runtime(run['id'])['started_at']
        today = datetime.fromisoformat(anchor).astimezone(SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0)
        entry = self.service.dialogue.state(run['id']).get('entry', {})
        direction = self.db.one("SELECT answer FROM run_inputs WHERE run_id=? AND input_key='entry:1' AND status='answered'", (run['id'],))
        return {'goal': run['goal'], 'resolved_goal': entry.get('resolved_goal', ''), 'current_time': anchor,
            'selected_research_direction':direction['answer'] if direction and self.service.dialogue.state(run['id']).get('policy_redirected') else None,
            'default_end_ms': int(today.timestamp()*1000), 'default_start_ms': int((today-timedelta(days=160)).timestamp()*1000),
            'timezone': 'Asia/Shanghai', 'tools': [{k:t[k] for k in ('name','description','input_schema','max_retries')}
                for t in self.service.tools() if t['name'] in run['config']['allowed_tools']],
            'documents':self.service.documents.listing(state['owner'])[:20],
            'conversation': [{'role':m['role'],'text':m['content']['text'][:6000]} for m in self.service.dialogue.messages(state['owner'],run['thread_id'],run['id'])]}

    async def model_plan(self, state, phase, previous=None):
        run = self.service.resource('runs', state['run_id'], state['owner'])
        context = self.model_context(state)
        if previous:
            context.update(previous_plan=previous, output_schema=Plan.model_json_schema())
            validator = lambda p: validate_plan(self.service, p, run['config']['allowed_tools'], previous)
            return await self.model_request(state, phase, context, PLANNER_RULES, validator)
        context['output_schema'] = Entry.model_json_schema()
        errors = self.service.dialogue.state(run['id']).get('phase_errors', {})
        if errors:
            context['repair_errors'] = errors
            last = self.db.one("SELECT response_json FROM model_calls WHERE run_id=? AND status='failed' AND response_json IS NOT NULL ORDER BY started_at DESC LIMIT 1",(run['id'],))
            if last:
                context['rejected_draft'] = json.loads(last['response_json'])
        return await self.model_request(state, phase, context, ENTRY_RULES + '\n' + PLANNER_RULES,
            lambda p: validate_entry(self.service, p, run['config']['allowed_tools'], run['goal']))

    def wait_input(self, state, key, question, kind='clarify', candidates=None):
        run_id, owner = state['run_id'], state['owner']
        row = self.db.one('SELECT * FROM run_inputs WHERE run_id=? AND input_key=?', (run_id, key))
        if not row:
            if self.db.one('SELECT COUNT(*) AS n FROM run_inputs WHERE run_id=?', (run_id,))['n'] >= 3:
                raise ServiceError(409, '澄清次数已达到上限，请提出更明确的研究目标')
            identifier = uid()
            body = {**question, 'kind':kind, 'candidates':candidates or []}
            with self.db.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                run = conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
                if run['status'] not in {'running','queued'}:
                    raise ServiceError(409, '当前运行不能提出新问题')
                conn.execute("INSERT INTO run_inputs VALUES(?,?,?,?,?,'pending',NULL,?,NULL)", (identifier,owner,run_id,key,encode(body),now()))
                self.service.dialogue.add_message(conn, dict(run), 'assistant', 'question:'+identifier, body['text'], question_id=identifier, options=body['options'])
                conn.execute("UPDATE runs SET status='awaiting_input',updated_at=? WHERE id=?", (now(),run_id))
                self.db.event(conn,owner,run_id,'input.requested',{'question_id':identifier,'question':body})
            row = self.db.one('SELECT * FROM run_inputs WHERE id=?', (identifier,))
        while row['status'] == 'pending':
            interrupt({'kind':'input','question_id':row['id']})
            row = self.db.one('SELECT * FROM run_inputs WHERE id=?', (row['id'],))
            if not self.alive(state) or self.runtime(run_id)['deadline'] <= time.time():
                raise ModelFailure('input_expired_or_stopped')
        if row['status'] != 'answered':
            raise ModelFailure('input_expired_or_stopped')
        return row['answer']

    async def plan(self, state):
        runtime = self.runtime(state['run_id'])
        if runtime['deadline'] <= time.time() and (runtime['plan'] or {}).get('intent') == 'clarify':
            with self.db.connect() as conn:
                conn.execute('UPDATE run_runtime SET plan_json=? WHERE run_id=?',
                    (encode({'tasks':[],'limitations':['未在运行时限内取得必要补充信息。']}),state['run_id']))
            self.service.dialogue.put(state['run_id'],planning_error='input_expired_or_stopped')
            return {}
        while True:
            existing = self.runtime(state['run_id'])['plan']
            if existing and existing.get('intent','research') != 'clarify':
                return {}
            rounds = 0
            if existing and existing.get('intent') == 'clarify':
                rounds = self.service.dialogue.state(state['run_id']).get('entry_round',0) + 1
                if rounds > 3:
                    with self.db.connect() as conn:
                        conn.execute('UPDATE run_runtime SET plan_json=? WHERE run_id=?',
                            (encode({'tasks':[],'limitations':['澄清次数已达到上限，请提供更明确的研究目标。']}),state['run_id']))
                    self.service.dialogue.put(state['run_id'],planning_error='clarification_limit')
                    return {}
                self.wait_input(state,'entry:'+str(rounds),existing['question'])
            success = await self.plan_round(state, rounds)
            if not success or self.runtime(state['run_id'])['plan'].get('intent') != 'clarify':
                return {}

    async def plan_round(self, state, rounds=0):
        if not self.controlled(state) or self.runtime(state['run_id'])['deadline'] <= time.time():
            return False
        if rounds == 0:
            from server.harness.boundaries import trading_question
            run = self.service.resource('runs', state['run_id'], state['owner'])
            redirect = trading_question(run['goal'])
            if redirect:
                plan = validate_entry(self.service, redirect, run['config']['allowed_tools'], run['goal'])
                self.service.dialogue.put(state['run_id'], entry=plan, entry_round=0, planning_error=None, review=None, review_error=None, policy_redirected=True)
                self.persist_plan(state, plan)
                with self.db.connect() as conn:
                    self.db.event(conn, state['owner'], state['run_id'], 'policy.redirect', {'reason':'trading_decision', 'financial_calls':0})
                return True
        prefix = 'plan' if rounds == 0 else 'plan_input_' + str(rounds)
        last_error = 'planning_failed'
        for phase in (prefix, prefix + '_retry'):
            if not self.controlled(state) or self.runtime(state["run_id"])["deadline"] <= time.time():
                break
            try:
                plan = await self.model_plan(state, phase)
                if self.service.dialogue.state(state['run_id']).get('policy_redirected'):
                    from server.harness.boundaries import SCOPE_NOTE
                    plan['limitations'] = list(dict.fromkeys(plan['limitations'] + [SCOPE_NOTE]))
                self.service.dialogue.put(state['run_id'], entry=plan, entry_round=rounds, planning_error=None, review=None, review_error=None)
                self.persist_plan(state, plan)
                return True
            except ModelFailure as exc:
                last_error = str(exc)
                with self.db.connect() as conn:
                    self.db.event(conn, state["owner"], state["run_id"], "plan.failed", {"phase": phase, "reason": last_error})
                if self.service.resource("runs", state["run_id"], state["owner"])["config"].get("budget_denied"):
                    break
        with self.db.connect() as conn:
            conn.execute("UPDATE run_runtime SET plan_json=?,last_error='planning_failed' WHERE run_id=?",
                         (encode({"tasks": [], "limitations": ["未得到通过验证的研究计划"]}), state["run_id"]))
        self.service.dialogue.put(state['run_id'], planning_error=last_error)
        return False

    async def gate(self, state):
        if not self.controlled(state):
            return {"route": "end"}
        run = self.service.resource("runs", state["run_id"], state["owner"])
        runtime = self.runtime(run["id"])
        plan = runtime["plan"] or {"tasks": []}
        if plan.get('intent') == 'answer' or self.service.dialogue.state(run['id']).get('planning_error'):
            return {'route':'report'}
        ids = self.physical_ids(run["id"], plan)
        tasks = {t["id"]: t for t in self.service.collection(state["owner"], run["id"], "tasks")}
        pending = [t for t in plan["tasks"] if tasks[ids[t["id"]]]["status"] in {"pending", "running"}]
        if runtime["deadline"] <= time.time() or run["config"].get("budget_denied") or runtime["model_tokens"] > run["config"]["budget"]["model_tokens"]:
            self.block_pending(state, "time_limit" if runtime["deadline"] <= time.time() else "budget_limit_or_rejected")
            return {"route": "report"}
        ready = []
        for task in pending:
            deps = [tasks[ids[d]]["status"] for d in task["depends_on"]]
            if any(s in {"failed", "blocked"} for s in deps):
                self.task_status(state, ids[task["id"]], "blocked", "dependency_failed")
                continue
            if any(s != "completed" for s in deps):
                continue
            if not self.ensure_budget(state, {"tool": task["tool"], "logical_id": task["id"], "arguments": task["arguments"]},
                                      {"tool_attempts": run["attempts_used"] + 1}, {"tool_attempts": run["attempts_used"] + len(pending)}):
                self.block_pending(state, "budget_or_control_limit")
                return {"route": "report" if self.alive(state) else "end"}
            ready.append(task)
            if len(ready) >= self.settings.tool_concurrency:
                break
        if ready:
            # Ask once for the complete batch reservation before starting any remote call.
            if not self.ensure_budget(state, {"batch": [t['id'] for t in ready]},
                {"tool_attempts":run['attempts_used']+len(ready)}, {"tool_attempts":run['attempts_used']+len(pending)}):
                self.block_pending(state, 'budget_or_control_limit')
                return {'route':'report' if self.alive(state) else 'end'}
            return {"route":"execute", "task":ready[0], "batch":ready}
        return {'route':'review'}

    async def review(self, state):
        if not self.controlled(state):
            return {'route':'end'}
        run = self.service.resource('runs',state['run_id'],state['owner'])
        runtime = self.runtime(run['id'])
        plan = runtime['plan'] or {'tasks':[],'objectives':[]}
        if not plan.get('objectives'):
            # Old checkpoints retain their already executed plans.
            plan['objectives'] = [{'id':'research_goal','text':run['goal'],'required_fields':[]}]
            with self.db.connect() as conn:
                conn.execute('UPDATE run_runtime SET plan_json=? WHERE run_id=?',(encode(plan),run['id']))
        report = build_report(self.service,state['owner'],run['id'],include_agent=False)
        if report['validation_status'] == 'failed':
            return {'route':'report'}
        metadata = self.service.dialogue.state(run['id'])
        if metadata.get('review') or metadata.get('recovery_analysis'):
            return {'route':'report'}
        if runtime['deadline'] <= time.time() or run['config'].get('budget_denied') or runtime['model_tokens'] > run['config']['budget']['model_tokens']:
            self.service.dialogue.put(run['id'],review_error='budget_or_time_limit')
            return {'route':'report'}
        facts = facts_for_model(report)
        context = self.model_context(state) | {'stage':'review','previous_plan':plan,'objectives':plan['objectives'],
            'facts':compact_facts(facts), 'data_gaps':report['gaps'], 'limitations':report['limitations'],
            'outcomes':[{'id':t['id'],'tool':t['tool'],'status':t['status']} for t in self.service.collection(state['owner'],run['id'],'tasks')],
            'can_extend':runtime['replan_count'] < run['config'].get('max_replans',1), 'output_schema':Review.model_json_schema()}
        inputs = self.db.one('SELECT COUNT(*) AS n FROM run_inputs WHERE run_id=?',(run['id'],))['n']
        prefix = f"review_{runtime['replan_count']}_{inputs}" + (f"_v{run['plan_version']}" if run['plan_version'] > 1 else '')
        result, last_error = None, 'answer_validation_failed'
        def validate_response(payload):
            response = validate_review(self.service,payload,run['config']['allowed_tools'],plan,report)
            if run['config'].get('narrative_version') and facts and response['action']=='finish' and not any(
                    p['kind']!='fact' for s in response['sections'] for p in s['paragraphs']):
                raise ValueError('analysis_description_required')
            return response
        for phase in (prefix,prefix+'_retry'):
            try:
                if last_error != 'answer_validation_failed':
                    context['repair_error'] = last_error
                    last = self.db.one('SELECT response_json FROM model_calls WHERE run_id=? AND phase=?',(run['id'],prefix))
                    if last and last['response_json']:
                        context['rejected_draft'] = json.loads(last['response_json'])
                result = await self.model_request(state,phase,context,REVIEW_RULES,
                    validate_response)
                break
            except ModelFailure as exc:
                last_error = str(exc)
                if self.service.resource('runs',run['id'],state['owner'])['config'].get('budget_denied'):
                    break
        if not result:
            self.service.dialogue.put(run['id'],review_error=last_error)
            recoverable = {'model_output_incomplete',
                'model_output_validation_failed:calendar_reference_required',
                'model_output_validation_failed:analysis_description_required'}
            if facts and run['config'].get('narrative_version') and last_error in recoverable and self.controlled(state):
                fresh = self.service.resource('runs',run['id'],state['owner'])
                runtime = self.runtime(run['id'])
                # Recovery is one ledger-controlled model call. It cannot extend the plan or tools.
                if not fresh['config'].get('budget_denied') and runtime['model_calls'] < fresh['config']['budget']['model_calls']:
                    recovery_context = self.model_context(state) | {'stage':'analysis_recovery',
                        'facts':compact_facts(facts), 'objectives':plan['objectives'],
                        'data_gaps':report['gaps'], 'limitations':report['limitations'],
                        'output_schema':AnalysisDraft.model_json_schema()}
                    try:
                        draft = await self.model_request(state,prefix+'_analysis',recovery_context,ANALYSIS_RULES,
                            lambda p:validate_analysis(p,report))
                        self.service.dialogue.put(run['id'],recovery_analysis=draft,recovery_facts_hash=fingerprint(facts))
                        with self.db.connect() as conn:
                            self.db.event(conn,state['owner'],run['id'],'analysis.recovered',{'phase':prefix+'_analysis','goal_coverage_confirmed':False})
                    except ModelFailure as exc:
                        self.service.dialogue.put(run['id'],recovery_error=str(exc))
            return {'route':'report'}
        with self.db.connect() as conn:
            self.db.event(conn,state['owner'],run['id'],'research.observed',{'coverage':result['coverage'],'action':result['action']})
        if result['action'] == 'extend':
            if not context['can_extend']:
                self.service.dialogue.put(run['id'],review_error='replan_limit')
                return {'route':'report'}
            new_plan = validate_plan(self.service,{k:result[k] for k in ('tasks','limitations','unresolved_goals')},run['config']['allowed_tools'],previous=plan)
            new_plan.update({k:v for k,v in plan.items() if k not in new_plan})
            self.persist_plan(state,new_plan,replan=True)
            return {'route':'gate'}
        if result['action'] == 'clarify':
            if inputs >= 3:
                self.service.dialogue.put(run['id'],review_error='clarification_limit')
                return {'route':'report'}
            self.wait_input(state,prefix,result['question'])
            return await self.review(state)
        self.service.dialogue.put(run['id'],review=result,review_facts_hash=fingerprint(facts),review_error=None)
        return {'route':'report'}

    def task_status(self, state, task_id, status, reason):
        with self.db.connect() as conn:
            conn.execute("UPDATE tasks SET status=?,updated_at=? WHERE id=?", (status, now(), task_id))
            self.db.event(conn, state["owner"], state["run_id"], "task." + status, {"task_id": task_id, "reason": reason})

    def block_pending(self, state, reason):
        for task in self.service.collection(state["owner"], state["run_id"], "tasks"):
            if task["status"] in {"pending", "running"}:
                self.task_status(state, task["id"], "blocked", reason)

    async def execute(self, state):
        # Entity selection can interrupt LangGraph; dispatch reference-dependent tasks serially.
        batch = state.get('batch') or [state['task']]
        if any('@' in encode(t['arguments']) for t in batch):
            for task in batch:
                await self.execute_one({**state, 'task':task})
        else:
            with self.db.connect() as conn:
                self.db.event(conn,state['owner'],state['run_id'],'tasks.parallel',{'count':len(batch),'tasks':[t['id'] for t in batch]})
            await asyncio.gather(*(self.execute_one({**state,'task':task}) for task in batch))
        return {}

    async def execute_one(self, state):
        if not self.controlled(state):
            return {}
        run_id = state["run_id"]
        plan = self.runtime(run_id)["plan"]
        # A paused node can resume with an older checkpoint; use the authoritative edited plan.
        task = next((t for t in plan["tasks"] if t["id"] == state["task"]["id"]), None)
        if not task:
            return {}
        ids = self.physical_ids(run_id, plan)
        try:
            selections = self.service.dialogue.state(run_id).get('entity_selections', {})
            from server.harness.planning import transform
            references = []
            transform(task['arguments'], lambda logical, output: references.append((logical,output)) or 'reference')
            for logical, output in references:
                if output != 'thscode' or logical in selections:
                    continue
                call = self.db.one("SELECT result_json FROM tool_calls WHERE run_id=? AND task_id=? AND status='completed'",(run_id,ids[logical]))
                if not call:
                    continue
                eids = json.loads(call['result_json']).get('evidence_ids',[])
                if len(eids) != 1:
                    continue
                from server.harness.reporting import checked_evidence
                record = self.service.resource('evidence',eids[0],state['owner'])
                content = checked_evidence(self.service,state['owner'],run_id,record)
                candidates = content['records'] if content['quality'] == 'available' else []
                if len(candidates) > 1:
                    selected = self.wait_input(state,'entity:'+logical,{'text':'名称匹配到多个标的，请选择本次研究对象。',
                        'options':[r['thscode'] for r in candidates[:6]]},kind='entity',candidates=candidates[:6])
                    selections[logical] = selected
                    self.service.dialogue.put(run_id,entity_selections=selections)
            args = materialize(self.service, state["owner"], run_id, task, ids, selections)
            with self.db.connect() as conn:
                conn.execute("UPDATE tasks SET arguments_json=? WHERE id=?", (encode(args), ids[task["id"]]))
            await self.service.execute(state["owner"], run_id, task["tool"], args,
                                       idempotency_key="agent:" + task["id"], task_id=ids[task["id"]], lease_owner=self.instance)
        except ServiceError:
            if self.service.resource("runs", run_id, state["owner"])["status"] not in {"paused", "stopped"}:
                self.task_status(state, ids[task["id"]], "failed", "argument_dependency_or_dispatch_validation_failed")
        finally:
            self.service.contexts.capture(state['owner'], run_id)
        return {}

    async def report(self, state):
        if not self.controlled(state):
            return {}
        report = build_report(self.service, state["owner"], state["run_id"])
        # Stop is checked again under the transaction before publishing the artifact.
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status FROM runs WHERE id=?", (state["run_id"],)).fetchone()
            if row["status"] in {"stopped", "paused", "awaiting_approval"}:
                return {}
        artifact_id = save_report(self.service, state["owner"], state["run_id"], report)
        if not artifact_id:
            return {}
        target = "blocked" if report["validation_status"] == "failed" else "failed" if report['status'] == 'failed' else "partial" if report["status"] == "partial" else "completed"
        with self.db.connect() as conn:
            conn.execute("UPDATE runs SET status=?,updated_at=? WHERE id=? AND status NOT IN ('stopped','paused','awaiting_approval')", (target, now(), state["run_id"]))
            run = dict(conn.execute('SELECT * FROM runs WHERE id=?',(state['run_id'],)).fetchone())
            text = '\n\n'.join('### '+s['title']+'\n\n'+'\n\n'.join(p['text'] for p in s['paragraphs'])
                for s in report.get('narrative',[]))
            if not text:
                text = '\n'.join(c['text'] for c in report['claims'][:8])
            if report['gaps']:
                text += '\n\n' + '\n'.join(g.get('detail') or g['reason'] for g in report['gaps'])
            if report.get('execution_failures'):
                from server.ui.presentation import reason
                text += '\n\n' + '\n'.join(reason(e['reason'])+'：'+e['detail'] for e in report['execution_failures'])
            self.service.dialogue.add_message(conn,run,'assistant','answer',text[:12000],answer_mode=report.get('answer_mode','research'),artifact_id=artifact_id)
            self.db.event(conn, state["owner"], state["run_id"], "run.finished", {"status": target, "validation_status": report["validation_status"]})
        return {}

    def recover_calls(self, row):
        with self.db.connect() as conn:
            for call in conn.execute("SELECT * FROM tool_calls WHERE run_id=? AND status='running'", (row["id"],)).fetchall():
                summary = json.loads(call["result_json"] or "{}")
                done = summary.get("quality") in {"available", "unverified"}
                from server.tools.specs import SPECS
                retries = SPECS[call["tool"]].max_retries if call["tool"] in SPECS else 0
                final_failure = bool(summary) and (not summary.get("retryable", False) or call["attempts"] >= retries + 1)
                target = "completed" if done else "failed" if final_failure else "interrupted"
                conn.execute("UPDATE tool_calls SET status=?,finished_at=? WHERE id=?", (target, now() if done else None, call["id"]))
                conn.execute("UPDATE tasks SET status=?,updated_at=? WHERE id=?", ("completed" if done else "failed" if final_failure else "pending", now(), call["task_id"]))
                conn.execute("UPDATE tool_attempts SET finished_at=?,error='unconfirmed_after_restart' WHERE call_id=? AND finished_at IS NULL", (now(), call["id"]))
                self.db.event(conn, row["session_id"], row["id"], "tool.recovered", {"call_id": call["id"], "result": target,
                    "uncertain_remote_dispatch": not done, "completed_results_reused": done})
            conn.execute("UPDATE model_calls SET status='interrupted',finished_at=? WHERE run_id=? AND status='running'", (now(), row["id"]))

    async def heartbeat(self, run_id):
        while True:
            with self.db.connect() as conn:
                conn.execute("UPDATE run_runtime SET lease_until=?,heartbeat_at=? WHERE run_id=? AND lease_owner=?",
                             (time.time() + self.lease_seconds, now(), run_id, self.instance))
            await asyncio.sleep(2)

    @traced('research')
    async def run_once(self, row):
        run_id = row["id"]
        started = time.perf_counter()
        beat = asyncio.create_task(self.heartbeat(run_id))
        try:
            self.recover_calls(row)
            config = {"configurable": {"thread_id": run_id}, "recursion_limit": 80}
            saved = await self.graph.aget_state(config)
            state = {"run_id": run_id, "owner": row["session_id"], "plan_version": row["plan_version"]}
            edited = bool(saved.values) and row["plan_version"] > saved.values.get("plan_version", 1)
            if edited:
                # Old interrupt indices and arguments belong to the previous plan.
                # Start at the persisted plan; gate skips all ledger-completed work.
                graph_input = state
            elif saved.tasks and any(task.interrupts for task in saved.tasks):
                graph_input = Command(resume={"source": "validated_database_control"})
            elif saved.values:
                graph_input = None
            else:
                graph_input = state
            # Graph may have completed before its final run status commit; ledger reconciles it.
            if saved.values and not saved.next and not edited:
                await self.report(state)
            else:
                await self.graph.ainvoke(graph_input, config)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            with self.db.connect() as conn:
                conn.execute("UPDATE runs SET status='recoverable_failed',updated_at=? WHERE id=? AND status NOT IN ('stopped','paused','awaiting_approval','awaiting_input','completed','partial','blocked','failed')", (now(), run_id))
                conn.execute("UPDATE run_runtime SET last_error='graph_execution_failed' WHERE run_id=?", (run_id,))
                self.db.event(conn, row["session_id"], run_id, "run.recoverable_failed", {"reason": "graph_execution_failed", "error_type": type(error).__name__,
                    "locations": [{"function": frame.name, "line": frame.lineno} for frame in traceback.extract_tb(error.__traceback__)[-5:]]})
        finally:
            beat.cancel()
            with suppress(asyncio.CancelledError):
                await beat
            with self.db.connect() as conn:
                conn.execute("UPDATE run_runtime SET lease_owner=NULL,lease_until=0,elapsed_ms=elapsed_ms+? WHERE run_id=? AND lease_owner=?",
                             (round((time.perf_counter()-started)*1000), run_id, self.instance))

    async def schedule(self):
        while True:
            await self.dispatch.tick()
            self.active = {k: v for k, v in self.active.items() if not v.done()}
            # One task per process; SQL lease also prevents concurrent processes owning a run.
            if not self.active:
                rows = self.db.all("SELECT r.* FROM runs r JOIN run_runtime x ON x.run_id=r.id WHERE r.mode='agent' AND r.status IN ('queued','running') AND x.lease_until<=? ORDER BY r.created_at LIMIT 1", (time.time(),))
                for row in rows:
                    with self.db.connect() as conn:
                        conn.execute("BEGIN IMMEDIATE")
                        claimed = conn.execute("UPDATE run_runtime SET lease_owner=?,lease_until=?,heartbeat_at=? WHERE run_id=? AND lease_until<=?",
                                               (self.instance, time.time()+self.lease_seconds, now(), row["id"], time.time()))
                        if not claimed.rowcount:
                            continue
                        conn.execute("UPDATE runs SET status='running',updated_at=? WHERE id=? AND status IN ('queued','running')", (now(), row["id"]))
                        self.db.event(conn, row["session_id"], row["id"], "run.leased", {"recovery": row["status"] == "running", "lease_seconds": self.lease_seconds})
                    self.active[row["id"]] = asyncio.create_task(self.run_once(row), name="research-"+row["id"])
            # Waiting is durable, but a time limit is not suspended by browser absence or approval.
            expired = self.db.all("SELECT r.* FROM runs r JOIN run_runtime x ON r.id=x.run_id WHERE r.status IN ('awaiting_approval','paused','awaiting_input') AND x.deadline<=? AND x.lease_until<=?", (time.time(), time.time()))
            for row in expired:
                with self.db.connect() as conn:
                    conn.execute("UPDATE approvals SET status='expired' WHERE run_id=? AND status='pending'", (row["id"],))
                    conn.execute("UPDATE run_inputs SET status='expired' WHERE run_id=? AND status='pending'", (row['id'],))
                    config = json.loads(row["config_json"])
                    config["budget_denied"] = True
                    conn.execute("UPDATE runs SET status='queued',config_json=? WHERE id=? AND status IN ('paused','awaiting_approval','awaiting_input')", (encode(config), row["id"]))
                    self.db.event(conn, row["session_id"], row["id"], "run.deadline_reached", {})
            await asyncio.sleep(self.poll_seconds)
