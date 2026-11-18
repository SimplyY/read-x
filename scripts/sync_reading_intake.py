#!/usr/bin/env python3
"""Synchronize durable Read-X intake records to the reading Base."""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from archive_reading_context import BASE, TABLE, LarkBase, load_journal, save_journal
from reading_intake import list_pending, normalize_url, _open_database


def find_source(base, canonical_url):
    matches = []
    offset = 0
    while True:
        page = base.call('+record-list', '--limit', '200', '--offset', str(offset))
        fields, rows, ids = page['fields'], page['data'], page['record_id_list']
        if len(rows) != len(ids):
            raise ValueError('invalid Base page')
        for record_id, values in zip(ids, rows):
            if len(values) != len(fields):
                raise ValueError('invalid Base row')
            record = dict(zip(fields, values))
            source = record.get('原文链接')
            if not source and record.get('校验清单'):
                try:
                    source = json.loads(record['校验清单']).get('source_url')
                except (TypeError, ValueError):
                    continue
            try:
                same = source and normalize_url(source) == canonical_url
            except ValueError:
                same = False
            if same:
                matches.append({'id': record_id, 'fields': record})
        if not page.get('has_more'):
            return matches
        if not rows:
            raise ValueError('Base pagination stalled')
        offset += len(rows)


def sync_one(item, base, journal, persist):
    key = 'url:' + hashlib.sha256(item['canonicalUrl'].encode()).hexdigest()
    entry = journal['jobs'].setdefault(str(item['id']), {'key': key, 'url': item['canonicalUrl']})
    if entry['key'] != key or entry['url'] != item['canonicalUrl']:
        raise ValueError('intake journal conflict')
    persist()
    matches = find_source(base, item['canonicalUrl'])
    keyed = base.find(key)
    matches = list({record['id']: record for record in matches + keyed}.values())
    if len(matches) > 1:
        raise ValueError('duplicate reading records')
    if not matches:
        if entry.get('create_attempted'):
            raise RuntimeError('record creation outcome unresolved')
        entry['create_attempted'] = True
        persist()
        fields = {'标题': item['originalUrl'], '文章键': key, '原文链接': item['originalUrl'],
                  '阅读状态': '待读', '人工版本': 1, '抓取状态': '待处理',
                  '评分状态': '待处理', '双包状态': '不生成' if item.get('packPolicy') == 'score_only' else '待处理'}
        try:
            base.call('+record-upsert', '--json', json.dumps(fields, ensure_ascii=False))
        except RuntimeError:
            pass
        matches = base.find(key)
        if len(matches) != 1:
            raise RuntimeError('record creation outcome unresolved')
    record = matches[0]
    if entry.get('record_id') not in (None, record['id']):
        raise ValueError('record identity changed')
    entry['record_id'] = record['id']
    entry['status'] = 'verified'
    persist()
    return record['id']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-dir', type=Path, default=Path(__file__).resolve().parents[1] / 'state/reading-workbench')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    state = args.state_dir.absolute()
    pending = list_pending(state)
    if not args.execute:
        print(json.dumps({'status': 'dry_run', 'pending': len(pending)}))
        return 0
    fd = os.open(state / 'sync.lock', os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        journal_path = state / 'sync-journal.json'
        journal = load_journal(journal_path)
        base = LarkBase(BASE, TABLE)
        base.verify_identity('cli_aab030a473f8dcd2')
        complete = 0
        for item in list_pending(state):
            record_id = sync_one(item, base, journal, lambda: save_journal(journal_path, journal))
            conn = _open_database(state)
            try:
                conn.execute("UPDATE intake_articles SET sync_status='synced' WHERE id=? AND sync_status='pending'", (item['id'],))
            finally:
                conn.close()
            complete += 1
        print(json.dumps({'status': 'ok', 'synced': complete}))
        return 0
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        print(json.dumps({'status': 'needs_review', 'reason': str(error)}), file=sys.stderr)
        raise SystemExit(1)
