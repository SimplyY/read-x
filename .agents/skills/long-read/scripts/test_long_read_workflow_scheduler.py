#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from long_read_workflow_scheduler import due_report_runs, run_due_reports
from long_read_workflow_store import WorkflowStore


TZ = ZoneInfo("Asia/Shanghai")


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "state"
        with WorkflowStore(self.state) as store:
            store.connection.execute("UPDATE workflow_meta SET value=? WHERE key='tracking_started_at'",
                                      (str(datetime(2026, 10, 1, 10, tzinfo=TZ).timestamp()),))

    def tearDown(self):
        self.temp.cleanup()

    def test_catchup_finds_each_due_period_and_monthly_precedes_weekly_on_same_day(self):
        due = due_report_runs(self.state, now=datetime(2026, 11, 1, 10, tzinfo=TZ))
        same_day = [(period, at) for period, at in due if at.date().isoformat() == "2026-11-01"]
        self.assertEqual([period for period, _ in same_day], ["monthly", "weekly"])
        self.assertEqual(len(due), 6)

    def test_run_catches_up_fixed_snapshots_and_reuses_them_after_restart(self):
        now = datetime(2026, 11, 1, 10, tzinfo=TZ)
        first = run_due_reports(self.state, now=now)
        second = run_due_reports(self.state, now=now)
        self.assertEqual(first["due_count"], 6)
        self.assertEqual(second["due_count"], 6)
        self.assertTrue(all(item["reused"] for item in second["reports"]))
        self.assertEqual(first["reports"][-2]["period"], "monthly")
        self.assertEqual(first["reports"][-1]["period"], "weekly")


if __name__ == "__main__":
    unittest.main()
