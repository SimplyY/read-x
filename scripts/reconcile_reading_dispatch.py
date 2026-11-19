#!/usr/bin/env python3
"""Reconcile only dispatches with durable score and archive evidence."""
import json
from pathlib import Path
import sqlite3
import sys

from reading_dispatch_guard import reconcile

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / 'state/reading-workbench'
WORKFLOW = ROOT / 'state/long-read/workflow.sqlite3'
ARCHIVE = ROOT / 'state/long-read/reading-archive-journal.json'
APP = 'cli_aab030a473f8dcd2'


def main() -> int:
    database = STATE / 'intake.sqlite3'
    if not database.is_file() or database.is_symlink():
        print(json.dumps({'status':'ok','completed':0,'pending':0}))
        return 0
    connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'reading_dispatch' not in tables:
            print(json.dumps({'status':'ok','completed':0,'pending':0}))
            return 0
        messages = [row[0] for row in connection.execute(
            "SELECT message_id FROM reading_dispatch WHERE app_id=? AND phase IN ('running','needs_review','failed')", (APP,)
        )]
    finally:
        connection.close()
    completed = 0
    pending = 0
    for message_id in messages:
        try:
            if reconcile(STATE, WORKFLOW, ARCHIVE, message_id) == 'completed':
                completed += 1
            else:
                pending += 1
        except (OSError, ValueError, sqlite3.Error):
            pending += 1
    print(json.dumps({'status':'ok','completed':completed,'pending':pending}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
