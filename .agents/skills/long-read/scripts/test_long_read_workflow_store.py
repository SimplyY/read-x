#!/usr/bin/env python3
"""Disk-backed recovery/concurrency checks; no Bridge or Feishu calls."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import uuid

from long_read_workflow_store import LeaseConflict, RetryExhausted, StoreConflict, UncertainEffect, WorkflowStore


DIGEST = hashlib.sha256(b"image-input").hexdigest()
SPECS = [{"key": "main", "kind": "document"}, {"key": "core", "kind": "image"}]


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "private-state"
        self.clock = Clock()
        self.store = WorkflowStore(self.root, clock=self.clock)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def submit(self, **kwargs):
        return self.store.submit({"source_sha256": DIGEST, "private": "article text"}, app_id="app-one", artifacts=SPECS, **kwargs)

    def effect(self, lease, action="insert"):
        return self.store.prepare_effect(lease, "core", action, DIGEST)

    def test_reliability_events_are_append_only_supported_and_field_validated(self):
        job = self.submit(source_message_id="om_repair")
        repaired = self.store.record_reliability_event(job["job_id"], "repair_applied", {
            "repair_id": "fix-42", "failure_fingerprint": "bridge:response-unstable",
            "summary": "保留原 Bridge 会话以便只读恢复", "change_ref": "abc1234",
        })
        repaired_again = self.store.record_reliability_event(job["job_id"], "repair_applied", {
            "repair_id": "fix-42", "failure_fingerprint": "bridge:response-unstable",
            "summary": "保留原 Bridge 会话以便只读恢复", "change_ref": "abc1234",
        })
        comparison = self.store.record_reliability_event(job["job_id"], "repair_comparison", {
            "repair_id": "fix-42", "metric": "final_failure_rate",
            "before_value": 0.2, "after_value": 0.05,
            "before_sample_size": 20, "after_sample_size": 20,
            "before_window": "2026-09", "after_window": "2026-10",
        })
        self.assertEqual(repaired["status"], "recorded")
        self.assertTrue(repaired_again["reused"])
        self.assertEqual(repaired_again["seq"], repaired["seq"])
        self.assertGreater(comparison["seq"], repaired["seq"])
        with self.assertRaises(ValueError):
            self.store.record_reliability_event(job["job_id"], "repair_applied", {
                "repair_id": "fix-42", "failure_fingerprint": "bridge:unstable",
                "summary": "no raw data", "change_ref": "abc1234", "article_text": "private",
            })
        with self.assertRaises(ValueError):
            self.store.record_reliability_event(job["job_id"], "repair_comparison", {
                "repair_id": "fix-42", "metric": "final_failure_rate",
                "before_value": 1.2, "after_value": 0.1,
                "before_sample_size": 20, "after_sample_size": 20,
                "before_window": "2026-09", "after_window": "2026-10",
            })
        with self.assertRaises(StoreConflict):
            self.store.record_reliability_event("missing-job", "manual_intervention", {
                "category": "reconciliation", "summary": "核验"})

    def test_repair_comparison_requires_real_ordered_windows_and_nonzero_samples(self):
        job = self.submit(source_message_id="om_repair_windows")
        base = {
            "repair_id": "fix-42", "metric": "final_failure_rate",
            "before_value": 0.2, "after_value": 0.05,
            "before_sample_size": 20, "after_sample_size": 20,
            "before_window": "2026-09", "after_window": "2026-10",
        }
        invalid_cases = [
            {"before_window": "2026-99"},
            {"before_window": "2026-02-29"},
            {"before_window": "2026-12-01..2026-10-01"},
            {"before_window": "2026-10", "after_window": "2026-09"},
            {"before_sample_size": 0},
            {"after_sample_size": 0},
        ]
        for override in invalid_cases:
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.store.record_reliability_event(
                    job["job_id"], "repair_comparison", {**base, **override},
                )

    def test_optional_message_artifact_backfill_is_lease_guarded_and_idempotent(self):
        job = self.submit(source_message_id="om_optional_backfill")
        lease = self.store.acquire_lease(job["job_id"], "backfill")
        self.assertTrue(self.store.ensure_optional_artifact(
            lease, "content-review-notification", "message",
        ))
        version_after_insert = lease.version
        self.assertFalse(self.store.ensure_optional_artifact(
            lease, "content-review-notification", "message",
        ))
        self.assertEqual(lease.version, version_after_insert)
        item = next(item for item in self.store.artifacts(job["job_id"])
                    if item["artifact_key"] == "content-review-notification")
        self.assertEqual((item["kind"], item["required"], item["status"]), ("message", 0, "pending"))
        self.assertEqual(sum(event["event_type"] == "optional_artifact_added"
                             for event in self.store.events(job["job_id"])), 1)

    def test_failed_effect_can_be_confirmed_only_by_exact_readback(self):
        job = self.submit(source_message_id="om_failed_readback")
        self.store.connection.execute(
            "INSERT INTO artifacts(job_id,artifact_key,kind,required) VALUES(?,?,?,0)",
            (job["job_id"], "notice", "message"),
        )
        lease = self.store.acquire_lease(job["job_id"], "failed-readback")
        effect = self.store.prepare_effect(lease, "notice", "message-send", DIGEST)
        self.store.begin_effect(lease, effect["op_key"])
        self.store.finish_effect(lease, effect["op_key"], "failed", metadata={
            "failure_stage": "notification-send", "reason_code": "api-rejected",
        })
        with self.assertRaises(StoreConflict):
            self.store.reconcile_effect(
                lease, effect["op_key"], "confirmed", remote_id="om_found",
                metadata={"verification": "some-readback"},
            )
        self.store.reconcile_effect(
            lease, effect["op_key"], "confirmed", remote_id="om_found",
            metadata={"verification": "exact-card-readback"},
        )
        self.assertEqual(self.store.get_effect(effect["op_key"])["status"], "confirmed")

    def test_permissions_pragmas_and_reopen(self):
        job = self.submit(source_message_id="om_source")
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        for suffix in ("", "-wal", "-shm"):
            self.assertEqual(Path(str(self.store.db_path) + suffix).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.store.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        self.store.close()
        self.store = WorkflowStore(self.root, clock=self.clock)
        self.assertEqual(self.store.get_job(job["job_id"])["payload"]["private"], "article text")
        self.assertEqual(len(self.store.artifacts(job["job_id"])), 2)
        self.assertNotIn("article text", json.dumps(self.store.events(job["job_id"])))
        self.assertNotIn("payload", self.store.list_jobs()[0])

    def test_submit_idempotency_and_conflict(self):
        first = self.submit(source_message_id="om_source")
        again = self.submit(source_message_id="om_source")
        self.assertTrue(first["created"])
        self.assertFalse(again["created"])
        self.assertEqual(first["job_id"], again["job_id"])
        with self.assertRaises(StoreConflict):
            self.store.submit({"different": True}, app_id="app-one", source_message_id="om_source", artifacts=SPECS)
        with self.assertRaises(StoreConflict):
            self.store.submit(first["payload"], app_id="app-one", source_message_id="om_source", artifacts=[])
        other_app = self.store.submit(first["payload"], app_id="app-two", source_message_id="om_source", artifacts=SPECS)
        self.assertNotEqual(first["job_id"], other_app["job_id"])
        self.assertNotEqual(self.submit()["job_id"], self.submit()["job_id"])
        self.assertEqual(len(self.store.events(first["job_id"])), 1)

    def test_corrupt_payload_is_read_as_unadmitted_without_decoding_crash(self):
        job = self.submit()
        self.store.connection.execute("UPDATE jobs SET payload_json='{' WHERE job_id=?", (job["job_id"],))
        decoded = self.store.get_job(job["job_id"])
        self.assertIsNone(decoded["payload"])
        self.assertEqual(decoded["payload_decode_error"], "invalid-json")
        self.assertEqual(self.store.unadmitted_nonterminal_jobs(), [{
            "job_id": job["job_id"], "status": "pending", "reason_code": "invalid-workflow-payload",
        }])

        self.store.connection.execute("UPDATE jobs SET payload_json=CAST(x'ff' AS TEXT) WHERE job_id=?", (job["job_id"],))
        decoded = self.store.get_job(job["job_id"])
        self.assertIsNone(decoded["payload"])
        self.assertEqual(decoded["payload_decode_error"], "invalid-json")
        self.assertEqual(self.store.unadmitted_nonterminal_jobs(), [{
            "job_id": job["job_id"], "status": "pending", "reason_code": "invalid-workflow-payload",
        }])

    def test_concurrent_submissions_share_one_job(self):
        barrier = threading.Barrier(4)

        def worker(_):
            with WorkflowStore(self.root) as store:
                barrier.wait(timeout=10)
                return store.submit({"article_hash": DIGEST}, app_id="app-one", source_message_id="om_same", artifacts=SPECS)

        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(worker, range(4)))
        self.assertEqual(len({row["job_id"] for row in rows}), 1)
        self.assertEqual(sum(row["created"] for row in rows), 1)

    def test_concurrent_lease_has_one_winner(self):
        job = self.submit()
        barrier = threading.Barrier(4)

        def worker(index):
            with WorkflowStore(self.root, clock=self.clock) as store:
                barrier.wait(timeout=10)
                try:
                    return store.acquire_lease(job["job_id"], f"worker-{index}")
                except LeaseConflict:
                    return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            winners = [row for row in pool.map(worker, range(4)) if row]
        self.assertEqual(len(winners), 1)

    def test_stale_version_token_owner_and_expiry_are_fenced(self):
        lease = self.store.acquire_lease(self.submit()["job_id"], "worker", 10)
        old = replace(lease)
        self.store.set_status(lease, "running")
        for invalid in (old, replace(lease, token="different"), replace(lease, owner="other"), replace(lease, fencing=0)):
            with self.assertRaises(LeaseConflict):
                self.store.set_status(invalid, "waiting")
        self.clock.value += 11
        with self.assertRaises(LeaseConflict):
            self.store.renew_lease(lease)
        next_lease = self.store.acquire_lease(lease.job_id, "new-worker")
        self.assertGreater(next_lease.fencing, lease.fencing)
        with self.assertRaises(LeaseConflict):
            self.store.set_artifact(lease, "core", "confirmed")

    def test_lease_renew_and_release(self):
        lease = self.store.acquire_lease(self.submit()["job_id"], "worker", 10)
        self.clock.value += 5
        self.store.renew_lease(lease, 20)
        self.assertEqual(lease.expires_at, 1025)
        self.store.release_lease(lease)
        with self.assertRaises(LeaseConflict):
            self.store.set_status(lease, "running")
        self.assertGreater(self.store.acquire_lease(lease.job_id, "other").fencing, lease.fencing)

    def test_attempt_is_committed_before_external_call(self):
        lease = self.store.acquire_lease(self.submit()["job_id"], "worker")
        effect = self.effect(lease)
        self.assertEqual(str(uuid.UUID(effect["request_id"])), effect["request_id"])
        self.assertTrue(self.store.begin_effect(lease, effect["op_key"])["execute"])
        with WorkflowStore(self.root, clock=self.clock) as second:
            persisted = second.get_effect(effect["op_key"])
            self.assertEqual(persisted["status"], "attempting")
            self.assertEqual(persisted["request_id"], effect["request_id"])
        with self.assertRaises(UncertainEffect):
            self.store.begin_effect(lease, effect["op_key"])

    def test_effect_duration_is_persisted_from_attempt_to_verified_result(self):
        lease = self.store.acquire_lease(self.submit()["job_id"], "worker")
        effect = self.effect(lease)
        self.store.begin_effect(lease, effect["op_key"])
        self.clock.value += 0.125
        self.store.finish_effect(lease, effect["op_key"], "confirmed", remote_id="remote-id")
        persisted = self.store.get_effect(effect["op_key"])
        self.assertEqual(persisted["metadata"]["duration_ms"], 125)

    def test_preallocated_request_id_is_reused_and_retry_state_is_round_ordered(self):
        lease = self.store.acquire_lease(self.submit()["job_id"], "worker")
        request_id = "c4e8af13-e0bb-469e-a1a6-a1f5fc98d520"
        first = self.store.prepare_effect(lease, "core", "image-generate", DIGEST,
                                         request_id=request_id)
        self.assertEqual(first["request_id"], request_id)
        self.assertEqual(self.store.prepare_effect(lease, "core", "image-generate", DIGEST,
                                                   request_id=request_id)["request_id"], request_id)
        self.store.begin_effect(lease, first["op_key"])
        self.store.finish_effect(lease, first["op_key"], "failed")
        self.store.consume_image_retry(lease, "core")
        retry = self.store.prepare_effect(lease, "core", "image-generate:retry-1", DIGEST)
        self.store.begin_effect(lease, retry["op_key"])
        self.store.finish_effect(lease, retry["op_key"], "confirmed", remote_id=request_id)
        latest = self.store.latest_effect_for_action(lease.job_id, "core", "image-generate")
        self.assertEqual(latest["action"], "image-generate:retry-1")
        self.store.set_artifact(lease, "core", "confirmed")

    def test_process_loss_reopen_marks_uncertain_without_new_operation(self):
        lease = self.store.acquire_lease(self.submit()["job_id"], "worker", 10)
        effect = self.effect(lease)
        self.store.begin_effect(lease, effect["op_key"])
        self.store.close()  # Simulate loss after remote insert but before receipt.
        self.clock.value += 11
        self.store = WorkflowStore(self.root, clock=self.clock)
        recovered = self.store.acquire_lease(lease.job_id, "recovery")
        same = self.effect(recovered)
        self.assertEqual(same["op_key"], effect["op_key"])
        self.assertEqual(same["status"], "uncertain")
        self.assertEqual(same["attempts"], 1)
        self.assertEqual(self.store.get_job(lease.job_id)["status"], "needs_review")
        with self.assertRaises(UncertainEffect):
            self.store.begin_effect(recovered, same["op_key"])
        with self.assertRaises(StoreConflict):
            self.store.finish_effect(lease, same["op_key"], "confirmed", remote_id="block-id")
        self.store.reconcile_effect(recovered, same["op_key"], "confirmed", remote_id="block-id")
        self.assertFalse(self.store.begin_effect(recovered, same["op_key"])["execute"])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM effects").fetchone()[0], 1)

    def test_response_loss_is_explicitly_uncertain(self):
        lease = self.store.acquire_lease(self.submit()["job_id"], "worker")
        effect = self.effect(lease, "send")
        self.store.begin_effect(lease, effect["op_key"])
        self.store.finish_effect(lease, effect["op_key"], "uncertain", metadata={"reason_code": "response-lost"})
        with self.assertRaises(StoreConflict):
            self.store.set_status(lease, "failed")
        with self.assertRaises(UncertainEffect):
            self.store.begin_effect(lease, effect["op_key"])
        with self.assertRaises(StoreConflict):
            self.store.consume_image_retry(lease, "core")
        with self.assertRaises(StoreConflict):
            self.store.prepare_effect(lease, "core", "send", "0" * 64)
        self.store.reconcile_effect(lease, effect["op_key"], "confirmed", remote_id="om_image")
        self.assertEqual(self.store.get_effect(effect["op_key"])["remote_id"], "om_image")
        self.assertEqual(next(item for item in self.store.artifacts(lease.job_id) if item["artifact_key"] == "core")["status"], "pending")

    def test_retry_budget_is_shared_by_image_stages_and_survives_reopen(self):
        lease = self.store.acquire_lease(self.submit()["job_id"], "worker")
        generate = self.effect(lease, "generate")
        self.store.begin_effect(lease, generate["op_key"])
        self.store.finish_effect(lease, generate["op_key"], "failed")
        with self.assertRaises(StoreConflict):
            self.store.begin_effect(lease, generate["op_key"])
        self.assertEqual(self.store.consume_image_retry(lease, "core"), 1)
        self.store.begin_effect(lease, generate["op_key"])
        self.store.finish_effect(lease, generate["op_key"], "confirmed")
        insert = self.effect(lease, "insert")
        self.store.begin_effect(lease, insert["op_key"])
        self.store.finish_effect(lease, insert["op_key"], "failed")
        self.assertEqual(self.store.consume_image_retry(lease, "core"), 2)
        self.store.begin_effect(lease, insert["op_key"])
        self.store.finish_effect(lease, insert["op_key"], "confirmed")
        send = self.effect(lease, "send")
        self.store.begin_effect(lease, send["op_key"])
        self.store.finish_effect(lease, send["op_key"], "failed")
        self.store.close()
        self.store = WorkflowStore(self.root, clock=self.clock)
        with self.assertRaises(RetryExhausted):
            self.store.consume_image_retry(lease, "core")
        with self.assertRaises(StoreConflict):
            self.store.begin_effect(lease, send["op_key"])
        self.assertEqual(next(item for item in self.store.artifacts(lease.job_id) if item["artifact_key"] == "core")["retry_count"], 2)

    def test_completion_gate_and_terminal_lease(self):
        lease = self.store.acquire_lease(self.submit()["job_id"], "worker")
        with self.assertRaises(StoreConflict):
            self.store.set_status(lease, "succeeded")
        with self.assertRaises(StoreConflict):
            self.store.set_artifact(lease, "main", "confirmed")
        for artifact_key, action in (("main", "document-publish"), ("core", "image-generate")):
            effect = self.store.prepare_effect(lease, artifact_key, action, DIGEST)
            self.store.begin_effect(lease, effect["op_key"])
            self.store.finish_effect(lease, effect["op_key"], "confirmed", remote_id=f"remote-{artifact_key}")
        with self.assertRaises(StoreConflict):
            self.store.set_status(lease, "succeeded")
        self.store.finalize_artifact_plan(lease, {"main", "core"})
        self.store.set_artifact(lease, "main", "confirmed")
        self.store.set_artifact(lease, "core", "confirmed")
        self.store.set_status(lease, "succeeded")
        with self.assertRaises(StoreConflict):
            self.store.set_status(lease, "running")
        self.store.release_lease(lease)
        with self.assertRaises(LeaseConflict):
            self.store.acquire_lease(lease.job_id, "other")

    def test_conditional_artifact_plan_freezes_after_analysis_and_is_idempotent(self):
        job = self.submit()
        lease = self.store.acquire_lease(job["job_id"], "worker")
        self.store.finalize_artifact_plan(lease, {"main", "core"})
        saved = self.store.get_job(job["job_id"])
        self.assertTrue(saved["payload"]["artifact_plan_finalized"])
        self.assertEqual(saved["payload"]["required_artifacts"], ["core", "main"])
        version = saved["version"]
        self.store.finalize_artifact_plan(lease, {"main", "core"})
        self.assertEqual(self.store.get_job(job["job_id"])["version"], version)
        with self.assertRaises(StoreConflict):
            self.store.finalize_artifact_plan(lease, {"main"})

    def test_success_gate_requires_all_image_insert_effects(self):
        specs = [{"key": "core-insert", "kind": "image_insert"}]
        job = self.store.submit({"source_sha256": DIGEST}, app_id="app-two", artifacts=specs)
        lease = self.store.acquire_lease(job["job_id"], "worker")
        for action in ("image-block-create", "image-upload"):
            effect = self.store.prepare_effect(lease, "core-insert", action, DIGEST)
            self.store.begin_effect(lease, effect["op_key"])
            self.store.finish_effect(lease, effect["op_key"], "confirmed", remote_id=f"remote-{action}")
        with self.assertRaises(StoreConflict):
            self.store.set_artifact(lease, "core-insert", "confirmed")

    def test_unknown_effect_prevents_conditional_artifact_plan_freeze(self):
        job = self.submit()
        lease = self.store.acquire_lease(job["job_id"], "worker")
        effect = self.effect(lease, "send")
        self.store.begin_effect(lease, effect["op_key"])
        with self.assertRaises(UncertainEffect):
            self.store.finalize_artifact_plan(lease, {"main", "core"})

    def test_append_only_events_foreign_keys_and_metadata(self):
        lease = self.store.acquire_lease(self.submit()["job_id"], "worker")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute("UPDATE events SET event_type='changed'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute("DELETE FROM events")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.prepare_effect(lease, "missing", "send", DIGEST)
        with self.assertRaises(ValueError):
            self.store.set_artifact(lease, "core", "failed", {"raw_response": "private"})
        with self.assertRaises(ValueError):
            self.store.set_artifact(lease, "core", "failed", [])
        with self.assertRaises(ValueError):
            self.store.set_artifact(lease, "core", "failed", {"reason_code": "private article text"})
        self.assertEqual(self.store.get_job(lease.job_id)["version"], lease.version)

    def test_symlink_paths_and_invalid_inputs_fail_closed(self):
        linked = Path(self.temporary.name) / "linked"
        linked.symlink_to(self.root)
        with self.assertRaises(ValueError):
            WorkflowStore(linked)
        other = Path(self.temporary.name) / "other"
        other.mkdir()
        (other / "workflow.sqlite3").symlink_to(self.store.db_path)
        with self.assertRaises(ValueError):
            WorkflowStore(other)
        with self.assertRaises(ValueError):
            self.store.submit({"bad": float("nan")}, app_id="app")
        with self.assertRaises(ValueError):
            self.submit(source_message_id="")
        with self.assertRaises(ValueError):
            self.store.acquire_lease(self.submit()["job_id"], "worker", float("inf"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
