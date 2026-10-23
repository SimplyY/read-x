#!/usr/bin/env python3
"""Notification outbox tests use a fake Feishu endpoint only."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import long_read_workflow_worker as workflow_worker
from feishu_long_read_delivery import DeliveryError, RetryableDeliveryError, UnknownRemoteResult
from long_read_workflow_notifications import (
    NOTIFICATION_RECONCILE_INTERVAL_SECONDS, final_delivery_receipt,
    notify_content_review, notify_final, send_status_card,
    _correlate_card, _preflight, _target,
)
from long_read_workflow_store import VERIFIED_EFFECTS_BY_KIND, WorkflowStore
from render_long_read_delivery_card import REQUIRED_SUCCESS_ARTIFACTS, render_card


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class FakeMessages:
    def __init__(self, history=None, *, lose_send=False, lose_before_write=False):
        self.history = list(history or [])
        self.lose_send = lose_send
        self.lose_before_write = lose_before_write
        self.sends = 0
        self.history_reads = 0
        self.fail_history = False
        self.identity_ok = True

    def verify_app_identity(self, expected_app_id):
        if not self.identity_ok or expected_app_id != "app_bot":
            raise DeliveryError("simulated bot profile mismatch")
        return True

    def list_chat_messages(self, *, chat_id, start_time, end_time):
        self.history_reads += 1
        if self.fail_history:
            raise DeliveryError("simulated incomplete history")
        return [item for item in self.history
                if start_time <= (item["create_time"] // 1000 if item["create_time"] >= 100_000_000_000
                                  else item["create_time"]) <= end_time]

    def send_interactive_card(self, *, card, idempotency_key, chat_id=None, user_id=None):
        self.sends += 1
        if self.lose_before_write:
            self.lose_before_write = False
            raise UnknownRemoteResult("response lost before remote write")
        message_id = f"om_{self.sends}"
        actual_chat = chat_id or "oc_private"
        self.history.append({
            "message_id": message_id, "msg_type": "interactive", "chat_id": actual_chat,
            "create_time": 1000, "sender": {"id": "app_bot", "id_type": "app_id"},
            "body": {"content": json.dumps(card, ensure_ascii=False)},
        })
        if self.lose_send:
            self.lose_send = False
            raise UnknownRemoteResult("response lost")
        return message_id

    def get_message(self, message_id):
        return next(item for item in self.history if item["message_id"] == message_id)


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name) / "state"
        self.trigger = {
            "appId": "app_bot", "feishuProfile": "bridge-read-x", "larkCliProfile": "app_bot", "identity": "bot",
            "chatType": "p2p", "chatId": "oc_private", "privateChatId": "oc_private",
            "senderId": "ou_user", "senderType": "user",
        }
        evidence_path = Path(self.temporary.name) / "evidence.json"
        evidence_path.write_text(json.dumps({"metadata": {"title": "文章"}}), encoding="utf-8")
        self.payload = {
            "trigger": self.trigger,
            "inputs": {"evidence.json": {"path": str(evidence_path)}},
        }
        self.clock_value = [1000.0]
        self.store = WorkflowStore(self.state, clock=lambda: self.clock_value[0])
        job = self.store.submit(self.payload, app_id="app_bot", artifacts=[
            {"key": "document-notification", "kind": "message"},
            {"key": "content-review-notification", "kind": "message", "required": False},
        ])
        self.job_id = job["job_id"]
        self.lease = self.store.acquire_lease(self.job_id, "notify-test", 3600)
        self.card = {"schema": "2.0", "header": {"title": {"content": "处理中"}}}

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def send(self, client, *, now=None):
        now = now or (lambda: self.clock_value[0])
        return send_status_card(
            store=self.store, lease=self.lease, payload=self.payload, root=self.state,
            artifact_key="document-notification", action="message-send", card=self.card,
            client=client, clock=now,
        )

    def test_group_trigger_without_private_chat_id_is_rejected_without_send(self):
        self.payload["trigger"] = {**self.trigger, "chatType": "group", "privateChatId": None}
        client = FakeMessages()
        result = self.send(client)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(client.sends, 0)
        self.assertIsNone(_target(self.payload["trigger"]))

    def test_group_notification_uses_matching_verified_report_target(self):
        self.payload["trigger"] = {**self.trigger, "chatType": "group", "privateChatId": None}
        target = {
            "identity": "bot", "profile": "app_bot", "user_id": "ou_user",
            "chat_id": "oc_private", "sender_id": "app_bot", "sender_id_type": "app_id",
        }
        with self.store._transaction():
            self.store.connection.execute(
                "INSERT INTO workflow_meta(key,value) VALUES('report_delivery_target',?)",
                (json.dumps(target),),
            )
        client = FakeMessages()

        result = self.send(client)

        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(client.sends, 1)
        self.assertEqual(client.history[0]["chat_id"], "oc_private")

    def test_group_notification_rejects_report_target_for_another_user_or_app(self):
        self.payload["trigger"] = {**self.trigger, "chatType": "group", "privateChatId": None}
        target = {
            "identity": "bot", "profile": "other_profile", "user_id": "ou_other",
            "chat_id": "oc_other", "sender_id": "other_app", "sender_id_type": "app_id",
        }
        with self.store._transaction():
            self.store.connection.execute(
                "INSERT INTO workflow_meta(key,value) VALUES('report_delivery_target',?)",
                (json.dumps(target),),
            )
        client = FakeMessages()

        result = self.send(client)

        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(client.sends, 0)

    def test_four_recent_sends_defer_before_a_fifth_send(self):
        history = [{
            "message_id": f"om_old_{index}", "msg_type": "text", "chat_id": "oc_private",
            "create_time": 999, "sender": {"id": "app_bot", "id_type": "app_id"},
            "body": {"content": "{}"},
        } for index in range(4)]
        client = FakeMessages(history)
        self.assertFalse(_preflight(client, chat_id="oc_private", sender_id="app_bot", now=1000.0))
        result = self.send(client)
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(client.sends, 0)

    def test_unreadable_recent_sender_blocks_send(self):
        client = FakeMessages([{
            "message_id": "om_unknown", "msg_type": "text", "chat_id": "oc_private",
            "create_time": 999, "sender": {}, "body": {"content": "{}"},
        }])
        result = self.send(client)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(client.sends, 0)

    def test_unverified_bot_profile_stops_before_status_card_send(self):
        client = FakeMessages()
        client.identity_ok = False

        result = self.send(client)

        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["reason_code"], "notification-identity-unverified")
        self.assertEqual(client.sends, 0)

    def test_millisecond_create_time_counts_in_the_seconds_window(self):
        history = [{
            "message_id": f"om_recent_{index}", "msg_type": "text", "chat_id": "oc_private",
            "create_time": 1_789_999_999_000, "sender": {"id": "app_bot", "id_type": "app_id"},
            "body": {"content": "{}"},
        } for index in range(4)]
        client = FakeMessages(history)
        self.assertFalse(_preflight(client, chat_id="oc_private", sender_id="app_bot", now=1_790_000_000.0))

    def test_lost_send_response_is_recovered_from_exact_card_history(self):
        client = FakeMessages(lose_send=True)
        now = [1000.0]
        first = self.send(client, now=lambda: now[0])
        self.assertEqual(first["status"], "uncertain")
        second = self.send(client, now=lambda: now[0] + NOTIFICATION_RECONCILE_INTERVAL_SECONDS - 1)
        self.assertEqual(second["status"], "uncertain")
        now[0] += NOTIFICATION_RECONCILE_INTERVAL_SECONDS
        second = self.send(client, now=lambda: now[0])
        self.assertEqual(second["status"], "confirmed")
        self.assertTrue(second["recovered"])
        self.assertEqual(client.sends, 1)
        artifact = next(item for item in self.store.artifacts(self.job_id)
                        if item["artifact_key"] == "document-notification")
        self.assertEqual(artifact["status"], "confirmed")

    def test_interrupted_accepted_card_uses_original_intent_time_for_readback(self):
        class AcceptedThenInterrupted(FakeMessages):
            def send_interactive_card(self, **kwargs):
                super().send_interactive_card(**kwargs)
                raise KeyboardInterrupt("worker stopped after remote acceptance")

        client = AcceptedThenInterrupted()
        with self.assertRaises(KeyboardInterrupt):
            self.send(client)

        self.clock_value[0] += 120
        result = self.send(client)

        self.assertEqual(result["status"], "confirmed")
        self.assertTrue(result["recovered"])
        self.assertEqual(client.sends, 1)

    def test_rejected_after_write_is_confirmed_by_immediate_readback(self):
        class RejectedAfterWrite(FakeMessages):
            def send_interactive_card(self, **kwargs):
                message_id = super().send_interactive_card(**kwargs)
                raise DeliveryError("simulated rejection after remote write")

        client = RejectedAfterWrite()
        now = [1000.0]
        first = self.send(client, now=lambda: now[0])
        self.assertEqual(first["status"], "confirmed")
        self.assertTrue(first["recovered"])
        request_id = self.store.find_effect(self.job_id, "document-notification", "message-send")["request_id"]
        self.assertEqual(client.sends, 1)
        effect = self.store.find_effect(self.job_id, "document-notification", "message-send")
        self.assertEqual(effect["request_id"], request_id)
        self.assertEqual(effect["status"], "confirmed")

    def test_nonretryable_rejection_uses_paced_readback_without_resending(self):
        class Rejected(FakeMessages):
            def send_interactive_card(self, **kwargs):
                self.sends += 1
                raise DeliveryError("permanent simulated rejection")

        client = Rejected()
        now = [1000.0]
        self.assertEqual(self.send(client, now=lambda: now[0])["status"], "failed")
        self.assertEqual(client.sends, 1)
        first_reads = client.history_reads
        artifact = next(item for item in self.store.artifacts(self.job_id)
                        if item["artifact_key"] == "document-notification")
        self.assertEqual(artifact["metadata"]["next_reconcile_at"],
                         now[0] + NOTIFICATION_RECONCILE_INTERVAL_SECONDS)

        now[0] += NOTIFICATION_RECONCILE_INTERVAL_SECONDS - 1
        self.assertEqual(self.send(client, now=lambda: now[0])["status"], "failed")
        self.assertEqual(client.history_reads, first_reads)
        self.assertFalse(workflow_worker._notification_retry_due(
            self.store, self.store.artifacts(self.job_id), now[0],
        ))

        now[0] += 1
        self.assertEqual(self.send(client, now=lambda: now[0])["status"], "failed")
        self.assertEqual(client.sends, 1)
        self.assertEqual(client.history_reads, first_reads + 1)
        artifact = next(item for item in self.store.artifacts(self.job_id)
                        if item["artifact_key"] == "document-notification")
        self.assertEqual(artifact["metadata"]["next_reconcile_at"],
                         now[0] + NOTIFICATION_RECONCILE_INTERVAL_SECONDS)

    def test_retryable_send_with_incomplete_retry_readback_is_due_for_read_only_check(self):
        class RejectOnce(FakeMessages):
            def send_interactive_card(self, **kwargs):
                self.sends += 1
                raise RetryableDeliveryError("simulated transient rejection")

        client = RejectOnce()
        now = [1000.0]
        self.assertEqual(self.send(client, now=lambda: now[0])["status"], "deferred")
        self.assertEqual(client.sends, 1)
        now[0] += 60
        client.fail_history = True
        self.assertEqual(self.send(client, now=lambda: now[0])["status"], "needs_review")
        self.assertEqual(client.sends, 1)
        artifact = next(item for item in self.store.artifacts(self.job_id)
                        if item["artifact_key"] == "document-notification")
        due_at = artifact["metadata"]["next_reconcile_at"]
        self.assertFalse(workflow_worker._notification_retry_due(
            self.store, self.store.artifacts(self.job_id), due_at - 1,
        ))
        self.assertTrue(workflow_worker._notification_retry_due(
            self.store, self.store.artifacts(self.job_id), due_at,
        ))
        now[0] = due_at
        self.assertEqual(self.send(client, now=lambda: now[0])["status"], "needs_review")
        self.assertEqual(client.sends, 1)
        artifact = next(item for item in self.store.artifacts(self.job_id)
                        if item["artifact_key"] == "document-notification")
        self.assertEqual(artifact["metadata"]["next_reconcile_at"],
                         due_at + NOTIFICATION_RECONCILE_INTERVAL_SECONDS)

    def test_known_rejected_send_retries_after_backoff_with_same_effect(self):
        class RejectOnce(FakeMessages):
            def send_interactive_card(self, **kwargs):
                if self.sends == 0:
                    self.sends += 1
                    raise RetryableDeliveryError("simulated definite rejection")
                return super().send_interactive_card(**kwargs)

        client = RejectOnce()
        now = [1000.0]
        first = self.send(client, now=lambda: now[0])
        effect = self.store.find_effect(self.job_id, "document-notification", "message-send")
        request_id = effect["request_id"]
        self.assertEqual(first["status"], "deferred")
        self.assertEqual(effect["attempts"], 1)
        artifact = next(item for item in self.store.artifacts(self.job_id)
                        if item["artifact_key"] == "document-notification")
        self.assertEqual(artifact["metadata"]["next_retry_at"], 1060.0)
        now[0] = 1059.0
        early = self.send(client, now=lambda: now[0])
        self.assertEqual(early["status"], "deferred")
        self.assertEqual(client.sends, 1)
        now[0] = 1060.0
        final = self.send(client, now=lambda: now[0])
        self.assertEqual(final["status"], "confirmed")
        self.assertEqual(client.sends, 2)
        effect = self.store.find_effect(self.job_id, "document-notification", "message-send")
        self.assertEqual(effect["request_id"], request_id)
        self.assertEqual(effect["attempts"], 2)

    def test_failed_notification_backoff_survives_interruption_before_artifact_retry_state(self):
        class RejectOnce(FakeMessages):
            def send_interactive_card(self, **kwargs):
                if self.sends == 0:
                    self.sends += 1
                    raise RetryableDeliveryError("simulated definite rejection")
                return super().send_interactive_card(**kwargs)

        client = RejectOnce()
        now = [1000.0]
        first = self.send(client, now=lambda: now[0])
        self.assertEqual(first["status"], "deferred")
        artifact = next(item for item in self.store.artifacts(self.job_id)
                        if item["artifact_key"] == "document-notification")
        self.assertEqual(artifact["status"], "pending")
        self.assertEqual(artifact["metadata"]["next_retry_at"], 1060.0)

        # Simulate interruption after finish_effect committed but before the
        # separate artifact scheduling update could change failed -> pending.
        with self.store._transaction():
            self.store.connection.execute(
                "UPDATE artifacts SET status='failed' WHERE job_id=? AND artifact_key=?",
                (self.job_id, "document-notification"),
            )

        now[0] = 1059.0
        early = self.send(client, now=lambda: now[0])

        self.assertEqual(early["status"], "deferred")
        self.assertEqual(client.sends, 1)

    def test_notification_retries_stop_after_two_extra_send_attempts(self):
        class RejectAlways(FakeMessages):
            def send_interactive_card(self, **kwargs):
                self.sends += 1
                raise RetryableDeliveryError("simulated definite rejection")

        client = RejectAlways()
        now = [1000.0]
        statuses = []
        for attempt in range(3):
            result = self.send(client, now=lambda: now[0])
            statuses.append(result["status"])
            now[0] += 60
        self.assertEqual(statuses, ["deferred", "deferred", "failed"])
        self.assertEqual(client.sends, 3)
        self.assertEqual(self.store.find_effect(
            self.job_id, "document-notification", "message-send",
        )["attempts"], 3)

    def test_exact_history_can_confirm_a_rejected_send_after_retry_window_expires(self):
        class RejectAlways(FakeMessages):
            def send_interactive_card(self, **kwargs):
                self.sends += 1
                raise RetryableDeliveryError("simulated definite rejection")

        client = RejectAlways()
        now = [1000.0]
        for _ in range(3):
            result = self.send(client, now=lambda: now[0])
            now[0] += 60
        self.assertEqual(result["status"], "failed")
        effect = self.store.find_effect(self.job_id, "document-notification", "message-send")
        correlated = _correlate_card(self.card, effect["request_id"])
        client.history.append({
            "message_id": "om_late_readback", "msg_type": "interactive", "chat_id": "oc_private",
            "create_time": 1000, "sender": {"id": "app_bot", "id_type": "app_id"},
            "body": {"content": json.dumps(correlated, ensure_ascii=False)},
        })
        now[0] = 5000.0

        recovered = self.send(client, now=lambda: now[0])

        self.assertEqual(recovered["status"], "confirmed")
        self.assertTrue(recovered["recovered"])
        self.assertEqual(client.sends, 3)

    def test_incomplete_history_stops_retry_and_marks_notification_uncertain(self):
        class RejectOnce(FakeMessages):
            def __init__(self):
                super().__init__()
                self.fail_history = False

            def list_chat_messages(self, **kwargs):
                if self.fail_history:
                    raise DeliveryError("history pagination incomplete")
                return super().list_chat_messages(**kwargs)

            def send_interactive_card(self, **kwargs):
                self.sends += 1
                raise RetryableDeliveryError("simulated rejection")

        client = RejectOnce()
        now = [1000.0]
        self.assertEqual(self.send(client, now=lambda: now[0])["status"], "deferred")
        client.fail_history = True
        now[0] = 1060.0

        result = self.send(client, now=lambda: now[0])

        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(client.sends, 1)
        artifact = next(item for item in self.store.artifacts(self.job_id)
                        if item["artifact_key"] == "document-notification")
        self.assertEqual(artifact["status"], "uncertain")

    def test_card_contains_the_persisted_effect_request_id_for_remote_correlation(self):
        client = FakeMessages()
        result = self.send(client)
        self.assertEqual(result["status"], "confirmed")
        effect = self.store.find_effect(self.job_id, "document-notification", "message-send")
        sent_card = json.loads(client.history[0]["body"]["content"])
        self.assertIn(effect["request_id"], sent_card["config"]["summary"]["content"])

    def test_old_identical_card_cannot_confirm_uncertain_current_send(self):
        client = FakeMessages([{
            "message_id": "om_previous_job", "msg_type": "interactive", "chat_id": "oc_private",
            "create_time": 999, "sender": {"id": "app_bot", "id_type": "app_id"},
            "body": {"content": json.dumps(self.card, ensure_ascii=False)},
        }], lose_before_write=True)

        first = self.send(client)
        recovered = self.send(client)

        self.assertEqual(first["status"], "uncertain")
        self.assertEqual(recovered["status"], "uncertain")
        self.assertEqual(client.sends, 1)
        effect = self.store.find_effect(self.job_id, "document-notification", "message-send")
        self.assertEqual(effect["status"], "uncertain")
        self.assertIsNone(effect["remote_id"])

    def test_success_receipt_does_not_mark_its_own_pending_card_unresolved(self):
        class CompletedDelivery:
            @staticmethod
            def artifacts(_job_id):
                rows = [{"artifact_key": key, "required": True, "status": "confirmed"}
                        for key in REQUIRED_SUCCESS_ARTIFACTS]
                rows.append({"artifact_key": "final-notification", "required": True, "status": "pending"})
                return rows

        receipt = final_delivery_receipt(
            CompletedDelivery(), "job-1", main_url="https://feishu.cn/docx/doc_1",
            munger_embedded=True,
        )
        self.assertEqual(receipt["unresolved_actions"], [])
        self.assertNotIn("final-notification", receipt["artifacts"])
        card = render_card(
            title="文章", main_url="https://feishu.cn/docx/doc_1",
            delivery_receipt=receipt,
        )
        self.assertEqual(card["header"]["title"]["content"], "长文精读完成")

    def test_content_review_notice_recovers_lost_response_without_duplicate_send(self):
        client = FakeMessages(lose_send=True)
        now = [1000.0]
        kwargs = {
            "store": self.store, "lease": self.lease, "payload": self.payload,
            "root": self.state, "client": client, "clock": lambda: now[0],
        }
        first = notify_content_review(**kwargs)
        second = notify_content_review(**kwargs)
        self.assertEqual(first["status"], "uncertain")
        self.assertEqual(second["status"], "uncertain")
        now[0] += NOTIFICATION_RECONCILE_INTERVAL_SECONDS
        second = notify_content_review(**kwargs)
        self.assertEqual(first["status"], "uncertain")
        self.assertEqual(second["status"], "confirmed")
        self.assertTrue(second["recovered"])
        self.assertEqual(client.sends, 1)
        card = json.loads(client.history[0]["body"]["content"])
        self.assertEqual(card["header"]["title"]["content"], "交付结果待核实")
        body = card["body"]["elements"][-1]["content"]
        self.assertIn("先只读核验，不盲目重发", body)
        self.assertNotIn("长文精读完成", body)

    def test_final_success_notification_is_sent_and_read_back_after_all_artifacts_confirm(self):
        evidence_path = Path(self.temporary.name) / "evidence.json"
        evidence_path.write_text(json.dumps({"metadata": {"title": "文章"}}), encoding="utf-8")
        payload = {
            "trigger": self.trigger,
            "inputs": {"evidence.json": {"path": str(evidence_path)}},
        }
        artifact_specs = [
            ("analysis:article-decode", "analysis"),
            ("analysis:munger-analysis", "analysis"),
            ("main-document", "document"),
            ("document-notification", "message"),
            ("core-image", "image"),
            ("core-image-insert", "image_insert"),
            ("core-image-send", "message"),
            ("munger-image", "image"),
            ("munger-image-insert", "image_insert"),
            ("munger-image-send", "message"),
            ("final-notification", "message"),
        ]
        job = self.store.submit(payload, app_id="app_bot", artifacts=[
            {"key": key, "kind": kind} for key, kind in artifact_specs
        ])
        self.job_id = job["job_id"]
        self.lease = self.store.acquire_lease(self.job_id, "final-notify-test", 3600)
        required = {key for key, _kind in artifact_specs}
        for key, kind in artifact_specs:
            if key == "final-notification":
                continue
            for action in VERIFIED_EFFECTS_BY_KIND[kind]:
                effect = self.store.prepare_effect(self.lease, key, action, "b" * 64)
                effect = self.store.begin_effect(self.lease, effect["op_key"])
                self.store.finish_effect(self.lease, effect["op_key"], "confirmed",
                                         remote_id=f"remote_{key.replace(':', '_')}_{action}",
                                         metadata={"verification": "fake-readback"})
            self.store.set_artifact(self.lease, key, "confirmed", metadata={
                "verification": "fake-readback",
            })
        self.store.finalize_artifact_plan(self.lease, required)

        client = FakeMessages()
        result = notify_final(
            store=self.store, lease=self.lease, payload=payload, root=self.state,
            status="succeeded", document_id="doc_1", client=client,
            clock=lambda: 1000.0,
        )
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(client.sends, 1)
        final_artifact = next(item for item in self.store.artifacts(self.job_id)
                              if item["artifact_key"] == "final-notification")
        self.assertEqual(final_artifact["status"], "confirmed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
