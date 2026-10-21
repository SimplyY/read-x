#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime
import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from feishu_long_read_delivery import DeliveryError, UnknownRemoteResult
from long_read_workflow_report_delivery import deliver_report
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

    @staticmethod
    def message(message_id, chat_id, msg_type, content):
        return {
            "message_id": message_id, "chat_id": chat_id, "msg_type": msg_type,
            "sender": {"id": "app_bot", "id_type": "app_id"},
            "body": {"content": json.dumps(content, ensure_ascii=False, separators=(",", ":"))},
        }

    def upload_markdown_attachment(self, *, path):
        self.uploads += 1
        assert path.is_file()
        return "file_report"

    def send_file_message(self, *, file_key, user_id, idempotency_key):
        self.file_sends += 1
        self.messages.append(self.message(f"om_file_{self.file_sends}", "oc_self", "file", {"file_key": file_key}))
        if self.lose_file_response:
            self.lose_file_response = False
            raise UnknownRemoteResult("response lost after remote side effect")
        return self.messages[-1]["message_id"]

    def send_interactive_card(self, *, card, user_id, idempotency_key):
        self.card_sends += 1
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
        if self.hide_history:
            raise DeliveryError("history not readable")
        return [message for message in self.messages if message["chat_id"] == chat_id]


class ReportDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "state"
        with WorkflowStore(self.state):
            pass
        self.report = create_report_snapshot(self.state, "monthly", now=NOW)
        self.client = FakeFeishu()

    def tearDown(self):
        self.temp.cleanup()

    def deliver(self):
        return deliver_report(
            state_dir=self.state, report_key=self.report["report_key"], identity="bot",
            profile="read-x", user_id="ou_self", chat_id="oc_self", sender_id="app_bot",
            client=self.client, clock=lambda: NOW.timestamp(),
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
        self.assertTrue(self.deliver()["reused"])
        self.assertEqual((self.client.uploads, self.client.file_sends, self.client.card_sends), (1, 1, 1))

    def test_lost_file_send_response_is_reconciled_without_duplicate_send(self):
        self.client.lose_file_response = True
        first = self.deliver()
        self.assertEqual(first, {"status": "needs_review", "report_key": self.report["report_key"], "stage": "markdown-send"})
        self.assertEqual(self.client.file_sends, 1)
        second = self.deliver()
        self.assertEqual(second["status"], "delivered")
        self.assertEqual((self.client.file_sends, self.client.card_sends), (1, 1))

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


if __name__ == "__main__":
    unittest.main()
