#!/usr/bin/env python3
"""Durable image delivery tests use fake Bridge and Feishu endpoints only."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import run_chatgpt_core_image as core_image
from feishu_long_read_delivery import DeliveryError, RetryableDeliveryError, UnknownRemoteResult
from long_read_image_delivery import _write_private, deliver_image
from long_read_workflow_store import WorkflowStore


PNG = b"\x89PNG\r\n\x1a\nread-x-test-image"


class FakeImageRunner:
    def __init__(self):
        self.calls = 0
        self.raise_after_submit = False
        self.pending_recoveries = 0
        self.recover_calls = 0

    def run(self, source_path, output_path, *, summary_path, conversation_url,
            prompt_fetcher, request_id, context_pack_delivery=None):
        self.calls += 1
        source = source_path.read_text(encoding="utf-8")
        asset = prompt_fetcher(core_image.PROMPT_GOVERNANCE_ID)
        prompt = core_image.build_prompt(source, asset["content"])
        summary = {
            "status": "succeeded", "runId": request_id,
            "caller_label": core_image.CALLER_LABEL,
            "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "conversation_url_requested": conversation_url,
            "prompt_id": asset["prompt_id"], "prompt_sha256": asset["prompt_sha256"],
            "input_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "submissionConfirmed": True, "verification": "live-dom+snapshot",
            "image_sha256": hashlib.sha256(PNG).hexdigest(), "image_bytes": len(PNG),
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(PNG)
        summary_path.write_text(json.dumps(summary), encoding="utf-8")
        result = {**summary, "output": str(output_path)}
        if self.raise_after_submit:
            self.raise_after_submit = False
            raise RuntimeError("response lost after image generation")
        return result

    def recover(self, source_path, output_path, summary_path, *, request_id, observe_fn=None):
        self.recover_calls += 1
        if self.pending_recoveries:
            self.pending_recoveries -= 1
            return {"status": "needs_review", "runId": request_id}
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        return {**summary, "status": "succeeded", "output": str(output_path)}

    @staticmethod
    def _reuse_image(output, summary, source_sha, request_id, conversation_url):
        if (output.read_bytes() != PNG or summary.get("runId") != request_id
                or summary.get("source_sha256") != source_sha
                or summary.get("conversation_url_requested") != conversation_url
                or summary.get("status") != "succeeded"
                or summary.get("submissionConfirmed") is not True
                or summary.get("verification") != "live-dom+snapshot"):
            raise ValueError("image receipt mismatch")
        return summary


class FakeFeishu:
    def __init__(self, *, lose_create=False, lose_send=False, unreadable_create=False,
                 create_error=None):
        self.blocks = [
            {"block_id": "title", "block_type": 3, "parent_id": "doc_1"},
            {"block_id": "source", "block_type": 3, "parent_id": "doc_1"},
        ]
        self.lose_create = lose_create
        self.lose_send = lose_send
        self.unreadable_create = unreadable_create
        self.create_error = create_error
        self.created_blocks = 0
        self.message_sends = 0
        self.messages = {}
        self.uploads = 0
        self.patches = 0
        self.identity_ok = True

    def verify_app_identity(self, expected_app_id):
        if not self.identity_ok or expected_app_id != "app_bot":
            raise DeliveryError("simulated bot profile mismatch")
        return True

    def list_document_blocks(self, document_id):
        if self.unreadable_create and self.created_blocks:
            raise DeliveryError("incomplete block readback")
        return list(self.blocks)

    def get_document_block(self, document_id, block_id):
        item = next((row for row in self.blocks if row["block_id"] == block_id), None)
        if item is None:
            raise DeliveryError("missing block")
        return dict(item)

    def create_image_block(self, *, document_id, parent_block_id, index, client_token):
        if self.create_error is not None:
            raise self.create_error
        self.created_blocks += 1
        block_id = f"img_block_{self.created_blocks}"
        self.blocks.insert(index, {"block_id": block_id, "block_type": 27,
                                   "parent_id": parent_block_id, "image": {}})
        if self.lose_create:
            self.lose_create = False
            raise UnknownRemoteResult("create response lost")
        return block_id

    def upload_document_image(self, *, document_id, image_block_id, png_path):
        self.uploads += 1
        return "media_image_1"

    def patch_image_block(self, *, document_id, block_id, file_token, client_token):
        self.patches += 1
        item = next(row for row in self.blocks if row["block_id"] == block_id)
        item["image"] = {"token": file_token}

    def verify_document_image(self, *, document_id, block_id, parent_block_id, file_token):
        item = self.get_document_block(document_id, block_id)
        if item.get("parent_id") != parent_block_id or item.get("image", {}).get("token") != file_token:
            raise UnknownRemoteResult("image block readback mismatch")
        return item

    def verify_new_image_child(self, *, before_child_ids, after_child_ids, index,
                               blocks_by_id, parent_block_id):
        added = [block_id for block_id in after_child_ids if block_id not in before_child_ids]
        if len(added) != 1 or after_child_ids[index] != added[0]:
            raise UnknownRemoteResult("image block create result ambiguous")
        item = blocks_by_id[added[0]]
        if item.get("parent_id") != parent_block_id or item.get("block_type") != 27:
            raise UnknownRemoteResult("new image block readback mismatch")
        return added[0]

    def upload_message_image(self, *, png_path):
        return "img_message_1"

    def send_image_message(self, *, image_key, idempotency_key, chat_id=None, user_id=None):
        self.message_sends += 1
        message_id = f"om_image_{self.message_sends}"
        actual_chat = chat_id or "oc_private"
        self.messages[message_id] = {
            "message_id": message_id, "msg_type": "image", "chat_id": actual_chat,
            "create_time": 1000,
            "sender": {"id": "app_bot", "id_type": "app_id"},
            "body": {"content": json.dumps({"image_key": image_key})},
        }
        if self.lose_send:
            self.lose_send = False
            raise UnknownRemoteResult("send response lost")
        return message_id

    def get_message(self, message_id):
        return self.messages[message_id]

    def list_chat_messages(self, *, chat_id, start_time, end_time):
        return [item for item in self.messages.values()
                if item["chat_id"] == chat_id and start_time <= item["create_time"] <= end_time]


class ImageWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name) / "state"
        self.root = self.state / "jobs" / "00000000-0000-4000-8000-000000000001"
        self.root.mkdir(parents=True)
        (self.root / "analysis").mkdir()
        (self.root / "document").mkdir()
        self.source = self.root / "document" / "main-doc.md"
        self.source.write_text("# Article\n\nEvidence and argument.\n", encoding="utf-8")
        self.payload = {"trigger": {
            "feishuProfile": "bridge-read-x", "larkCliProfile": "app_bot", "appId": "app_bot", "identity": "bot",
            "chatType": "p2p", "chatId": "oc_private", "senderId": "ou_user",
            "senderType": "user",
        }}
        self.clock_value = [1000.0]
        self.store = WorkflowStore(self.state, clock=lambda: self.clock_value[0])
        specs = [
            {"key": "core-image", "kind": "image"},
            {"key": "core-image-insert", "kind": "image_insert"},
            {"key": "core-image-send", "kind": "message"},
        ]
        job = self.store.submit(self.payload, app_id="app_bot", artifacts=specs)
        self.job_id = job["job_id"]
        self.lease = self.store.acquire_lease(self.job_id, "test-worker", 3600)
        self.image_runner = FakeImageRunner()
        self.client = FakeFeishu()
        prompt = "Generate a concise evidence-grounded image."
        self.prompt_asset = {
            "prompt_id": core_image.PROMPT_GOVERNANCE_ID,
            "content": prompt,
            "prompt_source": "https://example.invalid/prompt",
            "prompt_revision": 7,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "prompt_fetched_at": "2026-10-21T00:00:00Z",
        }

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_private_writer_rejects_symlink_destination_without_touching_target(self):
        outside = Path(self.temporary.name) / "outside.bin"
        outside.write_bytes(b"original")
        link = self.state / "jobs" / "linked-image.png"
        link.symlink_to(outside)

        with self.assertRaises(ValueError):
            _write_private(link, b"replacement")

        self.assertEqual(outside.read_bytes(), b"original")
        self.assertTrue(link.is_symlink())

    def test_private_writer_rejects_symlink_parent_without_creating_outside_file(self):
        outside_dir = Path(self.temporary.name) / "outside-dir"
        outside_dir.mkdir()
        linked_parent = self.state / "linked-dir"
        linked_parent.symlink_to(outside_dir, target_is_directory=True)
        target = linked_parent / "image.png"

        with self.assertRaises(ValueError):
            _write_private(target, b"image")

        self.assertFalse((outside_dir / "image.png").exists())
        self.assertTrue(linked_parent.is_symlink())

    def deliver(self):
        return deliver_image(
            store=self.store, lease=self.lease, payload=self.payload, root=self.root,
            image_key="core-image", image_name="core-image", source_path=self.source,
            document_id="doc_1", image_index=1, image_runner=self.image_runner,
            client=self.client, prompt_fetcher=lambda _prompt_id: self.prompt_asset,
            clock=lambda: self.clock_value[0],
        )

    def test_normal_delivery_confirms_all_three_remote_stages_and_reuses_them(self):
        first = self.deliver()
        second = self.deliver()
        self.assertEqual(first["status"], "confirmed")
        self.assertEqual(second["status"], "confirmed")
        self.assertEqual(self.image_runner.calls, 1)
        self.assertEqual(self.client.created_blocks, 1)
        self.assertEqual(self.client.uploads, 1)
        self.assertEqual(self.client.patches, 1)
        self.assertEqual(self.client.message_sends, 1)
        by_key = {item["artifact_key"]: item for item in self.store.artifacts(self.job_id)}
        self.assertTrue(all(by_key[key]["status"] == "confirmed" for key in (
            "core-image", "core-image-insert", "core-image-send")))

    def test_lost_create_response_is_resolved_by_exact_document_diff_without_duplicate(self):
        self.client.lose_create = True
        result = self.deliver()
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(self.client.created_blocks, 1)
        self.assertEqual(len([item for item in self.client.blocks if item["block_type"] == 27]), 1)

    def test_lost_send_response_is_resolved_from_exact_history_without_duplicate(self):
        self.client.lose_send = True
        result = self.deliver()
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(self.client.message_sends, 1)
        self.assertEqual(len(self.client.messages), 1)

    def test_interrupted_accepted_image_send_uses_original_intent_time_for_readback(self):
        class AcceptedThenInterrupted(FakeFeishu):
            def send_image_message(self, **kwargs):
                super().send_image_message(**kwargs)
                raise KeyboardInterrupt("worker stopped after remote acceptance")

        client = AcceptedThenInterrupted()
        self.client = client
        with self.assertRaises(KeyboardInterrupt):
            self.deliver()

        self.clock_value[0] += 120
        result = self.deliver()

        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(client.message_sends, 1)
        self.assertEqual(len(client.messages), 1)

    def test_lost_generation_response_is_observed_without_second_submission(self):
        self.image_runner.raise_after_submit = True
        self.image_runner.pending_recoveries = 1
        first = self.deliver()
        self.assertEqual(first["status"], "uncertain")
        self.assertEqual(self.image_runner.calls, 1)
        self.assertEqual(self.image_runner.recover_calls, 1)

        second = self.deliver()
        self.assertEqual(second["status"], "confirmed")
        self.assertEqual(self.image_runner.calls, 1)
        self.assertEqual(self.image_runner.recover_calls, 2)
        self.assertEqual(self.client.created_blocks, 1)
        self.assertEqual(self.client.message_sends, 1)

    def test_permanent_insert_rejection_does_not_consume_image_retry_budget(self):
        self.client.create_error = DeliveryError("invalid document block parameters")
        result = self.deliver()
        self.assertEqual(result["status"], "failed")
        artifact = next(item for item in self.store.artifacts(self.job_id)
                        if item["artifact_key"] == "core-image")
        self.assertEqual(artifact["status"], "failed")
        self.assertEqual(artifact["retry_count"], 0)
        self.assertFalse(artifact["metadata"]["retryable"])
        effect = self.store.latest_effect_for_action(self.job_id, "core-image-insert", "image-block-create")
        self.assertEqual(effect["status"], "failed")
        self.assertFalse(effect["metadata"]["retryable"])

    def test_documented_transient_insert_rejection_schedules_one_bounded_round(self):
        self.client.create_error = RetryableDeliveryError("Feishu API code=99991400: rate limited")
        result = self.deliver()
        self.assertEqual(result["status"], "retry_scheduled")
        self.assertEqual(result["next_retry_at"], 1240.0)
        artifact = next(item for item in self.store.artifacts(self.job_id)
                        if item["artifact_key"] == "core-image")
        self.assertEqual(artifact["retry_count"], 0)
        self.assertTrue(artifact["metadata"]["retryable"])

    def test_unconfirmed_insert_stops_before_private_send(self):
        self.client.unreadable_create = True
        self.client.lose_create = True
        result = self.deliver()
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(self.client.message_sends, 0)
        effect = self.store.latest_effect_for_action(self.job_id, "core-image-insert", "image-block-create")
        self.assertEqual(effect["status"], "uncertain")

    def test_private_message_preflight_defers_when_it_would_be_the_fifth_send(self):
        for index in range(4):
            message_id = f"om_existing_{index}"
            self.client.messages[message_id] = {
                "message_id": message_id, "msg_type": "text", "chat_id": "oc_private",
                "create_time": 1000,
                "sender": {"id": "app_bot", "id_type": "app_id"},
                "body": {"content": "{}"},
            }
        result = self.deliver()
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(self.client.message_sends, 0)
        self.assertEqual(result["retry_at"], 1060.0)
        send_artifact = next(item for item in self.store.artifacts(self.job_id)
                             if item["artifact_key"] == "core-image-send")
        self.assertEqual(send_artifact["status"], "pending")
        self.assertEqual(send_artifact["metadata"]["next_retry_at"], 1060.0)

    def test_group_trigger_without_verified_private_history_target_stops_before_generation(self):
        self.payload["trigger"] = {
            "feishuProfile": "bridge-read-x", "larkCliProfile": "app_bot", "appId": "app_bot", "identity": "bot",
            "chatType": "group", "senderId": "ou_user", "senderType": "user",
        }
        result = self.deliver()
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["reason_code"], "private-chat-readback-target-missing")
        self.assertEqual(self.image_runner.calls, 0)

    def test_group_trigger_uses_matching_saved_private_target_for_image_delivery(self):
        self.payload["trigger"] = {
            "feishuProfile": "bridge-read-x", "larkCliProfile": "app_bot", "appId": "app_bot", "identity": "bot",
            "chatType": "group", "chatId": "oc_group", "senderId": "ou_user", "senderType": "user",
        }
        target = {
            "identity": "bot", "profile": "app_bot", "user_id": "ou_user",
            "chat_id": "oc_private", "sender_id": "app_bot", "sender_id_type": "app_id",
        }
        with self.store._transaction():
            self.store.connection.execute(
                "INSERT INTO workflow_meta(key,value) VALUES('report_delivery_target',?)",
                (json.dumps(target),),
            )

        result = self.deliver()

        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(self.client.message_sends, 1)
        self.assertEqual(next(iter(self.client.messages.values()))["chat_id"], "oc_private")
        send_effect = self.store.latest_effect_for_action(self.job_id, "core-image-send", "message-send")
        self.assertEqual(send_effect["status"], "confirmed")

    def test_group_image_delivery_rejects_saved_target_for_another_user_or_bot(self):
        self.payload["trigger"] = {
            "feishuProfile": "bridge-read-x", "larkCliProfile": "app_bot", "appId": "app_bot", "identity": "bot",
            "chatType": "group", "chatId": "oc_group", "senderId": "ou_user", "senderType": "user",
        }
        target = {
            "identity": "bot", "profile": "other-profile", "user_id": "ou_other",
            "chat_id": "oc_other", "sender_id": "other-app", "sender_id_type": "app_id",
        }
        with self.store._transaction():
            self.store.connection.execute(
                "INSERT INTO workflow_meta(key,value) VALUES('report_delivery_target',?)",
                (json.dumps(target),),
            )

        result = self.deliver()

        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["reason_code"], "private-chat-readback-target-missing")
        self.assertEqual(self.image_runner.calls, 0)
        self.assertEqual(self.client.message_sends, 0)

    def test_unverified_bot_profile_stops_before_image_or_feishu_writes(self):
        self.client.identity_ok = False

        result = self.deliver()

        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["reason_code"], "image-delivery-identity-unverified")
        self.assertEqual(self.image_runner.calls, 0)
        self.assertEqual(self.client.created_blocks, 0)
        self.assertEqual(self.client.message_sends, 0)
        self.assertEqual(self.client.created_blocks, 0)
        self.assertEqual(self.client.message_sends, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
