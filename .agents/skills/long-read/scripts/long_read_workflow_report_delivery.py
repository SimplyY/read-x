#!/usr/bin/env python3
"""Read-back-first delivery of immutable reliability reports to Feishu."""
from __future__ import annotations

import hashlib
import fcntl
from functools import wraps
import json
import os
from pathlib import Path
import re
import time

from feishu_long_read_delivery import (
    DeliveryError,
    FeishuLongReadDelivery,
    UnknownRemoteResult,
    match_report_messages,
)
from long_read_workflow_reports import render_report_card
from long_read_workflow_store import WorkflowStore


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_state_file(state_dir: Path, filename: str) -> Path:
    root = Path(state_dir).absolute()
    for part in (root, *root.parents):
        if part.is_symlink() and part not in {Path("/tmp"), Path("/var")}:
            raise ValueError("report state path must not traverse symlinks")
    reports_dir = root / "reports"
    if reports_dir.is_symlink():
        raise ValueError("report directory must not be a symlink")
    reports_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(reports_dir, 0o700)
    path = reports_dir / filename
    if path.is_symlink():
        raise ValueError("report attachment must not be a symlink")
    return path


def _materialize_markdown(state_dir: Path, report_key: str, markdown: str, expected_sha: str) -> Path:
    if not re.fullmatch(r"(?:weekly|monthly):\d{4}-\d{2}(?:-\d{2})?", report_key):
        raise ValueError("invalid report key")
    safe_name = report_key.replace(":", "-") + ".md"
    path = _safe_state_file(state_dir, safe_name)
    content = markdown.encode("utf-8")
    if _sha(content) != expected_sha:
        raise ValueError("report Markdown hash does not match the frozen snapshot")
    if path.exists():
        if not path.is_file() or path.read_bytes() != content:
            raise ValueError("existing report attachment differs from the frozen snapshot")
        if path.stat().st_mode & 0o777 != 0o600:
            raise ValueError("existing report attachment permissions are unsafe")
        return path
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def _effect(store: WorkflowStore, report_key: str, artifact_key: str) -> dict | None:
    for item in store.report_effects(report_key):
        if item["artifact_key"] == artifact_key:
            return item
    return None


def _message_matches(message: dict, *, chat_id: str, sender_id: str,
                     sender_id_type: str | None, artifact: str, expected: dict) -> bool:
    matches = match_report_messages(
        [message], chat_id=chat_id, sender_id=sender_id,
        sender_id_type=sender_id_type, artifact=artifact, expected=expected,
    )
    return len(matches) == 1


def _verify_known_message(client: FeishuLongReadDelivery, message_id: str, *, chat_id: str,
                          sender_id: str, sender_id_type: str | None,
                          artifact: str, expected: dict) -> bool:
    try:
        message = client.get_message(message_id)
    except DeliveryError:
        return False
    return _message_matches(message, chat_id=chat_id, sender_id=sender_id,
                            sender_id_type=sender_id_type, artifact=artifact, expected=expected)


def _reconcile_send(store: WorkflowStore, client: FeishuLongReadDelivery, report_key: str,
                    artifact: str, *, chat_id: str, sender_id: str,
                    sender_id_type: str | None, expected: dict, now: float) -> str:
    item = _effect(store, report_key, artifact)
    if item is None:
        return "prepared"
    if item["status"] == "attempting":
        store.finish_report_effect(report_key, artifact, "uncertain",
                                   remote_id=item.get("remote_id"),
                                   metadata={"reason_code": "worker-interrupted"})
        item = _effect(store, report_key, artifact)
    if item["status"] == "confirmed":
        return "confirmed"
    if item["status"] != "uncertain":
        return item["status"]
    if item.get("remote_id") and _verify_known_message(
            client, item["remote_id"], chat_id=chat_id, sender_id=sender_id,
            sender_id_type=sender_id_type, artifact=artifact, expected=expected):
        store.finish_report_effect(report_key, artifact, "confirmed", remote_id=item["remote_id"],
                                   metadata={"verification": "message-id-readback"}, reconcile=True)
        return "confirmed"
    start_time = max(0, int(float(item["updated_at"]) - 60))
    end_time = max(start_time, int(now + 60))
    try:
        messages = client.list_chat_messages(chat_id=chat_id, start_time=start_time, end_time=end_time)
    except DeliveryError:
        return "uncertain"
    matches = match_report_messages(messages, chat_id=chat_id, sender_id=sender_id,
                                   sender_id_type=sender_id_type, artifact=artifact, expected=expected)
    if len(matches) != 1:
        return "uncertain"
    message_id = matches[0]["message_id"]
    store.finish_report_effect(report_key, artifact, "confirmed", remote_id=message_id,
                               metadata={"verification": "history-exact-match"}, reconcile=True)
    return "confirmed"


def _run_write(store: WorkflowStore, client: FeishuLongReadDelivery, report_key: str,
               artifact: str, request_hash: str, action, *, readback: dict | None = None,
               chat_id: str | None = None, sender_id: str | None = None,
               sender_id_type: str | None = None, now: float | None = None) -> tuple[str, str | None]:
    item = store.prepare_report_effect(report_key, artifact, request_hash)
    if item["status"] in {"attempting", "uncertain"}:
        if artifact == "markdown-upload":
            if item["status"] == "attempting":
                store.finish_report_effect(report_key, artifact, "uncertain",
                                           metadata={"reason_code": "worker-interrupted"})
            return "uncertain", None
        state = _reconcile_send(store, client, report_key, artifact, chat_id=chat_id,
                                sender_id=sender_id, sender_id_type=sender_id_type,
                                expected=readback or {}, now=now or time.time())
        current = _effect(store, report_key, artifact)
        return state, current.get("remote_id") if current else None
    if item["status"] == "confirmed":
        return "confirmed", item.get("remote_id")
    attempt = store.begin_report_effect(report_key, artifact)
    if not attempt.get("execute"):
        return "confirmed", attempt.get("remote_id")
    try:
        remote_id = action(attempt["request_id"])
    except UnknownRemoteResult:
        store.finish_report_effect(report_key, artifact, "uncertain",
                                   metadata={"reason_code": "remote-result-unknown"})
        return "uncertain", None
    except DeliveryError:
        store.finish_report_effect(report_key, artifact, "failed",
                                   metadata={"reason_code": "remote-rejected"})
        return "failed", None
    if readback is not None:
        if _verify_known_message(client, remote_id, chat_id=chat_id, sender_id=sender_id,
                                 sender_id_type=sender_id_type, artifact=artifact, expected=readback):
            store.finish_report_effect(report_key, artifact, "confirmed", remote_id=remote_id,
                                       metadata={"verification": "message-id-readback"})
            return "confirmed", remote_id
        store.finish_report_effect(report_key, artifact, "uncertain", remote_id=remote_id,
                                   metadata={"reason_code": "message-readback-unconfirmed"})
        return "uncertain", remote_id
    store.finish_report_effect(report_key, artifact, "confirmed", remote_id=remote_id,
                               metadata={"verification": "upload-receipt"})
    return "confirmed", remote_id


def _serialized_report_delivery(function):
    @wraps(function)
    def wrapped(*, state_dir: Path, **kwargs):
        root = Path(state_dir).absolute()
        if root.is_symlink():
            raise ValueError("report state directory must not be a symlink")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        lock_path = root / ".report-delivery.lock"
        if lock_path.is_symlink():
            raise ValueError("report delivery lock must not be a symlink")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.fchmod(fd, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"status": "busy"}
            return function(state_dir=root, **kwargs)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
    return wrapped


@_serialized_report_delivery
def deliver_report(*, state_dir: Path, report_key: str, identity: str,
                   profile: str, user_id: str, chat_id: str, sender_id: str,
                   sender_id_type: str | None = "app_id", client=None,
                   clock=time.time) -> dict:
    """Send one frozen report as Markdown file plus Card 2.0 with durable receipts."""
    if not all(isinstance(value, str) and value.strip() for value in (profile, user_id, chat_id, sender_id)):
        raise ValueError("report delivery requires explicit profile, recipient, history chat, and sender")
    if identity != "bot":
        raise ValueError("reliability reports must use the configured bot identity")
    client = client or FeishuLongReadDelivery(identity=identity, profile=profile)
    with WorkflowStore(Path(state_dir)) as store:
        row = store.connection.execute("SELECT * FROM reports WHERE report_key=?", (report_key,)).fetchone()
        if row is None:
            raise KeyError(report_key)
        if row["status"] == "suppressed_weekly":
            return {"status": "suppressed_weekly", "report_key": report_key}
        if row["status"] == "delivered":
            return {"status": "delivered", "report_key": report_key, "reused": True}
        if row["status"] != "pending_delivery":
            raise ValueError("report is not pending delivery")
        markdown = row["markdown"]
        markdown_sha = row["markdown_sha256"]
        snapshot = json.loads(row["snapshot_json"])
        card = render_report_card(snapshot)
        markdown_path = _materialize_markdown(Path(state_dir), report_key, markdown, markdown_sha)
        target = {"identity": identity, "profile": profile, "user_id": user_id, "chat_id": chat_id}
        upload_hash = _sha(_canonical({"action": "markdown-upload", "markdown_sha256": markdown_sha,
                                       "file_name": markdown_path.name, "target": target}))
        upload_state, file_key = _run_write(
            store, client, report_key, "markdown-upload", upload_hash,
            lambda _request_id: client.upload_markdown_attachment(path=markdown_path),
            now=clock(),
        )
        upload_effect = _effect(store, report_key, "markdown-upload")
        file_key = upload_effect.get("remote_id") if upload_effect and upload_effect["status"] == "confirmed" else file_key
        if upload_state != "confirmed" or not file_key:
            return {"status": "needs_review" if upload_state == "uncertain" else upload_state,
                    "report_key": report_key, "stage": "markdown-upload"}

        file_expected = {"file_key": file_key}
        file_hash = _sha(_canonical({"action": "markdown-send", "file_key": file_key,
                                     "target": target, "sender_id": sender_id}))
        file_state, _ = _run_write(
            store, client, report_key, "markdown-send", file_hash,
            lambda request_id: client.send_file_message(file_key=file_key, user_id=user_id,
                                                        idempotency_key=request_id),
            readback=file_expected, chat_id=chat_id, sender_id=sender_id,
            sender_id_type=sender_id_type, now=clock(),
        )
        if file_state != "confirmed":
            return {"status": "needs_review" if file_state == "uncertain" else file_state,
                    "report_key": report_key, "stage": "markdown-send"}

        card_hash = _sha(_canonical({"action": "card-send", "card": card,
                                     "target": target, "sender_id": sender_id}))
        card_state, _ = _run_write(
            store, client, report_key, "card-send", card_hash,
            lambda request_id: client.send_interactive_card(card=card, user_id=user_id,
                                                            idempotency_key=request_id),
            readback=card, chat_id=chat_id, sender_id=sender_id,
            sender_id_type=sender_id_type, now=clock(),
        )
        if card_state != "confirmed":
            return {"status": "needs_review" if card_state == "uncertain" else card_state,
                    "report_key": report_key, "stage": "card-send"}

        store.mark_report_delivered(report_key)
        return {"status": "delivered", "report_key": report_key, "reused": False}
