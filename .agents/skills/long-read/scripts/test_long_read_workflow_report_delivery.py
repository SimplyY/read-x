#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime
import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from feishu_long_read_delivery import DeliveryError, RetryableDeliveryError, UnknownRemoteResult
from long_read_workflow_report_delivery import (
    REPORT_RECONCILE_INTERVAL_SECONDS, configure_report_target, deliver_report,
    load_report_target, verify_report_target,
)
from long_read_workflow_reports import create_report_snapshot
from long_read_workflow_store import WorkflowStore


TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 21, 12, tzinfo=TZ)


class FakeFeishu:
    def __init__(self):
        self.messages = []
        self.uploads = 0
        self.file_sends = 0
        self.card_sends = 0
        self.lose_file_response = False
        self.lose_card_response = False
        self.hide_history = False
        self.file_request_ids = []
        self.card_request_ids = []
        self.history_reads = 0

    @staticmethod
    def message(message_id, chat_id, msg_type, content):
        return {
            "message_id": message_id, "chat_id": chat_id, "msg_type": msg_type,
            "create_time": int(NOW.timestamp()),
            "sender": {"id": "app_bot", "id_type": "app_id"},
            "body": {"content": json.dumps(content, ensure_ascii=False, separators=(",", ":"))},
        }

    def upload_markdown_attachment(self, *, path):
        self.uploads += 1
        assert path.is_file()
        return "file_report"

    def send_file_message(self, *, file_key, user_id, idempotency_key):
        self.file_sends += 1
        self.file_request_ids.append(idempotency_key)
        self.messages.append(self.message(f"om_file_{self.file_sends}", "oc_self", "file", {"file_key": file_key}))
        if self.lose_file_response:
            self.lose_file_response = False
            raise UnknownRemoteResult("response lost after remote side effect")
        return self.messages[-1]["message_id"]

    def send_interactive_card(self, *, card, user_id, idempotency_key):
        self.card_sends += 1
        self.card_request_ids.append(idempotency_key)
        self.messages.append(self.message(f"om_card_{self.card_sends}", "oc_self", "interactive", card))
        if self.lose_card_response:
            self.lose_card_response = False
            raise UnknownRemoteResult("response lost after remote side effect")
        return self.messages[-1]["message_id"]

    def get_message(self, message_id):
        for message in self.messages:
            if message["message_id"] == message_id:
                return message
        raise DeliveryError("not found")

    def list_chat_messages(self, *, chat_id, start_time, end_time):
        self.history_reads += 1
        if self.hide_history:
            raise DeliveryError("history not readable")
        return [message for message in self.messages
                if message["chat_id"] == chat_id and start_time <= message["create_time"] <= end_time]


class FakeTargetReader:
    def __init__(self, identity):
        self.identity = identity
        self.calls = []

    def auth_status(self):
        self.calls.append(("auth-status", False))
        return {"identity": "user", "verified": True, "identities": {
            "user": {"openId": "ou_self", "tokenStatus": "valid"},
        }}

    def _run(self, args, *, write):
        self.calls.append((list(args), write))
        if args[:3] == ["im", "chats", "get"]:
            return {"data": {"chat_mode": "p2p", "chat_status": "normal"}}
        if args[:2] == ["im", "+chat-members-list"]:
            return {"data": {
                "users": [{"member_id": "ou_self"}],
                "bots": [{"app_id": "app_bot"}],
                "truncations": [], "has_more": False,
            }}
        raise AssertionError(f"unexpected read command: {args}")


def verified_target(**_kwargs):
    return {"status": "verified"}


class ReportDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "state"
        with WorkflowStore(self.state):
            pass
        configure_report_target(state_dir=self.state, profile="read-x", user_id="ou_self",
                                chat_id="oc_self", sender_id="app_bot",
                                target_verifier=verified_target)
        self.report = create_report_snapshot(self.state, "monthly", now=NOW)
        self.client = FakeFeishu()
        self.now = [NOW.timestamp()]

    def tearDown(self):
        self.temp.cleanup()

    def deliver(self):
        return deliver_report(
            state_dir=self.state, report_key=self.report["report_key"], identity="bot",
            profile="read-x", user_id="ou_self", chat_id="oc_self", sender_id="app_bot",
            client=self.client, clock=lambda: self.now[0], target_verifier=verified_target,
        )

    def test_month_report_requires_file_and_card_readback_and_reuses_terminal_result(self):
        result = self.deliver()
        self.assertEqual(result["status"], "delivered")
        self.assertEqual((self.client.uploads, self.client.file_sends, self.client.card_sends), (1, 1, 1))
        with WorkflowStore(self.state) as store:
            row = store.connection.execute("SELECT status FROM reports WHERE report_key=?", (self.report["report_key"],)).fetchone()
            self.assertEqual(row["status"], "delivered")
            effects = {item["artifact_key"]: item for item in store.report_effects(self.report["report_key"])}
            self.assertEqual({key: item["status"] for key, item in effects.items()}, {
                "markdown-upload": "confirmed", "markdown-send": "confirmed", "card-send": "confirmed",
            })
            self.assertTrue(all(isinstance(item["metadata"].get("duration_ms"), int)
                                for item in effects.values()))
        self.assertTrue(self.deliver()["reused"])
        self.assertEqual((self.client.uploads, self.client.file_sends, self.client.card_sends), (1, 1, 1))

    def test_lost_file_send_response_is_reconciled_without_duplicate_send(self):
        self.client.lose_file_response = True
        first = self.deliver()
        self.assertEqual(first, {"status": "needs_review", "report_key": self.report["report_key"], "stage": "markdown-send"})
        self.assertEqual(self.client.file_sends, 1)
        self.now[0] += REPORT_RECONCILE_INTERVAL_SECONDS
        second = self.deliver()
        self.assertEqual(second["status"], "delivered")
        self.assertEqual((self.client.file_sends, self.client.card_sends), (1, 1))

    def test_uncertain_report_send_reconciliation_is_paced_and_never_resends(self):
        class LostFileResponse(FakeFeishu):
            def send_file_message(self, **kwargs):
                super().send_file_message(**kwargs)
                raise UnknownRemoteResult("response lost after remote side effect")

        self.client = LostFileResponse()
        first = self.deliver()
        self.assertEqual(first["status"], "needs_review")
        history_reads = self.client.history_reads
        self.assertEqual(self.client.file_sends, 1)

        self.assertEqual(self.deliver()["status"], "needs_review")
        self.now[0] += REPORT_RECONCILE_INTERVAL_SECONDS - 1
        self.assertEqual(self.deliver()["status"], "needs_review")
        self.assertEqual(self.client.history_reads, history_reads)

        self.now[0] += 1
        self.assertEqual(self.deliver()["status"], "delivered")
        self.assertEqual(self.client.file_sends, 1)
        self.assertEqual(self.client.history_reads, history_reads + 2)  # exact readback + next message preflight

    def test_retryable_file_send_response_is_read_back_before_retry(self):
        class RetryOnce(FakeFeishu):
            def send_file_message(self, **kwargs):
                if self.file_sends == 0:
                    self.file_sends += 1
                    self.file_request_ids.append(kwargs["idempotency_key"])
                    raise RetryableDeliveryError("simulated transient rejection")
                return super().send_file_message(**kwargs)

        self.client = RetryOnce()
        first = self.deliver()
        self.assertEqual(first["status"], "deferred")
        self.assertEqual(self.client.file_sends, 1)
        self.assertEqual(self.client.card_sends, 0)
        self.assertGreaterEqual(self.client.history_reads, 2)  # rate preflight + exact readback

        self.now[0] += 59
        early = self.deliver()
        self.assertEqual(early["status"], "deferred")
        self.assertEqual(self.client.file_sends, 1)

        self.now[0] += 1
        final = self.deliver()
        self.assertEqual(final["status"], "delivered")
        self.assertEqual((self.client.file_sends, self.client.card_sends), (2, 1))
        self.assertEqual(len(set(self.client.file_request_ids)), 1)

    def test_retryable_send_that_reached_feishu_is_confirmed_by_exact_readback(self):
        class RetryableAfterWrite(FakeFeishu):
            def send_file_message(self, **kwargs):
                message_id = super().send_file_message(**kwargs)
                raise RetryableDeliveryError("simulated transient response after remote write")

        self.client = RetryableAfterWrite()
        result = self.deliver()
        self.assertEqual(result["status"], "delivered")
        self.assertEqual((self.client.file_sends, self.client.card_sends), (1, 1))
        self.assertEqual(self.client.file_request_ids[0], self.client.file_request_ids[-1])

    def test_retryable_send_with_incomplete_history_becomes_uncertain_without_resend(self):
        class RetryableAfterWrite(FakeFeishu):
            def send_file_message(self, **kwargs):
                super().send_file_message(**kwargs)
                raise RetryableDeliveryError("simulated transient response after remote write")

        class IncompleteHistory(RetryableAfterWrite):
            def list_chat_messages(self, **kwargs):
                self.history_reads += 1
                if self.file_sends:
                    raise DeliveryError("history pagination incomplete")
                return super().list_chat_messages(**kwargs)

        self.client = IncompleteHistory()
        result = self.deliver()
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(self.client.file_sends, 1)
        self.assertEqual(self.deliver()["status"], "needs_review")
        self.assertEqual(self.client.file_sends, 1)
        with WorkflowStore(self.state) as store:
            effect = next(item for item in store.report_effects(self.report["report_key"])
                          if item["artifact_key"] == "markdown-send")
            self.assertEqual(effect["status"], "uncertain")

    def test_retryable_report_send_has_a_bounded_total_attempt_budget(self):
        class RetryAlways(FakeFeishu):
            def send_file_message(self, *, file_key, user_id, idempotency_key):
                self.file_sends += 1
                self.file_request_ids.append(idempotency_key)
                raise RetryableDeliveryError("simulated rate limit")

        self.client = RetryAlways()
        first = self.deliver()
        self.assertEqual(first["status"], "deferred")
        self.now[0] += 60
        second = self.deliver()
        self.assertEqual(second["status"], "deferred")
        self.now[0] += 60
        third = self.deliver()
        self.assertEqual(third, {"status": "failed", "report_key": self.report["report_key"],
                                 "stage": "markdown-send"})
        self.assertEqual(self.client.file_sends, 3)
        self.assertEqual(len(set(self.client.file_request_ids)), 1)
        history_reads = self.client.history_reads
        self.assertEqual(self.deliver()["status"], "deferred")
        self.assertEqual(self.client.history_reads, history_reads)
        self.now[0] += REPORT_RECONCILE_INTERVAL_SECONDS
        self.client.messages.append(FakeFeishu.message(
            "om_late_file", "oc_self", "file", {"file_key": "file_report"},
        ))
        self.assertEqual(self.deliver()["status"], "delivered")
        self.assertEqual(self.client.file_sends, 3)

    def test_exhausted_report_retry_budget_keeps_paced_readback_without_resending(self):
        class RetryAlways(FakeFeishu):
            def send_file_message(self, *, file_key, user_id, idempotency_key):
                self.file_sends += 1
                self.file_request_ids.append(idempotency_key)
                raise RetryableDeliveryError("simulated rate limit")

        self.client = RetryAlways()
        self.deliver()
        self.now[0] += 60
        self.deliver()
        self.now[0] += 60
        self.deliver()
        self.assertEqual(self.client.file_sends, 3)

        self.now[0] += REPORT_RECONCILE_INTERVAL_SECONDS
        self.assertEqual(self.deliver()["status"], "needs_review")
        self.assertEqual(self.client.file_sends, 3)
        with WorkflowStore(self.state) as store:
            effect = next(item for item in store.report_effects(self.report["report_key"])
                          if item["artifact_key"] == "markdown-send")
            self.assertEqual(effect["status"], "uncertain")
            self.assertEqual(effect["metadata"]["reason_code"], "report-history-no-match")
            self.assertGreater(effect["metadata"]["next_reconcile_at"], self.now[0])

    def test_late_report_message_remains_searchable_after_an_earlier_empty_readback(self):
        class RetryAlways(FakeFeishu):
            def send_file_message(self, *, file_key, user_id, idempotency_key):
                self.file_sends += 1
                self.file_request_ids.append(idempotency_key)
                raise RetryableDeliveryError("simulated rate limit")

        self.client = RetryAlways()
        for attempt in range(3):
            self.deliver()
            if attempt < 2:
                self.now[0] += 60
        with WorkflowStore(self.state) as store:
            original = next(item for item in store.report_effects(self.report["report_key"])
                            if item["artifact_key"] == "markdown-send")
        self.now[0] += REPORT_RECONCILE_INTERVAL_SECONDS
        self.assertEqual(self.deliver()["status"], "needs_review")

        late_message = FakeFeishu.message(
            "om_late_after_readback", "oc_self", "file", {"file_key": "file_report"},
        )
        late_message["create_time"] = int(original["created_at"] + 30)
        self.client.messages.append(late_message)
        self.now[0] += REPORT_RECONCILE_INTERVAL_SECONDS

        self.assertEqual(self.deliver()["status"], "delivered")
        self.assertEqual(self.client.file_sends, 3)

    def test_unreadable_history_keeps_lost_card_response_uncertain(self):
        self.client.lose_card_response = True
        self.client.hide_history = True
        first = self.deliver()
        self.assertEqual(first["status"], "needs_review")
        self.client.hide_history = False
        self.client.messages = [message for message in self.client.messages if not message["msg_type"] == "interactive"]
        second = self.deliver()
        self.assertEqual(second["status"], "needs_review")
        self.assertEqual(self.client.card_sends, 1)
        with WorkflowStore(self.state) as store:
            card_effect = next(item for item in store.report_effects(self.report["report_key"]) if item["artifact_key"] == "card-send")
            self.assertEqual(card_effect["status"], "uncertain")

    def test_lost_upload_response_is_never_retried_automatically(self):
        class LostUpload(FakeFeishu):
            def upload_markdown_attachment(self, *, path):
                self.uploads += 1
                raise UnknownRemoteResult("upload response lost")

        self.client = LostUpload()
        first = self.deliver()
        second = self.deliver()
        self.assertEqual(first["stage"], "markdown-upload")
        self.assertEqual(second["stage"], "markdown-upload")
        self.assertEqual(self.client.uploads, 1)

    def test_report_delivery_obeys_the_fifth_message_preflight(self):
        self.client.messages = [FakeFeishu.message(
            f"om_recent_{index}", "oc_self", "text", {"text": "recent"},
        ) for index in range(4)]
        result = self.deliver()
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(self.client.uploads, 1)
        self.assertEqual(self.client.file_sends, 0)
        self.assertEqual(self.client.card_sends, 0)
        with WorkflowStore(self.state) as store:
            effects = {item["artifact_key"]: item for item in store.report_effects(self.report["report_key"])}
            self.assertEqual(effects["markdown-send"]["status"], "prepared")

    def test_second_report_worker_is_rejected_while_delivery_lock_is_held(self):
        lock_path = self.state / ".report-delivery.lock"
        fd = lock_path.open("w")
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.deliver()
            self.assertEqual(result["status"], "busy")
            self.assertEqual((self.client.uploads, self.client.file_sends, self.client.card_sends), (0, 0, 0))
        finally:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            fd.close()

    def test_report_recipient_is_bound_to_persisted_private_target(self):
        saved = load_report_target(self.state)
        self.assertEqual(saved["user_id"], "ou_self")
        with self.assertRaisesRegex(ValueError, "does not match the persisted target"):
            deliver_report(
                state_dir=self.state, report_key=self.report["report_key"], identity="bot",
                profile="read-x", user_id="ou_not_the_configured_user", chat_id="oc_self",
                sender_id="app_bot", client=self.client,
            )
        self.assertEqual((self.client.uploads, self.client.file_sends, self.client.card_sends), (0, 0, 0))

    def test_configured_report_target_cannot_be_changed_without_explicit_replace(self):
        with self.assertRaisesRegex(ValueError, "already exists"):
            configure_report_target(state_dir=self.state, profile="read-x", user_id="ou_other",
                                    chat_id="oc_other", sender_id="app_bot",
                                    target_verifier=verified_target)
        configure_report_target(state_dir=self.state, profile="read-x", user_id="ou_other",
                                chat_id="oc_other", sender_id="app_bot", replace=True,
                                target_verifier=verified_target)
        self.assertEqual(load_report_target(self.state)["user_id"], "ou_other")

    def test_report_target_verifier_binds_current_user_bot_and_exact_p2p_membership(self):
        user_client, bot_client = FakeTargetReader("user"), FakeTargetReader("bot")
        result = verify_report_target(
            profile="read-x", user_id="ou_self", chat_id="oc_self", sender_id="app_bot",
            user_client=user_client, bot_client=bot_client, clock=lambda: NOW.timestamp(),
        )
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["verified_at"], int(NOW.timestamp()))
        self.assertTrue(all(not write for _args, write in user_client.calls + bot_client.calls))
        self.assertEqual(len(user_client.calls), 1)
        self.assertEqual(len(bot_client.calls), 2)

    def test_report_target_verification_fails_closed_before_chat_lookup_for_wrong_user(self):
        user_client, bot_client = FakeTargetReader("user"), FakeTargetReader("bot")
        with self.assertRaisesRegex(ValueError, "does not match the verified current user"):
            verify_report_target(
                profile="read-x", user_id="ou_other", chat_id="oc_self", sender_id="app_bot",
                user_client=user_client, bot_client=bot_client,
            )
        self.assertEqual(len(user_client.calls), 1)
        self.assertEqual(bot_client.calls, [])

    def test_delivery_verifies_target_before_any_report_side_effect(self):
        def reject_target(**_kwargs):
            raise ValueError("target identity changed")

        with self.assertRaisesRegex(ValueError, "target identity changed"):
            deliver_report(
                state_dir=self.state, report_key=self.report["report_key"],
                client=self.client, target_verifier=reject_target,
            )
        self.assertEqual((self.client.uploads, self.client.file_sends, self.client.card_sends), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
