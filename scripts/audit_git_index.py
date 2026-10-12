"""Audit actual Git index blobs before upload without printing secret values."""
import json
import os
import subprocess
from pathlib import Path

from dotenv import dotenv_values

ROOT=Path(__file__).resolve().parents[1]
PRIVATE_DIRS={'.venv','.cache','.git','.idea','.agents','.codex','data','artifacts',
    'output','__pycache__','.pytest_cache','.docker-probe','.playwright-cli','node_modules'}


def git(*arguments):
    return subprocess.check_output(['git',*arguments],cwd=ROOT)


def main():
    config={**dotenv_values(ROOT/'.env'),**dotenv_values(ROOT/'.env.docker'),**os.environ}
    secrets=[value for key,value in config.items() if value and len(value)>=8 and
        any(word in key.upper() for word in ('API_KEY','TOKEN','SECRET','PASSWORD'))]
    files=[p.decode('utf-8') for p in git('ls-files','--cached','-z').split(b'\0') if p]
    violations=[]
    for name in files:
        path=Path(name)
        if (any(p in PRIVATE_DIRS for p in path.parts) or path.name.endswith('.local.json') or
            (path.name.startswith('.env') and name!='.env.example') or
            path.suffix.lower() in {'.sqlite3','.log','.pyc','.pem','.key','.p12','.pfx','.zip'} or
            path.name.endswith(('.sqlite3-wal','.sqlite3-shm'))):
            violations.append({'file':name,'reason':'private_or_runtime_path'})
            continue
        content=git('show',':'+name)
        if any(secret.encode() in content for secret in secrets):
            violations.append({'file':name,'reason':'configured_secret'})
    required={'README.md','.env.example','docs/submission.md','docs/ai-usage.md','docs/testing.md','docs/verification-log.md'}
    for name in sorted(required-set(files)):
        violations.append({'file':name,'reason':'required_delivery_file_missing'})
    print(json.dumps({'status':'failed' if violations else 'passed','tracked_files_checked':len(files),
        'scope':'git_index_blobs; no_runtime_data_or_configured_secrets','violations':violations},ensure_ascii=False))
    return 1 if violations else 0


if __name__=='__main__':raise SystemExit(main())
