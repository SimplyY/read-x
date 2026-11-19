#!/usr/bin/env python3
"""Periodically recover and synchronize Read-X message intake."""
from pathlib import Path
import subprocess
import sys
import time
import json

ROOT = Path(__file__).resolve().parents[1]


def run_stage(script):
    try:
        result = subprocess.run([sys.executable, str(ROOT / 'scripts' / script), '--execute'],
                                cwd=ROOT, capture_output=True, text=True, timeout=180)
        try:
            payload = json.loads(result.stdout or result.stderr)
        except (json.JSONDecodeError, TypeError):
            payload = {}
        allowed = {'status', 'synced', 'needs_review', 'pending', 'attempted', 'books',
                   'created', 'updated', 'unchanged', 'messages', 'scanned'}
        summary = {key: payload[key] for key in allowed if key in payload}
        if result.returncode != 0:
            summary['status'] = 'needs_review'
        if not summary:
            summary = {'status': 'ok' if result.returncode == 0 else 'needs_review',
                       'exit_code': result.returncode}
        print(f'{script}: {json.dumps(summary, ensure_ascii=False, sort_keys=True)}', flush=True)
    except (OSError, subprocess.TimeoutExpired):
        print(f'{script}: needs_review', flush=True)


def main():
    while True:
        run_stage('backfill_reading_messages.py')
        run_stage('sync_reading_intake.py')
        run_stage('sync_reading_books.py')
        run_stage('archive_pending_context.py')
        run_stage('reading_score_receipt.py')
        run_stage('reconcile_reading_dispatch.py')
        time.sleep(60)


if __name__ == '__main__':
    main()
