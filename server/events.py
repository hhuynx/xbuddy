"""Persisted event readers shared by the NiceGUI feed and SSE clients."""
from __future__ import annotations

import asyncio
import time

from server.harness.runtime import TERMINAL
from server.services import ServiceError, view
from server.storage.database import encode


def latest_cursor(service, owner, run_id=None):
    sql = "SELECT COALESCE(MAX(id),0) AS cursor FROM run_events WHERE session_id=?"
    args = (owner,)
    if run_id:
        sql += " AND run_id=?"
        args += (run_id,)
    return service.db.one(sql, args)["cursor"]


def workspace_snapshot(service, owner, run_id):
    # Cursor BEFORE the snapshot: events committed while reading are replayed, never skipped.
    cursor = latest_cursor(service, owner)
    run = service.harness.snapshot(owner, run_id)
    run["tasks"] = service.collection(owner, run_id, "tasks")
    run["tool_calls"] = service.collection(owner, run_id, "tool_calls")
    run["evidence"] = service.collection(owner, run_id, "evidence")
    run["metrics"] = service.collection(owner, run_id, "derived_metrics")
    run["model_usage"] = [view(row) for row in service.db.all(
        "SELECT phase,status,usage_json,elapsed_ms,started_at,finished_at FROM model_calls WHERE run_id=? AND session_id=? ORDER BY started_at", (run_id, owner))]
    return {"run": run, "event_cursor": cursor}


async def workspace_batches(service, owner, after=0, poll_seconds=.35):
    """Read-only subscription; no graph execution or fabricated progress."""
    cursor = max(0, after)
    while True:
        session = service.db.one("SELECT expires_at FROM sessions WHERE id=?", (owner,))
        if not session or session["expires_at"] <= time.time():
            raise ServiceError(401, "会话已过期，请刷新页面")
        rows = [view(row) for row in service.db.all(
            "SELECT * FROM run_events WHERE session_id=? AND id>? ORDER BY id LIMIT 200", (owner, cursor))]
        if rows:
            cursor = rows[-1]["id"]
            yield rows
        else:
            await asyncio.sleep(poll_seconds)


def sse_frame(kind, data, event_id=None):
    return (f"id: {event_id}\n" if event_id is not None else "") + f"event: {kind}\ndata: {encode(data)}\n\n"


async def run_stream(service, owner, run_id, after, request, expires_at):
    cursor, heartbeat = after, time.monotonic()
    snapshot = workspace_snapshot(service, owner, run_id)
    current = latest_cursor(service, owner, run_id)
    if cursor > current:
        cursor = 0
        yield sse_frame("reset", {"reason": "cursor_ahead", "cursor": 0})
    yield sse_frame("snapshot", snapshot)
    while not await request.is_disconnected():
        if expires_at <= time.time():
            yield sse_frame("session_expired", {"reason": "refresh_required"})
            return
        rows = service.events(owner, run_id, cursor, 200)
        for row in rows:
            yield sse_frame("run-event", row, row["id"])
            cursor = row["id"]
        if rows:
            continue
        if service.resource("runs", run_id, owner)["status"] in TERMINAL:
            yield sse_frame("complete", {"cursor": cursor})
            return
        if time.monotonic() - heartbeat >= 10:
            yield ": heartbeat\n\n"
            heartbeat = time.monotonic()
        await asyncio.sleep(.35)
