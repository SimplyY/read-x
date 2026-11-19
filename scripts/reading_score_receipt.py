#!/usr/bin/env python3
"""Persist and synchronize score metadata produced by Read-X's existing workflow."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
from archive_reading_context import BASE, TABLE, LarkBase
from reading_intake import _open_database, normalize_url

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / 'state/reading-workbench'
APP = 'cli_aab030a473f8dcd2'


def workbench_topic(category, title, confidence):
    if confidence not in ('high', 'medium', 'low'):
        return None
    direct = {
        'AI/技术': 'AI与技术',
        '投资/财经': '投资与商业',
        '商业/产业': '投资与商业',
        '认知/成长': '学习与教育',
    }
    if category in direct:
        return direct[category]
    if category == '人文/生活':
        historical = re.search(
            r'历史|文明|史学|朝代|王朝|帝国|考古|古代|中世纪|文明史|'
            r'\bhistory\b|\bhistorical\b|\bcivilization\b|\bcivilisation\b|'
            r'\bdynasty\b|\bancient\b|\bmedieval\b|\barcheolog',
            str(title or ''), re.IGNORECASE,
        )
        return '历史与文明' if historical else '生活与人文'
    return None


def schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS score_receipts (
      app_id TEXT NOT NULL, message_id TEXT NOT NULL, url TEXT NOT NULL,
      metadata TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
      PRIMARY KEY(app_id,message_id))''')


def prepare_recovery(*, state=STATE, message_id, url, title, author, result, gate_path, scoring_sha):
    """Save recovery data before sending; an actual matching gate is required to replay."""
    if not re.fullmatch(r'om_[A-Za-z0-9_-]{1,150}', message_id or ''):
        raise ValueError('invalid message identity')
    db = _open_database(state)
    db.close()
    directory = state / 'score-recovery'
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise ValueError('unsafe recovery directory')
    payload = {'message_id':message_id,'url':url,'title':title,'author':author,
               'result':{k:result.get(k) for k in ('score_status','quality_score','decision_score',
                         'route','reading_category','reading_category_confidence')},
               'gate_path':str(gate_path.absolute()),'scoring_sha':scoring_sha}
    data = json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    path = directory / (hashlib.sha256(message_id.encode()).hexdigest()+'.json')
    try:
        fd = os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|getattr(os,'O_NOFOLLOW',0),0o600)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != data:
            raise ValueError('recovery receipt conflicts')
        return
    with os.fdopen(fd,'wb') as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    directory_fd = os.open(directory,os.O_RDONLY)
    try: os.fsync(directory_fd)
    finally: os.close(directory_fd)


def recover_receipts(state=STATE):
    directory = state / 'score-recovery'
    if not directory.exists(): return 0
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise ValueError('unsafe recovery directory')
    errors = 0
    for path in directory.glob('*.json'):
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
                raise ValueError('invalid recovery receipt')
            receipt = json.loads(path.read_text())
            gate_path = Path(receipt['gate_path'])
            if gate_path.is_symlink() or not gate_path.is_file() or gate_path.stat().st_size > 16384:
                continue
            gate = json.loads(gate_path.read_text())
            if (gate.get('sent_at_command') is not True or
                    gate.get('idempotency_key') != 'readx-score-'+receipt['message_id'] or
                    gate.get('scoring_result_sha256') != receipt['scoring_sha'] or
                    not str(gate.get('message_id','')).startswith('om_')):
                raise ValueError('recovery gate mismatch')
            record_receipt(state=state,**{k:receipt[k] for k in ('message_id','url','title','author','result')})
            path.unlink()
        except (OSError,ValueError,KeyError,TypeError,sqlite3.Error):
            errors += 1
    return errors


def record_receipt(*, state=STATE, message_id, url, title, author, result):
    """Record only metadata, never source text; duplicate receipts must agree."""
    if not re.fullmatch(r'om_[A-Za-z0-9_-]{1,150}', message_id or ''):
        raise ValueError('invalid message identity')
    url = normalize_url(url)
    status = result.get('score_status')
    if status not in ('scored', 'needs_full_text', 'needs_review'):
        raise ValueError('invalid score state')
    values = {'标题': str(title)[:200], '作者': str(author)[:200],
              '评分状态': {'scored':'已评分', 'needs_full_text':'正文不完整', 'needs_review':'待复核'}[status],
              }
    # A scoring review can be caused by scoring evidence or calibration even
    # when the fetched source is complete. Only source-derived score states may
    # update the independent fetch stage.
    if status == 'scored':
        values['抓取状态'] = '已完成'
    elif status == 'needs_full_text':
        values['抓取状态'] = '正文不完整'
    if status == 'scored':
        for source, target in [('quality_score','质量分'), ('decision_score','决策分')]:
            value = result.get(source)
            maximum = 10 if source == 'quality_score' else 20
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= maximum:
                raise ValueError('invalid score value')
            values[target] = value
        category = workbench_topic(
            result.get('reading_category'), title, result.get('reading_category_confidence'))
        if category:
            values['自动主题'] = category
    encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    route = result.get('route')
    if route in ('card', 'long_read'):
        values['_route'] = route
        encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    db = _open_database(state)
    try:
        schema(db)
        db.execute('BEGIN IMMEDIATE')
        prior = db.execute('SELECT url,metadata FROM score_receipts WHERE app_id=? AND message_id=?', (APP,message_id)).fetchone()
        if prior and prior != (url,encoded):
            raise ValueError('score receipt conflicts with earlier result')
        db.execute('INSERT OR IGNORE INTO score_receipts(app_id,message_id,url,metadata) VALUES(?,?,?,?)',
                   (APP,message_id,url,encoded))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def sync_receipts(state=STATE):
    recovery_errors = recover_receipts(state)
    db = _open_database(state)
    schema(db)
    fd = os.open(ROOT / 'state/long-read/reading-archive.lock', os.O_CREAT | os.O_RDWR | getattr(os,'O_NOFOLLOW',0), 0o600)
    synced, problems = 0, recovery_errors
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        base = LarkBase(BASE,TABLE)
        base.verify_identity(APP)
        rows = db.execute('''SELECT sr.message_id,sr.url,sr.metadata FROM score_receipts sr
            WHERE sr.state='pending' AND EXISTS (SELECT 1 FROM intake_message_articles ma
            JOIN intake_articles a ON a.id=ma.article_id WHERE ma.app_id=sr.app_id
            AND ma.message_id=sr.message_id AND a.canonical_url=sr.url)
            ORDER BY RANDOM() LIMIT 100''').fetchall()
        for message_id,url,encoded in rows:
            newer = db.execute('''SELECT 1 FROM score_receipts earlier JOIN score_receipts later
                ON later.app_id=earlier.app_id AND later.url=earlier.url AND later.rowid>earlier.rowid
                WHERE earlier.app_id=? AND earlier.message_id=? AND later.state='synced' LIMIT 1''',
                (APP,message_id)).fetchone()
            if newer:
                db.execute("UPDATE score_receipts SET state='superseded' WHERE app_id=? AND message_id=?", (APP,message_id))
                continue
            # A score-card command alone does not establish user ownership.
            association = db.execute('''SELECT 1 FROM intake_messages m JOIN intake_message_articles ma
                ON ma.app_id=m.app_id AND ma.message_id=m.message_id JOIN intake_articles a ON a.id=ma.article_id
                WHERE m.app_id=? AND m.message_id=? AND a.canonical_url=?''', (APP,message_id,url)).fetchone()
            if not association:
                continue
            try:
                matches = base.find_source(url)
                if len(matches) != 1:
                    raise ValueError('article record absent or ambiguous')
                record = matches[0]
                metadata = json.loads(encoded)
                fields = {key:value for key,value in metadata.items() if not key.startswith('_')}
                # Do not overwrite an established title or author with stale data.
                if record['fields'].get('标题') not in (None, '', url): fields.pop('标题',None)
                if record['fields'].get('作者'): fields.pop('作者',None)
                if fields:
                    try:
                        base.call('+record-upsert','--record-id',record['id'],'--json',json.dumps(fields,ensure_ascii=False))
                    except RuntimeError:
                        pass  # A readback resolves an uncertain write.
                    check = base.find_source(url)
                    if len(check) != 1 or check[0]['id'] != record['id'] or any(check[0]['fields'].get(k) != v for k,v in fields.items()):
                        raise RuntimeError('score update not confirmed')
                db.execute("UPDATE score_receipts SET state='synced' WHERE app_id=? AND message_id=?",(APP,message_id))
                synced += 1
            except (ValueError, RuntimeError, OSError):
                problems += 1
        return synced, problems
    finally:
        fcntl.flock(fd,fcntl.LOCK_UN)
        os.close(fd)
        db.close()


if __name__ == '__main__':
    try:
        count, errors = sync_receipts()
        print(json.dumps({'synced':count,'needs_review':errors}))
        raise SystemExit(1 if errors else 0)
    except (OSError,ValueError,sqlite3.Error):
        print(json.dumps({'status':'needs_review'}),file=sys.stderr)
        raise SystemExit(1)
