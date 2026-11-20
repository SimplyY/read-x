#!/usr/bin/env python3
"""Deterministic child identity for articles within a trusted Read-X message.

An empty article key preserves the pre-existing single-URL wire identity.  A
non-empty key is valid only when the caller established that the URL belonged
to the original, authenticated event; this module does not authenticate it.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import sqlite3
import uuid

from reading_intake import normalize_url


def article_key(url: str, *, multi: bool) -> str:
    canonical = normalize_url(url)
    if not multi:
        return ''
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def context_task_id(app_id: str, message_id: str, key: str = '') -> str:
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', app_id or ''):
        raise ValueError('invalid application identity')
    if not re.fullmatch(r'om_[A-Za-z0-9_-]{1,150}', message_id or ''):
        raise ValueError('invalid message identity')
    if key and not re.fullmatch(r'[0-9a-f]{64}', key):
        raise ValueError('invalid article identity')
    name = f'read-x:context:{app_id}:{message_id}'
    if key:
        name += f':article:{key}'
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


def score_request_id(message_id: str, key: str = '') -> str:
    if not re.fullmatch(r'om_[A-Za-z0-9_-]{1,150}', message_id or ''):
        raise ValueError('invalid message identity')
    if key and not re.fullmatch(r'[0-9a-f]{64}', key):
        raise ValueError('invalid article identity')
    if not key:
        return f'readx-score-{message_id}'
    # lark-cli accepts at most 50 characters. 128 digest bits preserve a
    # large collision margin while keeping the complete key at 47 characters.
    return 'readx-score-v2-' + hashlib.sha256(f'{message_id}:{key}'.encode()).hexdigest()[:32]


def identity_from_trusted_intake(state: Path, app_id: str, message_id: str, url: str) -> str:
    """Derive a child key only from a matching persisted source-message URL.

    Intake must already have authenticated the original event. This function
    does not infer membership from model output or permit a synthetic message ID.
    """
    context_task_id(app_id, message_id)
    canonical = normalize_url(url)
    database = Path(state).absolute() / 'intake.sqlite3'
    if any(part.is_symlink() for part in (database, *database.parents)) or not database.is_file():
        raise ValueError('trusted intake unavailable')
    db = sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True)
    try:
        rows = db.execute('''SELECT DISTINCT a.canonical_url FROM intake_message_articles ma
            JOIN intake_articles a ON a.id=ma.article_id
            WHERE ma.app_id=? AND ma.message_id=? AND a.app_id=ma.app_id''',
            (app_id,message_id)).fetchall()
    finally:
        db.close()
    urls = {row[0] for row in rows}
    if canonical not in urls:
        raise ValueError('URL does not belong to trusted message')
    return article_key(canonical, multi=len(urls) > 1)


def verify_child_authority(state: Path, app_id: str, message_id: str, key: str, trigger: dict) -> None:
    """Require the active child and its original authorized routing identity."""
    database=Path(state).absolute()/'intake.sqlite3'
    if any(part.is_symlink() for part in (database,*database.parents)) or not database.is_file():
        raise ValueError('trusted child authority unavailable')
    db=sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        row=db.execute('''SELECT chat_id,sender_id,thread_id,phase FROM reading_dispatch_children
            WHERE app_id=? AND message_id=? AND article_key=?''',(app_id,message_id,key)).fetchone()
    finally: db.close()
    if (not row or row[3]!='running' or trigger.get('appId')!=app_id or
            trigger.get('sourceMessageId')!=message_id or trigger.get('chatType')!='group' or
            trigger.get('senderType')!='user' or trigger.get('chatId')!=row[0] or
            trigger.get('senderId')!=row[1] or (trigger.get('threadId') or None)!=(row[2] or None)):
        raise ValueError('trigger does not match trusted child authority')
