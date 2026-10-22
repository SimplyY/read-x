#!/usr/bin/env python3
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from long_read_workflow_retention import (
    CONTENT_RETENTION_SECONDS, STATISTICS_RETENTION_SECONDS, _FAILURE_STAGES, _REASON_CODES,
    apply_retention, assert_report_window_retained,
)
from long_read_workflow_store import StoreConflict, WorkflowStore


class Clock:
    def __init__(self, value: float = 1_800_000_000.0):
        self.value = value

    def __call__(self):
        return self.value


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "private-state"
        self.clock = Clock()
        self.store = WorkflowStore(self.root, clock=self.clock)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_reason_and_stage_allowlists_cover_producer_literals(self):
        produced = {"reason_code": set(), "failure_stage": set()}

        def record(key, node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                produced[key].add(node.value)

        scripts = Path(__file__).parent
        sources = sorted(scripts.glob("long_read_*.py")) + sorted(scripts.glob("feishu_long_read_*.py"))
        for source in sources:
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                if isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        if isinstance(key, ast.Constant) and key.value in produced:
                            record(key.value, value)
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "get" and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and node.args[0].value in produced and len(node.args) > 1):
                    record(node.args[0].value, node.args[1])
                if not isinstance(node, ast.Call):
                    continue
                name = (node.func.id if isinstance(node.func, ast.Name)
                        else node.func.attr if isinstance(node.func, ast.Attribute) else "")
                for keyword in node.keywords:
                    if keyword.arg in produced:
                        record(keyword.arg, keyword.value)
                if name == "_mark_failed_if_open" and len(node.args) >= 5:
                    record("reason_code", node.args[3])
                    record("failure_stage", node.args[4])
                if name == "_mark_uncertain":
                    for keyword in node.keywords:
                        if keyword.arg == "stage":
                            record("failure_stage", keyword.value)

        self.assertEqual(sorted(produced["reason_code"] - _REASON_CODES), [])
        self.assertEqual(sorted(produced["failure_stage"] - _FAILURE_STAGES), [])

    def submit(self, *, status: str, payload: dict | None = None, source_id: str = "om_source") -> dict:
        value = {"route": "long_read", "trigger": {"chatId": "oc_private", "sourceMessageId": source_id},
                 "snapshot_sha256": hashlib.sha256(source_id.encode()).hexdigest(), "article": "private text"}
        if payload:
            value.update(payload)
        job = self.store.submit(value, app_id="app-private", source_message_id=source_id,
                                artifacts=[{"key": "main-document", "kind": "document"}])
        lease = self.store.acquire_lease(job["job_id"], "retention-test")
        if status == "failed":
            self.store.set_status(lease, "failed")
        elif status == "succeeded":
            raise AssertionError("success fixture needs verified effects")
        self.store.release_lease(lease)
        return job

    def write_content(self, job: dict):
        payload = job["payload"]
        digest = payload["snapshot_sha256"]
        job_dir = self.root / "jobs" / job["job_id"]
        snapshot_dir = self.root / "artifacts" / digest
        job_dir.mkdir(parents=True)
        snapshot_dir.mkdir(parents=True)
        (job_dir / "analysis.md").write_text("private analysis", encoding="utf-8")
        (snapshot_dir / "source.md").write_text("private article", encoding="utf-8")
        return job_dir, snapshot_dir

    def test_terminal_content_is_removed_after_30_days_and_stats_are_scrubbed(self):
        job = self.submit(status="pending")
        job_dir, snapshot_dir = self.write_content(job)
        lease = self.store.acquire_lease(job["job_id"], "effect-fixture")
        effect = self.store.prepare_effect(lease, "main-document", "document-publish", "a" * 64)
        effect = self.store.begin_effect(lease, effect["op_key"])
        self.store.finish_effect(lease, effect["op_key"], "failed", remote_id="doc_private",
                                 metadata={"reason_code": "api-rejected", "failure_stage": "document-publish",
                                           "content_sha256": "b" * 64, "duration_ms": 14})
        self.store.set_status(lease, "failed")
        self.store.release_lease(lease)
        now = self.clock.value + CONTENT_RETENTION_SECONDS + 1

        result = apply_retention(self.root, now=now)

        self.assertFalse(job_dir.exists())
        self.assertFalse(snapshot_dir.exists())
        self.assertEqual(result["content_jobs_scrubbed"], 1)
        retained = self.store.get_job(job["job_id"])
        self.assertEqual(retained["app_id"], "retained")
        self.assertIsNone(retained["source_message_id"])
        self.assertEqual(retained["payload"]["schema"], "retained-long-read-statistics/v1")
        self.assertNotIn("private text", json.dumps(retained["payload"]))
        effect_after = self.store.get_effect(effect["op_key"])
        self.assertIsNone(effect_after["remote_id"])
        self.assertNotEqual(effect_after["request_sha256"], "a" * 64)
        self.assertNotIn("doc_private", json.dumps(effect_after))
        events = self.store.events(job["job_id"])
        self.assertTrue(events)
        self.assertNotIn("input_sha256", json.dumps(events))
        self.assertNotIn("op_key", json.dumps(events))
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE job_id=?", (job["job_id"],)
        ).fetchone()[0], 1)

    def test_unresolved_job_and_its_content_are_never_pruned(self):
        job = self.submit(status="pending")
        job_dir, snapshot_dir = self.write_content(job)

        result = apply_retention(self.root, now=self.clock.value + STATISTICS_RETENTION_SECONDS + 1)

        self.assertEqual(result["unresolved_jobs_preserved"], 1)
        self.assertTrue(job_dir.exists())
        self.assertTrue(snapshot_dir.exists())
        self.assertEqual(self.store.get_job(job["job_id"])["payload"]["article"], "private text")

    def test_terminal_job_with_prepared_effect_is_preserved_until_processed(self):
        job = self.submit(status="pending")
        job_dir, snapshot_dir = self.write_content(job)
        lease = self.store.acquire_lease(job["job_id"], "pending-effect")
        self.store.prepare_effect(lease, "main-document", "document-publish", "a" * 64)
        self.store.set_status(lease, "failed")
        self.store.release_lease(lease)

        result = apply_retention(self.root, now=self.clock.value + STATISTICS_RETENTION_SECONDS + 1)

        self.assertEqual(result["unresolved_jobs_preserved"], 1)
        self.assertEqual(result["content_jobs_scrubbed"], 0)
        self.assertEqual(result["statistics_jobs_removed"], 0)
        self.assertTrue(job_dir.exists())
        self.assertTrue(snapshot_dir.exists())
        self.assertIsNotNone(self.store.get_job(job["job_id"]))

    def test_terminal_statistics_expire_after_180_days(self):
        job = self.submit(status="failed")
        synthetic = self.submit(status="failed", payload={"synthetic": True}, source_id="om_synthetic")
        test_task = self.submit(status="failed", payload={"test_task": True}, source_id="om_test_task")
        self.write_content(job)

        result = apply_retention(self.root, now=self.clock.value + STATISTICS_RETENTION_SECONDS + 1)

        self.assertEqual(result["content_jobs_scrubbed"], 3)
        self.assertEqual(result["statistics_jobs_removed"], 3)
        self.assertIsNone(self.store.get_job(job["job_id"]))
        self.assertIsNone(self.store.get_job(synthetic["job_id"]))
        self.assertIsNone(self.store.get_job(test_task["job_id"]))
        self.assertEqual(self.store.events(job["job_id"]), [])
        archived = json.loads(self.store.connection.execute(
            "SELECT value FROM workflow_meta WHERE key='lifetime_archived_counts'"
        ).fetchone()[0])
        self.assertEqual(archived, {"F": 1, "N": 1, "S": 0})

    def test_orphan_and_abandoned_incoming_snapshots_are_removed(self):
        snapshot_root = self.root / "artifacts"
        orphan = snapshot_root / hashlib.sha256(b"orphan").hexdigest()
        incoming = snapshot_root / ".incoming-crashed-submit"
        orphan.mkdir(parents=True)
        incoming.mkdir()
        (orphan / "source.md").write_text("private rejected article", encoding="utf-8")
        (incoming / "source.md").write_text("private interrupted article", encoding="utf-8")

        result = apply_retention(self.root, now=self.clock.value)

        self.assertEqual(result["snapshots_removed"], 2)
        self.assertFalse(orphan.exists())
        self.assertFalse(incoming.exists())

    def test_unknown_failure_metadata_values_are_redacted(self):
        job = self.submit(status="pending")
        self.write_content(job)
        private_value = "private-article-title"
        lease = self.store.acquire_lease(job["job_id"], "unknown-reason")
        effect = self.store.prepare_effect(lease, "main-document", "document-publish", "a" * 64)
        effect = self.store.begin_effect(lease, effect["op_key"])
        metadata = {"reason_code": private_value, "failure_stage": "content-assembly",
                    "verification": private_value, "code": private_value, "duration_ms": 12}
        self.store.finish_effect(lease, effect["op_key"], "failed", metadata=metadata)
        artifact_metadata = {**metadata, "failure_stage": private_value}
        self.store.set_artifact(lease, "main-document", "failed", artifact_metadata)
        self.store._event(job["job_id"], "metadata_probe", {
            "reason_code": private_value, "failure_stage": "upstream-delivery", "metadata": metadata,
        })
        producer_reasons = {
            "document-notification-failed", "image-delivery-failed", "main-document-publish-failed",
            "main-document-result-unconfirmed", "main-document-unavailable",
            "required-content-branch-failed", "unsupported-image-state",
        }
        producer_stages = {"image-delivery"}
        for reason in producer_reasons:
            self.store._event(job["job_id"], "producer_reason_probe", {"reason_code": reason})
        for stage in producer_stages:
            self.store._event(job["job_id"], "producer_stage_probe", {"failure_stage": stage})
        self.store.set_status(lease, "failed")
        self.store.release_lease(lease)

        apply_retention(self.root, now=self.clock.value + CONTENT_RETENTION_SECONDS + 1)

        sanitized_effect = self.store.get_effect(effect["op_key"])["metadata"]
        sanitized_artifact = next(item for item in self.store.artifacts(job["job_id"])
                                  if item["artifact_key"] == "main-document")["metadata"]
        events_json = json.dumps(self.store.events(job["job_id"]))
        self.assertNotIn(private_value, json.dumps(sanitized_effect))
        self.assertNotIn(private_value, json.dumps(sanitized_artifact))
        self.assertNotIn(private_value, events_json)
        self.assertEqual(sanitized_effect["reason_code"], "unknown")
        self.assertEqual(sanitized_effect["failure_stage"], "content-assembly")
        self.assertEqual(sanitized_effect["verification"], "unknown")
        self.assertEqual(sanitized_effect["code"], "unknown")
        self.assertEqual(sanitized_artifact["reason_code"], "unknown")
        self.assertEqual(sanitized_artifact["failure_stage"], "unknown")
        self.assertEqual(sanitized_artifact["verification"], "unknown")
        self.assertEqual(sanitized_artifact["code"], "unknown")
        probe = next(event for event in self.store.events(job["job_id"])
                     if event["event_type"] == "metadata_probe")
        self.assertEqual(probe["details"]["failure_stage"], "upstream-delivery")
        retained_reasons = {
            event["details"].get("reason_code") for event in self.store.events(job["job_id"])
            if event["event_type"] == "producer_reason_probe"
        }
        retained_stages = {
            event["details"].get("failure_stage") for event in self.store.events(job["job_id"])
            if event["event_type"] == "producer_stage_probe"
        }
        self.assertEqual(retained_reasons, producer_reasons)
        self.assertEqual(retained_stages, producer_stages)

    def test_active_job_protects_a_shared_snapshot_until_its_own_terminal_retention(self):
        first = self.submit(status="failed", source_id="om_first")
        second = self.submit(status="pending", source_id="om_second")
        shared_digest = first["payload"]["snapshot_sha256"]
        second_payload = second["payload"]
        second_payload["snapshot_sha256"] = shared_digest
        self.store.connection.execute("UPDATE jobs SET payload_json=? WHERE job_id=?",
                                      (json.dumps(second_payload), second["job_id"]))
        job_dir, snapshot_dir = self.write_content(first)
        now = self.clock.value + CONTENT_RETENTION_SECONDS + 1

        apply_retention(self.root, now=now)

        self.assertFalse(job_dir.exists())
        self.assertTrue(snapshot_dir.exists())
        second_lease = self.store.acquire_lease(second["job_id"], "finish-fixture")
        self.store.set_status(second_lease, "failed")
        self.store.release_lease(second_lease)
        self.clock.value += CONTENT_RETENTION_SECONDS + 1
        apply_retention(self.root, now=self.clock.value)
        self.assertFalse(snapshot_dir.exists())

    def test_shared_snapshot_is_removed_when_all_old_terminal_jobs_expire_together(self):
        first = self.submit(status="failed", source_id="om_old_first")
        second = self.submit(status="failed", source_id="om_old_second")
        digest = first["payload"]["snapshot_sha256"]
        second_payload = second["payload"]
        second_payload["snapshot_sha256"] = digest
        self.store.connection.execute("UPDATE jobs SET payload_json=? WHERE job_id=?",
                                      (json.dumps(second_payload), second["job_id"]))
        _, snapshot_dir = self.write_content(first)

        apply_retention(self.root, now=self.clock.value + STATISTICS_RETENTION_SECONDS + 1)

        self.assertFalse(snapshot_dir.exists())
        self.assertIsNone(self.store.get_job(first["job_id"]))
        self.assertIsNone(self.store.get_job(second["job_id"]))

    def test_report_retention_floor_is_monotonic_and_guards_expired_windows(self):
        reports_dir = self.root / "reports"
        reports_dir.mkdir()
        attachment = reports_dir / "monthly-old.md"
        attachment.write_text("private report body", encoding="utf-8")
        pending_attachment = reports_dir / "monthly-pending.md"
        pending_attachment.write_text("pending report body", encoding="utf-8")
        self.store.connection.execute(
            "INSERT INTO reports(report_key,period_type,window_start,window_end,snapshot_json,snapshot_sha256,markdown,markdown_sha256,status,created_at) "
            "VALUES('monthly:old','monthly',1,2,'{}','a','report','b','delivered',?)",
            (self.clock.value - STATISTICS_RETENTION_SECONDS - 10,),
        )
        self.store.connection.execute(
            "INSERT INTO reports(report_key,period_type,window_start,window_end,snapshot_json,snapshot_sha256,markdown,markdown_sha256,status,created_at) "
            "VALUES('monthly:pending','monthly',1,2,'{}','c','pending','d','pending_delivery',?)",
            (self.clock.value - STATISTICS_RETENTION_SECONDS - 10,),
        )
        floor = self.clock.value - STATISTICS_RETENTION_SECONDS
        apply_retention(self.root, now=self.clock.value)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0], 1)
        self.assertFalse(attachment.exists())
        self.assertTrue(pending_attachment.exists())
        self.assertGreaterEqual(float(self.store.connection.execute(
            "SELECT value FROM workflow_meta WHERE key='report_retention_floor'"
        ).fetchone()[0]), floor)
        with self.assertRaises(ValueError):
            assert_report_window_retained(self.store, floor - 1)

    def test_event_log_remains_append_only_outside_retention_transaction(self):
        job = self.submit(status="pending")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute("DELETE FROM events WHERE job_id=?", (job["job_id"],))


if __name__ == "__main__":
    unittest.main()
