#!/usr/bin/env python3
"""Sync Learn-X book metadata into the book index table without copying content."""
from __future__ import annotations

import argparse
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
LEARNX_DB = Path('/Users/yuwei/code/learn-x/.runtime/reading/workbench/weread.sqlite3')
BASE = 'HZLvb3lnUaH3z3sn3plcSRg0nPc'
TABLE = 'tblDwjpNOH6hWcUo'
APP = 'cli_aab030a473f8dcd2'
STATE = ROOT / 'state/reading-workbench'
JOURNAL = STATE / 'book-sync-journal.json'
SHANGHAI = ZoneInfo('Asia/Shanghai')
FIELDS = ['记录类型', '书名', '来源书籍ID', '作者', '最近阅读时间', '划线条数', '个人想法条数']


def read_books(database: Path = LEARNX_DB) -> list[dict]:
    if database.is_symlink() or not database.is_file():
        raise ValueError('Learn-X reading store unavailable')
    db = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        rows = db.execute('''SELECT b.book_id,b.title,b.author,MAX(e.date),
            SUM(CASE WHEN e.kind='highlight' THEN 1 ELSE 0 END),
            SUM(CASE WHEN e.kind='thought' THEN 1 ELSE 0 END)
            FROM books b LEFT JOIN entries e ON e.book_id=b.book_id
            GROUP BY b.book_id,b.title,b.author ORDER BY b.book_id''').fetchall()
    finally:
        db.close()
    books = []
    for book_id, title, author, latest, highlights, thoughts in rows:
        if not isinstance(book_id, str) or not book_id or len(book_id) > 200:
            raise ValueError('invalid stable book identity')
        recent_ms = None
        if latest:
            recent_ms = int(datetime.fromisoformat(latest).replace(tzinfo=SHANGHAI).timestamp() * 1000)
        books.append({'id': book_id, 'fields': {
            '记录类型': '书籍', '书名': str(title or '未命名书籍')[:200],
            '来源书籍ID': book_id, '作者': str(author or '')[:200],
            '最近阅读时间': recent_ms, '划线条数': int(highlights or 0),
            '个人想法条数': int(thoughts or 0),
        }})
    return books


def load_journal() -> dict:
    if STATE.is_symlink():
        raise ValueError('unsafe book state directory')
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE, 0o700)
    if JOURNAL.is_symlink():
        raise ValueError('unsafe book journal')
    if not JOURNAL.exists():
        return {'schema': 1, 'books': {}}
    if not JOURNAL.is_file() or JOURNAL.stat().st_mode & 0o077:
        raise ValueError('unsafe book journal')
    value = json.loads(JOURNAL.read_text(encoding='utf-8'))
    if value.get('schema') != 1 or not isinstance(value.get('books'), dict):
        raise ValueError('invalid book journal')
    return value


def save_journal(journal: dict) -> None:
    data = json.dumps(journal, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    fd, name = tempfile.mkstemp(prefix='.book-sync-', dir=STATE)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, JOURNAL)
        directory = os.open(STATE, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class BaseError(RuntimeError):
    def __init__(self, code: int | None):
        super().__init__('Base operation failed or unknown')
        self.code = code


def cli(args: list[str]) -> dict:
    try:
        result = subprocess.run(['lark-cli', *args, '--format', 'json'], capture_output=True,
                                text=True, timeout=90, check=False)
        response = json.loads(result.stdout or result.stderr)
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as error:
        raise RuntimeError('Base result unknown') from error
    if result.returncode or response.get('ok') is not True:
        detail = response.get('error')
        code = detail.get('code') if isinstance(detail, dict) else None
        raise BaseError(code if isinstance(code, int) else None)
    return response['data']


def verify_identity() -> None:
    try:
        result = subprocess.run(['lark-cli', 'auth', 'status', '--json', '--verify'],
                                capture_output=True, text=True, timeout=30, check=False)
        response = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as error:
        raise RuntimeError('cannot verify Base service identity') from error
    bot = response.get('identities', {}).get('bot', {})
    if result.returncode or response.get('appId') != APP or bot.get('verified') is not True:
        raise ValueError('Base service identity mismatch')


def list_records() -> list[dict]:
    common = ['--base-token', BASE, '--table-id', TABLE, '--as', 'bot']
    records, offset = [], 0
    while True:
        page = cli(['base', '+record-list', *common, '--limit', '200', '--offset', str(offset),
                    *[item for name in FIELDS for item in ('--field-id', name)]])
        names, values, ids = page.get('fields'), page.get('data'), page.get('record_id_list')
        if not isinstance(names, list) or not isinstance(values, list) or not isinstance(ids, list) or len(values) != len(ids):
            raise ValueError('invalid book Base page')
        if any(name not in names for name in FIELDS):
            raise ValueError('book table fields incomplete')
        for record_id, row in zip(ids, values):
            if len(row) != len(names):
                raise ValueError('invalid book Base row')
            records.append({'id': record_id, 'fields': dict(zip(names, row))})
        if not page.get('has_more'):
            return records
        if not values:
            raise ValueError('book Base pagination stalled')
        offset += len(values)


def matches_value(actual, expected, name: str) -> bool:
    if name == '最近阅读时间':
        if expected is None:
            return actual in (None, '')
        if isinstance(actual, (int, float)) and not isinstance(actual, bool):
            return abs(actual - expected) < 1000
        if isinstance(actual, str):
            try:
                return abs(datetime.fromisoformat(actual).timestamp() * 1000 - expected) < 1000
            except ValueError:
                return False
        return False
    return actual == expected


def run(*, execute: bool) -> dict:
    books = read_books()
    archive_lock = ROOT / 'state/long-read/reading-archive.lock'
    fd = os.open(archive_lock, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        records = list_records() if execute else []
        journal = load_journal()
        creates = updates = unchanged = review = 0
        if execute:
            verify_identity()
        else:
            return {'status': 'dry_run', 'books': len(books)}
        for book in books:
            book_id, fields = book['id'], book['fields']
            entry = journal['books'].setdefault(hashlib.sha256(book_id.encode()).hexdigest(), {'book_id': book_id})
            if entry.get('book_id') != book_id:
                raise ValueError('book journal identity conflict')
            matches = [item for item in records if item['fields'].get('来源书籍ID') == book_id]
            if len(matches) > 1:
                review += 1
                continue
            if not matches:
                if entry.get('create_attempted') and entry.get('create_error_code') != 1254037:
                    review += 1
                    continue
                if entry.get('create_error_code') == 1254037:
                    # The previously used non-UUID token was explicitly rejected
                    # by Feishu; no create side effect occurred.
                    entry.pop('create_attempted', None)
                    entry.pop('create_error_code', None)
                token = entry.get('client_token')
                if not token:
                    token = str(uuid.uuid4())
                    entry['client_token'] = token
                entry['create_attempted'] = True
                save_journal(journal)
                path = f'/open-apis/bitable/v1/apps/{BASE}/tables/{TABLE}/records'
                try:
                    cli(['api', 'POST', path, '--params', json.dumps({'client_token': token}),
                         '--data', json.dumps({'fields': fields}, ensure_ascii=False), '--as', 'bot'])
                except BaseError as error:
                    if error.code == 1254037:
                        entry['create_error_code'] = error.code
                        save_journal(journal)
                    # Unknown create results remain guarded by the same token.
                except RuntimeError:
                    pass
                records = list_records()
                matches = [item for item in records if item['fields'].get('来源书籍ID') == book_id]
                if len(matches) != 1:
                    review += 1
                    continue
                creates += 1
            record = matches[0]
            if entry.get('record_id') not in (None, record['id']):
                review += 1
                continue
            entry['record_id'] = record['id']
            changed = {name: value for name, value in fields.items()
                       if not matches_value(record['fields'].get(name), value, name)}
            if changed:
                try:
                    cli(['base', '+record-upsert', '--base-token', BASE, '--table-id', TABLE,
                         '--record-id', record['id'], '--json', json.dumps(changed, ensure_ascii=False), '--as', 'bot'])
                except RuntimeError:
                    pass
                records = list_records()
                matches = [item for item in records if item['id'] == record['id']]
                if len(matches) != 1 or any(not matches_value(matches[0]['fields'].get(name), value, name)
                                             for name, value in changed.items()):
                    review += 1
                    continue
                updates += 1
            else:
                unchanged += 1
            save_journal(journal)
        return {'status': 'needs_review' if review else 'ok', 'books': len(books),
                'created': creates, 'updated': updates, 'unchanged': unchanged, 'needs_review': review}
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    try:
        result = run(execute=args.execute)
        print(json.dumps(result))
        raise SystemExit(1 if result['status'] == 'needs_review' else 0)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, json.JSONDecodeError) as error:
        print(json.dumps({'status': 'needs_review', 'reason': str(error)}), file=sys.stderr)
        raise SystemExit(1)
