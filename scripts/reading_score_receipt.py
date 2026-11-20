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
from reading_article_identity import score_request_id, identity_from_trusted_intake

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
    db.execute('''CREATE TABLE IF NOT EXISTS score_receipts_v2 (
      app_id TEXT NOT NULL, message_id TEXT NOT NULL, article_key TEXT NOT NULL,
      url TEXT NOT NULL, metadata TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
      PRIMARY KEY(app_id,message_id,article_key))''')
    db.execute('''CREATE TABLE IF NOT EXISTS score_send_effects_v2 (
      app_id TEXT NOT NULL, message_id TEXT NOT NULL, article_key TEXT NOT NULL,
      request_id TEXT NOT NULL, scoring_sha TEXT NOT NULL, card_sha TEXT NOT NULL,
      target_type TEXT NOT NULL, target_id TEXT NOT NULL, status TEXT NOT NULL,
      PRIMARY KEY(app_id,message_id,article_key))''')


def begin_score_send(*, state=STATE,message_id,url,article_key,scoring_sha,request_id,card_sha,target_type,target_id):
    if identity_from_trusted_intake(state,APP,message_id,url) != article_key or not article_key:
        raise ValueError('invalid article identity')
    if (request_id != score_request_id(message_id,article_key) or not re.fullmatch(r'[0-9a-f]{64}',scoring_sha)
            or not re.fullmatch(r'[0-9a-f]{64}',card_sha) or target_type not in ('chat_id','user_id')
            or not isinstance(target_id,str) or not target_id):
        raise ValueError('invalid score send identity')
    db=_open_database(state)
    try:
        schema(db)
        columns={row[1] for row in db.execute('PRAGMA table_info(score_send_effects_v2)')}
        if not {'card_sha','target_type','target_id'} <= columns:
            raise ValueError('offline score effect schema migration required')
        db.execute('BEGIN IMMEDIATE')
        authority=db.execute('''SELECT chat_id,sender_id,phase FROM reading_dispatch_children
            WHERE app_id=? AND message_id=? AND article_key=?''',(APP,message_id,article_key)).fetchone()
        if not authority or authority[2] != 'running' or target_type != 'user_id' or target_id != authority[1]:
            raise ValueError('score target does not match trusted source')
        if db.execute('SELECT 1 FROM score_receipts WHERE app_id=? AND message_id=?',(APP,message_id)).fetchone():
            raise ValueError('legacy message score requires reconciliation')
        legacy_spool=state/'score-recovery'/(hashlib.sha256(message_id.encode()).hexdigest()+'.json')
        if legacy_spool.exists() or legacy_spool.is_symlink():
            raise ValueError('legacy message score requires reconciliation')
        prior=db.execute('SELECT status FROM score_send_effects_v2 WHERE app_id=? AND message_id=? AND article_key=?',
                         (APP,message_id,article_key)).fetchone()
        if prior:
            raise ValueError('score send already attempted; reconcile before retry')
        db.execute("INSERT INTO score_send_effects_v2(app_id,message_id,article_key,request_id,scoring_sha,card_sha,target_type,target_id,status) VALUES (?,?,?,?,?,?,?,?,'attempting')",
                   (APP,message_id,article_key,request_id,scoring_sha,card_sha,target_type,target_id))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally: db.close()


def confirm_score_send(*,state=STATE,message_id,article_key,evidence):
    if (evidence.get('sent_at_command') is not True or
            evidence.get('idempotency_key') != score_request_id(message_id,article_key) or
            not str(evidence.get('message_id','')).startswith('om_')):
        raise ValueError('invalid score send evidence')
    db=_open_database(state)
    try:
        schema(db)
        row=db.execute('SELECT target_type,target_id,status,request_id,scoring_sha,card_sha FROM score_send_effects_v2 WHERE app_id=? AND message_id=? AND article_key=?',
                       (APP,message_id,article_key)).fetchone()
        if not row or evidence.get(row[0]) != row[1]: raise ValueError('score target conflicts')
        if row[2] == 'confirmed' and (row[3],row[4],row[5]) == (evidence['idempotency_key'],evidence.get('scoring_result_sha256'),evidence.get('card_sha256')):
            return
        cursor=db.execute("UPDATE score_send_effects_v2 SET status='confirmed' WHERE app_id=? AND message_id=? AND article_key=? AND request_id=? AND scoring_sha=? AND card_sha=? AND status='attempting'",
                          (APP,message_id,article_key,evidence['idempotency_key'],evidence.get('scoring_result_sha256'),evidence.get('card_sha256')))
        if cursor.rowcount != 1: raise ValueError('score send evidence conflicts')
    finally: db.close()


def prepare_recovery(*, state=STATE, message_id, url, title, author, result, gate_path, scoring_sha, article_key=''):
    """Save recovery data before sending; an actual matching gate is required to replay."""
    if not re.fullmatch(r'om_[A-Za-z0-9_-]{1,150}', message_id or ''):
        raise ValueError('invalid message identity')
    if article_key and identity_from_trusted_intake(state,APP,message_id,url) != article_key:
        raise ValueError('invalid article identity')
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
    if article_key:
        payload['article_key'] = article_key
    data = json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    identity = message_id + (':'+article_key if article_key else '')
    path = directory / (hashlib.sha256(identity.encode()).hexdigest()+'.json')
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
            article_key = receipt.get('article_key','')
            if (gate.get('sent_at_command') is not True or
                    gate.get('idempotency_key') != score_request_id(receipt['message_id'],article_key) or
                    gate.get('scoring_result_sha256') != receipt['scoring_sha'] or
                    not str(gate.get('message_id','')).startswith('om_')):
                raise ValueError('recovery gate mismatch')
            if article_key:
                confirm_score_send(state=state,message_id=receipt['message_id'],article_key=article_key,evidence=gate)
            record_receipt(state=state,article_key=article_key,**{k:receipt[k] for k in ('message_id','url','title','author','result')})
            path.unlink()
        except (OSError,ValueError,KeyError,TypeError,sqlite3.Error):
            errors += 1
    return errors


def record_receipt(*, state=STATE, message_id, url, title, author, result, article_key=''):
    """Record only metadata, never source text; duplicate receipts must agree."""
    if not re.fullmatch(r'om_[A-Za-z0-9_-]{1,150}', message_id or ''):
        raise ValueError('invalid message identity')
    url = normalize_url(url)
    if article_key and identity_from_trusted_intake(state,APP,message_id,url) != article_key:
        raise ValueError('invalid article identity')
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
        if article_key:
            prior = db.execute('SELECT url,metadata FROM score_receipts_v2 WHERE app_id=? AND message_id=? AND article_key=?', (APP,message_id,article_key)).fetchone()
        else:
            prior = db.execute('SELECT url,metadata FROM score_receipts WHERE app_id=? AND message_id=?', (APP,message_id)).fetchone()
        if prior and prior != (url,encoded):
            raise ValueError('score receipt conflicts with earlier result')
        if article_key:
            db.execute('INSERT OR IGNORE INTO score_receipts_v2(app_id,message_id,article_key,url,metadata) VALUES(?,?,?,?,?)',
                       (APP,message_id,article_key,url,encoded))
        else:
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
                # Do not write blank metadata or overwrite an established title
                # or author with stale data. Base may read an unset field as
                # None, which must not make an empty source author look mismatched.
                if not fields.get('标题') or record['fields'].get('标题') not in (None, '', url):
                    fields.pop('标题',None)
                if not fields.get('作者') or record['fields'].get('作者'):
                    fields.pop('作者',None)
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
        rows_v2 = db.execute('''SELECT sr.message_id,sr.article_key,sr.url,sr.metadata FROM score_receipts_v2 sr
            WHERE sr.state='pending' AND EXISTS (SELECT 1 FROM intake_message_articles ma
            JOIN intake_articles a ON a.id=ma.article_id WHERE ma.app_id=sr.app_id
            AND ma.message_id=sr.message_id AND a.canonical_url=sr.url)
            ORDER BY RANDOM() LIMIT 100''').fetchall()
        for message_id,article_key,url,encoded in rows_v2:
            try:
                if identity_from_trusted_intake(state,APP,message_id,url) != article_key:
                    raise ValueError('article identity changed')
                # Across the legacy and v2 tables ordering is not comparable;
                # do not let an old receipt overwrite a newer score.
                if (db.execute('SELECT 1 FROM score_receipts WHERE app_id=? AND url=? LIMIT 1',(APP,url)).fetchone()
                        or db.execute('SELECT 1 FROM score_receipts_v2 WHERE app_id=? AND url=? AND (message_id!=? OR article_key!=?) LIMIT 1',
                                      (APP,url,message_id,article_key)).fetchone()):
                    raise ValueError('multiple score histories require reconciliation')
                matches=base.find_source(url)
                if len(matches) != 1:
                    raise ValueError('article record absent or ambiguous')
                record=matches[0]
                fields={k:v for k,v in json.loads(encoded).items() if not k.startswith('_')}
                if not fields.get('标题') or record['fields'].get('标题') not in (None,'',url):
                    fields.pop('标题',None)
                if not fields.get('作者') or record['fields'].get('作者'):
                    fields.pop('作者',None)
                if fields:
                    try:
                        base.call('+record-upsert','--record-id',record['id'],'--json',json.dumps(fields,ensure_ascii=False))
                    except RuntimeError:
                        pass
                    check=base.find_source(url)
                    if len(check)!=1 or check[0]['id']!=record['id'] or any(check[0]['fields'].get(k)!=v for k,v in fields.items()):
                        raise RuntimeError('score update not confirmed')
                db.execute("UPDATE score_receipts_v2 SET state='synced' WHERE app_id=? AND message_id=? AND article_key=?",
                           (APP,message_id,article_key))
                synced += 1
            except (ValueError,RuntimeError,OSError):
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
