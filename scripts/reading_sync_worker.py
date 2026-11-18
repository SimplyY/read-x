#!/usr/bin/env python3
"""Periodically recover and synchronize Read-X message intake."""
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def run_stage(script):
    try:
        result = subprocess.run([sys.executable, str(ROOT / 'scripts' / script), '--execute'],
                                cwd=ROOT, capture_output=True, text=True, timeout=180)
        print(f'{script}: {"ok" if result.returncode == 0 else "needs_review"}', flush=True)
    except (OSError, subprocess.TimeoutExpired):
        print(f'{script}: needs_review', flush=True)


def main():
    while True:
        run_stage('backfill_reading_messages.py')
        run_stage('sync_reading_intake.py')
        time.sleep(60)


if __name__ == '__main__':
    main()
