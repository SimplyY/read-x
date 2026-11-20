#!/usr/bin/env python3
"""Durable intake for already authenticated Read-X bridge events.

The caller must apply the Bridge access policy before passing a single event.
No network calls or Base writes are performed here.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
from urllib.parse import urlsplit, urlunsplit


ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,160}$")


def normalize_url(value: str) -> str:
    """Remove only the fragment and normalize the origin, preserving query data."""
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 for c in value):
        raise ValueError("invalid URL")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise ValueError("invalid URL") from exc
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("invalid URL")
    if any(c.isspace() for c in value) or "\\" in value:
        raise ValueError("invalid URL")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if port is not None:
        host += f":{port}"
    return urlunsplit((parts.scheme.lower(), host, parts.path or "/", parts.query, ""))


def _id(value: object, prefix: str) -> str:
    if not isinstance(value, str) or not value.startswith(prefix) or len(value) <= len(prefix) or not ID_PATTERN.fullmatch(value):
        raise ValueError("invalid event identity")
    return value


def _open_database(state_dir: Path) -> sqlite3.Connection:
    state_dir = state_dir.absolute()
    # Reject symlinks throughout the path, including a pre-existing database.
    for part in reversed((state_dir, *state_dir.parents)):
        if part.is_symlink():
            raise ValueError("state path contains a symlink")
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = stat.S_IMODE(state_dir.stat().st_mode)
    if mode & 0o077:
        raise ValueError("state directory must be private")
    db_path = state_dir / "intake.sqlite3"
    if db_path.is_symlink():
        raise ValueError("database is a symlink")
    try:
        fd = os.open(db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if not db_path.is_file() or stat.S_IMODE(db_path.stat().st_mode) & 0o077:
            raise ValueError("database must be a private file")
    else:
        os.close(fd)
    connection = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS intake_messages (
            app_id TEXT NOT NULL, message_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL, received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (app_id, message_id)
        );
        CREATE TABLE IF NOT EXISTS intake_articles (
            id INTEGER PRIMARY KEY, app_id TEXT NOT NULL, canonical_url TEXT NOT NULL,
            original_url TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            sync_status TEXT NOT NULL DEFAULT 'pending',
            UNIQUE (app_id, canonical_url)
        );
        CREATE TABLE IF NOT EXISTS intake_message_articles (
            app_id TEXT NOT NULL, message_id TEXT NOT NULL, article_id INTEGER NOT NULL,
            PRIMARY KEY (app_id, message_id, article_id),
            FOREIGN KEY (app_id, message_id) REFERENCES intake_messages(app_id, message_id),
            FOREIGN KEY (article_id) REFERENCES intake_articles(id)
        );
    """)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(intake_articles)")}
    if "pack_policy" not in columns:
        try:
            connection.execute("ALTER TABLE intake_articles ADD COLUMN pack_policy TEXT NOT NULL DEFAULT 'normal'")
        except sqlite3.OperationalError:
            if "pack_policy" not in {row[1] for row in connection.execute("PRAGMA table_info(intake_articles)")}:
                raise
    return connection


def register_event(state_dir: Path, event: dict, *, allowed_app_id: str,
                   allowed_chat_id: str, allowed_sender_id: str,
                   live_dispatch: bool = False) -> dict:
    """Persist the URLs from one trusted event, atomically and idempotently.

    Event fields are appId, chatId, chatType, senderId, senderType, messageId,
    and urls. The caller supplies the verified identities separately from event
    data. A repeated message with changed content is rejected.
    """
    if not isinstance(event, dict):
        raise ValueError("invalid event")
    app_id = _id(event.get("appId"), "cli_")
    chat_id = _id(event.get("chatId"), "oc_")
    sender_id = _id(event.get("senderId"), "ou_")
    message_id = _id(event.get("messageId"), "om_")
    if (app_id != allowed_app_id or chat_id != allowed_chat_id or sender_id != allowed_sender_id
            or event.get("chatType") not in {"group", "topic"} or event.get("senderType") != "user"):
        raise ValueError("event not authorized")
    urls = event.get("urls")
    if not isinstance(urls, list) or not urls or len(urls) > 100:
        raise ValueError("event must contain URLs")
    normalized = [(normalize_url(url), url) for url in urls]
    source_created = event.get("sourceCreatedAt")
    source_created_ms = (int(source_created) if isinstance(source_created, str)
                         and re.fullmatch(r"[0-9]{13}", source_created) else None)
    source_created_at = None
    if source_created_ms is not None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        if 946684800000 <= source_created_ms <= now_ms + 86400000:
            source_created_at = datetime.fromtimestamp(source_created_ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    score_only = event.get("scoreOnly") is True
    fingerprint = hashlib.sha256(json.dumps({
        "appId": app_id, "chatId": chat_id, "chatType": event["chatType"],
        "senderId": sender_id, "senderType": event["senderType"],
        "urls": normalized,
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    connection = _open_database(Path(state_dir))
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT fingerprint FROM intake_messages WHERE app_id=? AND message_id=?",
            (app_id, message_id),
        ).fetchone()
        message_was_registered = existing is not None
        if existing:
            if existing[0] != fingerprint:
                raise ValueError("message replay differs from original")
        else:
            connection.execute("INSERT INTO intake_messages(app_id,message_id,fingerprint) VALUES(?,?,?)",
                               (app_id, message_id, fingerprint))
        created = 0
        policy_upgraded = False
        for canonical, original in normalized:
            prior = connection.execute(
                "SELECT pack_policy FROM intake_articles WHERE app_id=? AND canonical_url=?",
                (app_id, canonical),
            ).fetchone()
            if source_created_at:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO intake_articles(app_id,canonical_url,original_url,created_at,pack_policy) VALUES(?,?,?,?,?)",
                    (app_id, canonical, original, source_created_at, "score_only" if score_only else "normal"),
                )
            else:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO intake_articles(app_id,canonical_url,original_url,pack_policy) VALUES(?,?,?,?)",
                    (app_id, canonical, original, "score_only" if score_only else "normal"),
                )
            created += cursor.rowcount
            if not score_only:
                policy_upgraded = policy_upgraded or (prior is not None and prior[0] == "score_only")
                connection.execute("UPDATE intake_articles SET pack_policy='normal' WHERE app_id=? AND canonical_url=?", (app_id, canonical))
            article_id = connection.execute(
                "SELECT id FROM intake_articles WHERE app_id=? AND canonical_url=?",
                (app_id, canonical),
            ).fetchone()[0]
            connection.execute(
                "INSERT OR IGNORE INTO intake_message_articles(app_id,message_id,article_id) VALUES(?,?,?)",
                (app_id, message_id, article_id),
            )
        if app_id == 'cli_aab030a473f8dcd2' and len({canonical for canonical,_ in normalized}) > 1:
            thread_id=event.get('threadId')
            if thread_id is not None: _id(thread_id,'omt_')
            from reading_dispatch_guard import admit_children_transaction
            uncertain = event.get('routingUncertain') is True
            source_created=event.get('sourceCreatedAt')
            source_created_ms=(int(source_created) if isinstance(source_created,str) and re.fullmatch(r'[0-9]{13}',source_created) else None)
            if not live_dispatch:
                cutover_exists=connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reading_dispatch_cutover'").fetchone()
                cutover=(connection.execute('SELECT cutover_ms FROM reading_dispatch_cutover WHERE singleton=1').fetchone()
                         if cutover_exists else None)
                # A historical message may already have unknown side effects.
                # Only a source timestamp after the explicitly established,
                # drained cutover can be recovered automatically.
                if not cutover or source_created_ms is None or source_created_ms<cutover[0]:
                    uncertain=True
            admit_children_transaction(connection,message_id,[original for _,original in normalized],
                                       chat_id=chat_id,sender_id=sender_id,thread_id=thread_id,
                                       routing_uncertain=uncertain,source_created_ms=source_created_ms)
        retry_candidate = False
        if live_dispatch and len(normalized) == 1 and not created and not policy_upgraded and not message_was_registered:
            # A new user message may retry only an explicitly terminated Agent
            # run whose durable side-effect checks all pass.
            from reading_dispatch_guard import can_retry_url
            retry_candidate = can_retry_url(Path(state_dir), normalized[0][0], connection=connection)
        connection.commit()
        result = {"status": "duplicate" if message_was_registered else "accepted", "articles": created}
        if live_dispatch:
            # Multi-URL events are durably admitted too; claim() marks them
            # needs_review until the workflow can process each source safely.
            result["dispatch_candidate"] = (
                created > 0 or policy_upgraded or message_was_registered or retry_candidate
            )
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_pending(state_dir: Path, *, limit: int = 100) -> list[dict]:
    """Read pending articles for a Base worker without changing their state."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValueError("invalid limit")
    connection = _open_database(Path(state_dir))
    try:
        rows = connection.execute(
            "SELECT id,app_id,canonical_url,original_url,created_at,pack_policy FROM intake_articles "
            "WHERE sync_status='pending' ORDER BY id LIMIT ?", (limit,),
        ).fetchall()
        return [dict(zip(("id", "appId", "canonicalUrl", "originalUrl", "createdAt", "packPolicy"), row))
                for row in rows]
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--allowed-app-id", required=True)
    parser.add_argument("--allowed-chat-id", required=True)
    parser.add_argument("--allowed-sender-id", required=True)
    parser.add_argument("--live", action="store_true", help="claim the message for one live Bridge dispatch")
    parser.add_argument("--finish-dispatch", choices=("failed", "needs_review"),
                        help="persist a terminal outcome for one previously claimed live dispatch")
    parser.add_argument("--message-id", help="trusted Feishu message ID for --finish-dispatch")
    parser.add_argument("--article-key", help="multi-link child identity for --finish-dispatch")
    parser.add_argument("--claim-child", action="store_true")
    parser.add_argument("--list-queued-children", action="store_true")
    args = parser.parse_args()
    try:
        if args.list_queued_children:
            if args.allowed_app_id != 'cli_aab030a473f8dcd2': raise ValueError('invalid app')
            from reading_dispatch_guard import queued_children
            from reading_article_identity import score_request_id
            rows=queued_children(args.state_dir,chat_id=args.allowed_chat_id,sender_id=args.allowed_sender_id)
            rows=[{**row,'scoreId':score_request_id(row['messageId'],row['articleKey'])} for row in rows]
            print(json.dumps({'status':'ok','children':rows})); return 0
        if args.claim_child:
            if args.allowed_app_id != 'cli_aab030a473f8dcd2' or not args.message_id or not args.article_key:
                raise ValueError('invalid child identity')
            from reading_dispatch_guard import claim_child
            state=args.state_dir.absolute()
            phase=claim_child(state,state.parent/'long-read'/'workflow.sqlite3',args.message_id,args.article_key,
                              f'bridge:{os.getpid()}',allowed_chat=args.allowed_chat_id,allowed_sender=args.allowed_sender_id)
            print(json.dumps({'status':phase})); return 0
        if args.finish_dispatch:
            if (args.allowed_app_id != "cli_aab030a473f8dcd2" or not args.message_id or
                    not re.fullmatch(r"om_[A-Za-z0-9_-]{1,160}", args.message_id)):
                raise ValueError("invalid dispatch identity")
            if args.article_key:
                from reading_dispatch_guard import finish_child
                result = {"status": finish_child(args.state_dir,args.message_id,args.article_key,args.finish_dispatch)}
            else:
                from reading_dispatch_guard import finish_dispatch
                result = {"status": finish_dispatch(args.state_dir, args.message_id, args.finish_dispatch)}
            print(json.dumps(result))
            return 0
        if args.message_id or args.article_key:
            raise ValueError("message ID requires a terminal dispatch outcome")
        event = json.load(sys.stdin)
        result = register_event(args.state_dir, event, allowed_app_id=args.allowed_app_id,
                                allowed_chat_id=args.allowed_chat_id, allowed_sender_id=args.allowed_sender_id,
                                live_dispatch=args.live)
        if args.live:
            candidate = result.pop("dispatch_candidate", False)
            result["dispatch"] = "suppressed"
            if len({normalize_url(url) for url in event['urls']}) > 1:
                from reading_dispatch_guard import admit_children
                from reading_article_identity import article_key, score_request_id
                state=args.state_dir.absolute()
                admit_children(state,event['messageId'],event['urls'],chat_id=event['chatId'],sender_id=event['senderId'],thread_id=event.get('threadId'))
                result['children']=[]
                for url in dict.fromkeys(normalize_url(url) for url in event['urls']):
                    key=article_key(url,multi=True)
                    result['children'].append({'url':url,'articleKey':key,'scoreId':score_request_id(event['messageId'],key),'dispatch':'queued'})
                result['dispatch']='multi'
            elif candidate:
                from reading_dispatch_guard import admit, claim
                state = args.state_dir.absolute()
                phase = admit(state, event["messageId"], exclusive_intake=True)
                if phase == "queued":
                    phase = claim(state, state.parent / "long-read" / "workflow.sqlite3",
                                  event["messageId"], f"bridge:{os.getpid()}")
                result["dispatch"] = phase
    except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError):
        print(json.dumps({"status": "rejected", "reason": "invalid-event-or-unavailable-store"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
