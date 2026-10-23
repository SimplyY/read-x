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
    RetryableDeliveryError,
    UnknownRemoteResult,
    _data, match_report_messages, message_rate_slot,
)
from long_read_workflow_reports import render_report_card
from long_read_workflow_store import WorkflowStore


REPORT_RETRY_DELAY_SECONDS = 60
REPORT_RETRY_LIMIT = 2
REPORT_MESSAGE_IDEMPOTENCY_WINDOW_SECONDS = 3600
REPORT_RECONCILE_INTERVAL_SECONDS = 300


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def verify_report_target(*, profile: str, user_id: str, chat_id: str, sender_id: str,
                         user_client=None, bot_client=None, clock=time.time) -> dict:
    """Read-only verify that the configured recipient and bot share this P2P chat."""
    user_client = user_client or FeishuLongReadDelivery(identity="user", profile=profile)
    bot_client = bot_client or FeishuLongReadDelivery(identity="bot", profile=profile)
    try:
        user_status = user_client.auth_status()
        status_data = user_status.get("data") if isinstance(user_status.get("data"), dict) else user_status
        identities = user_status.get("identities", status_data.get("identities"))
        user_record = identities.get("user") if isinstance(identities, dict) else None
        identity = user_status.get("identity", status_data.get("identity"))
        verified = user_status.get("verified", status_data.get("verified"))
        if (identity != "user" or verified is not True
                or not isinstance(user_record, dict)
                or user_record.get("openId") != user_id
                or user_record.get("tokenStatus") not in {None, "valid"}):
            raise ValueError("report target user_id does not match the verified current user identity")

        chat = _data(bot_client._run(
            ["im", "chats", "get", "--chat-id", chat_id, "--user-id-type", "open_id"],
            write=False,
        ))
        if chat.get("chat_mode") != "p2p" or chat.get("chat_status") != "normal":
            raise ValueError("report chat_id is not an active private chat")
        members = _data(bot_client._run(
            ["im", "+chat-members-list", "--chat-id", chat_id,
             "--member-id-type", "open_id", "--member-types", "user,bot",
             "--page-all", "--page-limit", "2"],
            write=False,
        ))
        users = members.get("users")
        bots = members.get("bots")
        if (not isinstance(users, list) or not isinstance(bots, list)
                or members.get("truncations") or members.get("has_more") is not False
                or len(users) != 1 or len(bots) != 1
                or not isinstance(users[0], dict) or not isinstance(bots[0], dict)
                or users[0].get("member_id") != user_id
                or bots[0].get("app_id") != sender_id):
            raise ValueError("report chat membership does not exactly match the verified user and sender bot")
    except DeliveryError as exc:
        raise ValueError("report recipient identity or private chat could not be verified") from exc
    return {"status": "verified", "verified_at": int(float(clock()))}


def configure_report_target(*, state_dir: Path, profile: str, user_id: str,
                            chat_id: str, sender_id: str, replace: bool = False,
                            target_verifier=verify_report_target) -> dict:
    """Verify and persist the selected private recipient in the protected workflow DB."""
    target = {"identity": "bot", "profile": profile, "user_id": user_id,
              "chat_id": chat_id, "sender_id": sender_id, "sender_id_type": "app_id"}
    for key, value in target.items():
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:/+-]{1,200}", value):
            raise ValueError(f"report target {key} must be a safe non-empty identifier")
    verification = target_verifier(profile=profile, user_id=user_id, chat_id=chat_id,
                                   sender_id=sender_id)
    if not isinstance(verification, dict) or verification.get("status") != "verified":
        raise ValueError("report delivery target could not be verified")
    encoded = _canonical(target).decode("utf-8")
    with WorkflowStore(Path(state_dir)) as store:
        row = store.connection.execute(
            "SELECT value FROM workflow_meta WHERE key='report_delivery_target'"
        ).fetchone()
        if row and row["value"] != encoded and not replace:
            raise ValueError("report delivery target already exists; use --replace to change it")
        with store._transaction():
            store.connection.execute(
                "INSERT INTO workflow_meta(key,value) VALUES('report_delivery_target',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (encoded,),
            )
    return {"status": "configured", "target_sha256": _sha(encoded.encode("utf-8")),
            "identity": "bot", "profile": profile}


def report_target_from_store(store: WorkflowStore) -> dict:
    row = store.connection.execute(
        "SELECT value FROM workflow_meta WHERE key='report_delivery_target'"
    ).fetchone()
    if not row:
        raise ValueError("report delivery target is not configured")
    target = json.loads(row["value"])
    required = {"identity", "profile", "user_id", "chat_id", "sender_id", "sender_id_type"}
    if (not isinstance(target, dict) or set(target) != required or target.get("identity") != "bot"
            or target.get("sender_id_type") != "app_id"
            or any(not isinstance(target.get(key), str) or not target[key]
                   for key in ("profile", "user_id", "chat_id", "sender_id"))):
        raise ValueError("persisted report delivery target is invalid")
    return target


def load_report_target(state_dir: Path) -> dict:
    with WorkflowStore(Path(state_dir)) as store:
        return report_target_from_store(store)


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


def _history_match(client: FeishuLongReadDelivery, item: dict, *, chat_id: str, sender_id: str,
                   sender_id_type: str | None, artifact: str, expected: dict,
                   now: float) -> tuple[str, str | None]:
    start_time = max(0, int(float(item["created_at"]) - 60))
    end_time = max(start_time, int(now + 60))
    try:
        messages = client.list_chat_messages(chat_id=chat_id, start_time=start_time, end_time=end_time)
    except DeliveryError:
        return "incomplete", None
    matches = match_report_messages(messages, chat_id=chat_id, sender_id=sender_id,
                                    sender_id_type=sender_id_type, artifact=artifact, expected=expected)
    if len(matches) > 1:
        return "ambiguous", None
    if matches:
        return "matched", matches[0]["message_id"]
    return "absent", None


def _reconcile_retryable_failure(store: WorkflowStore, client: FeishuLongReadDelivery,
                                 item: dict, *, report_key: str, artifact: str,
                                 chat_id: str, sender_id: str, sender_id_type: str | None,
                                 expected: dict, now: float) -> tuple[str, str | None]:
    """Read back failed sends, including after the resend budget has been exhausted."""
    age = max(0.0, now - float(item["created_at"]))
    metadata = item.get("metadata", {})
    retry_allowed = metadata.get("retry_allowed", True)
    retry_allowed = (retry_allowed is True
                     and int(item["attempts"]) < REPORT_RETRY_LIMIT + 1
                     and age < REPORT_MESSAGE_IDEMPOTENCY_WINDOW_SECONDS)
    due_key = "next_retry_at" if retry_allowed else "next_reconcile_at"
    due_at = metadata.get(due_key)
    if isinstance(due_at, (int, float)) and float(due_at) > now:
        return "deferred", None
    history_state, message_id = _history_match(
        client, item, chat_id=chat_id, sender_id=sender_id,
        sender_id_type=sender_id_type, artifact=artifact, expected=expected, now=now,
    )
    if history_state == "matched":
        store.finish_report_effect(
            report_key, artifact, "confirmed", remote_id=message_id,
            metadata={"verification": "history-exact-match"}, reconcile=True,
        )
        return "confirmed", message_id
    if history_state in {"incomplete", "ambiguous"}:
        store.finish_report_effect(
            report_key, artifact, "uncertain",
            metadata={"reason_code": f"report-history-{history_state}",
                      "next_reconcile_at": now + REPORT_RECONCILE_INTERVAL_SECONDS}, reconcile=True,
        )
        return "uncertain", None
    if not retry_allowed:
        store.finish_report_effect(
            report_key, artifact, "uncertain",
            metadata={"reason_code": "report-history-no-match",
                      "next_reconcile_at": now + REPORT_RECONCILE_INTERVAL_SECONDS},
            reconcile=True,
        )
        return "uncertain", None
    return "retry", None


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
    next_reconcile_at = item.get("metadata", {}).get("next_reconcile_at")
    if isinstance(next_reconcile_at, (int, float)) and float(next_reconcile_at) > now:
        return "uncertain"
    if item.get("remote_id") and _verify_known_message(
            client, item["remote_id"], chat_id=chat_id, sender_id=sender_id,
            sender_id_type=sender_id_type, artifact=artifact, expected=expected):
        store.finish_report_effect(report_key, artifact, "confirmed", remote_id=item["remote_id"],
                                   metadata={"verification": "message-id-readback"}, reconcile=True)
        return "confirmed"
    # Keep searching the original send window across every recovery pass. A
    # matching message can become visible after an earlier complete readback.
    start_time = max(0, int(float(item["created_at"]) - 60))
    end_time = max(start_time, int(now + 60))
    try:
        messages = client.list_chat_messages(chat_id=chat_id, start_time=start_time, end_time=end_time)
    except DeliveryError:
        store.finish_report_effect(
            report_key, artifact, "uncertain",
            metadata={"reason_code": "report-history-incomplete",
                      "next_reconcile_at": now + REPORT_RECONCILE_INTERVAL_SECONDS}, reconcile=True,
        )
        return "uncertain"
    matches = match_report_messages(messages, chat_id=chat_id, sender_id=sender_id,
                                   sender_id_type=sender_id_type, artifact=artifact, expected=expected)
    if len(matches) != 1:
        store.finish_report_effect(
            report_key, artifact, "uncertain",
            metadata={"reason_code": "report-history-ambiguous" if len(matches) > 1 else "report-history-no-match",
                      "next_reconcile_at": now + REPORT_RECONCILE_INTERVAL_SECONDS}, reconcile=True,
        )
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
    if item["status"] == "failed":
        if item.get("metadata", {}).get("retryable") is not True:
            return "failed", item.get("remote_id")
        state, remote_id = _reconcile_retryable_failure(
            store, client, item, report_key=report_key, artifact=artifact,
            chat_id=chat_id or "", sender_id=sender_id or "",
            sender_id_type=sender_id_type, expected=readback or {},
            now=now if now is not None else time.time(),
        )
        if state != "retry":
            return state, remote_id
    if readback is not None:
        try:
            if not message_rate_slot(
                    client, chat_id=chat_id, sender_id=sender_id or "",
                    sender_id_type=sender_id_type or "app_id",
                    now=now if now is not None else time.time()):
                return "deferred", None
        except DeliveryError:
            return "needs_review", None
    attempt = store.begin_report_effect(report_key, artifact)
    if not attempt.get("execute"):
        return "confirmed", attempt.get("remote_id")
    try:
        remote_id = action(attempt["request_id"])
    except UnknownRemoteResult:
        store.finish_report_effect(report_key, artifact, "uncertain",
                                   metadata={"reason_code": "remote-result-unknown",
                                             "next_reconcile_at": (now if now is not None else time.time())
                                             + REPORT_RECONCILE_INTERVAL_SECONDS})
        return "uncertain", None
    except RetryableDeliveryError:
        current = _effect(store, report_key, artifact) or attempt
        now_value = now if now is not None else time.time()
        if readback is None:
            store.finish_report_effect(report_key, artifact, "uncertain",
                                       metadata={"reason_code": "upload-result-unconfirmed"})
            return "uncertain", None
        history_state, message_id = _history_match(
            client, current, chat_id=chat_id or "", sender_id=sender_id or "",
            sender_id_type=sender_id_type, artifact=artifact, expected=readback,
            now=now_value,
        )
        if history_state == "matched":
            store.finish_report_effect(
                report_key, artifact, "confirmed", remote_id=message_id,
                metadata={"verification": "history-exact-match"},
            )
            return "confirmed", message_id
        if history_state in {"incomplete", "ambiguous"}:
            store.finish_report_effect(
                report_key, artifact, "uncertain",
                metadata={"reason_code": f"report-history-{history_state}",
                          "next_reconcile_at": now_value + REPORT_RECONCILE_INTERVAL_SECONDS},
            )
            return "uncertain", None
        age = max(0.0, now_value - float(current["created_at"]))
        attempts = int(current["attempts"])
        retry_allowed = (attempts < REPORT_RETRY_LIMIT + 1
                         and age < REPORT_MESSAGE_IDEMPOTENCY_WINDOW_SECONDS)
        metadata = {"reason_code": "remote-rejected", "retryable": True,
                    "retry_allowed": retry_allowed}
        if retry_allowed:
            metadata["next_retry_at"] = now_value + REPORT_RETRY_DELAY_SECONDS
        else:
            metadata["reason_code"] = "report-retry-exhausted"
            metadata["code"] = "remote-rejected"
            metadata["next_reconcile_at"] = now_value + REPORT_RECONCILE_INTERVAL_SECONDS
        store.finish_report_effect(report_key, artifact, "failed", metadata=metadata)
        return ("deferred", None) if retry_allowed else ("failed", None)
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
def deliver_report(*, state_dir: Path, report_key: str, identity: str | None = None,
                   profile: str | None = None, user_id: str | None = None,
                   chat_id: str | None = None, sender_id: str | None = None,
                   sender_id_type: str | None = None, client=None,
                   clock=time.time, target_verifier=verify_report_target) -> dict:
    """Send one frozen report as Markdown file plus Card 2.0 with durable receipts."""
    saved = load_report_target(state_dir)
    supplied = {"identity": identity, "profile": profile, "user_id": user_id,
                "chat_id": chat_id, "sender_id": sender_id,
                "sender_id_type": sender_id_type}
    for key, value in supplied.items():
        if value is not None and value != saved[key]:
            raise ValueError(f"report delivery {key} does not match the persisted target")
    identity = saved["identity"]
    profile = saved["profile"]
    user_id = saved["user_id"]
    chat_id = saved["chat_id"]
    sender_id = saved["sender_id"]
    sender_id_type = saved["sender_id_type"]
    verification = target_verifier(
        profile=profile, user_id=user_id, chat_id=chat_id, sender_id=sender_id,
        clock=clock,
    )
    if not isinstance(verification, dict) or verification.get("status") != "verified":
        raise ValueError("persisted report target could not be verified")
    client = client or FeishuLongReadDelivery(identity=identity, profile=profile)
    with WorkflowStore(Path(state_dir), clock=clock) as store:
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
