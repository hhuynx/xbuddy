"""XBuddy NiceGUI/FastAPI entry point."""
from __future__ import annotations

import os
from pathlib import Path

from nicegui import app, ui
from server.api.routes import install_api
from server.config import Settings
from server.ui.workbench import register_workbench

ROOT = Path(__file__).resolve().parents[1]
settings = Settings.load()
service = install_api(app, settings)
register_workbench(service)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "application": "xbuddy", "stage": "agent_core", "schema_version": 4}


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(host=os.getenv("XBUDDY_HOST", "127.0.0.1"),
           port=int(os.getenv("XBUDDY_PORT", "8080")),
           title="XBuddy · 投资研究", reload=False, show=False, native=False)
