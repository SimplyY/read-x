#!/usr/bin/env python3
"""Archive newly frozen context packages without repeating uncertain writes."""
import json
import secrets
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / 'state' / 'long-read'


def main():
    database = sqlite3.connect(f'file:{STATE / "workflow.sqlite3"}?mode=ro', uri=True)
    try:
        rows = database.execute("SELECT job_id,payload_json FROM jobs WHERE app_id != 'retained' ORDER BY created_at").fetchall()
    finally:
        database.close()
    journal_path = STATE / 'reading-archive-journal.json'
    journal = json.loads(journal_path.read_text(encoding='utf-8')) if journal_path.exists() else {'jobs':{}}
    pending=[]
    for job_id, payload_json in rows:
        payload = json.loads(payload_json)
        archived = journal.get('jobs', {}).get(job_id, {})
        if payload.get('workflow_contract_version') and payload.get('context_packs') and (
                archived.get('status') != 'verified' or archived.get('metadata_complete') is not True):
            pending.append(job_id)
    failures=0
    # One job per pass keeps the worker timeout bounded. Random selection lets
    # other jobs make progress if one job needs manual recovery.
    for job_id in ([secrets.choice(pending)] if pending else []):
        result=subprocess.run([sys.executable,str(ROOT / 'scripts/archive_reading_context.py'),'--job-id',job_id,'--execute'],
                              cwd=ROOT,capture_output=True,text=True,timeout=150)
        if result.returncode: failures+=1
    print(json.dumps({'pending':len(pending),'attempted':min(len(pending),1),'needs_review':failures}))
    return 1 if failures else 0

if __name__ == '__main__':
    try: raise SystemExit(main())
    except (ValueError,OSError,sqlite3.Error,subprocess.SubprocessError):
        print(json.dumps({'status':'needs_review'}),file=sys.stderr)
        raise SystemExit(1)
