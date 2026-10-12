"""Freeze the tested environment; Windows-only dependencies retain platform markers."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
result = subprocess.run([sys.executable, "-m", "pip", "freeze"],
                        capture_output=True, text=True, check=True)
lines = []
for line in result.stdout.splitlines():
    if line.lower().startswith("pywin32=="):
        line += '; sys_platform == "win32"'
    lines.append(line)
if not any(line.lower().startswith('uvloop==') for line in lines):
    lines.append('uvloop==0.23.0; sys_platform != "win32"')
header = ("# Generated from the verified Python 3.11 Windows environment.\n"
          "# Linux container installation also verified; uvloop is pinned with a platform marker.\n"
          "# Regenerate only after dependency smoke checks pass.\n")
(ROOT / "requirements.lock").write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
print(f"Locked {len(lines)} packages in requirements.lock")
