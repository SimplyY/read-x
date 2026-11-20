#!/usr/bin/env python3
"""Atomic Read-X dispatch admission primitives; no dispatch or replay side effects.

Only a Bridge entry point that commits admission *before* enqueueing and claims
before starting an agent can safely use these primitives. Legacy admissions
have no such proof and are deliberately not inferred from an intake record.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
import sqlite3
from reading_intake import _open_database

APP = 'cli_aab030a473f8dcd2'


def can_retry_url(state: Path, canonical_url: str, *, connection=None) -> bool:
    """Allow a user resubmission only after a terminated run with no side effects."""
    state = Path(state).absolute()
    if any(part.is_symlink() for part in (state, *state.parents)):
        return False
    owned = connection is None
    if owned:
        database = state / 'intake.sqlite3'
        if not database.is_file() or database.is_symlink():
            return False
        connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'reading_dispatch','intake_message_articles','intake_articles'} <= tables:
            return False
        columns = {row[1] for row in connection.execute('PRAGMA table_info(reading_dispatch)')}
        if not {'finished_at','failure_code'} <= columns or 'score_receipts' not in tables:
            return False
        row = connection.execute('''SELECT d.message_id,d.phase,d.finished_at,d.failure_code
            FROM reading_dispatch d JOIN intake_message_articles ma
              ON ma.app_id=d.app_id AND ma.message_id=d.message_id
            JOIN intake_articles a ON a.id=ma.article_id
            WHERE d.app_id=? AND a.canonical_url=? AND d.phase!='suppressed'
            ORDER BY d.rowid DESC LIMIT 1''',(APP,canonical_url)).fetchone()
        if not row or row[1] != 'failed' or row[3] != 'agent_run_exception' or not row[2]:
            return False
        message_id = row[0]
        if connection.execute(
                'SELECT 1 FROM score_receipts WHERE app_id=? AND message_id=? LIMIT 1',(APP,message_id)).fetchone():
            return False
        recovery_dir = state / 'score-recovery'
        if recovery_dir.is_symlink() or (recovery_dir.exists() and not recovery_dir.is_dir()):
            return False
        spool = recovery_dir / (hashlib.sha256(message_id.encode()).hexdigest() + '.json')
        try:
            spool.lstat()
            return False
        except FileNotFoundError:
            pass
        except OSError:
            return False
        workflow_db = state.parent / 'long-read' / 'workflow.sqlite3'
        if (any(part.is_symlink() for part in (workflow_db, *workflow_db.parents))
                or not workflow_db.is_file()):
            return False
        workflow = sqlite3.connect(workflow_db.resolve().as_uri() + '?mode=ro',uri=True)
        try:
            job = workflow.execute('SELECT 1 FROM jobs WHERE app_id=? AND source_message_id=? LIMIT 1',
                                    (APP,message_id)).fetchone()
        finally:
            workflow.close()
        return job is None
    except (OSError,sqlite3.Error,ValueError):
        return False
    finally:
        if owned and connection is not None:
            connection.close()


def _schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS reading_dispatch (
        app_id TEXT NOT NULL, message_id TEXT NOT NULL, phase TEXT NOT NULL,
        owner TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        started_at TEXT, finished_at TEXT, failure_code TEXT,
        PRIMARY KEY(app_id,message_id))''')
    columns = {row[1] for row in db.execute('PRAGMA table_info(reading_dispatch)')}
    if 'created_at' not in columns:
        db.execute('ALTER TABLE reading_dispatch ADD COLUMN created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP')
    if 'started_at' not in columns:
        db.execute('ALTER TABLE reading_dispatch ADD COLUMN started_at TEXT')
    if 'finished_at' not in columns:
        db.execute('ALTER TABLE reading_dispatch ADD COLUMN finished_at TEXT')
    if 'failure_code' not in columns:
        db.execute('ALTER TABLE reading_dispatch ADD COLUMN failure_code TEXT')
    db.execute('''CREATE TABLE IF NOT EXISTS score_receipts (
        app_id TEXT NOT NULL, message_id TEXT NOT NULL, url TEXT NOT NULL,
        metadata TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
        PRIMARY KEY(app_id,message_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS reading_dispatch_children (
        app_id TEXT NOT NULL, message_id TEXT NOT NULL, article_key TEXT NOT NULL,
        canonical_url TEXT NOT NULL, ordinal INTEGER NOT NULL, chat_id TEXT NOT NULL,
        sender_id TEXT NOT NULL, thread_id TEXT, source_created_ms INTEGER, phase TEXT NOT NULL, owner TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, started_at TEXT, finished_at TEXT,
        PRIMARY KEY(app_id,message_id,article_key))''')
    db.execute('''CREATE TABLE IF NOT EXISTS reading_dispatch_cutover (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), cutover_ms INTEGER NOT NULL)''')


def admit_children_transaction(db, message_id: str, urls: list[str], *, chat_id: str, sender_id: str, thread_id: str | None = None,
                               routing_uncertain: bool = False, source_created_ms: int | None = None) -> None:
    """Insert children in the already-authenticated intake transaction."""
    from reading_article_identity import article_key
    from reading_intake import normalize_url
    children=[]
    for url in urls:
        key=article_key(url,multi=True)
        child=(key,normalize_url(url))
        if child not in children: children.append(child)
    if len(children) < 2: raise ValueError('multi-link event required')
    _schema(db)
    legacy=db.execute('SELECT 1 FROM reading_dispatch WHERE app_id=? AND message_id=?',(APP,message_id)).fetchone()
    for ordinal,(key,url) in enumerate(children):
        known=db.execute('''SELECT 1 FROM intake_message_articles ma JOIN intake_articles a ON a.id=ma.article_id
            WHERE ma.app_id=? AND ma.message_id=? AND a.canonical_url=?''',(APP,message_id,url)).fetchone()
        if not known: raise ValueError('child was not admitted by trusted intake')
        prior=db.execute('''SELECT 1 FROM intake_message_articles ma JOIN intake_articles a ON a.id=ma.article_id
            WHERE ma.app_id=? AND ma.message_id!=? AND a.canonical_url=? LIMIT 1''',(APP,message_id,url)).fetchone()
        phase='needs_review' if legacy or prior or routing_uncertain else 'queued'
        db.execute('''INSERT OR IGNORE INTO reading_dispatch_children(app_id,message_id,article_key,canonical_url,ordinal,chat_id,sender_id,thread_id,source_created_ms,phase)
                      VALUES(?,?,?,?,?,?,?,?,?,?)''',(APP,message_id,key,url,ordinal,chat_id,sender_id,thread_id,source_created_ms,phase))
        actual=db.execute('''SELECT canonical_url,ordinal,chat_id,sender_id,thread_id,source_created_ms FROM reading_dispatch_children
            WHERE app_id=? AND message_id=? AND article_key=?''',(APP,message_id,key)).fetchone()
        if actual[:5] != (url,ordinal,chat_id,sender_id,thread_id): raise ValueError('child authority conflicts')
        if source_created_ms is not None and actual[5] not in (None,source_created_ms): raise ValueError('child source time conflicts')
        if source_created_ms is not None and actual[5] is None:
            db.execute('UPDATE reading_dispatch_children SET source_created_ms=? WHERE app_id=? AND message_id=? AND article_key=?',
                       (source_created_ms,APP,message_id,key))


def admit_children(state: Path, message_id: str, urls: list[str], *, chat_id: str, sender_id: str, thread_id: str | None = None) -> None:
    """Idempotently verify an already admitted event without dispatch."""
    db=_open_database(state)
    try:
        db.execute('BEGIN IMMEDIATE')
        admit_children_transaction(db,message_id,urls,chat_id=chat_id,sender_id=sender_id,thread_id=thread_id)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally: db.close()


def queued_children(state: Path, *, chat_id: str, sender_id: str) -> list[dict]:
    """Return only durable, never-claimed work and the minimum routing fields."""
    db=_open_database(state)
    try:
        _schema(db)
        rows=db.execute('''SELECT message_id,article_key,canonical_url,chat_id,sender_id,thread_id
            FROM reading_dispatch_children WHERE app_id=? AND chat_id=? AND sender_id=? AND phase='queued' ORDER BY rowid,ordinal LIMIT 100''',
            (APP,chat_id,sender_id)).fetchall()
        return [dict(zip(('messageId','articleKey','url','chatId','senderId','threadId'),row)) for row in rows]
    finally: db.close()


def claim_child(state: Path, workflow_db: Path, message_id: str, key: str, owner: str,
                *, allowed_chat: str | None = None, allowed_sender: str | None = None) -> str:
    """Claim a child exactly once. Any unknown prior operation fails closed."""
    from reading_article_identity import identity_from_trusted_intake
    db=_open_database(state)
    try:
        _schema(db)
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT canonical_url,phase,chat_id,sender_id FROM reading_dispatch_children WHERE app_id=? AND message_id=? AND article_key=?',
                       (APP,message_id,key)).fetchone()
        if not row:
            db.rollback(); return 'unmanaged'
        url,phase,chat_id,sender_id=row
        if (allowed_chat is not None and chat_id != allowed_chat) or (allowed_sender is not None and sender_id != allowed_sender):
            raise ValueError('child routing authority mismatch')
        if phase != 'queued':
            db.rollback(); return 'needs_review' if phase == 'running' else phase
        if identity_from_trusted_intake(state,APP,message_id,url) != key:
            raise ValueError('child identity changed')
        if not workflow_db.is_file() or workflow_db.is_symlink():
            db.rollback(); return 'needs_review'
        workflow=sqlite3.connect(workflow_db.resolve().as_uri()+'?mode=ro',uri=True)
        try:
            columns={r[1] for r in workflow.execute('PRAGMA table_info(jobs)')}
            if 'article_key' not in columns:
                db.rollback(); return 'needs_review'
            prior=workflow.execute("SELECT 1 FROM jobs WHERE app_id=? AND source_message_id=? AND article_key IN ('',?) LIMIT 1",
                                   (APP,message_id,key)).fetchone()
        finally: workflow.close()
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        receipt=db.execute('SELECT 1 FROM score_receipts WHERE app_id=? AND message_id=?',(APP,message_id)).fetchone()
        if 'score_receipts_v2' in tables:
            receipt=receipt or db.execute('SELECT 1 FROM score_receipts_v2 WHERE app_id=? AND message_id=? AND article_key=?',
                                          (APP,message_id,key)).fetchone()
        if 'score_send_effects_v2' in tables:
            receipt=receipt or db.execute('SELECT 1 FROM score_send_effects_v2 WHERE app_id=? AND message_id=? AND article_key=?',
                                          (APP,message_id,key)).fetchone()
        spool=state/'score-recovery'/(hashlib.sha256((message_id+':'+key).encode()).hexdigest()+'.json')
        legacy_spool=state/'score-recovery'/(hashlib.sha256(message_id.encode()).hexdigest()+'.json')
        if prior or receipt or spool.exists() or spool.is_symlink() or legacy_spool.exists() or legacy_spool.is_symlink():
            db.execute("UPDATE reading_dispatch_children SET phase='needs_review',finished_at=CURRENT_TIMESTAMP WHERE app_id=? AND message_id=? AND article_key=?",(APP,message_id,key))
            db.commit(); return 'needs_review'
        db.execute("UPDATE reading_dispatch_children SET phase='running',owner=?,started_at=CURRENT_TIMESTAMP WHERE app_id=? AND message_id=? AND article_key=? AND phase='queued'",
                   (owner,APP,message_id,key))
        db.commit(); return 'claimed'
    except Exception:
        db.rollback(); raise
    finally: db.close()


def finish_child(state: Path, message_id: str, key: str, outcome: str) -> str:
    if outcome not in ('failed','needs_review') or not re.fullmatch(r'[0-9a-f]{64}',key):
        raise ValueError('invalid child result')
    db=_open_database(state)
    try:
        _schema(db)
        db.execute('BEGIN IMMEDIATE')
        # A started child can have unknown sends; neither outcome requeues it.
        db.execute("UPDATE reading_dispatch_children SET phase=?,finished_at=CURRENT_TIMESTAMP WHERE app_id=? AND message_id=? AND article_key=? AND phase='running'",
                   (outcome,APP,message_id,key))
        row=db.execute('SELECT phase FROM reading_dispatch_children WHERE app_id=? AND message_id=? AND article_key=?',
                       (APP,message_id,key)).fetchone()
        db.commit()
        return row[0] if row else 'unmanaged'
    except Exception:
        db.rollback(); raise
    finally: db.close()


def reconcile_child(state: Path,workflow_db: Path,archive_journal: Path,message_id: str,key: str) -> str:
    """Mark completion only after this article's score and both packs are verified."""
    db=_open_database(state)
    try:
        _schema(db)
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT phase FROM reading_dispatch_children WHERE app_id=? AND message_id=? AND article_key=?',
                       (APP,message_id,key)).fetchone()
        if not row: db.rollback(); return 'unmanaged'
        if row[0]=='completed': db.rollback(); return 'completed'
        if row[0] not in ('running','needs_review','failed'): db.rollback(); return row[0]
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'score_receipts_v2' not in tables or not workflow_db.is_file() or workflow_db.is_symlink() or not archive_journal.is_file() or archive_journal.is_symlink():
            db.rollback(); return 'needs_review'
        score=db.execute('SELECT metadata,state FROM score_receipts_v2 WHERE app_id=? AND message_id=? AND article_key=?',
                         (APP,message_id,key)).fetchone()
        if not score or score[1]!='synced' or json.loads(score[0]).get('评分状态')!='已评分':
            db.rollback(); return 'needs_review'
        workflow=sqlite3.connect(workflow_db.resolve().as_uri()+'?mode=ro',uri=True)
        try:
            jobs=workflow.execute('SELECT job_id,payload_json FROM jobs WHERE app_id=? AND source_message_id=? AND article_key=?',
                                  (APP,message_id,key)).fetchall()
        finally: workflow.close()
        journal=json.loads(archive_journal.read_text())
        if len(jobs)!=1 or journal.get('schema')!=1:
            db.rollback(); return 'needs_review'
        job_id,payload_json=jobs[0]; payload=json.loads(payload_json)
        archived=journal.get('jobs',{}).get(job_id,{})
        verified=archived.get('verified',{})
        if (payload.get('article_key')!=key or len(payload.get('context_packs',[]))!=2 or
                not re.fullmatch(r'[0-9a-f]{64}',str(payload.get('snapshot_sha256',''))) or
                archived.get('status')!='verified' or archived.get('snapshot')!=payload.get('snapshot_sha256') or
                not all(isinstance(verified.get(name),dict) and verified[name].get('file_token') and
                        re.fullmatch(r'[0-9a-f]{64}',str(verified[name].get('sha256',''))) and
                        isinstance(verified[name].get('bytes'),int) and verified[name]['bytes']>0
                        for name in ('深层解码','芒格洞察'))):
            db.rollback(); return 'needs_review'
        db.execute("UPDATE reading_dispatch_children SET phase='completed',finished_at=CURRENT_TIMESTAMP WHERE app_id=? AND message_id=? AND article_key=?",
                   (APP,message_id,key))
        db.commit(); return 'completed'
    except Exception:
        db.rollback(); raise
    finally: db.close()


def finish_dispatch(state: Path, message_id: str, outcome: str) -> str:
    """Persist a terminal Bridge outcome without storing exception text."""
    if outcome not in ('failed', 'needs_review'):
        raise ValueError('invalid dispatch outcome')
    db = _open_database(state)
    try:
        _schema(db)
        db.execute('BEGIN IMMEDIATE')
        code = 'agent_run_exception' if outcome == 'failed' else 'agent_run_unverified'
        db.execute('''UPDATE reading_dispatch SET phase=?,finished_at=CURRENT_TIMESTAMP,failure_code=?
            WHERE app_id=? AND message_id=? AND phase='running' ''',
            (outcome,code,APP,message_id))
        phase = db.execute('SELECT phase FROM reading_dispatch WHERE app_id=? AND message_id=?',
                           (APP,message_id)).fetchone()
        db.commit()
        return phase[0] if phase else 'unmanaged'
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def admit(state: Path, message_id: str, *, exclusive_intake: bool = False) -> str:
    """Register before enqueue, only after legacy in-flight work has drained.

    The caller must establish that every live and recovery entry point uses
    this guard. An old, possibly running Bridge is not such evidence.
    """
    if exclusive_intake is not True:
        raise ValueError('exclusive managed intake not established')
    db = _open_database(state)
    try:
        _schema(db)
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM intake_messages WHERE app_id=? AND message_id=?',
                          (APP,message_id)).fetchone():
            raise ValueError('trusted intake identity missing')
        row = db.execute('SELECT phase FROM reading_dispatch WHERE app_id=? AND message_id=?',
                         (APP,message_id)).fetchone()
        if not row:
            db.execute("INSERT INTO reading_dispatch(app_id,message_id,phase) VALUES(?,?,'queued')",(APP,message_id))
        db.commit()
        return row[0] if row else 'queued'
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def claim(state: Path, workflow_db: Path, message_id: str, owner: str) -> str:
    """Claim only a managed queued item; running and ambiguous work never replay."""
    if not owner or len(owner) > 160:
        raise ValueError('invalid owner')
    db = _open_database(state)
    try:
        _schema(db)
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT phase FROM reading_dispatch WHERE app_id=? AND message_id=?',
                         (APP,message_id)).fetchone()
        if not row:
            db.rollback()
            return 'unmanaged'
        if row[0] != 'queued':
            db.rollback()
            return 'needs_review' if row[0] == 'running' else row[0]
        if not workflow_db.is_file() or workflow_db.is_symlink():
            db.rollback()
            return 'needs_review'
        source = db.execute('''SELECT a.canonical_url FROM intake_message_articles ma
            JOIN intake_articles a ON a.id=ma.article_id
            WHERE ma.app_id=? AND ma.message_id=?''',(APP,message_id)).fetchall()
        if len(source) != 1:
            db.execute("UPDATE reading_dispatch SET phase='needs_review',finished_at=CURRENT_TIMESTAMP,failure_code='invalid_url_count' WHERE app_id=? AND message_id=?",
                       (APP,message_id))
            db.commit()
            return 'needs_review'
        active = db.execute('''SELECT 1 FROM reading_dispatch d
            JOIN intake_message_articles ma ON ma.app_id=d.app_id AND ma.message_id=d.message_id
            JOIN intake_articles a ON a.id=ma.article_id
            WHERE d.app_id=? AND d.message_id<>? AND a.canonical_url=?
              AND d.phase IN ('queued','running') LIMIT 1''',
            (APP,message_id,source[0][0])).fetchone()
        if active:
            db.execute("UPDATE reading_dispatch SET phase='suppressed',finished_at=CURRENT_TIMESTAMP,failure_code='duplicate_active_url' WHERE app_id=? AND message_id=?",
                       (APP,message_id))
            db.commit()
            return 'suppressed'
        workflow = sqlite3.connect(workflow_db.resolve().as_uri()+'?mode=ro',uri=True)
        try:
            prior = workflow.execute('SELECT 1 FROM jobs WHERE app_id=? AND source_message_id=? LIMIT 1',
                                     (APP,message_id)).fetchone()
        finally:
            workflow.close()
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        receipt = ('score_receipts' in tables and db.execute(
            'SELECT 1 FROM score_receipts WHERE app_id=? AND message_id=?',(APP,message_id)).fetchone())
        spool = state / 'score-recovery' / (hashlib.sha256(message_id.encode()).hexdigest()+'.json')
        if prior or receipt or spool.exists() or spool.is_symlink():
            db.execute("UPDATE reading_dispatch SET phase='needs_review',finished_at=CURRENT_TIMESTAMP,failure_code='prior_side_effect' WHERE app_id=? AND message_id=?",(APP,message_id))
            db.commit()
            return 'needs_review'
        db.execute("UPDATE reading_dispatch SET phase='running',owner=?,started_at=CURRENT_TIMESTAMP,finished_at=NULL,failure_code=NULL WHERE app_id=? AND message_id=? AND phase='queued'",
                   (owner,APP,message_id))
        db.commit()
        return 'claimed'
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def reconcile(state: Path, workflow_db: Path, archive_journal: Path, message_id: str) -> str:
    """Recognize confirmed normal completion, never requeue uncertain attempts."""
    db = _open_database(state)
    try:
        _schema(db)
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT phase FROM reading_dispatch WHERE app_id=? AND message_id=?',
                         (APP,message_id)).fetchone()
        if not row:
            db.rollback()
            return 'unmanaged'
        if row[0] == 'completed':
            db.rollback()
            return 'completed'
        if row[0] not in ('running','needs_review','failed'):
            db.rollback()
            return row[0]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'reading_dispatch_children' in tables:
            children = db.execute('''SELECT phase FROM reading_dispatch_children
                WHERE app_id=? AND message_id=? ORDER BY ordinal''', (APP,message_id)).fetchall()
            if children:
                # Multi-link work is complete only after every independently
                # verified article child is complete. The legacy message-level
                # score receipt is not used for this path.
                if len(children) < 2 or any(child[0] != 'completed' for child in children):
                    db.rollback()
                    return 'needs_review'
                db.execute("UPDATE reading_dispatch SET phase='completed',finished_at=CURRENT_TIMESTAMP,failure_code=NULL WHERE app_id=? AND message_id=? AND phase IN ('running','needs_review','failed')",
                           (APP,message_id))
                db.commit()
                return 'completed'
        score = (db.execute('SELECT metadata,state FROM score_receipts WHERE app_id=? AND message_id=?',
                            (APP,message_id)).fetchone() if 'score_receipts' in tables else None)
        if not score or score[1] != 'synced' or workflow_db.is_symlink() or not workflow_db.is_file():
            db.rollback()
            return 'needs_review'
        score_metadata = json.loads(score[0])
        score_status = score_metadata.get('评分状态')
        workflow = sqlite3.connect(workflow_db.resolve().as_uri()+'?mode=ro',uri=True)
        try:
            jobs = workflow.execute('SELECT job_id,payload_json FROM jobs WHERE app_id=? AND source_message_id=?',
                                    (APP,message_id)).fetchall()
        finally:
            workflow.close()
        if not jobs:
            intake = db.execute('''SELECT a.pack_policy FROM intake_message_articles ma
                JOIN intake_articles a ON a.id=ma.article_id
                WHERE ma.app_id=? AND ma.message_id=?''',(APP,message_id)).fetchone()
            # Explicit score-only is a completed, frozen exception. A normal
            # article without a durable pack job remains unresolved.
            if intake and intake[0] == 'score_only' and score_status in ('已评分','正文不完整','待复核'):
                db.execute("UPDATE reading_dispatch SET phase='completed',finished_at=CURRENT_TIMESTAMP,failure_code=NULL WHERE app_id=? AND message_id=? AND phase IN ('running','needs_review','failed')",
                           (APP,message_id))
                db.commit()
                return 'completed'
            db.rollback()
            return 'needs_review'
        if score_status != '已评分' or archive_journal.is_symlink() or not archive_journal.is_file():
            db.rollback()
            return 'needs_review'
        journal = json.loads(archive_journal.read_text(encoding='utf-8'))
        if len(jobs) != 1 or journal.get('schema') != 1 or not isinstance(journal.get('jobs'),dict):
            db.rollback()
            return 'needs_review'
        job_id,payload_json = jobs[0]
        payload = json.loads(payload_json)
        archived = journal['jobs'].get(job_id,{})
        if (not isinstance(payload.get('context_packs'),list) or len(payload['context_packs']) != 2
                or not payload.get('snapshot_sha256')
                or archived.get('status') != 'verified'
                or archived.get('snapshot') != payload['snapshot_sha256']
                or not all(isinstance(v,dict) and isinstance(v.get('file_token'),str)
                           and re.fullmatch(r'[0-9a-f]{64}',str(v.get('sha256','')))
                           and isinstance(v.get('bytes'),int) and v['bytes'] > 0
                           for v in (archived.get('verified',{}).get(name) for name in ('深层解码','芒格洞察')))):
            db.rollback()
            return 'needs_review'
        db.execute("UPDATE reading_dispatch SET phase='completed',finished_at=CURRENT_TIMESTAMP,failure_code=NULL WHERE app_id=? AND message_id=? AND phase IN ('running','needs_review','failed')",
                   (APP,message_id))
        db.commit()
        return 'completed'
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
