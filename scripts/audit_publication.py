"""Audit candidate source files without displaying configured secrets or private data."""
from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {'.venv', '.cache', '.git', '.idea', '.agents', '.codex', 'data', 'artifacts',
            'output', '.pytest_cache', '__pycache__', '.docker-probe', '.playwright-cli', 'node_modules', 'dist'}


def main():
    config = {**dotenv_values(ROOT / '.env'), **dotenv_values(ROOT / '.env.docker'), **os.environ}
    secrets = [v for k, v in config.items() if v and len(v) >= 8 and any(w in k.upper() for w in ('API_KEY', 'TOKEN', 'SECRET', 'PASSWORD'))]
    violations, count = [], 0
    for directory, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in EXCLUDED]
        for name in files:
            if (name.startswith('.env') and name != '.env.example') or name.endswith('.local.json'):
                continue
            path = Path(directory) / name
            relative = path.relative_to(ROOT).as_posix()
            if any(name.endswith(suffix) for suffix in ('.sqlite3', '.sqlite3-wal', '.sqlite3-shm', '.log', '.pdf')):
                violations.append({'file': relative, 'reason': 'private_runtime_artifact_in_public_candidates'})
                continue
            try:
                content = path.read_text(encoding='utf-8')
            except UnicodeError:
                violations.append({'file': relative, 'reason': 'binary_requires_explicit_publication_review'})
                continue
            count += 1
            if any(secret in content for secret in secrets):
                violations.append({'file': relative, 'reason': 'configured_secret'})
            if path.suffix == '.json':
                try:
                    value = json.loads(content)
                except ValueError:
                    violations.append({'file': relative, 'reason': 'invalid_json'})
                    continue
                if isinstance(value, dict) and (isinstance(value.get('data'), dict) and 'item' in value['data']
                                              or 'token_hash' in value or 'csrf_token' in value or 'cookies' in value):
                    violations.append({'file': relative, 'reason': 'market_or_session_payload'})
    ignore = (ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()
    required = {'.env', '.env.*', '!.env.example', '.cache/', 'data/', '*.sqlite3', '*.sqlite3-wal',
                '*.sqlite3-shm', 'docs/verification/*.local.json', 'output/playwright/'}
    if required - set(ignore):
        violations.append({'file': '.gitignore', 'reason': 'missing_private_file_rules'})
    result = {'status': 'failed' if violations else 'passed', 'source_files_checked': count,
              'scope': 'publication_candidates; git_history_and_remote_not_audited', 'violations': violations}
    print(json.dumps(result, ensure_ascii=False))
    return 1 if violations else 0


if __name__ == '__main__':
    raise SystemExit(main())
