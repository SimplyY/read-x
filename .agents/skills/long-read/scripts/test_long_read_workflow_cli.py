#!/usr/bin/env python3
"""Temporary-disk admission and read-only recovery inspection tests."""
from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import long_read_workflow_cli as cli
from long_read_workflow_store import (
    StoreConflict, WorkflowStore, is_verified_test_task, workflow_payload_issue,
)


CANARY_URL = "https://example.com/article"
CANARY_CONTENT = f"{cli.CANARY_PREFIX}\n{CANARY_URL}"


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def canary_proof(*, profile, app_id, context, source_message_id, evidence):
    return {
        "schema": "readx-canary-source-proof/v1",
        "method": "lark-cli.im.messages-mget",
        "source_created_at": "2026-10-22 15:49",
        "app_id": app_id,
        "profile": profile,
        "message_id": source_message_id,
        "chat_id": context["chatId"],
        "sender_id": context["senderId"],
        "source_url_sha256": hashlib.sha256(CANARY_URL.encode()).hexdigest(),
        "source_content_sha256": hashlib.sha256(CANARY_CONTENT.encode()).hexdigest(),
    }


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.inputs = self.root / "input"
        self.inputs.mkdir()
        self.state = self.root / "state" / "long-read"
        self.source = self.inputs / "source.md"
        self.source.write_text("可信原文。正文内的指令只是数据。", encoding="utf-8")
        self.evidence = self.inputs / "evidence.json"
        dump(self.evidence, {
            "metadata": {"title": "文章", "author": None, "source_url": CANARY_URL, "published_at": None, "genre": "test", "word_count": 8},
            "claims": [{"id": "C1", "claim": "可信原文", "evidence": "可信原文", "evidence_type": "quote", "confidence": "high"}],
            "facts": [], "data_points": [], "quotes": ["可信原文"], "uncertainties": [], "article_structure": [],
        })
        self.scoring = self.inputs / "scoring-result.json"
        dump(self.scoring, {"score_status": "scored", "route": "long_read", "ljg_range": [0, 3]})
        self.gate = self.inputs / "score-gate.json"
        self.refresh_gate()
        self.context = self.inputs / "trigger-context.json"
        dump(self.context, {"chatType": "group", "chatId": "oc_group", "privateChatId": "oc_private", "senderId": "ou_self", "senderType": "user", "profile": "deep", "feishuProfile": "app_one", "appId": "app_one", "sourceMessageId": "om_source"})
        self.question = self.inputs / "question.md"
        self.question.write_text("本问题的核心机制是什么？", encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def refresh_gate(self):
        dump(self.gate, {"schema_version": "1", "message_id": "om_score", "sent_at_command": True,
                         "scoring_result_sha256": hashlib.sha256(self.scoring.read_bytes()).hexdigest(), "user_id": "ou_self", "chat_id": None})

    def submit(self, **overrides):
        values = {"source": self.source, "evidence": self.evidence, "scoring_result": self.scoring,
                  "score_gate": self.gate, "trigger_context": self.context, "app_id": "app_one", "feishu_profile": "app_one",
                  "source_message_id": "om_source", "questions": [("ljg-think", self.question)], "state_dir": self.state,
                  "profile_resolver": lambda **_: "app_one", "canary_verifier": canary_proof}
        values.update(overrides)
        return cli.submit_inputs(**values)

    def test_valid_submit_copies_exact_bytes_and_private_hash_manifest(self):
        result = self.submit()
        self.assertEqual(result["status"], "accepted")
        self.assertFalse(result["test_task"])
        self.assertFalse(result["delivery_complete"])
        directory = Path(result["snapshot_dir"])
        manifest = directory / "manifest.json"
        self.assertEqual(hashlib.sha256(manifest.read_bytes()).hexdigest(), directory.name)
        self.assertEqual((directory / "source.md").read_bytes(), self.source.read_bytes())
        self.assertEqual((directory / "question-01-ljg-think.md").read_bytes(), self.question.read_bytes())
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.state.parent.stat().st_mode & 0o777, 0o700)
        for path in directory.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with WorkflowStore(self.state) as store:
            payload = store.get_job(result["job_id"])["payload"]
        self.assertEqual(payload["route"], "long_read")
        self.assertIs(payload["test_task"], False)
        self.assertEqual(payload["workflow_contract_version"], "read-x-long-read-delivery/v2")
        self.assertEqual(payload["trigger"]["feishuProfile"], "app_one")
        self.assertEqual(payload["trigger"]["larkCliProfile"], "app_one")
        self.assertFalse(payload["artifact_plan_finalized"])
        self.assertNotIn(str(self.inputs), json.dumps(payload))
        for item in payload["inputs"].values():
            self.assertEqual(hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest(), item["sha256"])
        self.source.unlink()
        self.question.unlink()
        self.assertTrue((directory / "source.md").exists())

    def test_group_without_private_chat_or_matching_saved_target_is_not_admitted(self):
        context = json.loads(self.context.read_text())
        context.pop("privateChatId")
        dump(self.context, context)

        with self.assertRaisesRegex(ValueError, "private message delivery target"):
            self.submit()

        with WorkflowStore(self.state) as store:
            self.assertEqual(store.list_jobs(), [])

    def test_group_without_private_chat_uses_matching_saved_target_at_admission(self):
        context = json.loads(self.context.read_text())
        context.pop("privateChatId")
        dump(self.context, context)
        target = {
            "identity": "bot", "profile": "app_one", "user_id": "ou_self",
            "chat_id": "oc_private", "sender_id": "app_one", "sender_id_type": "app_id",
        }
        with WorkflowStore(self.state) as store:
            with store._transaction():
                store.connection.execute(
                    "INSERT INTO workflow_meta(key,value) VALUES('report_delivery_target',?)",
                    (json.dumps(target),),
                )

        result = self.submit()

        with WorkflowStore(self.state) as store:
            payload = store.get_job(result["job_id"])["payload"]
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(payload["trigger"]["privateChatId"], "oc_private")

    def test_explicit_test_task_is_persisted_for_report_exclusion(self):
        context = json.loads(self.context.read_text())
        context["readxCanary"] = True
        dump(self.context, context)
        result = self.submit(test_task=True)
        self.assertTrue(result["test_task"])
        with WorkflowStore(self.state) as store:
            payload = store.get_job(result["job_id"])["payload"]
        self.assertIs(payload["test_task"], True)
        self.assertEqual(payload["trigger"]["identity"], "bot")
        self.assertIs(payload["trigger"]["readxCanary"], True)
        self.assertEqual(payload["canary_source_proof"]["message_id"], "om_source")
        self.assertEqual(payload["canary_source_proof"]["profile"], "app_one")
        self.assertIsNone(workflow_payload_issue(
            payload, app_id="app_one", source_message_id="om_source",
        ))
        malformed_receipt = json.loads(json.dumps(payload))
        malformed_receipt["canary_source_proof"]["source_created_at"] = "not-a-time"
        self.assertEqual(workflow_payload_issue(malformed_receipt), "invalid-workflow-payload")
        self.assertFalse(is_verified_test_task(malformed_receipt))

    def test_remote_admission_lookups_do_not_hold_the_shared_state_lock(self):
        cases = ("profile-resolution", "canary-source-readback")
        for name in cases:
            with self.subTest(lookup=name):
                state_dir = self.root / name / "long-read"
                lookup_started = threading.Event()
                lookup_release = threading.Event()
                lock_acquired = threading.Event()
                outcome = {}
                canary = name == "canary-source-readback"
                if canary:
                    context = json.loads(self.context.read_text())
                    context["readxCanary"] = True
                    dump(self.context, context)

                def blocking_resolver(**_):
                    lookup_started.set()
                    if not lookup_release.wait(5):
                        raise TimeoutError("test lookup release timed out")
                    return "app_one"

                def blocking_canary_verifier(**kwargs):
                    lookup_started.set()
                    if not lookup_release.wait(5):
                        raise TimeoutError("test lookup release timed out")
                    return canary_proof(**kwargs)

                def submit_job():
                    try:
                        outcome["result"] = self.submit(
                            test_task=canary, state_dir=state_dir,
                            profile_resolver=(lambda **_: "app_one") if canary else blocking_resolver,
                            canary_verifier=blocking_canary_verifier if canary else canary_proof,
                        )
                    except Exception as exc:  # surfaced in the main test thread below
                        outcome["error"] = exc

                def acquire_lock():
                    with cli.state_lock(state_dir):
                        lock_acquired.set()

                submitter = threading.Thread(target=submit_job)
                submitter.start()
                self.assertTrue(lookup_started.wait(2), "remote lookup did not start")
                probe = threading.Thread(target=acquire_lock)
                probe.start()
                acquired_while_lookup_blocked = lock_acquired.wait(0.5)
                lookup_release.set()
                submitter.join(5)
                probe.join(5)
                self.assertTrue(acquired_while_lookup_blocked,
                                "remote profile or CANARY lookup held the shared state lock")
                self.assertFalse(submitter.is_alive())
                self.assertFalse(probe.is_alive())
                self.assertNotIn("error", outcome, outcome)
                self.assertEqual(outcome["result"]["status"], "accepted")

    def test_worker_profile_key_resolves_app_bound_cli_profile_and_pins_whoami(self):
        profiles = [{"name": "app_one", "appId": "app_one", "tokenStatus": "valid"}]
        identity = {"identity": "user", "appId": "app_one", "profile": "app_one"}
        with patch.object(cli, "_run_lark_cli_json", side_effect=[profiles, identity]) as run:
            self.assertEqual(
                cli.resolve_worker_profile_key(app_id="app_one", bridge_profile_key="deep"),
                "app_one",
            )
        self.assertEqual(run.call_args_list[0].args[0], ["profile", "list"])
        self.assertEqual(run.call_args_list[1].kwargs, {"profile": "app_one"})

    def test_worker_profile_key_rejects_ambiguous_or_mismatched_profile(self):
        with patch.object(cli, "_run_lark_cli_json", return_value=[
                {"name": "app_one", "appId": "app_one"},
                {"name": "app_one_copy", "appId": "app_one"},
        ]) as run:
            with self.assertRaisesRegex(ValueError, "does not resolve to one"):
                cli.resolve_worker_profile_key(app_id="app_one", bridge_profile_key="deep")
        self.assertEqual(run.call_count, 1)

        with patch.object(cli, "_run_lark_cli_json", side_effect=[
                [{"name": "app_one", "appId": "app_one"}],
                {"identity": "user", "appId": "app_other", "profile": "app_one"},
        ]) as run:
            with self.assertRaisesRegex(ValueError, "does not match"):
                cli.resolve_worker_profile_key(app_id="app_one", bridge_profile_key="deep")
        self.assertEqual(run.call_args_list[1].kwargs, {"profile": "app_one"})

    def test_lark_cli_read_helper_passes_profile_as_global_argument(self):
        completed = type("Completed", (), {"stdout": '{"ok":true}', "returncode": 0})()
        with patch.object(cli.subprocess, "run", return_value=completed) as run:
            self.assertEqual(cli._run_lark_cli_json(["whoami"], profile="app_one"), {"ok": True})
        self.assertEqual(run.call_args.args[0], ["lark-cli", "--profile", "app_one", "whoami"])

    def test_canary_source_verifier_handles_null_identity_and_missing_context(self):
        context = json.loads(self.context.read_text())
        context["readxCanary"] = True
        identity = {"identity": "user", "appId": "app_one", "onBehalfOf": None}
        with patch.object(cli, "_run_lark_cli_json", return_value=identity) as run:
            with self.assertRaisesRegex(ValueError, "reader does not match"):
                cli.verify_canary_source(
                    profile="app_one", app_id="app_one", context=context,
                    source_message_id="om_source",
                    evidence={"metadata": {"source_url": CANARY_URL}},
                )
        self.assertEqual(run.call_count, 1)

        incomplete = dict(context)
        incomplete.pop("chatId")
        with patch.object(cli, "_run_lark_cli_json") as run:
            with self.assertRaisesRegex(ValueError, "source context is incomplete"):
                cli.verify_canary_source(
                    profile="app_one", app_id="app_one", context=incomplete,
                    source_message_id="om_source",
                    evidence={"metadata": {"source_url": CANARY_URL}},
                )
        run.assert_not_called()

    def test_canary_source_verifier_reads_and_binds_the_exact_event(self):
        context = json.loads(self.context.read_text())
        context["readxCanary"] = True
        calls = [
            {"identity": "user", "appId": "app_one",
             "onBehalfOf": {"openId": "ou_self"}},
            {"ok": True, "data": {"chat_mode": "group"}},
            {"ok": True, "data": {"messages": [{
                "message_id": "om_source", "chat_id": "oc_group", "msg_type": "text",
                "deleted": False, "content": CANARY_CONTENT, "create_time": "2026-10-22 15:49",
                "sender": {"sender_type": "user", "id": "ou_self"},
            }]}},
        ]
        with patch.object(cli, "_run_lark_cli_json", side_effect=calls) as run:
            proof = cli.verify_canary_source(
                profile="app_one", app_id="app_one", context=context,
                source_message_id="om_source",
                evidence={"metadata": {"source_url": CANARY_URL}},
            )
        self.assertEqual(proof["message_id"], "om_source")
        self.assertEqual(proof["chat_id"], "oc_group")
        self.assertEqual(proof["sender_id"], "ou_self")
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args_list[1].args[0], [
            "im", "chats", "get", "--as", "user", "--chat-id", "oc_group",
        ])
        self.assertTrue(all(call.kwargs.get("profile") == "app_one" for call in run.call_args_list))

    def test_bridge_profile_is_preserved_separately_from_resolved_cli_profile(self):
        context = json.loads(self.context.read_text())
        context["feishuProfile"] = "deep"
        dump(self.context, context)
        result = self.submit(feishu_profile="deep")
        with WorkflowStore(self.state) as store:
            payload = store.get_job(result["job_id"])["payload"]
        self.assertEqual(payload["trigger"]["feishuProfile"], "deep")
        self.assertEqual(payload["trigger"]["larkCliProfile"], "app_one")

    def test_canary_source_verifier_rejects_sender_and_article_mismatches(self):
        context = json.loads(self.context.read_text())
        context["readxCanary"] = True
        identity = {"identity": "user", "appId": "app_one", "onBehalfOf": {"openId": "ou_self"}}
        mismatched = {"ok": True, "data": {"messages": [{
            "message_id": "om_source", "chat_id": "oc_group", "msg_type": "text",
            "deleted": False, "content": CANARY_CONTENT, "create_time": "2026-10-22 15:49",
            "sender": {"sender_type": "user", "id": "ou_other"},
        }]}}
        group_chat = {"ok": True, "data": {"chat_mode": "group"}}
        with patch.object(cli, "_run_lark_cli_json", side_effect=[identity, group_chat, mismatched]):
            with self.assertRaisesRegex(ValueError, "does not match the original user event"):
                cli.verify_canary_source(
                    profile="app_one", app_id="app_one", context=context,
                    source_message_id="om_source",
                    evidence={"metadata": {"source_url": CANARY_URL}},
                )

        valid_sender = json.loads(json.dumps(mismatched))
        valid_sender["data"]["messages"][0]["sender"]["id"] = "ou_self"
        with patch.object(cli, "_run_lark_cli_json", side_effect=[identity, group_chat, valid_sender]):
            with self.assertRaisesRegex(ValueError, "article URL does not match"):
                cli.verify_canary_source(
                    profile="app_one", app_id="app_one", context=context,
                    source_message_id="om_source",
                    evidence={"metadata": {"source_url": "https://example.com/other"}},
                )

    def test_canary_source_verifier_requires_remote_group_type_and_valid_timestamp(self):
        context = json.loads(self.context.read_text())
        context["readxCanary"] = True
        identity = {"identity": "user", "appId": "app_one", "onBehalfOf": {"openId": "ou_self"}}
        with patch.object(cli, "_run_lark_cli_json", side_effect=[
                identity, {"ok": True, "data": {"chat_mode": "p2p"}}]) as run:
            with self.assertRaisesRegex(ValueError, "not a verified group"):
                cli.verify_canary_source(
                    profile="app_one", app_id="app_one", context=context,
                    source_message_id="om_source",
                    evidence={"metadata": {"source_url": CANARY_URL}},
                )
        self.assertEqual(run.call_count, 2)

        for timestamp in ("not-a-time", "2026-13-99 90:90", "2026-10-22"):
            message = {"ok": True, "data": {"messages": [{
                "message_id": "om_source", "chat_id": "oc_group", "msg_type": "text",
                "deleted": False, "content": CANARY_CONTENT, "create_time": timestamp,
                "sender": {"sender_type": "user", "id": "ou_self"},
            }]}}
            with self.subTest(timestamp=timestamp), patch.object(
                    cli, "_run_lark_cli_json",
                    side_effect=[identity, {"ok": True, "data": {"chat_mode": "group"}}, message]):
                with self.assertRaisesRegex(ValueError, "does not match the original user event"):
                    cli.verify_canary_source(
                        profile="app_one", app_id="app_one", context=context,
                        source_message_id="om_source",
                        evidence={"metadata": {"source_url": CANARY_URL}},
                    )

        deleted_state_missing = {"ok": True, "data": {"messages": [{
            "message_id": "om_source", "chat_id": "oc_group", "msg_type": "text",
            "content": CANARY_CONTENT, "create_time": "2026-10-22 15:49",
            "sender": {"sender_type": "user", "id": "ou_self"},
        }]}}
        with patch.object(cli, "_run_lark_cli_json", side_effect=[
                identity, {"ok": True, "data": {"chat_mode": "group"}}, deleted_state_missing]):
            with self.assertRaisesRegex(ValueError, "does not match the original user event"):
                cli.verify_canary_source(
                    profile="app_one", app_id="app_one", context=context,
                    source_message_id="om_source",
                    evidence={"metadata": {"source_url": CANARY_URL}},
                )

    def test_canary_source_verification_failure_prevents_admission(self):
        context = json.loads(self.context.read_text())
        context["readxCanary"] = True
        dump(self.context, context)
        def reject(**_):
            raise ValueError("source event mismatch")
        with self.assertRaisesRegex(ValueError, "source event mismatch"):
            self.submit(test_task=True, canary_verifier=reject)
        self.assertFalse((self.state / "workflow.sqlite3").exists())

    def test_test_task_requires_the_trusted_bridge_canary_marker(self):
        with self.assertRaisesRegex(ValueError, "trusted bridge Read-X canary marker"):
            self.submit(test_task=True)
        self.assertFalse(self.state.exists())

    def test_trusted_canary_marker_cannot_be_omitted_from_submission_flag(self):
        context = json.loads(self.context.read_text())
        context["readxCanary"] = True
        dump(self.context, context)
        with self.assertRaisesRegex(ValueError, "trusted bridge Read-X canary marker"):
            self.submit()
        self.assertFalse(self.state.exists())

    def test_duplicate_event_same_input_reuses_job_and_snapshot(self):
        first, again = self.submit(), self.submit()
        self.assertEqual(first["job_id"], again["job_id"])
        self.assertFalse(again["created"])
        self.assertEqual(first["snapshot_dir"], again["snapshot_dir"])
        self.assertEqual(len(list((self.state / "artifacts").iterdir())), 1)
        copied_source = self.root / "same-content-another-path.md"
        copied_source.write_bytes(self.source.read_bytes())
        self.assertEqual(self.submit(source=copied_source)["job_id"], first["job_id"])

    def test_changed_input_on_same_event_is_conflict(self):
        first = self.submit()
        self.question.write_text("另一个问题。", encoding="utf-8")
        with self.assertRaises(StoreConflict):
            self.submit()
        with WorkflowStore(self.state) as store:
            self.assertEqual(len(store.list_jobs()), 1)
            self.assertEqual(store.get_job(first["job_id"])["status"], "pending")
        snapshots = [path for path in (self.state / "artifacts").iterdir() if path.is_dir()]
        self.assertEqual([path.name for path in snapshots], [first["snapshot_sha256"]])

    def test_feishu_profile_must_match_trusted_trigger_context(self):
        with self.assertRaisesRegex(ValueError, "does not match trusted trigger context"):
            self.submit(feishu_profile="another-profile")

    def test_missing_source_event_is_rejected_before_state_creation(self):
        with self.assertRaisesRegex(ValueError, "trusted Feishu source message id"):
            self.submit(source_message_id=None)
        self.assertFalse(self.state.exists())

    def test_broken_or_mismatched_gate_rejected_before_database_submit(self):
        bads = [{"schema_version": "1"}, {"schema_version": "1", "message_id": "om_score", "sent_at_command": True, "scoring_result_sha256": "0" * 64}]
        for gate in bads:
            dump(self.gate, gate)
            with self.assertRaises(ValueError):
                self.submit()
            self.assertFalse((self.state / "workflow.sqlite3").exists())
            self.assertEqual(list((self.state / "artifacts").iterdir()), [])

    def test_route_and_evidence_fail_closed(self):
        for status, route in (("needs_review", "long_read"), ("scored", "card")):
            dump(self.scoring, {"score_status": status, "route": route})
            self.refresh_gate()
            with self.assertRaises(ValueError):
                self.submit()
        dump(self.scoring, {"score_status": "scored", "route": "long_read"})
        self.refresh_gate()
        value = json.loads(self.evidence.read_text())
        value["quotes"] = ["编造引文"]
        dump(self.evidence, value)
        with self.assertRaises(ValueError):
            self.submit()
        self.assertFalse(self.state.exists())

    def test_gate_target_and_trigger_identity_are_checked(self):
        context = json.loads(self.context.read_text())
        context["appId"] = "another_app"
        dump(self.context, context)
        with self.assertRaises(ValueError):
            self.submit()
        context["senderId"] = "ou_self"
        context["chatType"] = "unknown"
        dump(self.context, context)
        with self.assertRaises(ValueError):
            self.submit()
        context["appId"] = "app_one"
        context["senderId"] = "ou_someone_else"
        dump(self.context, context)
        with self.assertRaises(ValueError):
            self.submit()

    def test_source_message_id_must_match_the_single_trigger_event(self):
        with self.assertRaisesRegex(ValueError, "does not match trusted trigger context"):
            self.submit(source_message_id="om_other")
        with self.assertRaisesRegex(ValueError, "trusted Feishu source message id"):
            self.submit(source_message_id="")

    def test_input_and_state_symlinks_rejected(self):
        linked = self.inputs / "linked.md"
        linked.symlink_to(self.source)
        with self.assertRaises(ValueError):
            self.submit(source=linked)
        linked_dir = self.root / "input-link"
        linked_dir.symlink_to(self.inputs)
        with self.assertRaises(ValueError):
            self.submit(source=linked_dir / "source.md")
        state_link = self.root / "state-link"
        state_link.symlink_to(self.inputs)
        with self.assertRaises(ValueError):
            self.submit(state_dir=state_link)
        self.state.mkdir(parents=True)
        (self.state / "artifacts").symlink_to(self.inputs)
        with self.assertRaises(ValueError):
            self.submit()

    def test_existing_snapshot_tampering_is_never_overwritten(self):
        result = self.submit()
        source = Path(result["snapshot_dir"]) / "source.md"
        source.write_text("tampered", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.submit()
        self.assertEqual(source.read_text(), "tampered")

    def test_existing_snapshot_permissions_fail_closed(self):
        result = self.submit()
        copied = Path(result["snapshot_dir"]) / "source.md"
        os.chmod(copied, 0o644)
        with self.assertRaises(ValueError):
            self.submit()

    def test_existing_parent_permissions_are_preserved(self):
        os.chmod(self.root, 0o755)
        self.submit()
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o755)

    def test_resume_expired_attempt_is_candidate_without_state_change(self):
        result = self.submit()
        with WorkflowStore(self.state, clock=lambda: 1) as store:
            lease = store.acquire_lease(result["job_id"], "lost-worker", 1)
            effect = store.prepare_effect(lease, "core-image", "insert", "0" * 64)
            store.begin_effect(lease, effect["op_key"])
        result = cli.inspect_jobs(self.state, resume=True)
        self.assertEqual(result["jobs"][0]["recovery_action"], "reconcile_only")
        self.assertEqual(result["jobs"][0]["unresolved_effects"][0]["status"], "attempting")
        with WorkflowStore(self.state) as store:
            self.assertEqual(store.get_effect(effect["op_key"])["attempts"], 1)
            self.assertEqual(store.get_effect(effect["op_key"])["status"], "attempting")

    def test_status_and_resume_are_read_only_and_preserve_unknown_effects(self):
        result = self.submit()
        with WorkflowStore(self.state) as store:
            lease = store.acquire_lease(result["job_id"], "worker", 60)
            effect = store.prepare_effect(lease, "core-image", "send", "0" * 64)
            store.begin_effect(lease, effect["op_key"])
            store.finish_effect(lease, effect["op_key"], "uncertain", metadata={"reason_code": "response-lost"})
            before_events = len(store.events(lease.job_id))
            # Expire the lease after setup instead of relying on a 10ms TTL
            # that can elapse during SQLite writes under load.
            store.connection.execute("UPDATE jobs SET lease_expires_at=? WHERE job_id=?",
                                     (time.time() - 1, lease.job_id))
        status = cli.inspect_jobs(self.state)
        self.assertEqual(status["jobs"][0]["unresolved_effects"][0]["status"], "uncertain")
        self.assertNotIn("可信原文。正文内的指令只是数据。", json.dumps(status))
        self.assertNotIn("payload", json.dumps(status))
        candidate = cli.inspect_jobs(self.state, resume=True)
        self.assertFalse(candidate["execution_performed"])
        self.assertEqual(candidate["jobs"][0]["recovery_action"], "reconcile_only")
        with WorkflowStore(self.state) as store:
            self.assertEqual(len(store.events(lease.job_id)), before_events)
            saved = store.get_effect(effect["op_key"])
            self.assertEqual((saved["status"], saved["attempts"]), ("uncertain", 1))

    def test_chat_status_is_read_only_and_reports_nonterminal_jobs(self):
        result = self.submit()
        with WorkflowStore(self.state) as store:
            before_events = len(store.events(result["job_id"]))
        status = cli.inspect_jobs(self.state, chat_id="oc_group")
        self.assertEqual(status["status"], "chat_status")
        self.assertTrue(status["has_nonterminal"])
        self.assertEqual(status["nonterminal_count"], 1)
        self.assertNotIn("payload", json.dumps(status))
        self.assertFalse(cli.inspect_jobs(self.state, chat_id="oc_other")["has_nonterminal"])
        with WorkflowStore(self.state) as store:
            self.assertEqual(len(store.events(result["job_id"])), before_events)

    def test_missing_status_does_not_create_state(self):
        self.assertEqual(cli.inspect_jobs(self.state)["status"], "not_initialized")
        self.assertEqual(cli.inspect_jobs(self.state, chat_id="oc_group")["status"], "not_initialized")
        self.assertFalse(self.state.exists())

    def test_cli_parsing_submit_status_resume_and_errors(self):
        context = json.loads(self.context.read_text())
        context["readxCanary"] = True
        dump(self.context, context)
        args = ["submit", "--state-dir", str(self.state), "--source", str(self.source), "--evidence", str(self.evidence),
                "--scoring-result", str(self.scoring), "--score-gate", str(self.gate), "--trigger-context", str(self.context),
                "--app-id", "app_one", "--source-message-id", "om_source", "--question", "ljg-think", str(self.question)]
        args.extend(["--feishu-profile", "app_one"])
        args.append("--test-task")
        output = StringIO()
        with patch.object(cli, "resolve_worker_profile_key", return_value="app_one"), \
                patch.object(cli, "verify_canary_source", side_effect=canary_proof), \
                redirect_stdout(output):
            self.assertEqual(cli.main(args), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "accepted")
        self.assertTrue(result["test_task"])
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli.main(["--state-dir", str(self.state), "status", "--job-id", result["job_id"]]), 0)
        self.assertEqual(len(json.loads(output.getvalue())["jobs"]), 1)
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli.main(["--state-dir", str(self.state), "status", "--chat-id", "oc_group"]), 0)
        self.assertTrue(json.loads(output.getvalue())["has_nonterminal"])
        import long_read_workflow_worker
        original = long_read_workflow_worker.run_once
        long_read_workflow_worker.run_once = lambda *args, **kwargs: {"status": "waiting_for_delivery_stages", "job_id": result["job_id"], "delivery_complete": False}
        try:
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli.main(["--state-dir", str(self.state), "resume", "--job-id", result["job_id"]]), 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "waiting_for_delivery_stages")
        finally:
            long_read_workflow_worker.run_once = original
        dump(self.gate, {})
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli.main(args), 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "blocked")

    def test_record_event_cli_persists_validated_repair_evidence(self):
        job = self.submit()
        output = StringIO()
        details = json.dumps({
            "repair_id": "fix-42", "failure_fingerprint": "bridge:response-unstable",
            "summary": "保留原 Bridge 会话以便只读恢复", "change_ref": "abc1234",
        }, ensure_ascii=False)
        with redirect_stdout(output):
            self.assertEqual(cli.main([
                "--state-dir", str(self.state), "record-event", "--job-id", job["job_id"],
                "--event-type", "repair_applied", "--details-json", details,
            ]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "recorded")
        self.assertEqual(result["details"]["repair_id"], "fix-42")
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli.main([
                "--state-dir", str(self.state), "record-event", "--job-id", job["job_id"],
                "--event-type", "repair_applied", "--details-json", '{"raw_text":"x"}',
            ]), 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "blocked")

    def test_report_command_persists_and_reuses_month_snapshot(self):
        report_state = self.root / "report-state"
        args = ["--state-dir", str(report_state), "report", "--period", "monthly", "--now", "2026-10-21T09:00:00+08:00"]
        first_output = StringIO()
        with redirect_stdout(first_output):
            self.assertEqual(cli.main(args), 0)
        first = json.loads(first_output.getvalue())
        self.assertEqual(first["status"], "pending_delivery")
        self.assertEqual(first["snapshot"]["counts"]["N"], 0)
        second_output = StringIO()
        with redirect_stdout(second_output):
            self.assertEqual(cli.main(args), 0)
        second = json.loads(second_output.getvalue())
        self.assertTrue(second["reused"])
        self.assertEqual(second["snapshot_sha256"], first["snapshot_sha256"])

    def test_report_status_shows_uncertain_outbox_without_report_body_or_remote_ids(self):
        report_key = "weekly:2026-10-18"
        with WorkflowStore(self.state) as store:
            store.connection.execute(
                "INSERT INTO reports(report_key,period_type,window_start,window_end,snapshot_json,snapshot_sha256,markdown,markdown_sha256,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (report_key, "weekly", 1, 2, "{}", "a" * 64, "private report body", "b" * 64,
                 "pending_delivery", 3),
            )
            store.prepare_report_effect(report_key, "markdown-upload", "c" * 64)
            store.begin_report_effect(report_key, "markdown-upload")
            store.finish_report_effect(report_key, "markdown-upload", "uncertain",
                                       metadata={"reason_code": "remote-result-unknown"})
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli.main(["--state-dir", str(self.state), "status", "--report-key", report_key]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "report_status")
        self.assertTrue(result["report"]["delivery_pending"])
        self.assertEqual(result["report"]["outbox"][0]["status"], "uncertain")
        self.assertFalse(result["report"]["outbox"][0]["remote_result_known"])
        self.assertFalse(result["execution_performed"])
        self.assertNotIn("private report body", output.getvalue())

    def test_run_due_reports_cli_catches_up_periods_without_sending(self):
        report_state = self.root / "scheduler-state"
        with WorkflowStore(report_state) as store:
            store.connection.execute(
                "UPDATE workflow_meta SET value=? WHERE key='tracking_started_at'",
                (str(datetime.fromisoformat("2026-10-01T10:00:00+08:00").timestamp()),),
            )
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli.main([
                "--state-dir", str(report_state), "run-due-reports",
                "--now", "2026-11-01T10:00:00+08:00",
            ]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "checked")
        self.assertEqual(result["due_count"], 6)
        self.assertFalse(result["delivery_enabled"])
        self.assertEqual([item["period"] for item in result["reports"][-2:]], ["monthly", "weekly"])

    def test_worker_service_tick_runs_report_scheduler_and_job_worker_together(self):
        import long_read_workflow_scheduler
        import long_read_workflow_worker

        calls = []
        target = {"identity": "bot", "profile": "read-x", "user_id": "ou_self",
                  "chat_id": "oc_private", "sender_id": "app_one"}
        with patch.object(long_read_workflow_scheduler, "run_due_reports",
                          side_effect=lambda *args, **kwargs: calls.append(("reports", kwargs["delivery_target"])) or {"status": "checked"}), \
             patch.object(long_read_workflow_worker, "run_once",
                          side_effect=lambda *args, **kwargs: calls.append(("worker", kwargs["owner"])) or {"status": "idle"}):
            result = cli.worker_service_tick(self.state, owner="worker-a", report_target=target)
        self.assertEqual([item[0] for item in calls], ["reports", "worker"])
        self.assertEqual(calls[0][1], target)
        self.assertEqual(calls[1][1], "worker-a")
        self.assertEqual(result, {"status": "tick", "reports": {"status": "checked"}, "worker": {"status": "idle"}})

    def test_worker_service_blocks_legacy_nonterminal_jobs_before_any_delivery(self):
        import long_read_workflow_scheduler
        import long_read_workflow_worker

        with WorkflowStore(self.state) as store:
            legacy = store.submit({"schema": "long-read-submission/v1"}, app_id="legacy-app",
                                  artifacts=[{"key": "old-analysis", "kind": "analysis"}])
        calls = []
        target = {"identity": "bot", "profile": "read-x", "user_id": "ou_self",
                  "chat_id": "oc_private", "sender_id": "app_one"}
        with patch.object(long_read_workflow_scheduler, "run_due_reports",
                          side_effect=lambda *args, **kwargs: calls.append("reports")), \
             patch.object(long_read_workflow_worker, "run_once",
                          side_effect=lambda *args, **kwargs: calls.append("worker")):
            with self.assertRaisesRegex(StoreConflict, "unadmitted legacy jobs"):
                cli.worker_service_tick(self.state, owner="worker-a", report_target=target)
        self.assertEqual(calls, [])
        with WorkflowStore(self.state) as store:
            self.assertEqual(store.get_job(legacy["job_id"])["status"], "pending")

    def test_resident_worker_requires_persisted_report_target(self):
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli.main(["--state-dir", str(self.state), "run-worker", "--serve"]), 2)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "blocked")
        self.assertIn("report delivery target is not configured", result["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
