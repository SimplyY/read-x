#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from feishu_long_read_delivery import (
    DeliveryError,
    FeishuLongReadDelivery,
    RetryableDeliveryError,
    UnknownRemoteResult,
    match_image_messages,
    match_report_messages,
)


def response(payload: dict, code: int = 0):
    return subprocess.CompletedProcess([], code, json.dumps(payload), "")


class FeishuDeliveryTests(unittest.TestCase):
    def test_auth_status_uses_its_documented_identity_flags_without_as_or_profile(self):
        calls = []

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return response({"identity": "user", "verified": True, "identities": {
                "user": {"openId": "ou_self", "tokenStatus": "valid"},
            }})

        client = FeishuLongReadDelivery(identity="user", profile="read-x", runner=runner)
        result = client.auth_status()
        self.assertEqual(result["identities"]["user"]["openId"], "ou_self")
        self.assertEqual(calls[0][0], [client.binary, "auth", "status", "--json", "--verify"])
        self.assertEqual(calls[0][1]["env"]["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"], "1")
        self.assertEqual(calls[0][1]["env"]["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"], "1")

    def test_bot_write_rechecks_the_pinned_profile_and_app_before_sending(self):
        calls = []
        outputs = [
            response([{"name": "app_bot", "appId": "app_bot"}]),
            response({"identity": "user", "appId": "app_bot", "profile": "app_bot"}),
            response({"code": 0, "data": {"message_id": "om_sent"}}),
        ]

        def runner(args, **kwargs):
            calls.append(args)
            return outputs.pop(0)

        client = FeishuLongReadDelivery(
            identity="bot", profile="app_bot", runner=runner, expected_app_id="app_bot",
        )

        self.assertEqual(client.send_image_message(
            image_key="img_asset", idempotency_key="request_1", chat_id="oc_private",
        ), "om_sent")
        self.assertEqual(calls[0], [client.binary, "profile", "list"])
        self.assertEqual(calls[1], [client.binary, "--profile", "app_bot", "whoami"])
        self.assertEqual(calls[2][1:3], ["im", "+messages-send"])

    def test_bot_write_stops_when_the_pinned_profile_resolves_to_another_app(self):
        calls = []
        outputs = [
            response([{"name": "wrong_profile", "appId": "app_other"}]),
        ]

        def runner(args, **kwargs):
            calls.append(args)
            return outputs.pop(0)

        client = FeishuLongReadDelivery(
            identity="bot", profile="app_bot", runner=runner, expected_app_id="app_bot",
        )
        with self.assertRaises(DeliveryError):
            client.send_image_message(
                image_key="img_asset", idempotency_key="request_1", chat_id="oc_private",
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1:], ["profile", "list"])

    def test_create_image_block_uses_explicit_position_and_client_token(self):
        calls = []

        def runner(args, **kwargs):
            calls.append(args)
            return response({"code": 0, "data": {"children": [{"block_id": "blk_new", "block_type": 27}]}})

        client = FeishuLongReadDelivery(identity="bot", profile="read-x", runner=runner)
        block_id = client.create_image_block(
            document_id="doc_1", parent_block_id="doc_1", index=1,
            client_token="c4e8af13-e0bb-469e-a1a6-a1f5fc98d520",
        )
        self.assertEqual(block_id, "blk_new")
        command = calls[0]
        self.assertTrue(command[0].endswith("/lark-cli"))
        self.assertEqual(command[1:5], ["api", "POST", "/open-apis/docx/v1/documents/doc_1/blocks/doc_1/children", "--params"])
        params = json.loads(command[command.index("--params") + 1])
        body = json.loads(command[command.index("--data") + 1])
        self.assertEqual(params["client_token"], "c4e8af13-e0bb-469e-a1a6-a1f5fc98d520")
        self.assertEqual(body, {"index": 1, "children": [{"block_type": 27, "image": {}}]})
        self.assertIn("--as", command)
        self.assertEqual(command[command.index("--as") + 1], "bot")
        self.assertEqual(command[command.index("--profile") + 1], "read-x")

    def test_create_response_without_block_id_is_unknown(self):
        client = FeishuLongReadDelivery(
            identity="bot", profile="read-x",
            runner=lambda *a, **k: response({"code": 0, "data": {"children": []}}),
        )
        with self.assertRaises(UnknownRemoteResult):
            client.create_image_block(
                document_id="doc_1", parent_block_id="doc_1", index=0,
                client_token="c4e8af13-e0bb-469e-a1a6-a1f5fc98d520",
            )

    def test_timeout_on_write_is_unknown_and_never_retried(self):
        calls = []

        def runner(args, **kwargs):
            calls.append(args)
            raise subprocess.TimeoutExpired(args, 1)

        client = FeishuLongReadDelivery(identity="bot", profile="read-x", runner=runner)
        with self.assertRaises(UnknownRemoteResult):
            client.create_image_block(
                document_id="doc_1", parent_block_id="doc_1", index=0,
                client_token="c4e8af13-e0bb-469e-a1a6-a1f5fc98d520",
            )
        self.assertEqual(len(calls), 1)

    def test_nonzero_write_with_success_shaped_json_is_unknown(self):
        client = FeishuLongReadDelivery(
            identity="bot", profile="read-x",
            runner=lambda *a, **k: response({"code": 0, "data": {"file_token": "media_1"}}, code=1),
        )
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "core.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nrest")
            with self.assertRaises(UnknownRemoteResult):
                client.upload_document_image(document_id="doc_1", image_block_id="blk_1", png_path=image)

    def test_nonzero_write_with_explicit_api_rejection_is_failed(self):
        client = FeishuLongReadDelivery(
            identity="bot", profile="read-x",
            runner=lambda *a, **k: response({"code": 12345, "msg": "rejected"}, code=1),
        )
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "core.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nrest")
            with self.assertRaises(DeliveryError):
                client.upload_document_image(document_id="doc_1", image_block_id="blk_1", png_path=image)

    def test_documented_feishu_rate_limit_is_distinguished_as_retryable(self):
        client = FeishuLongReadDelivery(
            identity="bot", profile="read-x",
            runner=lambda *a, **k: response({"code": 99991400, "msg": "request trigger frequency limit"}),
        )
        with self.assertRaises(RetryableDeliveryError):
            client.create_image_block(
                document_id="doc_1", parent_block_id="doc_1", index=0,
                client_token="c4e8af13-e0bb-469e-a1a6-a1f5fc98d520",
            )

    def test_upload_uses_docx_image_parent_and_returns_file_token(self):
        calls = []

        def runner(args, **kwargs):
            calls.append(args)
            return response({"code": 0, "data": {"file_token": "media_1"}})

        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "core.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nrest")
            client = FeishuLongReadDelivery(identity="bot", profile="read-x", runner=runner)
            self.assertEqual(client.upload_document_image(document_id="doc_1", image_block_id="blk_1", png_path=image), "media_1")
        command = calls[0]
        self.assertTrue(command[0].endswith("/lark-cli"))
        self.assertEqual(command[1:3], ["docs", "+media-upload"])
        self.assertEqual(command[command.index("--parent-type") + 1], "docx_image")
        self.assertEqual(command[command.index("--parent-node") + 1], "blk_1")

    def test_patch_and_readback_verify_exact_image_token_and_parent(self):
        calls = []

        def runner(args, **kwargs):
            calls.append(args)
            if args[1:3] == ["api", "PATCH"]:
                return response({"code": 0, "data": {}})
            return response({"code": 0, "data": {"block": {
                "block_id": "blk_1", "block_type": 27, "parent_id": "doc_1",
                "image": {"token": "media_1"},
            }}})

        client = FeishuLongReadDelivery(identity="bot", profile="read-x", runner=runner)
        client.patch_image_block(
            document_id="doc_1", block_id="blk_1", file_token="media_1",
            client_token="c4e8af13-e0bb-469e-a1a6-a1f5fc98d520",
        )
        block = client.verify_document_image(
            document_id="doc_1", block_id="blk_1", parent_block_id="doc_1", file_token="media_1",
        )
        body = json.loads(calls[0][calls[0].index("--data") + 1])
        self.assertEqual(body, {"replace_image": {"token": "media_1"}})
        self.assertEqual(block["image"]["token"], "media_1")
        with self.assertRaises(UnknownRemoteResult):
            client.verify_document_image(
                document_id="doc_1", block_id="blk_1", parent_block_id="other", file_token="media_1",
            )

    def test_lost_create_response_can_only_be_reconciled_from_one_exact_ordered_diff(self):
        result = FeishuLongReadDelivery.verify_new_image_child(
            before_child_ids=["title", "source"], after_child_ids=["title", "image", "source"],
            index=1, blocks_by_id={"image": {"block_id": "image", "parent_id": "doc", "block_type": 27}},
            parent_block_id="doc",
        )
        self.assertEqual(result, "image")
        with self.assertRaises(UnknownRemoteResult):
            FeishuLongReadDelivery.verify_new_image_child(
                before_child_ids=["title", "source"], after_child_ids=["title", "image", "other", "source"],
                index=1, blocks_by_id={"image": {"block_id": "image", "parent_id": "doc", "block_type": 27}},
                parent_block_id="doc",
            )

    def test_send_image_uses_idempotency_and_unreadable_receipt_is_unknown(self):
        calls = []

        def runner(args, **kwargs):
            calls.append(args)
            return response({"code": 0, "data": {"message_id": "om_123"}})

        client = FeishuLongReadDelivery(identity="bot", profile="read-x", runner=runner)
        message_id = client.send_image_message(chat_id="oc_chat", image_key="img_1", idempotency_key="request_1")
        self.assertEqual(message_id, "om_123")
        command = calls[0]
        self.assertIn("--chat-id", command)
        self.assertEqual(command[command.index("--chat-id") + 1], "oc_chat")
        self.assertEqual(command[command.index("--idempotency-key") + 1], "request_1")
        user_message = client.send_image_message(user_id="ou_user", image_key="img_1", idempotency_key="request_2")
        self.assertEqual(user_message, "om_123")
        self.assertIn("--user-id", calls[1])

        uncertain = FeishuLongReadDelivery(
            identity="bot", profile="read-x",
            runner=lambda *a, **k: response({"code": 0, "data": {}}),
        )
        with self.assertRaises(UnknownRemoteResult):
            uncertain.send_image_message(chat_id="oc_chat", image_key="img_1", idempotency_key="request_1")

    def test_exact_message_readback_matches_chat_sender_and_image_key(self):
        messages = [
            {"msg_type": "image", "chat_id": "oc_chat", "sender": {"id": "app_bot", "id_type": "app_id"},
             "body": {"content": '{"image_key":"img_1"}'}, "message_id": "om_1"},
            {"msg_type": "image", "chat_id": "oc_other", "sender": {"id": "app_bot", "id_type": "app_id"},
             "body": {"content": '{"image_key":"img_1"}'}, "message_id": "om_2"},
            {"msg_type": "image", "chat_id": "oc_chat", "sender": {"id": "app_bot", "id_type": "app_id"},
             "body": {"content": '{"image_key":"img_2"}'}, "message_id": "om_3"},
        ]
        self.assertEqual(
            [item["message_id"] for item in match_image_messages(messages, chat_id="oc_chat", image_key="img_1", sender_id="app_bot", sender_id_type="app_id")],
            ["om_1"],
        )

    def test_report_message_match_requires_exact_card_or_file_target_and_sender(self):
        card = {"schema": "2.0", "header": {"title": "Report"}}
        messages = [
            {"message_id": "om_card", "msg_type": "interactive", "chat_id": "oc_self",
             "sender": {"id": "app_bot", "id_type": "app_id"},
             "body": {"content": json.dumps(card)}},
            {"message_id": "om_other", "msg_type": "interactive", "chat_id": "oc_self",
             "sender": {"id": "app_other", "id_type": "app_id"},
             "body": {"content": json.dumps(card)}},
            {"message_id": "om_file", "msg_type": "file", "chat_id": "oc_self",
             "sender": {"id": "app_bot", "id_type": "app_id"},
             "body": {"content": '{"file_key":"file_report"}'}},
        ]
        self.assertEqual([item["message_id"] for item in match_report_messages(
            messages, chat_id="oc_self", sender_id="app_bot", sender_id_type="app_id",
            artifact="card-send", expected=card,
        )], ["om_card"])
        self.assertEqual([item["message_id"] for item in match_report_messages(
            messages, chat_id="oc_self", sender_id="app_bot", sender_id_type="app_id",
            artifact="markdown-send", expected={"file_key": "file_report"},
        )], ["om_file"])

    def test_get_message_uses_official_items_envelope(self):
        message = {"message_id": "om_123", "msg_type": "image"}
        client = FeishuLongReadDelivery(
            identity="bot", profile="read-x",
            runner=lambda *a, **k: response({"code": 0, "data": {"items": [message]}}),
        )
        self.assertEqual(client.get_message("om_123"), message)

    def test_chat_history_requests_original_card_json_for_exact_readback(self):
        calls = []

        def runner(args, **kwargs):
            calls.append(args)
            return response({"code": 0, "data": {"items": [], "has_more": False}})

        client = FeishuLongReadDelivery(identity="bot", profile="read-x", runner=runner)
        self.assertEqual(client.list_chat_messages(
            chat_id="oc_self", start_time=1000, end_time=1060,
        ), [])
        params = json.loads(calls[0][calls[0].index("--params") + 1])
        self.assertEqual(params["card_msg_content_type"], "user_card_content")

    def test_chat_history_requires_explicit_boolean_pagination_completion(self):
        for invalid_marker in ("missing", None, "false", 0):
            with self.subTest(has_more=invalid_marker):
                data = {"items": []}
                if invalid_marker != "missing":
                    data["has_more"] = invalid_marker
                client = FeishuLongReadDelivery(
                    identity="bot", profile="read-x",
                    runner=lambda *a, _data=data, **k: response({"code": 0, "data": _data}),
                )
                with self.assertRaises(DeliveryError):
                    client.list_chat_messages(chat_id="oc_self", start_time=1000, end_time=1060)

    def test_document_block_list_requires_explicit_boolean_pagination_completion(self):
        for invalid_marker in ("missing", None, "false", 0):
            with self.subTest(has_more=invalid_marker):
                data = {"items": []}
                if invalid_marker != "missing":
                    data["has_more"] = invalid_marker
                client = FeishuLongReadDelivery(
                    identity="bot", profile="read-x",
                    runner=lambda *a, _data=data, **k: response({"code": 0, "data": _data}),
                )
                with self.assertRaises(DeliveryError):
                    client.list_document_blocks("doc_1")

        client = FeishuLongReadDelivery(
            identity="bot", profile="read-x",
            runner=lambda *a, **k: response({"code": 0, "data": {"items": [], "has_more": True}}),
        )
        with self.assertRaises(DeliveryError):
            client.list_document_blocks("doc_1")

    def test_chat_history_requires_next_page_token_when_more_is_true(self):
        client = FeishuLongReadDelivery(
            identity="bot", profile="read-x",
            runner=lambda *a, **k: response({"code": 0, "data": {"items": [], "has_more": True}}),
        )
        with self.assertRaises(DeliveryError):
            client.list_chat_messages(chat_id="oc_self", start_time=1000, end_time=1060)

    def test_report_card_and_markdown_file_calls_keep_one_explicit_target(self):
        calls = []

        def runner(args, **kwargs):
            calls.append(args)
            if args[1:3] == ["im", "files"]:
                return response({"code": 0, "data": {"file_key": "file_report"}})
            return response({"code": 0, "data": {"message_id": "om_123"}})

        card = {"schema": "2.0", "header": {}, "body": {"elements": []}}
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "monthly.md"
            report.write_text("# Report\n", encoding="utf-8")
            client = FeishuLongReadDelivery(identity="bot", profile="read-x", runner=runner)
            file_key = client.upload_markdown_attachment(path=report)
        self.assertEqual(file_key, "file_report")
        self.assertEqual(client.send_file_message(file_key=file_key, user_id="ou_self", idempotency_key="file_1"), "om_123")
        self.assertEqual(client.send_interactive_card(card=card, user_id="ou_self", idempotency_key="card_1"), "om_123")
        upload = calls[0]
        self.assertEqual(upload[1:3], ["im", "files"])
        self.assertEqual(upload[upload.index("--data") + 1], '{"file_name":"monthly.md","file_type":"stream"}')
        self.assertEqual(calls[1][calls[1].index("--user-id") + 1], "ou_self")
        self.assertEqual(calls[2][calls[2].index("--msg-type") + 1], "interactive")
        self.assertEqual(calls[2][calls[2].index("--user-id") + 1], "ou_self")
        with self.assertRaises(ValueError):
            client.send_file_message(file_key=file_key, chat_id="oc_chat", user_id="ou_self", idempotency_key="dup")

    def test_block_and_message_lists_fail_closed_on_incomplete_pagination(self):
        outputs = [
            response({"code": 0, "data": {"items": [{"block_id": "doc", "children": []}], "has_more": True, "page_token": "p1"}}),
            response({"code": 0, "data": {"items": [], "has_more": True, "page_token": "p1"}}),
        ]
        client = FeishuLongReadDelivery(identity="bot", profile="read-x", runner=lambda *a, **k: outputs.pop(0))
        with self.assertRaises(DeliveryError):
            client.list_document_blocks("doc")

    def test_profile_and_identity_are_required(self):
        with self.assertRaises(ValueError):
            FeishuLongReadDelivery(identity="", profile="read-x")
        with self.assertRaises(ValueError):
            FeishuLongReadDelivery(identity="bot", profile="")


if __name__ == "__main__":
    unittest.main()
