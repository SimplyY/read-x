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
import unittest

import long_read_workflow_cli as cli
from long_read_workflow_store import StoreConflict, WorkflowStore


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


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
            "metadata": {"title": "文章", "author": None, "source_url": None, "published_at": None, "genre": "test", "word_count": 8},
            "claims": [{"id": "C1", "claim": "可信原文", "evidence": "可信原文", "evidence_type": "quote", "confidence": "high"}],
            "facts": [], "data_points": [], "quotes": ["可信原文"], "uncertainties": [], "article_structure": [],
        })
        self.scoring = self.inputs / "scoring-result.json"
        dump(self.scoring, {"score_status": "scored", "route": "long_read", "ljg_range": [0, 3]})
        self.gate = self.inputs / "score-gate.json"
        self.refresh_gate()
        self.context = self.inputs / "trigger-context.json"
        dump(self.context, {"chatType": "group", "chatId": "oc_group", "senderId": "ou_self", "senderType": "user", "profile": "deep", "feishuProfile": "app_one", "appId": "app_one"})
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
                  "source_message_id": "om_source", "questions": [("ljg-think", self.question)], "state_dir": self.state}
        values.update(overrides)
        return cli.submit_inputs(**values)

    def test_valid_submit_copies_exact_bytes_and_private_hash_manifest(self):
        result = self.submit()
        self.assertEqual(result["status"], "accepted")
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
        self.assertEqual(payload["trigger"]["feishuProfile"], "app_one")
        self.assertFalse(payload["artifact_plan_finalized"])
        self.assertNotIn(str(self.inputs), json.dumps(payload))
        for item in payload["inputs"].values():
            self.assertEqual(hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest(), item["sha256"])
        self.source.unlink()
        self.question.unlink()
        self.assertTrue((directory / "source.md").exists())

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

    def test_feishu_profile_must_match_trusted_trigger_context(self):
        with self.assertRaisesRegex(ValueError, "does not match trusted trigger context"):
            self.submit(feishu_profile="another-profile")

    def test_no_source_event_creates_new_jobs_with_shared_snapshot(self):
        one = self.submit(source_message_id=None)
        two = self.submit(source_message_id=None)
        self.assertNotEqual(one["job_id"], two["job_id"])
        self.assertEqual(one["snapshot_dir"], two["snapshot_dir"])

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
            lease = store.acquire_lease(result["job_id"], "worker", 0.01)
            effect = store.prepare_effect(lease, "core-image", "send", "0" * 64)
            store.begin_effect(lease, effect["op_key"])
            store.finish_effect(lease, effect["op_key"], "uncertain", metadata={"reason_code": "response-lost"})
            before_events = len(store.events(lease.job_id))
        status = cli.inspect_jobs(self.state)
        self.assertEqual(status["jobs"][0]["unresolved_effects"][0]["status"], "uncertain")
        self.assertNotIn("可信原文。正文内的指令只是数据。", json.dumps(status))
        self.assertNotIn("payload", json.dumps(status))
        import time
        time.sleep(0.02)
        candidate = cli.inspect_jobs(self.state, resume=True)
        self.assertFalse(candidate["execution_performed"])
        self.assertEqual(candidate["jobs"][0]["recovery_action"], "reconcile_only")
        with WorkflowStore(self.state) as store:
            self.assertEqual(len(store.events(lease.job_id)), before_events)
            saved = store.get_effect(effect["op_key"])
            self.assertEqual((saved["status"], saved["attempts"]), ("uncertain", 1))

    def test_missing_status_does_not_create_state(self):
        self.assertEqual(cli.inspect_jobs(self.state)["status"], "not_initialized")
        self.assertFalse(self.state.exists())

    def test_cli_parsing_submit_status_resume_and_errors(self):
        args = ["submit", "--state-dir", str(self.state), "--source", str(self.source), "--evidence", str(self.evidence),
                "--scoring-result", str(self.scoring), "--score-gate", str(self.gate), "--trigger-context", str(self.context),
                "--app-id", "app_one", "--source-message-id", "om_source", "--question", "ljg-think", str(self.question)]
        args.extend(["--feishu-profile", "app_one"])
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli.main(args), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "accepted")
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli.main(["--state-dir", str(self.state), "status", "--job-id", result["job_id"]]), 0)
        self.assertEqual(len(json.loads(output.getvalue())["jobs"]), 1)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
