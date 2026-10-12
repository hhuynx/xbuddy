from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid() -> str:
    return str(uuid4())


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
 id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, csrf_token TEXT NOT NULL,
 expires_at INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS threads(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 title TEXT NOT NULL, goal TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 thread_id TEXT NOT NULL REFERENCES threads(id), goal TEXT NOT NULL, mode TEXT NOT NULL,
 status TEXT NOT NULL, schema_version INTEGER NOT NULL DEFAULT 1, plan_version INTEGER NOT NULL DEFAULT 1,
 config_json TEXT NOT NULL DEFAULT '{}', attempts_used INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), run_id TEXT NOT NULL REFERENCES runs(id),
 title TEXT NOT NULL, tool TEXT NOT NULL, arguments_json TEXT NOT NULL,
 depends_on_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tool_calls(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), run_id TEXT NOT NULL REFERENCES runs(id),
 task_id TEXT REFERENCES tasks(id), tool TEXT NOT NULL, version TEXT NOT NULL, parameters_json TEXT NOT NULL,
 fingerprint TEXT NOT NULL, idempotency_key TEXT NOT NULL, status TEXT NOT NULL, quality TEXT,
 attempts INTEGER NOT NULL DEFAULT 0, elapsed_ms INTEGER, result_json TEXT, created_at TEXT NOT NULL, finished_at TEXT,
 UNIQUE(run_id,idempotency_key));
CREATE TABLE IF NOT EXISTS tool_attempts(
 id TEXT PRIMARY KEY, call_id TEXT NOT NULL REFERENCES tool_calls(id), attempt INTEGER NOT NULL,
 started_at TEXT NOT NULL, finished_at TEXT, elapsed_ms INTEGER, http_status INTEGER,
 business_code INTEGER, request_id TEXT, error TEXT, cost_status TEXT NOT NULL DEFAULT 'unknown',
 UNIQUE(call_id,attempt));
CREATE TABLE IF NOT EXISTS raw_records(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), call_id TEXT NOT NULL REFERENCES tool_calls(id),
 attempt INTEGER NOT NULL, provider TEXT NOT NULL, body_json TEXT NOT NULL, content_sha256 TEXT NOT NULL,
 fetched_at TEXT NOT NULL, UNIQUE(call_id,attempt));
CREATE TABLE IF NOT EXISTS evidence(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), run_id TEXT NOT NULL REFERENCES runs(id),
 call_id TEXT NOT NULL REFERENCES tool_calls(id), raw_id TEXT NOT NULL REFERENCES raw_records(id),
 content_json TEXT NOT NULL, content_sha256 TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS derived_metrics(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), run_id TEXT NOT NULL REFERENCES runs(id),
 call_id TEXT NOT NULL REFERENCES tool_calls(id), content_json TEXT NOT NULL,
 input_refs_json TEXT NOT NULL, content_sha256 TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS claims(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), run_id TEXT NOT NULL REFERENCES runs(id),
 content_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), run_id TEXT NOT NULL REFERENCES runs(id),
 kind TEXT NOT NULL, version INTEGER NOT NULL, content_json TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(run_id,kind,version));
CREATE TABLE IF NOT EXISTS approvals(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), run_id TEXT NOT NULL REFERENCES runs(id),
 status TEXT NOT NULL, content_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memories(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), thread_id TEXT REFERENCES threads(id),
 content_json TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run_events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES sessions(id),
 run_id TEXT NOT NULL REFERENCES runs(id), event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tool_capabilities(
 name TEXT PRIMARY KEY, provider TEXT NOT NULL, provider_fingerprint TEXT NOT NULL,
 status TEXT NOT NULL, details_json TEXT NOT NULL, checked_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mcp_tools(
 name TEXT PRIMARY KEY, provider_fingerprint TEXT NOT NULL, spec_json TEXT NOT NULL, discovered_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS threads_owner ON threads(session_id,updated_at);
CREATE INDEX IF NOT EXISTS calls_owner_run ON tool_calls(session_id,run_id);
CREATE INDEX IF NOT EXISTS events_owner_run ON run_events(session_id,run_id,id);
CREATE INDEX IF NOT EXISTS events_owner_cursor ON run_events(session_id,id);
INSERT OR IGNORE INTO schema_migrations VALUES(1,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
"""

# Additive migration: existing sessions, market records and their hashes stay intact.
HARNESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_runtime(
 run_id TEXT PRIMARY KEY REFERENCES runs(id), lease_owner TEXT, lease_until REAL NOT NULL DEFAULT 0,
 heartbeat_at TEXT, started_at TEXT NOT NULL, deadline REAL NOT NULL,
 model_calls INTEGER NOT NULL DEFAULT 0, model_tokens INTEGER NOT NULL DEFAULT 0,
 plan_json TEXT, replan_count INTEGER NOT NULL DEFAULT 0, last_error TEXT,
 elapsed_ms INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS model_calls(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), run_id TEXT NOT NULL REFERENCES runs(id),
 phase TEXT NOT NULL, status TEXT NOT NULL, request_hash TEXT NOT NULL, response_json TEXT,
 usage_json TEXT, elapsed_ms INTEGER, started_at TEXT NOT NULL, finished_at TEXT,
 UNIQUE(run_id,phase));
CREATE INDEX IF NOT EXISTS runtime_lease ON run_runtime(lease_until);
INSERT OR IGNORE INTO schema_migrations VALUES(2,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
"""

KNOWLEDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_links(
 run_id TEXT PRIMARY KEY REFERENCES runs(id), session_id TEXT NOT NULL REFERENCES sessions(id),
 parent_run_id TEXT NOT NULL REFERENCES runs(id), parent_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
 content_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_versions(
 memory_id TEXT NOT NULL REFERENCES memories(id), version INTEGER NOT NULL, status TEXT NOT NULL,
 content_json TEXT NOT NULL, content_sha256 TEXT NOT NULL, changed_at TEXT NOT NULL,
 PRIMARY KEY(memory_id,version));
CREATE TABLE IF NOT EXISTS run_memory_usages(
 run_id TEXT NOT NULL REFERENCES runs(id), session_id TEXT NOT NULL REFERENCES sessions(id), memory_id TEXT NOT NULL,
 memory_version INTEGER NOT NULL, content_json TEXT NOT NULL, content_sha256 TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'selected', selected_at TEXT NOT NULL, last_used_at TEXT, withdrawn_at TEXT,
 PRIMARY KEY(run_id,memory_id));
CREATE TABLE IF NOT EXISTS context_entries(
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES sessions(id), run_id TEXT NOT NULL REFERENCES runs(id),
 source_run_id TEXT NOT NULL REFERENCES runs(id), source_event_id INTEGER NOT NULL REFERENCES run_events(id),
 content_json TEXT NOT NULL, content_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(run_id,source_event_id));
CREATE TABLE IF NOT EXISTS context_snapshots(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), run_id TEXT NOT NULL REFERENCES runs(id),
 phase TEXT NOT NULL, input_hash TEXT NOT NULL, original_json TEXT NOT NULL, sent_json TEXT NOT NULL,
 summary_json TEXT NOT NULL, ranges_json TEXT NOT NULL, before_tokens INTEGER NOT NULL, after_tokens INTEGER NOT NULL,
 compressed INTEGER NOT NULL, created_at TEXT NOT NULL, UNIQUE(run_id,phase,input_hash));
CREATE TABLE IF NOT EXISTS artifact_checksums(
 artifact_id TEXT PRIMARY KEY REFERENCES artifacts(id), content_sha256 TEXT NOT NULL, verified_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS memory_owner_status ON memories(session_id,status);
CREATE INDEX IF NOT EXISTS context_owner_run ON context_entries(session_id,run_id,id);
INSERT OR IGNORE INTO schema_migrations VALUES(3,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
"""


DIALOGUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS thread_messages(
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES sessions(id),
 thread_id TEXT NOT NULL REFERENCES threads(id), run_id TEXT NOT NULL REFERENCES runs(id),
 message_key TEXT NOT NULL, role TEXT NOT NULL, content_json TEXT NOT NULL,
 content_sha256 TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(run_id,message_key));
CREATE TABLE IF NOT EXISTS agent_state(
 run_id TEXT PRIMARY KEY REFERENCES runs(id), content_json TEXT NOT NULL, content_sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run_inputs(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), run_id TEXT NOT NULL REFERENCES runs(id),
 input_key TEXT NOT NULL, question_json TEXT NOT NULL, status TEXT NOT NULL, answer TEXT,
 created_at TEXT NOT NULL, answered_at TEXT, UNIQUE(run_id,input_key));
CREATE INDEX IF NOT EXISTS messages_thread_owner ON thread_messages(thread_id,session_id,id);
INSERT OR IGNORE INTO schema_migrations VALUES(4,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
"""


EXTENSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS thread_deletions(
 thread_id TEXT PRIMARY KEY REFERENCES threads(id), session_id TEXT NOT NULL REFERENCES sessions(id),
 deleted_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS execution_spans(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), run_id TEXT NOT NULL REFERENCES runs(id),
 trace_id TEXT NOT NULL, parent_id TEXT, name TEXT NOT NULL, status TEXT NOT NULL,
 elapsed_ms INTEGER NOT NULL, attributes_json TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS spans_run ON execution_spans(run_id,started_at);
CREATE TABLE IF NOT EXISTS dispatch_outbox(
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), created_at TEXT NOT NULL,
 sent_at TEXT, attempts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS research_documents(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), title TEXT NOT NULL,
 source_url TEXT NOT NULL, published_at TEXT NOT NULL, body TEXT NOT NULL,
 content_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(session_id,content_sha256,source_url,published_at));
CREATE TABLE IF NOT EXISTS document_chunks(
 id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES research_documents(id),
 page INTEGER NOT NULL, offset_start INTEGER NOT NULL, offset_end INTEGER NOT NULL,
 text TEXT NOT NULL, content_sha256 TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS chunks_document ON document_chunks(document_id);
"""


class Database:
    def __init__(self, path: Path, database_url: str = ''):
        self.path = path
        self.database_url = database_url
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            if not database_url:
                connection.execute("PRAGMA journal_mode=WAL")
            else:
                connection.execute('SELECT pg_advisory_xact_lock(82491702)')
            connection.executescript(SCHEMA)
            connection.executescript(HARNESS_SCHEMA)
            columns = ({row['name'] for row in connection.execute('PRAGMA table_info(memories)')} if not database_url else
                {row['name'] for row in connection.execute("SELECT column_name AS name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='memories'")})
            if 'status' not in columns:
                connection.execute("ALTER TABLE memories ADD COLUMN status TEXT NOT NULL DEFAULT 'legacy_unverified'")
            if 'content_sha256' not in columns:
                connection.execute("ALTER TABLE memories ADD COLUMN content_sha256 TEXT NOT NULL DEFAULT ''")
            connection.executescript(KNOWLEDGE_SCHEMA)
            connection.executescript(DIALOGUE_SCHEMA)
            connection.executescript(EXTENSION_SCHEMA)

    @contextmanager
    def connect(self):
        if self.database_url:
            import psycopg
            from psycopg.rows import dict_row
            from server.storage.postgres import Connection
            with psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=10) as connection:
                yield Connection(connection)
            return
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def one(self, sql: str, parameters: tuple = ()) -> dict | None:
        with self.connect() as connection:
            row = connection.execute(sql, parameters).fetchone()
            return dict(row) if row else None

    def all(self, sql: str, parameters: tuple = ()) -> list[dict]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(sql, parameters).fetchall()]

    def owned(self, table: str, record_id: str, session_id: str) -> dict | None:
        if table not in {"threads", "runs", "tasks", "tool_calls", "raw_records", "evidence", "derived_metrics", "approvals", "artifacts", "claims", "memories", "context_snapshots"}:
            raise ValueError("Unsupported resource")
        row = self.one(f"SELECT * FROM {table} WHERE id=? AND session_id=?", (record_id, session_id))
        if not row:
            return None
        thread_id = record_id if table == 'threads' else row.get('thread_id')
        if not thread_id and row.get('run_id'):
            run = self.one('SELECT thread_id FROM runs WHERE id=?', (row['run_id'],))
            thread_id = run['thread_id'] if run else None
        if thread_id and self.one('SELECT thread_id FROM thread_deletions WHERE thread_id=?', (thread_id,)):
            return None
        return row

    @staticmethod
    def event(connection, session_id: str, run_id: str, kind: str, payload: dict):
        connection.execute("INSERT INTO run_events(session_id,run_id,event_type,payload_json,created_at) VALUES(?,?,?,?,?)",
                           (session_id, run_id, kind, encode(payload), now()))
        if kind in {'run.queued', 'run.resume', 'approval.decided', 'input.answered'}:
            connection.execute('INSERT INTO dispatch_outbox(id,run_id,created_at) VALUES(?,?,?)', (uid(), run_id, now()))
