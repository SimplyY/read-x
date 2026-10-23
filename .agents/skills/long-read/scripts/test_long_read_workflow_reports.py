#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime
import hashlib
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from long_read_workflow_reports import create_report_snapshot, render_report_card, report_window
from long_read_workflow_store import StoreConflict, UncertainEffect, WorkflowStore


TZ = ZoneInfo("Asia/Shanghai")


def add_job(state: Path, *, status: str, created_at: float, source_id: str,
            synthetic: bool = False, test_task: bool = False,
            unverified_test_task: bool = False, retries: int = 0):
    timeline = {"now": created_at}
    with WorkflowStore(state, clock=lambda: timeline["now"]) as store:
        payload = {
            "route": "long_read", "synthetic": synthetic, "test_task": test_task,
            "trigger": {
                "feishuProfile": "bridge-profile", "larkCliProfile": "app", "appId": "app", "identity": "bot",
                "chatType": "group", "chatId": "oc_canary", "senderId": "ou_self",
                "senderType": "user", "sourceMessageId": source_id,
                **({"readxCanary": True} if test_task else {}),
            },
        }
        if test_task and not unverified_test_task:
            url = "https://example.com/canary"
            payload["canary_source_proof"] = {
                "schema": "readx-canary-source-proof/v1",
                "method": "lark-cli.im.messages-mget",
                "source_created_at": "2026-10-22 15:49",
                "app_id": "app", "profile": "app", "message_id": source_id,
                "chat_id": "oc_canary", "sender_id": "ou_self",
                "source_url_sha256": hashlib.sha256(url.encode()).hexdigest(),
                "source_content_sha256": hashlib.sha256(
                    f"【CANARY｜Read-X long_read 端到端验收】\n{url}".encode()
                ).hexdigest(),
            }
        job = store.submit(payload, app_id="app", source_message_id=source_id,
                           artifacts=[{"key": "delivery", "kind": "document", "required": True}])
        lease = store.acquire_lease(job["job_id"], "test", 60)
        if status == "succeeded":
            store.set_status(lease, "running")
            effect = store.prepare_effect(lease, "delivery", "document-publish", "a" * 64)
            store.begin_effect(lease, effect["op_key"])
            store.finish_effect(lease, effect["op_key"], "confirmed")
            store.set_artifact(lease, "delivery", "confirmed")
            store.finalize_artifact_plan(lease, {"delivery"})
            store.set_status(lease, "succeeded")
        elif status in {"failed", "needs_review"}:
            store.set_status(lease, status)
        if retries:
            store.connection.execute("UPDATE artifacts SET retry_count=? WHERE job_id=?", (retries, job["job_id"]))
        store.release_lease(lease)
        return job["job_id"]


class ReportWindowTests(unittest.TestCase):
    def test_weekly_window_is_last_due_sunday_0900_and_seven_days(self):
        before = datetime(2026, 10, 18, 8, 59, tzinfo=TZ)
        key_before, start_before, end_before = report_window("weekly", now=before)
        self.assertEqual(key_before, "weekly:2026-10-11")
        self.assertEqual((end_before - start_before).total_seconds(), 7 * 86400)
        due = datetime(2026, 10, 18, 9, 0, tzinfo=TZ)
        key_due, start_due, end_due = report_window("weekly", now=due)
        self.assertEqual(key_due, "weekly:2026-10-18")
        self.assertEqual(start_due, end_before)
        self.assertEqual(end_due, due)

    def test_monthly_window_is_previous_calendar_month(self):
        key, start, end = report_window("monthly", now=datetime(2026, 10, 21, 12, tzinfo=TZ))
        self.assertEqual(key, "monthly:2026-09")
        self.assertEqual(start, datetime(2026, 9, 1, 0, tzinfo=TZ))
        self.assertEqual(end, datetime(2026, 10, 1, 0, tzinfo=TZ))


class ReportSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name) / "state"
        self.now = datetime(2026, 10, 20, 12, tzinfo=TZ)
        _, self.start, self.end = report_window("weekly", now=self.now)
        self.start_epoch = self.start.timestamp()
        with WorkflowStore(self.state, clock=lambda: self.start_epoch - 86400):
            pass

    def tearDown(self):
        self.temporary.cleanup()

    def test_weekly_suppresses_zero_and_exactly_ten_percent_but_keeps_all_in_denominator(self):
        for index in range(9):
            add_job(self.state, status="succeeded", created_at=self.start_epoch + 100 + index, source_id=f"ok-{index}")
        add_job(self.state, status="failed", created_at=self.start_epoch + 200, source_id="failed")
        result = create_report_snapshot(self.state, "weekly", now=self.now)
        self.assertEqual(result["status"], "suppressed_weekly")
        self.assertEqual(result["snapshot"]["counts"], {"N": 10, "S": 9, "F": 1, "U": 0, "processing": 0})
        self.assertFalse(result["snapshot"]["failure_rate_over_10_percent"])

    def test_weekly_sends_only_when_failure_share_is_strictly_above_ten_percent(self):
        for index in range(15):
            add_job(self.state, status="succeeded", created_at=self.start_epoch + 100 + index, source_id=f"ok-{index}")
        add_job(self.state, status="failed", created_at=self.start_epoch + 200, source_id="failed-1")
        add_job(self.state, status="failed", created_at=self.start_epoch + 300, source_id="failed-2")
        add_job(self.state, status="pending", created_at=self.start_epoch + 400, source_id="processing")
        add_job(self.state, status="needs_review", created_at=self.start_epoch + 450, source_id="unknown")
        add_job(self.state, status="succeeded", created_at=self.start_epoch + 500, source_id="synthetic", synthetic=True)
        result = create_report_snapshot(self.state, "weekly", now=self.now)
        self.assertEqual(result["status"], "pending_delivery")
        self.assertEqual(result["snapshot"]["counts"], {"N": 19, "S": 15, "F": 2, "U": 1, "processing": 1})
        self.assertAlmostEqual(result["snapshot"]["final_failure_rate"], 2 / 19)
        self.assertTrue(result["snapshot"]["failure_rate_over_10_percent"])

    def test_monthly_report_is_saved_even_when_no_jobs_exist(self):
        result = create_report_snapshot(self.state, "monthly", now=datetime(2026, 10, 21, 12, tzinfo=TZ))
        self.assertEqual(result["status"], "pending_delivery")
        self.assertEqual(result["snapshot"]["counts"], {"N": 0, "S": 0, "F": 0, "U": 0, "processing": 0})
        self.assertIsNone(result["snapshot"]["final_failure_rate"])
        self.assertIn("本统计月无有效任务", result["markdown"])

    def test_long_term_target_uses_cumulative_real_tasks_not_one_report_window(self):
        with WorkflowStore(self.state) as store:
            store.connection.execute(
                "UPDATE workflow_meta SET value=? WHERE key='tracking_started_at'",
                (str(self.start_epoch - 86400),),
            )
        for index in range(49):
            add_job(self.state, status="succeeded", created_at=self.start_epoch - 5000 + index,
                    source_id=f"old-ok-{index}")
        add_job(self.state, status="failed", created_at=self.start_epoch - 4000,
                source_id="old-failed")
        result = create_report_snapshot(self.state, "weekly", now=self.now)
        snapshot = result["snapshot"]
        self.assertEqual(snapshot["counts"]["N"], 0)
        self.assertEqual(snapshot["long_term_counts"], {"N": 50, "S": 49, "F": 1, "U": 0, "processing": 0})
        self.assertAlmostEqual(snapshot["long_term_final_failure_rate"], 0.02)
        self.assertTrue(snapshot["long_term_target_eligible"])
        self.assertTrue(snapshot["long_term_target_met"])
        self.assertIn("长期累计", result["markdown"])

    def test_period_snapshot_is_immutable_and_reused_on_recovery(self):
        add_job(self.state, status="failed", created_at=self.start_epoch + 100, source_id="failed")
        first = create_report_snapshot(self.state, "weekly", now=self.now)
        add_job(self.state, status="failed", created_at=self.start_epoch + 101, source_id="late-but-in-window")
        second = create_report_snapshot(self.state, "weekly", now=self.now)
        self.assertTrue(second["reused"])
        self.assertEqual(second["snapshot_sha256"], first["snapshot_sha256"])
        self.assertEqual(second["snapshot"]["counts"]["F"], 1)

    def test_late_completion_is_not_counted_before_the_fixed_report_watermark(self):
        timeline = {"now": self.start_epoch + 100}
        with WorkflowStore(self.state, clock=lambda: timeline["now"]) as store:
            job = store.submit({"route": "long_read"}, app_id="app", source_message_id="late-completion",
                               artifacts=[{"key": "delivery", "kind": "document", "required": True}])
            timeline["now"] = self.end.timestamp() + 60
            lease = store.acquire_lease(job["job_id"], "test", 60)
            store.set_status(lease, "running")
            effect = store.prepare_effect(lease, "delivery", "document-publish", "a" * 64)
            store.begin_effect(lease, effect["op_key"])
            store.finish_effect(lease, effect["op_key"], "confirmed")
            store.set_artifact(lease, "delivery", "confirmed")
            store.finalize_artifact_plan(lease, {"delivery"})
            store.set_status(lease, "succeeded")
            store.release_lease(lease)
        result = create_report_snapshot(self.state, "weekly", now=self.now)
        self.assertEqual(result["snapshot"]["counts"], {"N": 1, "S": 0, "F": 0, "U": 0, "processing": 1})
        self.assertEqual(result["snapshot"]["duration_seconds"]["completed_samples"], 0)

    def test_late_resolution_enters_later_cumulative_metric_without_rewriting_old_snapshot(self):
        timeline = {"now": self.start_epoch + 100}
        with WorkflowStore(self.state, clock=lambda: timeline["now"]) as store:
            job = store.submit({"route": "long_read"}, app_id="app", source_message_id="late-cumulative",
                               artifacts=[{"key": "delivery", "kind": "document", "required": True}])
        first = create_report_snapshot(self.state, "weekly", now=self.now)
        self.assertEqual(first["snapshot"]["counts"]["processing"], 1)
        timeline["now"] = self.end.timestamp() + 60
        with WorkflowStore(self.state, clock=lambda: timeline["now"]) as store:
            lease = store.acquire_lease(job["job_id"], "test", 60)
            store.set_status(lease, "running")
            effect = store.prepare_effect(lease, "delivery", "document-publish", "b" * 64)
            store.begin_effect(lease, effect["op_key"])
            store.finish_effect(lease, effect["op_key"], "confirmed")
            store.set_artifact(lease, "delivery", "confirmed")
            store.finalize_artifact_plan(lease, {"delivery"})
            store.set_status(lease, "succeeded")
            store.release_lease(lease)
        second = create_report_snapshot(
            self.state, "weekly", now=datetime(2026, 10, 27, 12, tzinfo=TZ),
        )
        self.assertEqual(second["snapshot"]["counts"]["N"], 0)
        self.assertEqual(second["snapshot"]["long_term_counts"]["S"], 1)
        self.assertEqual(second["snapshot"]["long_term_counts"]["processing"], 0)
        reused = create_report_snapshot(self.state, "weekly", now=self.now)
        self.assertEqual(reused["snapshot"]["counts"]["processing"], 1)

    def test_failure_fingerprint_and_stage_duration_come_from_in_window_event_receipts(self):
        with WorkflowStore(self.state, clock=lambda: self.start_epoch + 100) as store:
            job = store.submit({"route": "long_read"}, app_id="app", source_message_id="stage-failure",
                               artifacts=[{"key": "delivery", "kind": "document", "required": True}])
            lease = store.acquire_lease(job["job_id"], "test", 60)
            effect = store.prepare_effect(lease, "delivery", "publish", "a" * 64)
            store.begin_effect(lease, effect["op_key"])
            store.finish_effect(lease, effect["op_key"], "failed", metadata={
                "failure_stage": "document-publish", "reason_code": "api-rejected", "duration_ms": 240,
            })
            store.set_artifact(lease, "delivery", "failed", metadata={
                "failure_stage": "document-publish", "reason_code": "api-rejected",
            })
            store.set_status(lease, "failed")
            store.release_lease(lease)
        result = create_report_snapshot(self.state, "weekly", now=self.now)
        self.assertEqual(result["snapshot"]["failure_fingerprints"], [
            {"fingerprint": "document-publish:api-rejected", "count": 1},
        ])
        self.assertEqual(result["snapshot"]["stage_duration_ms"]["median"], 240.0)
        self.assertEqual(result["snapshot"]["stage_duration_coverage"], "完整记录")

    def test_report_snapshot_markdown_and_card_include_repair_and_intervention_evidence(self):
        job_id = add_job(
            self.state, status="failed", created_at=self.start_epoch + 100,
            source_id="repair-evidence",
        )
        with WorkflowStore(self.state, clock=lambda: self.start_epoch + 300) as store:
            store.record_reliability_event(job_id, "repair_applied", {
                "repair_id": "fix-42", "failure_fingerprint": "bridge:unstable",
                "summary": "保留原会话并只读恢复", "change_ref": "abc1234",
            })
            store.record_reliability_event(job_id, "repair_comparison", {
                "repair_id": "fix-42", "metric": "final_failure_rate",
                "before_value": 0.2, "after_value": 0.05,
                "before_sample_size": 20, "after_sample_size": 20,
                "before_window": "2026-09", "after_window": "2026-10",
            })
            store.record_reliability_event(job_id, "manual_intervention", {
                "category": "reconciliation", "summary": "确认旧请求不可观察并保留待核实",
            })
        report = create_report_snapshot(self.state, "weekly", now=self.now)
        snapshot = report["snapshot"]
        self.assertEqual(snapshot["repair_records"], 1)
        self.assertEqual(snapshot["repair_comparisons"], 1)
        self.assertEqual(snapshot["manual_interventions"], 1)
        self.assertEqual(len(snapshot["repair_evidence"]), 3)
        self.assertIn("保留原会话并只读恢复", report["markdown"])
        self.assertIn("2026-09", report["markdown"])
        card_markdown = render_report_card(snapshot)["body"]["elements"][0]["content"]
        self.assertIn("人工介入 **1**", card_markdown)
        self.assertIn("已记录修复 **1**", card_markdown)

    def test_repair_evidence_is_reported_by_event_window_for_older_job(self):
        job_id = add_job(
            self.state, status="failed", created_at=self.start_epoch - 1000,
            source_id="older-repair-evidence",
        )
        with WorkflowStore(self.state, clock=lambda: self.start_epoch + 300) as store:
            store.record_reliability_event(job_id, "repair_applied", {
                "repair_id": "fix-old", "failure_fingerprint": "bridge:unstable",
                "summary": "修复旧任务涉及的恢复问题", "change_ref": "abc1234",
            })
        report = create_report_snapshot(self.state, "weekly", now=self.now)
        self.assertEqual(report["snapshot"]["counts"]["N"], 0)
        self.assertEqual(report["snapshot"]["repair_records"], 1)
        self.assertEqual(len(report["snapshot"]["repair_evidence"]), 1)

    def test_synthetic_jobs_do_not_enter_the_denominator(self):
        add_job(self.state, status="failed", created_at=self.start_epoch + 100, source_id="only-synthetic", synthetic=True)
        result = create_report_snapshot(self.state, "weekly", now=self.now)
        self.assertEqual(result["status"], "suppressed_weekly")
        self.assertEqual(result["snapshot"]["counts"]["N"], 0)

    def test_explicit_test_tasks_do_not_enter_the_denominator(self):
        add_job(self.state, status="failed", created_at=self.start_epoch + 100,
                source_id="only-test-task", test_task=True)
        result = create_report_snapshot(self.state, "weekly", now=self.now)
        self.assertEqual(result["status"], "suppressed_weekly")
        self.assertEqual(result["snapshot"]["counts"]["N"], 0)

    def test_unverified_test_marker_stays_in_the_production_denominator(self):
        add_job(self.state, status="failed", created_at=self.start_epoch + 100,
                source_id="unverified-test-marker", test_task=True, unverified_test_task=True)
        result = create_report_snapshot(self.state, "weekly", now=self.now)
        self.assertEqual(result["snapshot"]["counts"]["N"], 1)
        self.assertEqual(result["snapshot"]["counts"]["F"], 1)

    def test_report_outbox_uncertain_upload_cannot_be_reused_or_pseudo_confirmed(self):
        report = create_report_snapshot(self.state, "monthly", now=datetime(2026, 10, 21, 12, tzinfo=TZ))
        with WorkflowStore(self.state) as store:
            first = store.prepare_report_effect(report["report_key"], "markdown-upload", "1" * 64)
            again = store.prepare_report_effect(report["report_key"], "markdown-upload", "1" * 64)
            self.assertEqual(first["request_id"], again["request_id"])
            with self.assertRaises(StoreConflict):
                store.prepare_report_effect(report["report_key"], "markdown-upload", "2" * 64)
            attempt = store.begin_report_effect(report["report_key"], "markdown-upload")
            self.assertTrue(attempt["execute"])
            store.finish_report_effect(report["report_key"], "markdown-upload", "uncertain")
            with self.assertRaises(UncertainEffect):
                store.begin_report_effect(report["report_key"], "markdown-upload")
            with self.assertRaises(ValueError):
                store.finish_report_effect(report["report_key"], "markdown-upload", "confirmed",
                                           remote_id="file_report", reconcile=True)
            with self.assertRaises(StoreConflict):
                store.mark_report_delivered(report["report_key"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
