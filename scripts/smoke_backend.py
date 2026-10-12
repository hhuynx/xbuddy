"""Verify installed backend libraries and real SQLite checkpoint reopen/resume."""
from __future__ import annotations

import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from typing import TypedDict

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]


class Goal(BaseModel):
    text: str = Field(min_length=1)


class State(TypedDict):
    completed_steps: int
    approved: bool


def build_graph(checkpointer: SqliteSaver, calls: list[str]):
    def data_node(state: State):
        calls.append("data_node")
        return {"completed_steps": state["completed_steps"] + 1}

    def approval_node(state: State):
        decision = interrupt({"action": "library_smoke_test"})
        return {"approved": bool(decision)}

    graph = StateGraph(State)
    graph.add_node("data", data_node)
    graph.add_node("approval", approval_node)
    graph.add_edge(START, "data")
    graph.add_edge("data", "approval")
    graph.add_edge("approval", END)
    return graph.compile(checkpointer=checkpointer)


def main():
    app = FastAPI()

    @app.post("/probe")
    def probe(goal: Goal):
        return {"valid": True, "length": len(goal.text)}

    with TestClient(app) as client:
        assert client.post("/probe", json={"text": "research"}).status_code == 200
        assert client.post("/probe", json={"text": ""}).status_code == 422
    scratch = ROOT / ".cache/verification"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as directory:
        db = Path(directory) / "checkpoint.sqlite"
        # sqlite3.Connection's context manager handles transactions, not close().
        with closing(sqlite3.connect(db)) as connection:
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            assert mode == "wal"
        config = {"configurable": {"thread_id": "batch1-library-smoke"}}
        calls: list[str] = []
        with SqliteSaver.from_conn_string(str(db)) as saver:
            graph = build_graph(saver, calls)
            result = graph.invoke({"completed_steps": 0, "approved": False}, config)
            assert result.get("__interrupt__")
            assert calls == ["data_node"]
        # Reopening the database with a new graph simulates a new process's runtime.
        with SqliteSaver.from_conn_string(str(db)) as saver:
            graph = build_graph(saver, calls)
            result = graph.invoke(Command(resume=True), config)
            assert result["approved"] is True and result["completed_steps"] == 1
            assert calls == ["data_node"], "Completed node unexpectedly ran twice"
    result = {"fastapi_request_validation": "passed", "sqlite_wal": "passed",
              "langgraph_persistent_interrupt_resume": "passed", "completed_node_replay": "not_repeated",
              "scope": "dependency_compatibility_only"}
    report = ROOT / "docs/verification/backend-smoke.local.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
