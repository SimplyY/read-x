#!/usr/bin/env python3
"""Return only URL-keyed article dispatch states, never message text or IDs."""
from __future__ import annotations
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from reading_dispatch_guard import can_retry_url

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / 'state/reading-workbench'
APP = 'cli_aab030a473f8dcd2'


def read_states(state: Path = STATE, *, now: datetime | None = None) -> dict:
    database = state / 'intake.sqlite3'
    if not database.is_file() or database.is_symlink():
        return {}
    connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rows=[]
        if 'reading_dispatch' in tables:
            columns = {row[1] for row in connection.execute('PRAGMA table_info(reading_dispatch)')}
            started = 'd.started_at' if 'started_at' in columns else 'NULL'
            created = 'd.created_at' if 'created_at' in columns else 'NULL'
            child_exclusion = ('AND NOT EXISTS (SELECT 1 FROM reading_dispatch_children child '
                               'WHERE child.app_id=d.app_id AND child.message_id=d.message_id)'
                               if 'reading_dispatch_children' in tables else '')
            rows += connection.execute(f'''SELECT a.canonical_url,d.phase,{created},{started},m.rowid,0,NULL
                FROM reading_dispatch d
                JOIN intake_messages m ON m.app_id=d.app_id AND m.message_id=d.message_id
                JOIN intake_message_articles ma ON ma.app_id=d.app_id AND ma.message_id=d.message_id
                JOIN intake_articles a ON a.id=ma.article_id
                WHERE d.app_id=? {child_exclusion}''',(APP,)).fetchall()
        if 'reading_dispatch_children' in tables:
            columns = {row[1] for row in connection.execute('PRAGMA table_info(reading_dispatch_children)')}
            started = 'd.started_at' if 'started_at' in columns else 'NULL'
            created = 'd.created_at' if 'created_at' in columns else 'NULL'
            source_time = 'd.source_created_ms' if 'source_created_ms' in columns else 'NULL'
            rows += connection.execute(f'''SELECT d.canonical_url,d.phase,{created},{started},m.rowid,1,{source_time}
                FROM reading_dispatch_children d
                JOIN intake_messages m ON m.app_id=d.app_id AND m.message_id=d.message_id
                WHERE d.app_id=?''',(APP,)).fetchall()
    finally:
        connection.close()
    candidates = {}
    now = now or datetime.now(timezone.utc)
    for url, phase, created_at, started_at, event_order, kind, source_time in rows:
        if phase == 'suppressed':
            continue
        effective = phase
        timestamp = started_at or created_at
        if phase in ('running','queued'):
            try:
                if not isinstance(timestamp,str): raise ValueError('missing timestamp')
                when = datetime.fromisoformat(timestamp)
                when = when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when.astimezone(timezone.utc)
                threshold = 30 * 60 if phase == 'running' else 10 * 60
                if (now - when).total_seconds() >= threshold:
                    effective = 'needs_review'
            except ValueError:
                effective = 'needs_review'
        if kind == 0 and phase == 'failed' and can_retry_url(state, url):
            effective = 'retryable'
        candidates.setdefault(url,[]).append((source_time,event_order,effective))
    states={}
    for url,items in candidates.items():
        if len(items)==1:
            states[url]=items[0][2]
        elif all(isinstance(item[0],int) for item in items):
            states[url]=max(items,key=lambda item:(item[0],item[1]))[2]
        elif len({item[2] for item in items})==1:
            states[url]=items[0][2]
        else:
            # Old receipts contain no original source timestamp; registration
            # order can be inverted by backfill and cannot prove recency.
            states[url]='needs_review'
    return states


def main() -> int:
    print(json.dumps({'schemaVersion':1,'status':'ok','articles':read_states()},ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
