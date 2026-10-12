"""Start the Python UI and verify HTTP/UI transport, then stop the owned server."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = {**os.environ, "XBUDDY_HOST": "127.0.0.1", "XBUDDY_PORT": str(port),
           "XBUDDY_DATA_DIR": str(ROOT / ".cache/verification/ui-smoke-data")}
    cache = ROOT / ".cache/verification"
    cache.mkdir(parents=True, exist_ok=True)
    log_path = cache / "ui-server.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, "-m", "server.main"], cwd=ROOT,
                                   env=env, stdout=log, stderr=log)
        try:
            base = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 30
            with httpx.Client(timeout=3) as client:
                while True:
                    if process.poll() is not None:
                        raise RuntimeError("UI server exited; see .cache/verification/ui-server.log")
                    try:
                        response = client.get(base + "/health")
                        if response.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    if time.monotonic() >= deadline:
                        raise TimeoutError("UI server did not become healthy within 30 seconds")
                    time.sleep(0.25)
                assert response.json()["stage"] == "agent_core" and response.json()['schema_version']==4
                page = client.get(base + "/")
                assert page.status_code == 200 and "XBuddy" in page.text
                assert client.get(base + "/api/docs").status_code == 200
                schema = client.get(base + "/api/openapi.json").json()
                assert "/api/threads" in schema["paths"]
                transport = client.get(base + "/_nicegui_ws/socket.io/", params={"EIO": 4, "transport": "polling"})
                assert transport.status_code == 200 and transport.text.startswith("0{")
                handshake = json.loads(transport.text[1:])
                assert "websocket" in handshake["upgrades"]
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    result = {"nicegui_page_http": "passed", "fastapi_health_route": "passed",
              "socketio_polling_handshake": "passed", "websocket_upgrade_advertised": True,
              "browser_interaction": "not_tested_by_this_script", "scope": "agent_core_ui_runtime_only"}
    report = ROOT / "docs/verification/ui-smoke.local.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
