#!/usr/bin/env python3
"""Persisted, read-back-verified long-read status cards."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import time
import uuid

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from feishu_long_read_delivery import (
    DeliveryError, FeishuLongReadDelivery, RetryableDeliveryError, UnknownRemoteResult,
    match_report_messages, message_rate_slot,
)
from long_read_workflow_store import WorkflowStore
from render_long_read_delivery_card import render_card, DELIVERY_RECEIPT_SCHEMA


NOTIFICATION_RETRY_DELAY_SECONDS = 60
NOTIFICATION_RETRY_LIMIT = 2
NOTIFICATION_RECONCILE_INTERVAL_SECONDS = 300
FEISHU_MESSAGE_IDEMPOTENCY_WINDOW_SECONDS = 3600


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _target(trigger: dict) -> tuple[dict, str] | None:
    """Require a known private chat for both rate preflight and loss recovery."""
    if trigger.get("chatType") == "p2p" and trigger.get("chatId"):
        chat_id = trigger["chatId"]
        return {"chat_id": chat_id}, chat_id
    if (trigger.get("chatType") == "group" and trigger.get("senderType") != "bot"
            and trigger.get("senderId") and trigger.get("privateChatId")):
        return {"user_id": trigger["senderId"]}, trigger["privateChatId"]
    return None


def _correlate_card(card: dict, request_id: str) -> dict:
    """Bind the visible card payload to this outbox operation for safe readback."""
    correlated = json.loads(_canonical(card))
    config = correlated.setdefault("config", {})
    if not isinstance(config, dict):
        raise ValueError("notification card config must be an object")
    summary = config.setdefault("summary", {})
    if not isinstance(summary, dict):
        raise ValueError("notification card summary must be an object")
    content = summary.get("content", "Read-X 交付状态")
    if not isinstance(content, str):
        raise ValueError("notification card summary content must be text")
    summary["content"] = f"{content} · 交付追踪 {request_id}"
    return correlated


def _matches(messages: list[dict], *, chat_id: str, sender_id: str, card: dict) -> list[dict]:
    return match_report_messages(
        messages, chat_id=chat_id, sender_id=sender_id, sender_id_type="app_id",
        artifact="card-send", expected=card,
    )


def _readback(client, message_id: str | None, *, chat_id: str, sender_id: str,
              card: dict, start_time: int, end_time: int) -> str | None:
    if message_id:
        try:
            message = client.get_message(message_id)
        except DeliveryError:
            message = None
        if message and len(_matches([message], chat_id=chat_id, sender_id=sender_id, card=card)) == 1:
            return message_id
    messages = client.list_chat_messages(chat_id=chat_id, start_time=start_time, end_time=end_time)
    matches = _matches(messages, chat_id=chat_id, sender_id=sender_id, card=card)
    return matches[0]["message_id"] if len(matches) == 1 else None


def _preflight(client, *, chat_id: str, sender_id: str, now: float) -> bool:
    return message_rate_slot(client, chat_id=chat_id, sender_id=sender_id,
                             sender_id_type="app_id", now=now)


def _schedule_notification_retry(*, store: WorkflowStore, lease, artifact_key: str,
                                 reason_code: str, now: float,
                                 retry_at: float | None = None) -> dict:
    retry_at = now + NOTIFICATION_RETRY_DELAY_SECONDS if retry_at is None else float(retry_at)
    store.set_artifact(lease, artifact_key, "pending", metadata={
        "failure_stage": "notification-send", "reason_code": reason_code,
        "next_retry_at": retry_at,
    })
    return {"status": "deferred", "reason_code": reason_code, "retry_at": retry_at}


def _delivery_card(*, title: str, main_url: str | None, status: str,
                   failure_reason: str | None = None, image_note: str | None = None,
                   receipt: dict | None = None) -> dict:
    url = main_url
    card_receipt = receipt or {
        "schema": DELIVERY_RECEIPT_SCHEMA,
        "status": status,
        "main_url": url,
        "munger_embedded": False,
        "artifacts": {},
        "unresolved_actions": [],
    }
    return render_card(
        title=title, main_url=url, munger_embedded=bool(card_receipt.get("munger_embedded")),
        failure_reason=failure_reason, image_note=image_note, delivery_receipt=card_receipt,
    )


def send_status_card(*, store: WorkflowStore, lease, payload: dict, root: Path,
                     artifact_key: str, action: str, card: dict,
                     client=None, clock=time.time) -> dict:
    trigger = payload.get("trigger") or {}
    profile = trigger.get("feishuProfile")
    sender_id = trigger.get("appId")
    if not isinstance(profile, str) or not profile or not isinstance(sender_id, str) or not sender_id:
        return {"status": "needs_review", "reason_code": "notification-identity-missing"}
    target = _target(trigger)
    if target is None:
        return {"status": "needs_review", "reason_code": "private-chat-readback-target-missing"}
    command_target, history_chat_id = target
    client = client or FeishuLongReadDelivery(identity="bot", profile=profile)
    now = float(clock())
    effect = store.find_effect(lease.job_id, artifact_key, action)
    request_id = effect["request_id"] if effect else str(uuid.uuid4())
    correlated_card = _correlate_card(card, request_id)
    card_hash = _sha(_canonical({"card": correlated_card, "target": command_target,
                                 "history_chat_id": history_chat_id, "sender_id": sender_id}))
    if effect is None:
        effect = store.prepare_effect(lease, artifact_key, action, card_hash, request_id=request_id)
    elif effect["request_sha256"] != card_hash:
        return {"status": "needs_review", "reason_code": "notification-payload-changed"}
    artifact = next((item for item in store.artifacts(lease.job_id)
                     if item["artifact_key"] == artifact_key), None)
    artifact_metadata = artifact.get("metadata", {}) if artifact else {}
    retry_failed = False
    if effect["status"] == "confirmed":
        return {"status": "confirmed", "message_id": effect.get("remote_id"), "reused": True}
    if effect["status"] == "failed":
        age = max(0.0, now - float(effect["created_at"]))
        effect_metadata = effect.get("metadata", {})
        retryable = effect_metadata.get("retryable") is True
        retry_at = artifact_metadata.get("next_retry_at", effect_metadata.get("next_retry_at"))
        can_retry = (retryable and effect["attempts"] < NOTIFICATION_RETRY_LIMIT + 1
                     and age < FEISHU_MESSAGE_IDEMPOTENCY_WINDOW_SECONDS)
        if can_retry and not isinstance(retry_at, (int, float)):
            retry_at = float(effect["updated_at"]) + NOTIFICATION_RETRY_DELAY_SECONDS
        if can_retry and isinstance(retry_at, (int, float)) and float(retry_at) > now:
            return {"status": "deferred", "reason_code": "notification-retry-backoff",
                    "retry_at": float(retry_at)}
        reconcile_at = artifact_metadata.get("next_reconcile_at", effect_metadata.get("next_reconcile_at"))
        if isinstance(reconcile_at, (int, float)) and float(reconcile_at) > now:
            return {"status": "deferred" if can_retry else "failed",
                    "reason_code": "notification-retry-backoff" if can_retry else "notification-retry-exhausted",
                    "retry_at": float(reconcile_at)}
        # The API may have accepted the card before the caller observed its
        # rejection. Read the full paginated window even after the send budget
        # expires; a limit on retries must never suppress result verification.
        try:
            messages = client.list_chat_messages(
                chat_id=history_chat_id, start_time=max(0, int(float(effect["created_at"]) - 60)),
                end_time=int(now + 60),
            )
        except DeliveryError:
            store.set_artifact(lease, artifact_key, "uncertain", metadata={
                "failure_stage": "notification-send", "reason_code": "notification-history-incomplete",
                "next_reconcile_at": now + NOTIFICATION_RECONCILE_INTERVAL_SECONDS,
            })
            return {"status": "needs_review", "reason_code": "notification-history-incomplete"}
        matches = _matches(messages, chat_id=history_chat_id, sender_id=sender_id,
                           card=correlated_card)
        if len(matches) > 1:
            store.set_artifact(lease, artifact_key, "uncertain", metadata={
                "failure_stage": "notification-send", "reason_code": "notification-readback-ambiguous",
                "next_reconcile_at": now + NOTIFICATION_RECONCILE_INTERVAL_SECONDS,
            })
            return {"status": "needs_review", "reason_code": "notification-readback-ambiguous"}
        if matches:
            message_id = matches[0]["message_id"]
            store.reconcile_effect(
                lease, effect["op_key"], "confirmed", remote_id=message_id,
                metadata={"verification": "exact-card-readback"},
            )
            store.set_artifact(lease, artifact_key, "confirmed", metadata={
                "verification": "exact-card-readback",
            })
            return {"status": "confirmed", "message_id": message_id, "recovered": True}
        if not retryable:
            store.set_artifact(lease, artifact_key, "failed", metadata={
                "failure_stage": "notification-send", "reason_code": "notification-send-rejected",
                "next_reconcile_at": now + NOTIFICATION_RECONCILE_INTERVAL_SECONDS,
            })
            return {"status": "failed", "reason_code": "notification-send-rejected"}
        if (effect["attempts"] >= NOTIFICATION_RETRY_LIMIT + 1
                or age >= FEISHU_MESSAGE_IDEMPOTENCY_WINDOW_SECONDS):
            store.set_artifact(lease, artifact_key, "failed", metadata={
                "failure_stage": "notification-send", "reason_code": "notification-retry-exhausted",
                "next_reconcile_at": now + NOTIFICATION_RECONCILE_INTERVAL_SECONDS,
            })
            return {"status": "failed", "reason_code": "notification-retry-exhausted"}
        retry_failed = True
    elif effect["status"] == "prepared":
        retry_at = artifact_metadata.get("next_retry_at")
        if isinstance(retry_at, (int, float)) and float(retry_at) > now:
            return {"status": "deferred", "reason_code": "notification-retry-backoff",
                    "retry_at": float(retry_at)}
    if effect["status"] in {"attempting", "uncertain"}:
        if effect["status"] == "attempting":
            store.finish_effect(
                lease, effect["op_key"], "uncertain",
                metadata={"failure_stage": "notification-send", "reason_code": "worker-interrupted"},
            )
            effect = store.get_effect(effect["op_key"])
        reconcile_at = artifact_metadata.get(
            "next_reconcile_at", effect.get("metadata", {}).get("next_reconcile_at"),
        )
        if isinstance(reconcile_at, (int, float)) and float(reconcile_at) > now:
            return {"status": "uncertain", "reason_code": "notification-send-result-unconfirmed",
                    "retry_at": float(reconcile_at)}
        try:
            message_id = _readback(
                client, effect.get("remote_id"), chat_id=history_chat_id,
                sender_id=sender_id, card=correlated_card,
                start_time=max(0, int(float(effect["updated_at"]) - 60)),
                end_time=int(now + 60),
            )
        except DeliveryError:
            message_id = None
        if message_id:
            store.reconcile_effect(
                lease, effect["op_key"], "confirmed", remote_id=message_id,
                metadata={"verification": "exact-card-readback"},
            )
            store.set_artifact(lease, artifact_key, "confirmed", metadata={
                "verification": "exact-card-readback",
            })
            return {"status": "confirmed", "message_id": message_id, "recovered": True}
        store.set_artifact(lease, artifact_key, "uncertain", metadata={
            "failure_stage": "notification-send", "reason_code": "notification-send-result-unconfirmed",
            "next_reconcile_at": now + NOTIFICATION_RECONCILE_INTERVAL_SECONDS,
        })
        return {"status": "uncertain", "reason_code": "notification-send-result-unconfirmed"}
    if effect["status"] != "prepared" and not retry_failed:
        return {"status": "needs_review", "reason_code": "unsupported-notification-state"}
    try:
        if not _preflight(client, chat_id=history_chat_id, sender_id=sender_id, now=now):
            return _schedule_notification_retry(
                store=store, lease=lease, artifact_key=artifact_key,
                reason_code="message-rate-limit", now=now,
            )
    except DeliveryError:
        return {"status": "needs_review", "reason_code": "message-rate-preflight-unavailable"}
    attempt = store.begin_effect(lease, effect["op_key"])
    try:
        message_id = client.send_interactive_card(
            card=correlated_card, idempotency_key=attempt["request_id"], **command_target,
        )
    except UnknownRemoteResult:
        store.finish_effect(
            lease, attempt["op_key"], "uncertain",
            metadata={"failure_stage": "notification-send", "reason_code": "send-response-unknown",
                      "next_reconcile_at": now + NOTIFICATION_RECONCILE_INTERVAL_SECONDS},
        )
        store.set_artifact(lease, artifact_key, "uncertain", metadata={
            "failure_stage": "notification-send", "reason_code": "notification-send-result-unconfirmed",
            "next_reconcile_at": now + NOTIFICATION_RECONCILE_INTERVAL_SECONDS,
        })
        return {"status": "uncertain", "reason_code": "notification-send-result-unconfirmed"}
    except RetryableDeliveryError:
        age = max(0.0, now - float(attempt["created_at"]))
        can_retry = (attempt["attempts"] < NOTIFICATION_RETRY_LIMIT + 1
                     and age < FEISHU_MESSAGE_IDEMPOTENCY_WINDOW_SECONDS)
        receipt = {"failure_stage": "notification-send", "reason_code": "api-rejected",
                   "retryable": True}
        if can_retry:
            retry_at = now + NOTIFICATION_RETRY_DELAY_SECONDS
            receipt["next_retry_at"] = retry_at
            store.finish_effect(lease, attempt["op_key"], "failed", metadata=receipt)
            return _schedule_notification_retry(
                store=store, lease=lease, artifact_key=artifact_key,
                reason_code="notification-send-rejected", now=now, retry_at=retry_at,
            )
        receipt["next_reconcile_at"] = now + NOTIFICATION_RECONCILE_INTERVAL_SECONDS
        store.finish_effect(lease, attempt["op_key"], "failed", metadata=receipt)
        store.set_artifact(lease, artifact_key, "failed", metadata={
            "failure_stage": "notification-send", "reason_code": "notification-retry-exhausted",
            "next_reconcile_at": now + NOTIFICATION_RECONCILE_INTERVAL_SECONDS,
        })
        return {"status": "failed", "reason_code": "notification-retry-exhausted"}
    except DeliveryError:
        store.finish_effect(
            lease, attempt["op_key"], "failed",
            metadata={"failure_stage": "notification-send", "reason_code": "api-rejected",
                      "next_reconcile_at": now + NOTIFICATION_RECONCILE_INTERVAL_SECONDS},
        )
        try:
            messages = client.list_chat_messages(
                chat_id=history_chat_id, start_time=max(0, int(float(attempt["created_at"]) - 60)),
                end_time=int(now + 60),
            )
        except DeliveryError:
            store.set_artifact(lease, artifact_key, "uncertain", metadata={
                "failure_stage": "notification-send", "reason_code": "notification-history-incomplete",
                "next_reconcile_at": now + NOTIFICATION_RECONCILE_INTERVAL_SECONDS,
            })
            return {"status": "needs_review", "reason_code": "notification-history-incomplete"}
        matches = _matches(messages, chat_id=history_chat_id, sender_id=sender_id,
                           card=correlated_card)
        if len(matches) > 1:
            store.set_artifact(lease, artifact_key, "uncertain", metadata={
                "failure_stage": "notification-send", "reason_code": "notification-readback-ambiguous",
                "next_reconcile_at": now + NOTIFICATION_RECONCILE_INTERVAL_SECONDS,
            })
            return {"status": "needs_review", "reason_code": "notification-readback-ambiguous"}
        if matches:
            message_id = matches[0]["message_id"]
            store.reconcile_effect(
                lease, attempt["op_key"], "confirmed", remote_id=message_id,
                metadata={"verification": "exact-card-readback"},
            )
            store.set_artifact(lease, artifact_key, "confirmed", metadata={
                "verification": "exact-card-readback",
            })
            return {"status": "confirmed", "message_id": message_id, "recovered": True}
        store.set_artifact(lease, artifact_key, "failed", metadata={
            "failure_stage": "notification-send", "reason_code": "api-rejected",
            "next_reconcile_at": now + NOTIFICATION_RECONCILE_INTERVAL_SECONDS,
        })
        return {"status": "failed", "reason_code": "notification-send-rejected"}
    try:
        verified_id = _readback(
            client, message_id, chat_id=history_chat_id, sender_id=sender_id,
            card=correlated_card, start_time=max(0, int(now - 60)), end_time=int(now + 60),
        )
    except DeliveryError:
        verified_id = None
    if not verified_id:
        store.finish_effect(
            lease, attempt["op_key"], "uncertain", remote_id=message_id,
            metadata={"failure_stage": "notification-send", "reason_code": "message-readback-unmatched"},
        )
        return {"status": "uncertain", "reason_code": "notification-readback-unconfirmed"}
    store.finish_effect(
        lease, attempt["op_key"], "confirmed", remote_id=verified_id,
        metadata={"verification": "message-id-card-readback"},
    )
    store.set_artifact(lease, artifact_key, "confirmed", metadata={
        "verification": "message-id-card-readback",
    })
    return {"status": "confirmed", "message_id": verified_id}


def notify_document_progress(*, store: WorkflowStore, lease, payload: dict, root: Path,
                             document_id: str, client=None, clock=time.time) -> dict:
    evidence_path = Path(payload["inputs"]["evidence.json"]["path"])
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    title = str(evidence["metadata"]["title"])
    url = f"https://feishu.cn/docx/{document_id}"
    card = _delivery_card(title=title, main_url=url, status="processing")
    return send_status_card(
        store=store, lease=lease, payload=payload, root=root,
        artifact_key="document-notification", action="message-send", card=card,
        client=client, clock=clock,
    )


def notify_document_publish_review(*, store: WorkflowStore, lease, payload: dict, root: Path,
                                   reason_code: str, client=None, clock=time.time) -> dict:
    """Tell the user a document publish outcome is unresolved without implying failure or success."""
    evidence_path = Path(payload["inputs"]["evidence.json"]["path"])
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    title = str(evidence["metadata"]["title"])
    receipt = {
        "schema": DELIVERY_RECEIPT_SCHEMA, "status": "needs_review",
        "main_url": None, "munger_embedded": False,
        "artifacts": {}, "unresolved_actions": ["main-document"],
    }
    card = _delivery_card(
        title=title, main_url=None, status="needs_review",
        failure_reason=f"document-publish:{reason_code}", receipt=receipt,
    )
    return send_status_card(
        store=store, lease=lease, payload=payload, root=root,
        artifact_key="document-publish-review-notification", action="message-send",
        card=card, client=client, clock=clock,
    )


def notify_content_review(*, store: WorkflowStore, lease, payload: dict, root: Path,
                          client=None, clock=time.time) -> dict:
    """Persist a stable notice when required text output cannot be confirmed."""
    evidence_path = Path(payload["inputs"]["evidence.json"]["path"])
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    title = str(evidence["metadata"]["title"])
    receipt = {
        "schema": DELIVERY_RECEIPT_SCHEMA, "status": "needs_review",
        "main_url": None, "munger_embedded": False,
        "artifacts": {}, "unresolved_actions": ["required-text-analysis"],
    }
    card = _delivery_card(
        title=title, main_url=None, status="needs_review",
        failure_reason="text-analysis:incomplete-or-unconfirmed",
        image_note=("必需文字分支尚未完成或结果待核实；主文档尚未交付。"
                    "系统会按工作流安全恢复，对不确定的远端请求先只读核验，不盲目重发。"),
        receipt=receipt,
    )
    return send_status_card(
        store=store, lease=lease, payload=payload, root=root,
        artifact_key="content-review-notification", action="message-send",
        card=card, client=client, clock=clock,
    )


def notify_image_recovery(*, store: WorkflowStore, lease, payload: dict, root: Path,
                          image_key: str, image_name: str, document_id: str,
                          failure_stage: str, reason_code: str, retries_used: int,
                          retry_at: float | None, client=None, clock=time.time) -> dict:
    evidence = json.loads(Path(payload["inputs"]["evidence.json"]["path"]).read_text(encoding="utf-8"))
    title = str(evidence["metadata"]["title"])
    url = f"https://feishu.cn/docx/{document_id}"
    when = f"；下一轮不早于 {int(retry_at)}" if retry_at else "；等待只读核验"
    note = (f"{image_name}：失败阶段 {failure_stage}，原因 {reason_code}，"
            f"已额外重试 {retries_used}/2 次{when}")
    round_number = min(2, max(0, retries_used))
    action = "message-send" if round_number == 0 else f"message-send:retry-{round_number}"
    card = _delivery_card(title=title, main_url=url, status="processing", image_note=note)
    return send_status_card(
        store=store, lease=lease, payload=payload, root=root,
        artifact_key=f"{image_key}-recovery-notification", action=action,
        card=card, client=client, clock=clock,
    )


def final_delivery_receipt(store: WorkflowStore, job_id: str, *, main_url: str,
                           munger_embedded: bool) -> dict:
    artifacts = {}
    for item in store.artifacts(job_id):
        key = item["artifact_key"]
        if key.endswith("recovery-notification") or key == "final-notification":
            continue
        if not item["required"]:
            artifacts[key] = {"required": False,
                              "status": "confirmed" if item["status"] == "confirmed" else "not_required"}
            continue
        if item["required"]:
            artifacts[key] = {"required": True, "status": item["status"]}
    unresolved = [item["artifact_key"] for item in store.artifacts(job_id)
                  if item["required"] and item["status"] != "confirmed"
                  and item["artifact_key"] != "final-notification"]
    return {
        "schema": DELIVERY_RECEIPT_SCHEMA, "status": "succeeded",
        "main_url": main_url, "munger_embedded": munger_embedded,
        "artifacts": artifacts, "unresolved_actions": unresolved,
    }


def notify_final(*, store: WorkflowStore, lease, payload: dict, root: Path,
                 status: str, document_id: str | None, failure_reason: str | None = None,
                 client=None, clock=time.time) -> dict:
    evidence = json.loads(Path(payload["inputs"]["evidence.json"]["path"]).read_text(encoding="utf-8"))
    title = str(evidence["metadata"]["title"])
    main_url = f"https://feishu.cn/docx/{document_id}" if document_id else None
    if status == "succeeded":
        munger_embedded = any(item["artifact_key"] == "analysis:munger-analysis" and item["status"] == "confirmed"
                              for item in store.artifacts(lease.job_id))
        receipt = final_delivery_receipt(
            store, lease.job_id, main_url=main_url, munger_embedded=munger_embedded,
        )
    elif status == "needs_review":
        receipt = {
            "schema": DELIVERY_RECEIPT_SCHEMA, "status": "needs_review",
            "main_url": main_url, "munger_embedded": False,
            "artifacts": {}, "unresolved_actions": ["required-delivery-result"],
        }
    else:
        receipt = {
            "schema": DELIVERY_RECEIPT_SCHEMA, "status": "failed",
            "main_url": main_url, "munger_embedded": False,
            "artifacts": {}, "unresolved_actions": [],
        }
    card = _delivery_card(title=title, main_url=main_url, status=status,
                          failure_reason=failure_reason, receipt=receipt)
    return send_status_card(
        store=store, lease=lease, payload=payload, root=root,
        artifact_key="final-notification", action="message-send", card=card,
        client=client, clock=clock,
    )
