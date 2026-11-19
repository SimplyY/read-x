#!/usr/bin/env python3
"""Return only URL-keyed article dispatch states, never message text or IDs."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from reading_dispatch_guard import can_retry_url

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / 'state/reading-workbench'
APP = 'cli_aab030a473f8dcd2'


def main() -> int:
    database = STATE / 'intake.sqlite3'
    if not database.is_file() or database.is_symlink():
        print(json.dumps({'schemaVersion':1,'status':'ok','articles':{}}))
        return 0
    connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'reading_dispatch' not in tables:
            print(json.dumps({'schemaVersion':1,'status':'ok','articles':{}}))
            return 0
        columns = {row[1] for row in connection.execute('PRAGMA table_info(reading_dispatch)')}
        started = 'd.started_at' if 'started_at' in columns else 'NULL'
        created = 'd.created_at' if 'created_at' in columns else "''"
        rows = connection.execute(f'''SELECT a.canonical_url,d.phase,{created},{started},d.rowid
            FROM reading_dispatch d
            JOIN intake_message_articles ma ON ma.app_id=d.app_id AND ma.message_id=d.message_id
            JOIN intake_articles a ON a.id=ma.article_id
            WHERE d.app_id=? ORDER BY d.rowid''',(APP,)).fetchall()
    finally:
        connection.close()
    states = {}
    now = datetime.now(timezone.utc)
    for url, phase, created_at, started_at, _rowid in rows:
        if phase == 'suppressed':
            continue
        effective = phase
        timestamp = started_at or created_at
        if phase in ('running','queued') and timestamp:
            try:
                when = datetime.fromisoformat(timestamp)
                when = when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when.astimezone(timezone.utc)
                threshold = 30 * 60 if phase == 'running' else 10 * 60
                if (now - when).total_seconds() >= threshold:
                    effective = 'needs_review'
            except ValueError:
                effective = 'needs_review'
        if phase == 'failed' and can_retry_url(STATE, url):
            effective = 'retryable'
        states[url] = effective
    print(json.dumps({'schemaVersion':1,'status':'ok','articles':states},ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
