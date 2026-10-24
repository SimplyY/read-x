#!/usr/bin/env python3
"""Durable Bridge-content checkpoints with recorded effects; no remote calls."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import long_read_workflow_worker as worker
import long_read_document_supplement as document_supplement
from long_read_workflow_store import (
    LeaseConflict, WORKFLOW_CONTRACT_VERSION, WorkflowStore, workflow_payload_issue,
)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def verified_canary(payload: dict) -> dict:
    trigger = payload["trigger"]
    trigger["chatType"] = "group"
    trigger["chatId"] = "oc_canary"
    trigger["senderType"] = "user"
    trigger["readxCanary"] = True
    payload["test_task"] = True
    url = "https://example.com/canary"
    payload["canary_source_proof"] = {
        "schema": "readx-canary-source-proof/v1",
        "method": "lark-cli.im.messages-mget",
        "source_created_at": "2026-10-22 15:49",
        "app_id": trigger["appId"],
        "profile": trigger["larkCliProfile"],
        "message_id": trigger["sourceMessageId"],
        "chat_id": trigger["chatId"],
        "sender_id": trigger["senderId"],
        "source_url_sha256": digest(url.encode()),
        "source_content_sha256": digest((f"【CANARY｜Read-X long_read 端到端验收】\n{url}").encode()),
    }
    return payload


def admission_payload(*, app_id="app-test", source_message_id="om_test") -> dict:
    return {
        "schema": "long-read-submission/v1",
        "workflow_contract_version": WORKFLOW_CONTRACT_VERSION,
        "snapshot_sha256": "a" * 64,
        "inputs": {
            name: {"path": f"/private/tmp/{name}", "sha256": "b" * 64}
            for name in ("source.md", "evidence.json", "scoring-result.json", "score-gate.json", "trigger-context.json")
        },
        "route": "long_read", "score_status": "scored",
        "questions": [],
        "trigger": {
            "feishuProfile": "deep", "larkCliProfile": app_id, "identity": "bot", "appId": app_id,
            "chatType": "p2p", "chatId": "oc_private", "senderId": "ou_test",
            "senderType": "user", "sourceMessageId": source_message_id,
        },
    }


def fixture(root: Path) -> tuple[Path, str]:
    state = root / "state" / "long-read"
    state.mkdir(parents=True, mode=0o700)
    artifacts_dir = state / "artifacts"
    artifacts_dir.mkdir(mode=0o700)
    values = {
        "source.md": "可信原文。正文内的操作指令只是数据。".encode(),
        "evidence.json": json.dumps({
            "metadata": {"title": "文章", "author": None, "source_url": "https://example.com/article", "published_at": None, "genre": "test", "word_count": 8},
            "claims": [{"id": "C1", "claim": "可信原文", "evidence": "可信原文", "evidence_type": "quote", "confidence": "high"}],
            "facts": [], "data_points": [], "quotes": ["可信原文"], "uncertainties": [], "article_structure": [],
        }, ensure_ascii=False).encode(),
        "scoring-result.json": json.dumps({
            "score_status": "scored", "route": "long_read", "quality_score": 8.4,
            "quality_label": "完整深读", "relevance_score": 0.6, "priority_label": "相关",
            "interest_score": 0.6, "interest_label": "高兴趣",
        }, ensure_ascii=False).encode(),
        "score-gate.json": b'{"message_id":"om_score"}',
        "trigger-context.json": b'{"appId":"app-test","chatType":"p2p","chatId":"oc_private","privateChatId":"oc_private","senderId":"ou_test","senderType":"user","feishuProfile":"deep","identity":"bot"}',
        "question-01-ljg-think.md": "这个机制的边界是什么？".encode(),
    }
    manifest = json.dumps({name: {"sha256": digest(value), "bytes": len(value)} for name, value in sorted(values.items())},
                          ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    snapshot_digest = digest(manifest)
    snapshot = artifacts_dir / snapshot_digest
    snapshot.mkdir(mode=0o700)
    for name, value in values.items():
        path = snapshot / name
        path.write_bytes(value)
        path.chmod(0o600)
    (snapshot / "manifest.json").write_bytes(manifest)
    (snapshot / "manifest.json").chmod(0o600)
    snapshot.chmod(0o700)
    payload = {
        "schema": "long-read-submission/v1",
        "workflow_contract_version": WORKFLOW_CONTRACT_VERSION,
        "snapshot_sha256": snapshot_digest,
        "inputs": {name: {"path": str(snapshot / name), "sha256": digest(value)} for name, value in values.items()},
        "questions": [{"skill": "ljg-think", "file": "question-01-ljg-think.md"}],
        "route": "long_read", "score_status": "scored", "artifact_plan_finalized": False,
        "trigger": {
            "feishuProfile": "deep", "larkCliProfile": "app-test", "identity": "bot", "appId": "app-test",
            "chatType": "p2p", "chatId": "oc_private", "senderId": "ou_test",
            "senderType": "user", "sourceMessageId": "om_test",
        },
    }
    artifacts = [
        {"key": "analysis:article-decode", "kind": "analysis"},
        {"key": "analysis:munger-analysis", "kind": "analysis"},
        {"key": "analysis:document-supplement", "kind": "analysis"},
        {"key": "analysis:ljg-think", "kind": "analysis"},
        {"key": "main-document", "kind": "document"},
        {"key": "document-notification", "kind": "message"},
        {"key": "content-review-notification", "kind": "message", "required": False},
        {"key": "document-publish-review-notification", "kind": "message", "required": False},
        {"key": "final-notification", "kind": "message"},
        {"key": "core-image-recovery-notification", "kind": "message", "required": False},
        {"key": "core-image", "kind": "image"},
        {"key": "core-image-insert", "kind": "image_insert"},
        {"key": "core-image-send", "kind": "message"},
        {"key": "munger-image", "kind": "image", "required": False},
        {"key": "munger-image-insert", "kind": "image_insert", "required": False},
        {"key": "munger-image-send", "kind": "message", "required": False},
        {"key": "munger-image-recovery-notification", "kind": "message", "required": False},
    ]
    with WorkflowStore(state) as store:
        job = store.submit(payload, app_id="app-test", source_message_id="om_test", artifacts=artifacts)
    return state, job["job_id"]


class FakeBridge:
    def __init__(self, lose_response_for: str | None = None):
        self.results = {}
        self.submissions = []
        self.lose_response_for = lose_response_for
        self.lost = False

    @staticmethod
    def _text(prompt: str) -> str:
        if document_supplement.PROMPT_MARKER in prompt:
            return document_supplement_text()
        if "name: article-decode" in prompt:
            return (
                "> 证据边界：除原文明确陈述外，以下均为我的判断。\n"
                "# 这篇文章真正的核心\n文章真正的核心。\n"
                "# 基石 / 边缘 / 暗流\n作者的支点、边缘洞见与未审视前提。\n"
                "# 作者心中的结构\n问题由证据逐步展开。\n"
            )
        if "name: ljg-think" in prompt:
            return "# 深读\n" + "分析。" * 30
        return "# 芒格洞察\n" + "分析。" * 30

    def run(self, prompt: str, **kwargs):
        run_id = kwargs["request_id"]
        text = self._text(prompt)
        conversation = f"https://chatgpt.com/c/{run_id}"
        result = {
            "status": "succeeded", "runId": run_id, "conversationUrl": conversation,
            "actualConversationUrl": conversation, "submissionConfirmed": True,
            "format": "markdown", "verification": "live-dom+snapshot", "text": text,
            "outputSha256": digest(text.encode()), "recoveryReceiptStatus": "ready",
        }
        self.results[run_id] = result
        self.submissions.append((run_id, kwargs["caller_label"], kwargs["conversation_url"]))
        if self.lose_response_for and not self.lost and self.lose_response_for in prompt:
            self.lost = True
            raise RuntimeError("caller-exited-after-bridge-accepted")
        return result

    def observe(self, **kwargs):
        return self.results.get(kwargs["original_run_id"], {
            "status": "needs_review", "runId": kwargs["original_run_id"], "reason": "original-run-result-pending",
        })


def document_supplement_text() -> str:
    return """### 核心结论
文章的核心判断来自证据如何修正原有假设。

### 问题
1. 哪个证据能说明假设修正有效？
2. 在反馈延迟时，这种方法何时会失效？

### 上下文
- 原文用案例说明假设修正。
- 反馈延迟的影响仍未直接验证。
"""


def asset_fetcher(prompt_id: str) -> dict:
    content = "你是全文阅读分析助手。只根据本次输入完成芒格洞察。"
    return {
        "prompt_id": prompt_id, "prompt_source": "test", "prompt_revision": 1,
        "prompt_sha256": digest(content.encode()), "prompt_fetched_at": "2026-10-21T00:00:00Z",
        "content": content,
    }


def fake_effect(store, lease, artifact_key, action, request_sha):
    effect = store.find_effect(lease.job_id, artifact_key, action)
    if effect is None:
        effect = store.prepare_effect(lease, artifact_key, action, request_sha)
    if effect["status"] == "prepared":
        effect = store.begin_effect(lease, effect["op_key"])
        store.finish_effect(lease, effect["op_key"], "confirmed", remote_id=f"remote-{artifact_key}-{action}",
                            metadata={"verification": "fake-readback"})
    return store.get_effect(effect["op_key"])


def fake_notifications(*, event, store, lease, payload, root, **kwargs):
    if event == "document":
        artifact_key, action = "document-notification", "message-send"
    elif event == "content_review":
        artifact_key, action = "content-review-notification", "message-send"
    elif event == "document_review":
        artifact_key, action = "document-publish-review-notification", "message-send"
    elif event == "image_recovery":
        artifact_key = f"{kwargs['image_key']}-recovery-notification"
        retries = kwargs.get("retries_used", 0)
        action = "message-send" if not retries else f"message-send:retry-{retries}"
    else:
        artifact_key, action = "final-notification", "message-send"
    effect = fake_effect(store, lease, artifact_key, action,
                         digest(json.dumps({"event": event, "args": {key: value for key, value in kwargs.items()
                                                                      if key != "clock"}}, sort_keys=True).encode()))
    store.set_artifact(lease, artifact_key, "confirmed", metadata={"verification": "fake-readback"})
    return {"status": "confirmed", "message_id": effect.get("remote_id")}


def fake_image_delivery(*, store, lease, image_key, **kwargs):
    artifact = next(item for item in store.artifacts(lease.job_id) if item["artifact_key"] == image_key)
    round_number = artifact["retry_count"]
    for key, base_action in ((image_key, "image-generate"),
                             (f"{image_key}-insert", "image-block-create"),
                             (f"{image_key}-insert", "image-upload"),
                             (f"{image_key}-insert", "image-block-patch"),
                             (f"{image_key}-send", "message-image-upload"),
                             (f"{image_key}-send", "message-send")):
        previous = store.latest_effect_for_action(lease.job_id, key, base_action)
        if previous and previous["status"] == "confirmed":
            continue
        action = base_action if round_number == 0 else f"{base_action}:retry-{round_number}"
        fake_effect(store, lease, key, action, digest(f"{key}:{action}".encode()))
    store.set_artifact(lease, image_key, "confirmed", metadata={"verification": "fake-readback"})
    store.set_artifact(lease, f"{image_key}-insert", "confirmed", metadata={"verification": "fake-readback"})
    store.set_artifact(lease, f"{image_key}-send", "confirmed", metadata={"verification": "fake-readback"})
    return {"status": "confirmed", "image": image_key}


def fake_retrying_image_delivery(*, store, lease, clock, image_key, **kwargs):
    artifact = next(item for item in store.artifacts(lease.job_id) if item["artifact_key"] == image_key)
    if image_key == "core-image" and artifact["retry_count"] == 0 and artifact["status"] == "pending":
        effect = store.find_effect(lease.job_id, image_key, "image-generate")
        if effect is None:
            effect = store.prepare_effect(lease, image_key, "image-generate", digest(b"first-image-generation"))
        effect = store.begin_effect(lease, effect["op_key"])
        store.finish_effect(lease, effect["op_key"], "failed", metadata={
            "failure_stage": "image-generate", "reason_code": "test-temporary-failure",
        })
        retry_at = float(clock()) + 240
        store.set_artifact(lease, image_key, "failed", metadata={
            "failure_stage": "image-generate", "reason_code": "test-temporary-failure",
            "retryable": True, "next_retry_at": retry_at,
        })
        return {"status": "retry_scheduled", "retry_at": retry_at,
                "failure_stage": "image-generate", "reason_code": "test-temporary-failure"}
    if image_key == "core-image" and artifact["status"] == "failed":
        retry_at = artifact["metadata"]["next_retry_at"]
        if float(clock()) < retry_at:
            return {"status": "retry_scheduled", "retry_at": retry_at}
        store.consume_image_retry(lease, image_key)
    return fake_image_delivery(store=store, lease=lease, image_key=image_key, **kwargs)


class FakeDocumentCompiler:
    def __init__(self, *, publish_status: str = "confirmed", recovery_status: str = "uncertain"):
        self.publish_status = publish_status
        self.recovery_status = recovery_status
        self.publish_calls = 0
        self.recover_calls = 0
        self.recover_document_ids = []

    def prepare_plan(self, *, job_id, document_dir, candidate_xml, profile, identity):
        self.assertion_profile = profile
        self.assertion_identity = identity
        xml_sha = digest(Path(candidate_xml).read_bytes())
        return {"request_sha256": digest(f"{job_id}:{xml_sha}".encode()),
                "receipt": {"xml_sha256": xml_sha}}, {"profile": profile}

    def publish(self, *, plan, state_dir, env):
        self.publish_calls += 1
        if self.publish_status == "confirmed":
            return {"status": "confirmed", "remote_id": "doc_test_123",
                    "metadata": {"verification": "document-compiler-readback-matched",
                                 "content_sha256": plan["receipt"]["xml_sha256"]}}
        return {"status": self.publish_status, "reason_code": "publish-result-unknown"}

    def recover(self, *, plan, state_dir, env, document_id=None):
        self.recover_calls += 1
        self.recover_document_ids.append(document_id)
        if self.recovery_status == "confirmed":
            return {"status": "confirmed", "remote_id": document_id or "doc_test_123",
                    "metadata": {"verification": "document-compiler-readonly-recovery-matched",
                                 "content_sha256": plan["receipt"]["xml_sha256"]}}
        return {"status": "uncertain", "reason_code": "document-compiler-recovery-unconfirmed"}


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state, self.job_id = fixture(Path(self.temporary.name))

    def tearDown(self):
        self.temporary.cleanup()

    def test_v2_receipts_preserve_evidence_and_resume_without_fabricating_legacy_verification(self):
        bridge = FakeBridge()

        def run_v2(prompt, **kwargs):
            result = bridge.run(prompt, **kwargs)
            result.update(resultVersion=2, evidenceVersion=2, verification="bridge-evidence/v2",
                          deliveryClass="usable_with_warnings", candidateSha256=result["outputSha256"],
                          evidenceSummary={"identity": "matched", "generation": "complete", "body": "available",
                                           "truncation": "unknown", "candidateSha256": result["outputSha256"]})
            return result

        first = worker.process_job(
            self.state, self.job_id, bridge_fn=run_v2, observe_fn=bridge.observe,
            prompt_fetcher=asset_fetcher, document_compiler=FakeDocumentCompiler(publish_status="uncertain"),
            notification_fn=fake_notifications,
        )
        self.assertEqual(first["status"], "needs_review")
        receipt_path = self.state / "jobs" / self.job_id / "analysis" / "article-decode.md.receipt.json"
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt["verification"], "bridge-evidence/v2")
        self.assertEqual(receipt["candidateSha256"], receipt["evidenceSummary"]["candidateSha256"])
        before = len(bridge.submissions)
        second = worker.process_job(
            self.state, self.job_id,
            bridge_fn=lambda *_args, **_kwargs: self.fail("completed v2 request cannot be submitted twice"),
            observe_fn=lambda **_kwargs: self.fail("intact v2 receipt needs no browser observation"),
            prompt_fetcher=lambda _prompt_id: self.fail("cached v2 content must not refetch Prompt"),
            document_compiler=FakeDocumentCompiler(recovery_status="confirmed"),
            image_delivery_fn=fake_image_delivery, notification_fn=fake_notifications,
        )
        self.assertEqual(second["status"], "succeeded", second)
        self.assertEqual(len(bridge.submissions), before)

    def test_delivery_outcome_lost_acknowledgement_is_not_replayed_and_readback_stays_unknown(self):
        calls = []

        def lost(event):
            calls.append(event)
            raise RuntimeError("private details must not reach diagnostics")

        with WorkflowStore(self.state) as store:
            lease = store.acquire_lease(self.job_id, "outcome-test")
            with patch("sys.stderr", io.StringIO()) as stderr:
                for _ in range(2):
                    worker._record_delivery_outcomes(store, lease, {"status": "needs_review"}, lost)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["outcomeKind"], "task")
            self.assertEqual(calls[0]["deliveryStatus"], "unknown")
            self.assertEqual(calls[0]["readbackStatus"], "unknown")
            self.assertNotIn(self.job_id, json.dumps(calls[0]))
            self.assertNotIn("private details", stderr.getvalue())
            events = store.events(self.job_id)
            self.assertEqual(len([item for item in events if item["event_type"] == "business_outcome_intent"]), 1)
            self.assertFalse(any(item["event_type"] == "business_outcome_persistence" for item in events))

    def test_manual_candidate_receipt_stays_reusable_and_task_outcome_retains_human_intervention(self):
        with WorkflowStore(self.state) as store:
            lease = store.acquire_lease(self.job_id, "manual-receipt-test")
            calls = []

            def bridge(prompt, **kwargs):
                calls.append(kwargs["request_id"])
                text = "用户确认接受的候选正文"
                candidate_sha = digest(text.encode())
                return {"status": "succeeded", "runId": kwargs["request_id"], "format": "markdown", "text": text,
                        "submissionConfirmed": True, "conversationUrl": "https://chatgpt.com/c/manual", "actualConversationUrl": "https://chatgpt.com/c/manual",
                        "resultVersion": 2, "evidenceVersion": 2, "verification": "bridge-evidence/v2", "deliveryClass": "usable_with_warnings",
                        "outputSha256": candidate_sha, "candidateSha256": candidate_sha, "candidateRevision": 2,
                        "humanConfirmed": True, "completionSource": "user-manual",
                        "evidenceSummary": {"identity": "matched", "generation": "unknown", "body": "available", "truncation": "present", "candidateSha256": candidate_sha}}

            kwargs = dict(store=store, lease=lease, lock=threading.RLock(), artifact_key="analysis:article-decode",
                          output=self.state / "jobs" / self.job_id / "analysis" / "manual.md",
                          source_sha="a" * 64, prompt="authorized prompt", validate=lambda value: value, bridge_fn=bridge)
            first = worker._run_text_branch(**kwargs)
            self.assertEqual(first["status"], "confirmed")
            self.assertTrue(first["human_confirmed"])
            second = worker._run_text_branch(**kwargs)
            self.assertTrue(second["reused"])
            self.assertTrue(second["human_confirmed"])
            self.assertEqual(len(calls), 1)
            outcomes = []
            worker._record_delivery_outcomes(store, lease, {"status": "needs_review"}, outcomes.append)
            task = next(item for item in outcomes if item["outcomeKind"] == "task")
            self.assertTrue(task["humanConfirmed"])
            self.assertEqual(task["readbackStatus"], "unknown")

    def test_output_validation_failure_remains_distinct_in_shared_business_outcome(self):
        bridge = FakeBridge()
        with WorkflowStore(self.state) as store:
            lease = store.acquire_lease(self.job_id, "validation-outcome")
            job = store.get_job(self.job_id)
            source_path = Path(job["payload"]["inputs"]["source.md"]["path"])
            result = worker._run_text_branch(
                store=store, lease=lease, lock=threading.RLock(),
                artifact_key="analysis:article-decode",
                output=self.state / "jobs" / self.job_id / "analysis" / "article-decode.md",
                source_sha=digest(source_path.read_bytes()), prompt="isolated validation test",
                validate=lambda _value: (_ for _ in ()).throw(ValueError("private validation details")),
                prompt_assets=[], bridge_fn=bridge.run,
            )
            outcomes = []
            self.assertTrue(worker._record_read_x_text_outcome(
                outcomes.append, result, 12, store=store, lease=lease,
                lock=threading.RLock(),
            ))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "ValueError")
        self.assertEqual(result["failure_stage"], "output-validation")
        self.assertEqual(outcomes[0]["status"], "failed")
        self.assertEqual(outcomes[0]["deliveryStatus"], "unknown")
        self.assertEqual(outcomes[0]["businessValidationStatus"], "failed")
        self.assertEqual(outcomes[0]["failureStage"], "output-validation")
        self.assertEqual(outcomes[0]["errorCode"], "output-validation")

    def test_unexpected_outcome_recorder_exception_is_logged_without_exception_details(self):
        stderr = io.StringIO()
        result = {
            "request_id": "12345678-1234-5678-1234-567812345678",
            "artifact": "analysis:article-decode", "status": "needs_review",
            "reason": "response-text-unstable",
        }
        def fail_recorder(_event):
            raise OSError("PRIVATE PATH SENTINEL")
        with WorkflowStore(self.state) as store:
            lease = store.acquire_lease(self.job_id, "outcome-recorder-error")
            store.prepare_effect(lease, result["artifact"], "bridge-submit", "b" * 64,
                                 request_id=result["request_id"])
            with patch.object(worker.sys, "stderr", stderr):
                self.assertFalse(worker._record_read_x_text_outcome(
                    fail_recorder, result, 23, store=store, lease=lease,
                    lock=threading.RLock(),
                ))
        report = stderr.getvalue()
        self.assertIn("bridge-business-outcome-diagnostic-failed", report)
        self.assertIn("diagnostic-write-failed", report)
        self.assertIn(result["request_id"], report)
        self.assertNotIn("PRIVATE PATH SENTINEL", report)

    def test_outcome_recorder_without_durable_context_fails_closed(self):
        outcomes = []
        result = {"request_id": "12345678-1234-5678-1234-567812345678", "status": "confirmed"}
        with patch.object(worker.sys, "stderr", io.StringIO()) as stderr:
            self.assertFalse(worker._record_read_x_text_outcome(outcomes.append, result, 10))
        self.assertEqual(outcomes, [])
        self.assertIn("outcome-state-persistence-failed", stderr.getvalue())

    def test_deferred_notification_outbox_is_scheduled_while_job_needs_review(self):
        with WorkflowStore(self.state) as store:
            lease = store.acquire_lease(self.job_id, "deferred-notice")
            request = store.prepare_effect(
                lease, "content-review-notification", "message-send", digest(b"notice"),
            )
            store.set_artifact(lease, "content-review-notification", "pending", metadata={
                "failure_stage": "notification-send", "reason_code": "message-rate-limit",
                "next_retry_at": 1060.0,
            })
            store.set_status(lease, "needs_review")
            store.release_lease(lease)
            job = store.get_job(self.job_id)
            self.assertFalse(worker._job_is_due(store, job, 1059.0))
            self.assertTrue(worker._job_is_due(store, job, 1060.0))
            self.assertEqual(store.get_effect(request["op_key"])["status"], "prepared")

    def test_failed_notification_effect_carries_retry_due_time_through_crash_window(self):
        with WorkflowStore(self.state) as store:
            lease = store.acquire_lease(self.job_id, "crash-window-notice")
            request = store.prepare_effect(
                lease, "document-notification", "message-send", digest(b"notice"),
            )
            attempt = store.begin_effect(lease, request["op_key"])
            now = time.time()
            retry_at = now + worker.workflow_notifications.NOTIFICATION_RETRY_DELAY_SECONDS
            store.finish_effect(lease, attempt["op_key"], "failed", metadata={
                "reason_code": "api-rejected", "retryable": True, "next_retry_at": retry_at,
            })
            store.set_status(lease, "needs_review")
            store.release_lease(lease)

            artifact = next(item for item in store.artifacts(self.job_id)
                            if item["artifact_key"] == "document-notification")
            self.assertEqual(artifact["status"], "failed")
            self.assertEqual(artifact["metadata"]["next_retry_at"], retry_at)
            job = store.get_job(self.job_id)
            self.assertFalse(worker._job_is_due(store, job, retry_at - 1))
            self.assertTrue(worker._job_is_due(store, job, retry_at))

    def test_exhausted_notification_outbox_is_scheduled_only_for_paced_readback(self):
        with WorkflowStore(self.state) as store:
            lease = store.acquire_lease(self.job_id, "exhausted-notice")
            request = store.prepare_effect(
                lease, "document-notification", "message-send", digest(b"notice"),
            )
            for _ in range(worker.workflow_notifications.NOTIFICATION_RETRY_LIMIT + 1):
                attempt = store.begin_effect(lease, request["op_key"])
                store.finish_effect(
                    lease, attempt["op_key"], "failed",
                    metadata={"reason_code": "api-rejected", "retryable": True,
                              "next_reconcile_at": time.time() + worker.workflow_notifications.NOTIFICATION_RECONCILE_INTERVAL_SECONDS},
                )
            now = time.time()
            store.set_artifact(lease, "document-notification", "failed", metadata={
                "reason_code": "notification-retry-exhausted",
                "next_reconcile_at": now + worker.workflow_notifications.NOTIFICATION_RECONCILE_INTERVAL_SECONDS,
            })
            store.set_status(lease, "needs_review")
            store.release_lease(lease)
            job = store.get_job(self.job_id)
            self.assertFalse(worker._job_is_due(store, job, now + 1))
            self.assertTrue(worker._job_is_due(
                store, job, now + worker.workflow_notifications.NOTIFICATION_RECONCILE_INTERVAL_SECONDS,
            ))
            self.assertEqual(store.get_effect(request["op_key"])["status"], "failed")

    def test_failed_notification_outbox_retries_only_after_persisted_backoff(self):
        now = time.time()
        with WorkflowStore(self.state) as store:
            lease = store.acquire_lease(self.job_id, "retryable-notice")
            request = store.prepare_effect(
                lease, "document-notification", "message-send", digest(b"notice"),
            )
            attempt = store.begin_effect(lease, request["op_key"])
            retry_at = now + worker.workflow_notifications.NOTIFICATION_RETRY_DELAY_SECONDS
            store.finish_effect(lease, attempt["op_key"], "failed", metadata={
                "reason_code": "api-rejected", "retryable": True, "next_retry_at": retry_at,
            })
            store.set_status(lease, "needs_review")
            store.release_lease(lease)
            job = store.get_job(self.job_id)
            self.assertFalse(worker._job_is_due(store, job, retry_at - 1))
            self.assertTrue(worker._job_is_due(store, job, retry_at))

    def test_uncertain_notification_history_is_reconciled_on_its_persisted_due_time(self):
        with WorkflowStore(self.state) as store:
            lease = store.acquire_lease(self.job_id, "uncertain-notice")
            request = store.prepare_effect(
                lease, "document-notification", "message-send", digest(b"notice"),
            )
            attempt = store.begin_effect(lease, request["op_key"])
            store.finish_effect(
                lease, attempt["op_key"], "uncertain",
                metadata={"reason_code": "send-response-unknown"},
            )
            now = time.time()
            store.set_artifact(lease, "document-notification", "uncertain", metadata={
                "reason_code": "notification-history-incomplete",
                "next_reconcile_at": now + worker.workflow_notifications.NOTIFICATION_RECONCILE_INTERVAL_SECONDS,
            })
            store.set_status(lease, "needs_review")
            store.release_lease(lease)
            job = store.get_job(self.job_id)
            self.assertFalse(worker._job_is_due(store, job, now + 1))
            self.assertTrue(worker._job_is_due(
                store, job, now + worker.workflow_notifications.NOTIFICATION_RECONCILE_INTERVAL_SECONDS,
            ))

    def test_retryable_notification_with_uncertain_history_is_scheduled_before_budget_expires(self):
        with WorkflowStore(self.state) as store:
            lease = store.acquire_lease(self.job_id, "retryable-uncertain-notice")
            request = store.prepare_effect(
                lease, "document-notification", "message-send", digest(b"notice"),
            )
            attempt = store.begin_effect(lease, request["op_key"])
            now = time.time()
            store.finish_effect(lease, attempt["op_key"], "failed", metadata={
                "reason_code": "api-rejected", "retryable": True,
                "next_retry_at": now + worker.workflow_notifications.NOTIFICATION_RETRY_DELAY_SECONDS,
            })
            reconcile_at = now + worker.workflow_notifications.NOTIFICATION_RECONCILE_INTERVAL_SECONDS
            store.set_artifact(lease, "document-notification", "uncertain", metadata={
                "reason_code": "notification-history-incomplete",
                "next_reconcile_at": reconcile_at,
            })
            store.set_status(lease, "needs_review")
            store.release_lease(lease)
            job = store.get_job(self.job_id)

            self.assertFalse(worker._job_is_due(store, job, reconcile_at - 1))
            self.assertTrue(worker._job_is_due(store, job, reconcile_at))

    def test_explicit_worker_refuses_unadmitted_legacy_job_without_claiming_it(self):
        state = Path(self.temporary.name) / "legacy-state"
        with WorkflowStore(state) as store:
            job = store.submit({"schema": "long-read-submission/v1"}, app_id="legacy-app",
                               artifacts=[{"key": "old-analysis", "kind": "analysis"}])
            before_events = len(store.events(job["job_id"]))
            self.assertFalse(worker._job_is_due(store, store.get_job(job["job_id"]), time.time()))
        result = worker.process_job(
            state, job["job_id"],
            bridge_fn=lambda *_args, **_kwargs: self.fail("legacy job must not submit to Bridge"),
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason_code"], "unadmitted-legacy-job")
        with WorkflowStore(state) as store:
            current = store.get_job(job["job_id"])
            self.assertEqual(current["status"], "pending")
            self.assertIsNone(current["lease_owner"])
            self.assertEqual(len(store.events(job["job_id"])), before_events)

    def test_current_contract_marker_does_not_admit_incomplete_payload(self):
        state = Path(self.temporary.name) / "incomplete-state"
        with WorkflowStore(state) as store:
            job = store.submit({
                "workflow_contract_version": WORKFLOW_CONTRACT_VERSION,
                "schema": "long-read-submission/v1",
                "route": "long_read",
                "score_status": "scored",
            }, app_id="incomplete-app", artifacts=[{"key": "old-analysis", "kind": "analysis"}])
            before_events = len(store.events(job["job_id"]))
            self.assertFalse(worker._job_is_due(store, store.get_job(job["job_id"]), time.time()))
        result = worker.process_job(
            state, job["job_id"],
            bridge_fn=lambda *_args, **_kwargs: self.fail("incomplete payload must not submit to Bridge"),
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason_code"], "invalid-workflow-payload")
        with WorkflowStore(state) as store:
            current = store.get_job(job["job_id"])
            self.assertEqual(current["status"], "pending")
            self.assertIsNone(current["lease_owner"])
            self.assertEqual(len(store.events(job["job_id"])), before_events)

    def test_current_contract_marker_does_not_admit_traversal_question_or_mismatched_source(self):
        state = Path(self.temporary.name) / "malformed-question-state"
        payload = admission_payload(app_id="malformed-app", source_message_id="om_store-source")
        payload["inputs"]["question-01-ljg-think.md"] = {
            "path": "/private/tmp/question-01-ljg-think.md", "sha256": "c" * 64,
        }
        payload["questions"] = [{"skill": "../outside", "file": "question-01-ljg-think.md"}]
        self.assertEqual(workflow_payload_issue(
            payload, app_id="malformed-app", source_message_id="om_store-source",
        ), "invalid-workflow-payload")
        valid = admission_payload(app_id="malformed-app", source_message_id="om_store-source")
        self.assertIsNone(workflow_payload_issue(
            valid, app_id="malformed-app", source_message_id="om_store-source",
        ))
        self.assertEqual(workflow_payload_issue(
            valid, app_id="malformed-app", source_message_id="om_other-source",
        ), "invalid-workflow-payload")
        with WorkflowStore(state) as store:
            job = store.submit(payload, app_id="malformed-app", source_message_id="om_store-source",
                               artifacts=[{"key": "old-analysis", "kind": "analysis"}])
            before_events = len(store.events(job["job_id"]))
            self.assertFalse(worker._job_is_due(store, store.get_job(job["job_id"]), time.time()))
        result = worker.process_job(
            state, job["job_id"],
            bridge_fn=lambda *_args, **_kwargs: self.fail("mismatched payload must not submit to Bridge"),
        )
        self.assertEqual(result["reason_code"], "invalid-workflow-payload")
        with WorkflowStore(state) as store:
            current = store.get_job(job["job_id"])
            self.assertEqual(current["status"], "pending")
            self.assertIsNone(current["lease_owner"])
            self.assertEqual(len(store.events(job["job_id"])), before_events)

    def test_legacy_admitted_canary_without_explicit_identity_remains_recoverable(self):
        payload = verified_canary(admission_payload())
        payload["trigger"].pop("identity")
        self.assertIsNone(workflow_payload_issue(
            payload, app_id="app-test", source_message_id="om_test",
        ))

    def test_canary_marker_is_required_for_test_tasks_and_must_match(self):
        payload = admission_payload()
        payload["test_task"] = True
        self.assertEqual(workflow_payload_issue(
            payload, app_id="app-test", source_message_id="om_test",
        ), "invalid-workflow-payload")
        payload["trigger"]["readxCanary"] = False
        self.assertEqual(workflow_payload_issue(
            payload, app_id="app-test", source_message_id="om_test",
        ), "invalid-workflow-payload")
        verified_canary(payload)
        self.assertIsNone(workflow_payload_issue(
            payload, app_id="app-test", source_message_id="om_test",
        ))

    def test_canary_source_proof_is_required_and_bound_to_the_source(self):
        payload = verified_canary(admission_payload())
        payload.pop("canary_source_proof")
        self.assertEqual(workflow_payload_issue(
            payload, app_id="app-test", source_message_id="om_test",
        ), "invalid-workflow-payload")

        payload = verified_canary(admission_payload())
        payload["canary_source_proof"]["message_id"] = "om_other"
        self.assertEqual(workflow_payload_issue(
            payload, app_id="app-test", source_message_id="om_test",
        ), "invalid-workflow-payload")

    def test_test_task_without_canary_marker_is_rejected_before_lease(self):
        state = Path(self.temporary.name) / "unmarked-canary-state"
        payload = admission_payload()
        payload["test_task"] = True
        with WorkflowStore(state) as store:
            job = store.submit(payload, app_id="app-test", source_message_id="om_test",
                               artifacts=[{"key": "analysis", "kind": "analysis"}])
            before_events = len(store.events(job["job_id"]))
        result = worker.process_job(
            state, job["job_id"],
            bridge_fn=lambda *_args, **_kwargs: self.fail("unmarked canary must not submit to Bridge"),
            observe_fn=lambda *_args, **_kwargs: self.fail("unmarked canary must not observe Bridge"),
        )
        self.assertEqual(result["reason_code"], "invalid-workflow-payload")
        with WorkflowStore(state) as store:
            current = store.get_job(job["job_id"])
            self.assertEqual(current["status"], "pending")
            self.assertIsNone(current["lease_owner"])
            self.assertEqual(len(store.events(job["job_id"])), before_events)

    def test_persisted_trigger_source_mismatch_is_rejected_before_lease(self):
        state = Path(self.temporary.name) / "source-mismatch-state"
        payload = admission_payload(app_id="source-mismatch-app", source_message_id="om_payload-source")
        with WorkflowStore(state) as store:
            job = store.submit(payload, app_id="source-mismatch-app", source_message_id="om_database-source",
                               artifacts=[{"key": "analysis", "kind": "analysis"}])
            before_events = len(store.events(job["job_id"]))
        result = worker.process_job(
            state, job["job_id"],
            bridge_fn=lambda *_args, **_kwargs: self.fail("mismatched source must not submit to Bridge"),
            observe_fn=lambda *_args, **_kwargs: self.fail("mismatched source must not observe Bridge"),
            prompt_fetcher=lambda *_args, **_kwargs: self.fail("mismatched source must not fetch Prompt"),
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason_code"], "invalid-workflow-payload")
        with WorkflowStore(state) as store:
            current = store.get_job(job["job_id"])
            self.assertEqual(current["status"], "pending")
            self.assertIsNone(current["lease_owner"])
            self.assertEqual(len(store.events(job["job_id"])), before_events)

    def test_damaged_json_job_does_not_abort_due_scan(self):
        state = Path(self.temporary.name) / "damaged-json-state"
        with WorkflowStore(state) as store:
            damaged = store.submit({"legacy": True}, app_id="damaged-app",
                                   artifacts=[{"key": "old-analysis", "kind": "analysis"}])
            store.connection.execute("UPDATE jobs SET payload_json='{' WHERE job_id=?", (damaged["job_id"],))
            good = store.submit(admission_payload(app_id="good-app"), app_id="good-app", source_message_id="om_test",
                                artifacts=[{"key": "analysis", "kind": "analysis"}])
            # Force the damaged legacy row to sort ahead of the valid candidate.
            store.connection.execute("UPDATE jobs SET created_at=0 WHERE job_id=?", (damaged["job_id"],))

        calls = []
        with patch.object(worker, "process_job", side_effect=lambda _state, job_id, **_kwargs:
                          calls.append(job_id) or {"status": "blocked", "job_id": job_id}):
            worker.run_once(state)
        self.assertEqual(calls, [good["job_id"]])

    def test_due_worker_retries_a_deferred_recovery_notice_before_the_image_retry(self):
        state = Path(self.temporary.name) / "deferred-notice-state"
        with WorkflowStore(state, clock=lambda: 1000.0) as store:
            job = store.submit(admission_payload(app_id="app"), app_id="app", artifacts=[
                {"key": "core-image", "kind": "image"},
                {"key": "core-image-recovery-notification", "kind": "message", "required": False},
                {"key": "final-notification", "kind": "message"},
            ])
            lease = store.acquire_lease(job["job_id"], "test", 3600)
            store.set_artifact(lease, "core-image", "failed", metadata={
                "retryable": True, "next_retry_at": 1240.0,
                "failure_stage": "image-generate", "reason_code": "temporary",
            })
            store.prepare_effect(
                lease, "core-image-recovery-notification", "message-send", "a" * 64,
            )
            store.set_artifact(lease, "core-image-recovery-notification", "pending", metadata={
                "next_retry_at": 1060.0, "reason_code": "message-rate-limit",
            })
            store.set_status(lease, "waiting")
            store.release_lease(lease)
            current = store.get_job(job["job_id"])
            self.assertFalse(worker._job_is_due(store, current, 1059.0))
            self.assertTrue(worker._job_is_due(store, current, 1060.0))

    def test_worker_confirms_the_full_required_delivery_contract(self):
        bridge = FakeBridge()
        compiler = FakeDocumentCompiler()
        business_outcomes = []
        result = worker.process_job(
            self.state, self.job_id, bridge_fn=bridge.run, observe_fn=bridge.observe,
            prompt_fetcher=asset_fetcher, document_compiler=compiler,
            image_delivery_fn=fake_image_delivery, notification_fn=fake_notifications,
            outcome_recorder=business_outcomes.append,
        )
        self.assertEqual(result["status"], "succeeded", result)
        self.assertTrue(result["delivery_complete"])
        self.assertEqual(compiler.assertion_profile, "deep")
        self.assertEqual(compiler.assertion_identity, "bot")
        self.assertEqual(len(bridge.submissions), 4)
        self.assertTrue(all(item[1] == "read-x:long-read-text" for item in bridge.submissions))
        self.assertTrue(all(item[2] == "https://chatgpt.com/" for item in bridge.submissions))
        text_outcomes = [item for item in business_outcomes if item["caller"] == "read-x:long-read-text"]
        image_outcomes = [item for item in business_outcomes if item["caller"] == "read-x:long-read-image"]
        task_outcomes = [item for item in business_outcomes if item.get("outcomeKind") == "task"]
        self.assertEqual(len(text_outcomes), 4)
        self.assertEqual(len(image_outcomes), 2)
        self.assertEqual(len(task_outcomes), 1)
        self.assertTrue(all(item["source"] == "read-x" for item in business_outcomes))
        self.assertTrue(all(item["status"] == "succeeded" for item in business_outcomes))
        self.assertTrue(all(item["deliveryStatus"] == "unknown" for item in text_outcomes))
        self.assertTrue(all(item["readbackStatus"] == "unknown" for item in text_outcomes))
        self.assertTrue(all(item["deliveryStatus"] == item["readbackStatus"] == "succeeded" for item in image_outcomes + task_outcomes))
        self.assertEqual({item["attemptId"] for item in text_outcomes},
                         {item["runId"] for item in text_outcomes})
        self.assertTrue(all(not ({"prompt", "response", "conversationUrl"} & set(item))
                            for item in business_outcomes))
        with WorkflowStore(self.state) as store:
            job = store.get_job(self.job_id)
            self.assertEqual(job["status"], "succeeded")
            self.assertEqual({item["status"] for item in store.artifacts(self.job_id) if item["artifact_key"].startswith("analysis:")}, {"confirmed"})
            effects = store.connection.execute("SELECT request_id,status,action,metadata_json FROM effects WHERE job_id=?", (self.job_id,)).fetchall()
            bridge_effects = [item for item in effects if item["action"] == "bridge-submit"]
            self.assertEqual(len(bridge_effects), 4)
            self.assertTrue(all(item["status"] == "confirmed" for item in effects))
            self.assertTrue(all(isinstance(json.loads(item["metadata_json"]).get("duration_ms"), int)
                                for item in effects))
            self.assertEqual(len({item["request_id"] for item in bridge_effects}), 4)
            self.assertTrue(job["payload"]["artifact_plan_finalized"])
            self.assertIn("analysis:document-supplement", job["payload"]["required_artifacts"])
            self.assertEqual({item["artifact_key"]: item for item in store.artifacts(self.job_id)}
                             ["analysis:document-supplement"]["status"], "confirmed")
            self.assertIn("munger-image-insert", job["payload"]["required_artifacts"])
            self.assertIn("core-image-insert", job["payload"]["required_artifacts"])
        self.assertEqual(result["document_candidate"]["status"], "candidate_ready")
        self.assertTrue(Path(result["document_candidate"]["xml_path"]).is_file())
        self.assertTrue(Path(result["document_candidate"]["markdown_path"]).is_file())
        self.assertEqual(result["document_delivery"]["status"], "confirmed")
        self.assertEqual(result["document_delivery"]["document_id"], "doc_test_123")
        for path in (self.state / "jobs" / self.job_id / "analysis").iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_sequential_bridge_waits_renew_lease_before_a_competing_worker_can_take_over(self):
        now = [1000.0]
        bridge = FakeBridge()
        with patch.object(worker, "LONG_EXTERNAL_WAIT_LEASE_SECONDS", 0.02), \
                WorkflowStore(self.state, clock=lambda: now[0]) as store:
            lease = store.acquire_lease(self.job_id, "long-bridge-call", 0.02)
            job = store.get_job(self.job_id)
            source_path = Path(job["payload"]["inputs"]["source.md"]["path"])
            source_sha = digest(source_path.read_bytes())

            def slow_bridge(prompt, **kwargs):
                now[0] += 0.015
                if "ljg-think" in prompt:
                    with WorkflowStore(self.state, clock=lambda: now[0]) as contender:
                        with self.assertRaises(LeaseConflict):
                            contender.acquire_lease(self.job_id, "competing-worker", 30)
                return bridge.run(prompt, **kwargs)

            first = worker._run_text_branch(
                store=store, lease=lease, lock=threading.RLock(),
                artifact_key="analysis:article-decode",
                output=self.state / "jobs" / self.job_id / "analysis" / "article-decode.md",
                source_sha=source_sha, prompt="bounded bridge operation one",
                validate=lambda value: value, prompt_assets=[], bridge_fn=slow_bridge,
            )
            second = worker._run_text_branch(
                store=store, lease=lease, lock=threading.RLock(),
                artifact_key="analysis:ljg-think",
                output=self.state / "jobs" / self.job_id / "analysis" / "ljg-think.md",
                source_sha=source_sha, prompt="bounded bridge operation two ljg-think",
                validate=lambda value: value, prompt_assets=[], bridge_fn=slow_bridge,
            )

            self.assertEqual(first["status"], "confirmed", first)
            self.assertEqual(second["status"], "confirmed", second)
            self.assertGreater(store.get_job(self.job_id)["lease_expires_at"], now[0])

    def test_unconfirmed_required_image_never_sends_final_success(self):
        bridge = FakeBridge()
        notifications = []

        def uncertain_image(*, image_key, **_kwargs):
            self.assertEqual(image_key, "core-image")
            return {
                "status": "uncertain", "failure_stage": "image-insert",
                "reason_code": "image-insert-result-unconfirmed",
            }

        def record_notification(**kwargs):
            notifications.append(kwargs["event"])
            return fake_notifications(**kwargs)

        result = worker.process_job(
            self.state, self.job_id, bridge_fn=bridge.run, observe_fn=bridge.observe,
            prompt_fetcher=asset_fetcher, document_compiler=FakeDocumentCompiler(),
            image_delivery_fn=uncertain_image, notification_fn=record_notification,
        )
        self.assertEqual(result["status"], "needs_review", result)
        self.assertEqual(notifications, ["document", "image_recovery"])
        with WorkflowStore(self.state) as store:
            job = store.get_job(self.job_id)
            self.assertEqual(job["status"], "needs_review")
            self.assertIsNone(store.find_effect(self.job_id, "final-notification", "message-send"))
            artifacts = {item["artifact_key"]: item for item in store.artifacts(self.job_id)}
            self.assertEqual(artifacts["core-image"]["status"], "uncertain")

    def test_image_recovery_does_not_duplicate_confirmed_text_outcomes(self):
        outcomes = []

        def uncertain_image(*, store, lease, image_key, **_kwargs):
            effect = store.find_effect(lease.job_id, image_key, "image-generate")
            if effect is None:
                effect = store.prepare_effect(
                    lease, image_key, "image-generate", digest(b"unknown-image-result"),
                )
                effect = store.begin_effect(lease, effect["op_key"])
                store.finish_effect(lease, effect["op_key"], "uncertain", metadata={
                    "failure_stage": "image-generate",
                    "reason_code": "simulated-observation-pending",
                })
            return {"status": "uncertain", "failure_stage": "image-generate",
                    "reason_code": "simulated-observation-pending"}

        for _ in range(2):
            bridge = FakeBridge()
            result = worker.process_job(
                self.state, self.job_id, bridge_fn=bridge.run, observe_fn=bridge.observe,
                prompt_fetcher=asset_fetcher, document_compiler=FakeDocumentCompiler(),
                image_delivery_fn=uncertain_image, notification_fn=fake_notifications,
                outcome_recorder=outcomes.append,
            )
            self.assertEqual(result["status"], "needs_review", result)
        self.assertEqual(len([event for event in outcomes if event["caller"] == "read-x:long-read-text"]), 4)
        self.assertEqual(len(outcomes), 6)
        self.assertEqual(len({event["attemptId"] for event in outcomes}), 6)
        self.assertTrue(all(event["deliveryStatus"] == event["readbackStatus"] == "unknown" for event in outcomes))
        with WorkflowStore(self.state) as store:
            effects = store.connection.execute(
                "SELECT metadata_json FROM effects WHERE job_id=? AND action='bridge-submit'",
                (self.job_id,),
            ).fetchall()
            self.assertTrue(all(json.loads(item["metadata_json"]).get("business_outcome_persistence") == "recorded"
                                for item in effects))

    def test_failed_outcome_journal_remains_durable_and_is_not_blindly_retried(self):
        def uncertain_image(*, store, lease, image_key, **_kwargs):
            effect = store.find_effect(lease.job_id, image_key, "image-generate")
            if effect is None:
                effect = store.prepare_effect(
                    lease, image_key, "image-generate", digest(b"unknown-image-result"),
                )
                effect = store.begin_effect(lease, effect["op_key"])
                store.finish_effect(lease, effect["op_key"], "uncertain", metadata={
                    "failure_stage": "image-generate",
                    "reason_code": "simulated-observation-pending",
                })
            return {"status": "uncertain", "failure_stage": "image-generate",
                    "reason_code": "simulated-observation-pending"}

        def failed_recorder(_event):
            raise OSError("simulated shared journal unavailable")

        def run(recorder):
            bridge = FakeBridge()
            return worker.process_job(
                self.state, self.job_id, bridge_fn=bridge.run, observe_fn=bridge.observe,
                prompt_fetcher=asset_fetcher, document_compiler=FakeDocumentCompiler(),
                image_delivery_fn=uncertain_image, notification_fn=fake_notifications,
                outcome_recorder=recorder,
            )

        with patch.object(worker.sys, "stderr", io.StringIO()):
            first = run(failed_recorder)
        self.assertEqual(first["status"], "needs_review", first)
        recovered_outcomes = []
        second = run(recovered_outcomes.append)
        self.assertEqual(second["status"], "needs_review", second)
        self.assertEqual(recovered_outcomes, [])
        with WorkflowStore(self.state) as store:
            effects = store.connection.execute(
                "SELECT metadata_json FROM effects WHERE job_id=? AND action='bridge-submit'",
                (self.job_id,),
            ).fetchall()
            self.assertEqual(len(effects), 4)
            metadata = [json.loads(item["metadata_json"]) for item in effects]
            self.assertTrue(all(item.get("business_outcome_persistence") == "uncertain" for item in metadata))
            self.assertTrue(all(item.get("business_outcome_error_code") == "diagnostic-write-failed"
                                for item in metadata))

    def test_interrupted_outcome_write_is_left_uncertain_without_replay(self):
        bridge = FakeBridge()
        outcomes = []
        with WorkflowStore(self.state) as store:
            job = store.get_job(self.job_id)
            source_path = Path(job["payload"]["inputs"]["source.md"]["path"])
            lease = store.acquire_lease(self.job_id, "outcome-interruption")
            lock = threading.RLock()
            result = worker._run_text_branch(
                store=store, lease=lease, lock=lock, artifact_key="analysis:article-decode",
                output=self.state / "jobs" / self.job_id / "analysis" / "article-decode.md",
                source_sha=digest(source_path.read_bytes()), prompt="outcome interruption test",
                validate=lambda value: value, prompt_assets=[], bridge_fn=bridge.run,
                observe_fn=bridge.observe,
            )
            effect = store.find_effect(self.job_id, "analysis:article-decode", "bridge-submit")
            store.update_bridge_outcome_state(
                lease, effect["op_key"], outcome_value="succeeded", persistence="attempting",
            )
            self.assertFalse(worker._record_read_x_text_outcome(
                outcomes.append, result, 0, store=store, lease=lease, lock=lock,
            ))
            self.assertEqual(outcomes, [])
            recovered = store.get_effect(effect["op_key"])
            self.assertEqual(recovered["metadata"]["business_outcome_persistence"], "uncertain")
            self.assertEqual(recovered["metadata"]["business_outcome_error_code"], "outcome-write-interrupted")

    def test_outcome_state_write_failure_does_not_escape_or_call_shared_recorder(self):
        bridge = FakeBridge()
        outcomes = []
        with WorkflowStore(self.state) as store:
            job = store.get_job(self.job_id)
            source_path = Path(job["payload"]["inputs"]["source.md"]["path"])
            lease = store.acquire_lease(self.job_id, "outcome-db-failure")
            result = worker._run_text_branch(
                store=store, lease=lease,
                lock=threading.RLock(), artifact_key="analysis:article-decode",
                output=self.state / "jobs" / self.job_id / "analysis" / "article-decode.md",
                source_sha=digest(source_path.read_bytes()), prompt="outcome state failure test",
                validate=lambda value: value, prompt_assets=[], bridge_fn=bridge.run,
                observe_fn=bridge.observe,
            )
            with patch.object(store, "update_bridge_outcome_state",
                              side_effect=sqlite3.OperationalError("database is locked")):
                with patch.object(worker.sys, "stderr", io.StringIO()) as stderr:
                    self.assertFalse(worker._record_read_x_text_outcome(
                        outcomes.append, result, 12, store=store, lease=lease,
                        lock=threading.RLock(),
                    ))
            self.assertEqual(result["status"], "confirmed")
            self.assertEqual(outcomes, [])
            self.assertIn("outcome-state-persistence-failed", stderr.getvalue())

    def test_missing_bridge_effect_is_diagnosed_without_shared_outcome_write(self):
        request_id = "12345678-1234-5678-1234-567812345678"
        result = {"request_id": request_id, "status": "confirmed",
                  "artifact": "analysis:missing", "business_validation_status": "succeeded"}
        outcomes = []
        stderr = io.StringIO()
        with WorkflowStore(self.state) as store:
            lease = store.acquire_lease(self.job_id, "outcome-effect-missing")
            with patch.object(worker.sys, "stderr", stderr):
                recorded = worker._record_read_x_text_outcome(
                    outcomes.append, result, 20, store=store, lease=lease,
                    lock=threading.RLock(),
                )
                repeated = worker._record_read_x_text_outcome(
                    outcomes.append, result, 20, store=store, lease=lease,
                    lock=threading.RLock(),
                )
            self.assertFalse(recorded)
            self.assertFalse(repeated)
            self.assertEqual(outcomes, [])
            diagnostics = [event for event in store.events(self.job_id)
                           if event["event_type"] == "bridge_business_outcome_diagnostic"]
            self.assertEqual(len(diagnostics), 1)
            self.assertEqual(diagnostics[0]["details"]["reason_code"], "outcome-effect-missing")
            self.assertEqual(diagnostics[0]["details"]["request_id"], request_id)
        self.assertIn("outcome-effect-missing", stderr.getvalue())

    def test_mismatched_bridge_effect_request_is_diagnosed_without_shared_outcome_write(self):
        outcomes = []
        with WorkflowStore(self.state) as store:
            lease = store.acquire_lease(self.job_id, "outcome-effect-mismatch")
            effect = store.prepare_effect(lease, "analysis:article-decode", "bridge-submit", "a" * 64)
            result = {"request_id": "12345678-1234-5678-1234-567812345678",
                      "status": "confirmed", "artifact": "analysis:article-decode",
                      "business_validation_status": "succeeded"}
            with patch.object(worker.sys, "stderr", io.StringIO()) as stderr:
                recorded = worker._record_read_x_text_outcome(
                    outcomes.append, result, 20, store=store, lease=lease,
                    lock=threading.RLock(),
                )
            self.assertFalse(recorded)
            self.assertEqual(outcomes, [])
            diagnostics = [event for event in store.events(self.job_id)
                           if event["event_type"] == "bridge_business_outcome_diagnostic"]
            self.assertEqual(len(diagnostics), 1)
            self.assertEqual(diagnostics[0]["details"]["reason_code"], "outcome-effect-mismatch")
            self.assertEqual(diagnostics[0]["details"]["durable_request_id"], effect["request_id"])
        self.assertIn("outcome-effect-mismatch", stderr.getvalue())

    def test_missing_bridge_artifact_is_diagnosed_without_shared_outcome_write(self):
        outcomes = []
        result = {"request_id": "12345678-1234-5678-1234-567812345678", "status": "confirmed"}
        with WorkflowStore(self.state) as store:
            lease = store.acquire_lease(self.job_id, "outcome-artifact-missing")
            with patch.object(worker.sys, "stderr", io.StringIO()) as stderr:
                self.assertFalse(worker._record_read_x_text_outcome(
                    outcomes.append, result, 20, store=store, lease=lease,
                    lock=threading.RLock(),
                ))
            self.assertEqual(outcomes, [])
            diagnostics = [event for event in store.events(self.job_id)
                           if event["event_type"] == "bridge_business_outcome_diagnostic"]
            self.assertEqual(len(diagnostics), 1)
            self.assertEqual(diagnostics[0]["details"]["reason_code"], "outcome-artifact-missing")
        self.assertIn("outcome-artifact-missing", stderr.getvalue())

    def test_subprocess_kill_after_bridge_side_effect_recovers_same_request(self):
        with WorkflowStore(self.state) as store:
            job = store.get_job(self.job_id)
            source_path = Path(job["payload"]["inputs"]["source.md"]["path"])
            source_sha = digest(source_path.read_bytes())
        output = self.state / "jobs" / self.job_id / "analysis" / "article-decode.md"
        output.parent.mkdir(parents=True, exist_ok=True)
        marker = Path(self.temporary.name) / "remote-effect.json"
        child = r'''
import hashlib, json, os, sys, threading
from pathlib import Path
sys.path.insert(0, sys.argv[4])
from long_read_workflow_store import WorkflowStore
import long_read_workflow_worker as worker
worker.LONG_EXTERNAL_WAIT_LEASE_SECONDS = 0.05
_run_text_branch = worker._run_text_branch
state, job_id, marker, scripts = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), sys.argv[4]
prompt = "durable subprocess request"
prompt_sha = hashlib.sha256(prompt.encode()).hexdigest()
with WorkflowStore(state) as store:
    lease = store.acquire_lease(job_id, "killed-child", 0.05)
    job = store.get_job(job_id)
    source_path = Path(job["payload"]["inputs"]["source.md"]["path"])
    source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
    def remote_side_effect(_prompt, **kwargs):
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump({"request_id": kwargs["request_id"]}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os._exit(86)
    _run_text_branch(store=store, lease=lease, lock=threading.RLock(),
        artifact_key="analysis:article-decode", output=Path(sys.argv[5]),
        source_sha=source_sha, prompt=prompt, validate=lambda value: value,
        prompt_assets=[], bridge_fn=remote_side_effect)
'''
        child_result = subprocess.run(
            [sys.executable, "-c", child, str(self.state), self.job_id,
             str(marker), str(Path(__file__).parent), str(output)],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(child_result.returncode, 86, child_result.stderr)
        self.assertTrue(marker.is_file())
        remote = json.loads(marker.read_text(encoding="utf-8"))
        time.sleep(0.08)

        with WorkflowStore(self.state) as store:
            lease = store.acquire_lease(self.job_id, "parent-recovery", 30)
            observed = []
            text = "# Recovered after a real child-process exit\n"
            def observe(**kwargs):
                observed.append(kwargs["original_run_id"])
                self.assertEqual(kwargs["original_run_id"], remote["request_id"])
                return {
                    "status": "succeeded", "runId": remote["request_id"], "format": "markdown",
                    "verification": "live-dom+snapshot", "submissionConfirmed": True,
                    "conversationUrl": f"https://chatgpt.com/c/{remote['request_id']}",
                    "actualConversationUrl": f"https://chatgpt.com/c/{remote['request_id']}",
                    "text": text, "outputSha256": digest(text.encode()),
                }
            result = worker._run_text_branch(
                store=store, lease=lease, lock=threading.RLock(),
                artifact_key="analysis:article-decode", output=output,
                source_sha=digest(Path(job["payload"]["inputs"]["source.md"]["path"]).read_bytes()),
                prompt=None, prompt_sha=digest(b"durable subprocess request"),
                validate=lambda value: value, observe_fn=observe,
                bridge_fn=lambda *_args, **_kwargs: self.fail("uncertain work must be observed, never resubmitted"),
            )
            store.release_lease(lease)
            persisted = store.find_effect(self.job_id, "analysis:article-decode", "bridge-submit")
        self.assertEqual(result["status"], "confirmed", result)
        self.assertTrue(result["recovered"])
        self.assertEqual(observed, [remote["request_id"]])
        self.assertEqual(persisted["status"], "confirmed")
        self.assertEqual(persisted["request_id"], remote["request_id"])
        self.assertEqual(persisted["attempts"], 1)
        self.assertEqual(marker.read_text(encoding="utf-8"), json.dumps(remote))
        self.assertEqual(output.read_text(encoding="utf-8"), text.strip())

    def test_image_retry_waits_four_minutes_and_resumes_without_repeating_successful_stages(self):
        bridge = FakeBridge()
        now = [1000.0]
        kwargs = {
            "bridge_fn": bridge.run, "observe_fn": bridge.observe,
            "prompt_fetcher": asset_fetcher, "document_compiler": FakeDocumentCompiler(),
            "image_delivery_fn": fake_retrying_image_delivery,
            "notification_fn": fake_notifications, "clock": lambda: now[0],
        }
        first = worker.process_job(self.state, self.job_id, **kwargs)
        self.assertEqual(first["status"], "waiting", first)
        self.assertEqual(first["next_retry_at"], 1240.0)
        with WorkflowStore(self.state) as store:
            job = store.get_job(self.job_id)
            self.assertFalse(worker._job_is_due(store, job, 1239.0))
            self.assertTrue(worker._job_is_due(store, job, 1240.0))
            self.assertEqual(next(item for item in store.artifacts(self.job_id)
                                  if item["artifact_key"] == "munger-image")["status"], "confirmed")
        now[0] = 1240.0
        second = worker.process_job(self.state, self.job_id, **kwargs)
        self.assertEqual(second["status"], "succeeded", second)
        with WorkflowStore(self.state) as store:
            core = {item["artifact_key"]: item for item in store.artifacts(self.job_id)}
            self.assertEqual(core["core-image"]["retry_count"], 1)
            self.assertEqual(core["core-image"]["status"], "confirmed")
            self.assertEqual(core["munger-image"]["status"], "confirmed")
            effects = store.effects_for(self.job_id, "core-image")
            self.assertEqual({item["action"] for item in effects}, {"image-generate", "image-generate:retry-1"})

    def test_rate_deferred_recovery_notice_is_persisted_and_sent_before_image_retry(self):
        bridge = FakeBridge()
        now = [1000.0]
        notification_calls = []

        def notification_fn(*, event, store, lease, payload, root, **kwargs):
            if event == "image_recovery":
                notification_calls.append(float(now[0]))
                if len(notification_calls) == 1:
                    image_key = kwargs["image_key"]
                    retries = kwargs.get("retries_used", 0)
                    artifact_key = f"{image_key}-recovery-notification"
                    action = "message-send" if not retries else f"message-send:retry-{retries}"
                    request = {key: value for key, value in kwargs.items() if key != "clock"}
                    store.prepare_effect(
                        lease, artifact_key, action,
                        digest(json.dumps({"event": event, "args": request}, sort_keys=True).encode()),
                    )
                    return {"status": "deferred", "reason_code": "message-rate-limit"}
            return fake_notifications(event=event, store=store, lease=lease,
                                     payload=payload, root=root, **kwargs)

        kwargs = {
            "bridge_fn": bridge.run, "observe_fn": bridge.observe,
            "prompt_fetcher": asset_fetcher, "document_compiler": FakeDocumentCompiler(),
            "image_delivery_fn": fake_retrying_image_delivery,
            "notification_fn": notification_fn, "clock": lambda: now[0],
        }
        first = worker.process_job(self.state, self.job_id, **kwargs)
        self.assertEqual(first["status"], "waiting", first)
        self.assertEqual(first["stage"], "image-recovery-notification")
        self.assertEqual(first["next_retry_at"], 1060.0)
        with WorkflowStore(self.state) as store:
            artifacts = {item["artifact_key"]: item for item in store.artifacts(self.job_id)}
            self.assertEqual(artifacts["core-image-recovery-notification"]["status"], "pending")
            self.assertEqual(artifacts["core-image-recovery-notification"]["metadata"]["next_retry_at"], 1060.0)
            job = store.get_job(self.job_id)
            self.assertTrue(worker._job_is_due(store, job, 1059.0))
            self.assertTrue(worker._job_is_due(store, job, 1060.0))

        now[0] = 1059.0
        second = worker.process_job(self.state, self.job_id, **kwargs)
        self.assertEqual(second["status"], "waiting", second)
        self.assertEqual(second["next_retry_at"], 1060.0)
        self.assertEqual(notification_calls, [1000.0])

        now[0] = 1060.0
        third = worker.process_job(self.state, self.job_id, **kwargs)
        self.assertEqual(third["status"], "waiting", third)
        self.assertEqual(third["next_retry_at"], 1240.0)
        self.assertEqual(notification_calls, [1000.0, 1060.0, 1060.0])

        now[0] = 1240.0
        fourth = worker.process_job(self.state, self.job_id, **kwargs)
        self.assertEqual(fourth["status"], "succeeded", fourth)
        with WorkflowStore(self.state) as store:
            artifacts = {item["artifact_key"]: item for item in store.artifacts(self.job_id)}
            self.assertEqual(artifacts["core-image"]["retry_count"], 1)
            self.assertEqual(artifacts["core-image-recovery-notification"]["status"], "confirmed")

    def test_legacy_job_backfills_content_review_notice_before_sending(self):
        with WorkflowStore(self.state) as store:
            store.connection.execute(
                "DELETE FROM artifacts WHERE job_id=? AND artifact_key='content-review-notification'",
                (self.job_id,),
            )
        bridge = FakeBridge(lose_response_for="name: ljg-think")
        notices = []

        def record_notice(**kwargs):
            notices.append(kwargs["event"])
            return fake_notifications(**kwargs)

        result = worker.process_job(
            self.state, self.job_id, bridge_fn=bridge.run, observe_fn=bridge.observe,
            prompt_fetcher=asset_fetcher, document_compiler=FakeDocumentCompiler(),
            notification_fn=record_notice,
        )
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(notices, ["content_review"])
        self.assertEqual(result["content_review_notification"]["status"], "confirmed")
        with WorkflowStore(self.state) as store:
            artifact = next(item for item in store.artifacts(self.job_id)
                            if item["artifact_key"] == "content-review-notification")
            self.assertEqual((artifact["kind"], artifact["required"], artifact["status"]),
                             ("message", 0, "confirmed"))
            self.assertEqual(sum(event["event_type"] == "optional_artifact_added"
                                 for event in store.events(self.job_id)), 1)

    def test_lost_bridge_response_recovers_original_request_without_second_submission(self):
        bridge = FakeBridge(lose_response_for="name: ljg-think")
        outcomes = []
        first = worker.process_job(
            self.state, self.job_id, bridge_fn=bridge.run, observe_fn=bridge.observe,
            prompt_fetcher=asset_fetcher, document_compiler=FakeDocumentCompiler(),
            notification_fn=fake_notifications, outcome_recorder=outcomes.append,
        )
        self.assertEqual(first["status"], "needs_review")
        self.assertEqual(first["content_review_notification"]["status"], "confirmed")
        self.assertEqual(len(bridge.submissions), 3)
        original_count = len(bridge.submissions)
        calls = []

        def counted_observe(**kwargs):
            calls.append(kwargs["original_run_id"])
            return bridge.observe(**kwargs)

        second = worker.process_job(
            self.state, self.job_id, bridge_fn=bridge.run, observe_fn=counted_observe,
            prompt_fetcher=lambda _prompt_id: (_ for _ in ()).throw(AssertionError("resume must not refetch or resubmit Prompt")),
            document_compiler=FakeDocumentCompiler(),
            image_delivery_fn=fake_image_delivery, notification_fn=fake_notifications,
            outcome_recorder=outcomes.append,
        )
        self.assertEqual(second["status"], "succeeded", second)
        self.assertEqual(len(bridge.submissions), original_count + 1)
        self.assertEqual(len(calls), 1)
        recovered_events = [event for event in outcomes if event["attemptId"] in calls]
        self.assertEqual([event["status"] for event in recovered_events], ["needs_review", "succeeded"])
        self.assertTrue((self.state / "jobs" / self.job_id / "analysis" / "ljg-think.md").is_file())
        munger_receipt = self.state / "jobs" / self.job_id / "analysis" / "munger-analysis.md.receipt.json"
        self.assertEqual(json.loads(munger_receipt.read_text(encoding="utf-8"))["prompt_assets"][0]["prompt_id"],
                         "read-x.munger-analysis")
        request_meta = self.state / "jobs" / self.job_id / "analysis" / "munger-analysis.md.request.json"
        self.assertEqual(request_meta.stat().st_mode & 0o777, 0o600)
        with WorkflowStore(self.state) as store:
            self.assertEqual(store.get_job(self.job_id)["status"], "succeeded")
            self.assertTrue(all(item["status"] == "confirmed" for item in store.artifacts(self.job_id) if item["artifact_key"].startswith("analysis:")))

    def test_prompt_governance_failure_isolated_to_munger_branch(self):
        bridge = FakeBridge()
        result = worker.process_job(
            self.state, self.job_id, bridge_fn=bridge.run, observe_fn=bridge.observe,
            prompt_fetcher=lambda _prompt_id: (_ for _ in ()).throw(RuntimeError("governance unavailable")),
            document_compiler=FakeDocumentCompiler(), notification_fn=fake_notifications,
        )
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(len(bridge.submissions), 3)
        with WorkflowStore(self.state) as store:
            statuses = {item["artifact_key"]: item for item in store.artifacts(self.job_id)}
            self.assertEqual(statuses["analysis:article-decode"]["status"], "confirmed")
            self.assertEqual(statuses["analysis:ljg-think"]["status"], "confirmed")
            self.assertEqual(statuses["analysis:munger-analysis"]["status"], "failed")
            self.assertEqual(statuses["analysis:munger-analysis"]["metadata"]["failure_stage"], "prompt-governance")

    def test_prepared_munger_request_survives_prompt_read_failure_for_later_resume(self):
        with WorkflowStore(self.state) as store:
            job = store.get_job(self.job_id)
            payload, snapshot, source, _ = worker._load_job_inputs(job)
            asset = asset_fetcher("read-x.munger-analysis")
            prompt = worker.munger.build_prompt(source, [asset])
            lease = store.acquire_lease(self.job_id, "prepare-munger")
            prepared = store.prepare_effect(
                lease, "analysis:munger-analysis", "bridge-submit",
                hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            )
            store.release_lease(lease)

        bridge = FakeBridge()
        first = worker.process_job(
            self.state, self.job_id, bridge_fn=bridge.run, observe_fn=bridge.observe,
            prompt_fetcher=lambda _prompt_id: (_ for _ in ()).throw(RuntimeError("governance unavailable")),
            document_compiler=FakeDocumentCompiler(), notification_fn=fake_notifications,
        )
        self.assertEqual(first["status"], "needs_review")
        with WorkflowStore(self.state) as store:
            self.assertEqual(store.get_effect(prepared["op_key"])["status"], "prepared")

        second = worker.process_job(
            self.state, self.job_id, bridge_fn=bridge.run, observe_fn=bridge.observe,
            prompt_fetcher=asset_fetcher, document_compiler=FakeDocumentCompiler(),
            image_delivery_fn=fake_image_delivery, notification_fn=fake_notifications,
        )
        self.assertEqual(second["status"], "succeeded", second)

    def test_document_publish_response_loss_recovers_without_second_publish(self):
        bridge = FakeBridge()
        compiler = FakeDocumentCompiler(publish_status="uncertain", recovery_status="uncertain")
        notifications = []
        def notice(**kwargs):
            notifications.append(kwargs["event"])
            return fake_notifications(**kwargs)
        first = worker.process_job(
            self.state, self.job_id, bridge_fn=bridge.run, observe_fn=bridge.observe,
            prompt_fetcher=asset_fetcher, document_compiler=compiler, notification_fn=notice,
        )
        self.assertEqual(first["status"], "needs_review")
        self.assertEqual(first["document_review_notification"]["status"], "confirmed")
        self.assertEqual(notifications, ["document_review"])
        self.assertEqual(first["document_delivery"]["status"], "uncertain")
        self.assertEqual(compiler.publish_calls, 1)
        self.assertEqual(compiler.recover_calls, 1)
        second = worker.process_job(
            self.state, self.job_id, bridge_fn=bridge.run, observe_fn=bridge.observe,
            prompt_fetcher=asset_fetcher, document_compiler=compiler, notification_fn=notice,
            image_delivery_fn=fake_image_delivery,
        )
        self.assertEqual(second["document_delivery"]["status"], "uncertain")
        self.assertEqual(compiler.publish_calls, 1)
        self.assertEqual(compiler.recover_calls, 2)
        with WorkflowStore(self.state) as store:
            effects = [item for item in store.connection.execute(
                "SELECT * FROM effects WHERE job_id=? AND action='document-publish'", (self.job_id,)
            )]
            self.assertEqual(len(effects), 1)
            self.assertEqual(effects[0]["status"], "uncertain")

    def test_manual_document_id_only_recovers_an_uncertain_publish_and_continues(self):
        bridge = FakeBridge()
        compiler = FakeDocumentCompiler(publish_status="uncertain", recovery_status="uncertain")
        first = worker.process_job(
            self.state, self.job_id, bridge_fn=bridge.run, observe_fn=bridge.observe,
            prompt_fetcher=asset_fetcher, document_compiler=compiler,
            notification_fn=fake_notifications,
        )
        self.assertEqual(first["status"], "needs_review")
        self.assertEqual(first["document_delivery"]["status"], "uncertain")
        self.assertEqual(compiler.publish_calls, 1)

        compiler.recovery_status = "confirmed"
        second = worker.process_job(
            self.state, self.job_id, bridge_fn=bridge.run, observe_fn=bridge.observe,
            prompt_fetcher=asset_fetcher, document_compiler=compiler,
            image_delivery_fn=fake_image_delivery, notification_fn=fake_notifications,
            manual_document_id="doc_test_123",
        )
        self.assertEqual(compiler.recover_document_ids, [None, "doc_test_123"])
        self.assertEqual(compiler.recover_document_ids[-1], "doc_test_123")
        self.assertEqual(compiler.publish_calls, 1)
        self.assertEqual(second["document_delivery"]["status"], "confirmed")

    def test_manual_document_id_is_rejected_before_workflow_advancement_when_invalid(self):
        bridge = FakeBridge()
        result = worker.process_job(
            self.state, self.job_id, bridge_fn=bridge.run, observe_fn=bridge.observe,
            manual_document_id="../other",
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason_code"], "invalid-document-id")
        self.assertEqual(bridge.submissions, [])

    def test_unconfirmed_document_review_notice_is_reconciled_before_final_success(self):
        bridge = FakeBridge()
        compiler = FakeDocumentCompiler(publish_status="uncertain", recovery_status="uncertain")
        review_reasons = []
        review_attempts = []
        now = [time.time()]

        def notice(**kwargs):
            if kwargs["event"] != "document_review":
                return fake_notifications(**kwargs)
            store, lease = kwargs["store"], kwargs["lease"]
            reason = kwargs["reason_code"]
            review_reasons.append(reason)
            request_sha = digest(json.dumps({"reason_code": reason}, sort_keys=True).encode())
            effect = store.find_effect(lease.job_id, "document-publish-review-notification", "message-send")
            if effect is None:
                effect = store.prepare_effect(
                    lease, "document-publish-review-notification", "message-send", request_sha,
                )
            else:
                self.assertEqual(effect["request_sha256"], request_sha)
            review_attempts.append(effect["status"])
            if effect["status"] == "prepared":
                effect = store.begin_effect(lease, effect["op_key"])
                store.finish_effect(
                    lease, effect["op_key"], "uncertain",
                    metadata={"failure_stage": "notification-send", "reason_code": "lost-response",
                              "next_reconcile_at": now[0] + worker.workflow_notifications.NOTIFICATION_RECONCILE_INTERVAL_SECONDS},
                )
                return {"status": "uncertain", "reason_code": "notification-send-result-unconfirmed"}
            if effect["status"] == "uncertain":
                store.reconcile_effect(
                    lease, effect["op_key"], "confirmed", remote_id="review-message",
                    metadata={"verification": "exact-card-readback"},
                )
                store.set_artifact(lease, "document-publish-review-notification", "confirmed", metadata={
                    "failure_stage": "document-publish", "reason_code": reason,
                    "verification": "exact-card-readback",
                })
                return {"status": "confirmed", "message_id": "review-message", "recovered": True}
            self.fail(f"unexpected review notification effect state: {effect['status']}")

        first = worker.process_job(
            self.state, self.job_id, bridge_fn=bridge.run, observe_fn=bridge.observe,
            prompt_fetcher=asset_fetcher, document_compiler=compiler, notification_fn=notice,
            clock=lambda: now[0],
        )
        self.assertEqual(first["status"], "needs_review")
        self.assertEqual(first["document_review_notification"]["status"], "uncertain")
        with WorkflowStore(self.state) as store:
            review_effect = store.find_effect(self.job_id, "document-publish-review-notification", "message-send")
            self.assertEqual(review_effect["status"], "uncertain")
            persisted_review = next(item for item in store.artifacts(self.job_id)
                                    if item["artifact_key"] == "document-publish-review-notification")
            self.assertTrue(persisted_review["metadata"]["reason_code"])

        compiler.recovery_status = "confirmed"
        now[0] += worker.workflow_notifications.NOTIFICATION_RECONCILE_INTERVAL_SECONDS
        second = worker.process_job(
            self.state, self.job_id, bridge_fn=bridge.run, observe_fn=bridge.observe,
            prompt_fetcher=asset_fetcher, document_compiler=compiler,
            image_delivery_fn=fake_image_delivery, notification_fn=notice, clock=lambda: now[0],
        )
        self.assertEqual(second["status"], "succeeded", second)
        self.assertEqual(review_attempts, ["prepared", "uncertain"])
        self.assertEqual(review_reasons[0], review_reasons[1])
        self.assertEqual(compiler.publish_calls, 1)
        self.assertEqual(compiler.recover_calls, 2)
        with WorkflowStore(self.state) as store:
            job = store.get_job(self.job_id)
            review_effect = store.find_effect(self.job_id, "document-publish-review-notification", "message-send")
            final_effect = store.find_effect(self.job_id, "final-notification", "message-send")
            self.assertEqual(job["status"], "succeeded")
            self.assertEqual(review_effect["status"], "confirmed")
            self.assertEqual(final_effect["status"], "confirmed")

    def test_mismatched_local_receipt_is_not_reused(self):
        bridge = FakeBridge()
        compiler = FakeDocumentCompiler(publish_status="uncertain", recovery_status="uncertain")
        first = worker.process_job(
            self.state, self.job_id, bridge_fn=bridge.run, observe_fn=bridge.observe,
            prompt_fetcher=asset_fetcher, document_compiler=compiler,
            notification_fn=fake_notifications,
        )
        self.assertEqual(first["status"], "needs_review")
        receipt = self.state / "jobs" / self.job_id / "analysis" / "article-decode.md.receipt.json"
        record = json.loads(receipt.read_text(encoding="utf-8"))
        record["caller_label"] = "another-caller"
        receipt.write_text(json.dumps(record), encoding="utf-8")
        observed = []

        def observe(**kwargs):
            observed.append(kwargs["original_run_id"])
            return bridge.observe(**kwargs)

        count = len(bridge.submissions)
        second = worker.process_job(
            self.state, self.job_id, bridge_fn=bridge.run, observe_fn=observe,
            prompt_fetcher=lambda _prompt_id: (_ for _ in ()).throw(AssertionError("confirmed Bridge work must not refetch the Prompt")),
            document_compiler=FakeDocumentCompiler(recovery_status="confirmed"),
            image_delivery_fn=fake_image_delivery, notification_fn=fake_notifications,
        )
        self.assertEqual(second["status"], "succeeded")
        self.assertEqual(len(bridge.submissions), count)
        self.assertEqual(len(observed), 1)


if __name__ == "__main__":
    unittest.main()
