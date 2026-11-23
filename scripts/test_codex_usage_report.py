#!/usr/bin/env python3
"""Tests for codex_usage_report on synthetic rollout JSONL (no real sessions)."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("codex_usage_report.py")
NL = chr(10)
J = json.dumps

FIXTURE_LINES = [
    J({"timestamp": "2026-11-23T01:00:00Z", "type": "session_meta", "payload": {"id": "sess-1", "cwd": "/Users/yuwei/code/voice-x"}}),
    J({"timestamp": "2026-11-23T01:00:01Z", "type": "turn_context", "payload": {"turn_id": "t1", "model": "gpt-6-luna", "effort": "low"}}),
    J({"timestamp": "2026-11-23T01:00:02Z", "type": "token_usage_record", "payload": {"response_id": "r1", "turn_id": "t1", "usage": {"input_tokens": 100, "cached_input_tokens": 90, "cache_write_input_tokens": 0, "output_tokens": 10, "reasoning_output_tokens": 5, "total_tokens": 110}}}),
    J({"timestamp": "2026-11-23T01:00:03Z", "type": "token_usage_record", "payload": {"response_id": "r1", "turn_id": "t1", "usage": {"input_tokens": 100, "cached_input_tokens": 90, "cache_write_input_tokens": 0, "output_tokens": 10, "reasoning_output_tokens": 5, "total_tokens": 110}}}),
    J({"timestamp": "2026-11-23T01:01:00Z", "type": "token_usage_record", "payload": {"response_id": "r2", "turn_id": "t2", "usage": {"input_tokens": 50, "cached_input_tokens": 40, "cache_write_input_tokens": 0, "output_tokens": 7, "reasoning_output_tokens": 2, "total_tokens": 57}}}),
    J({"timestamp": "2026-11-23T01:01:05Z", "type": "turn_aborted", "payload": {"turn_id": "t2", "reason": "x"}}),
    "this line is not json",
]


class UsageReportTests(unittest.TestCase):
    def run_cli(self, *extra: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-2026-11-23T01-00-00-test.jsonl"
            path.write_text(NL.join(FIXTURE_LINES) + NL, encoding="utf-8")
            proc = subprocess.run([sys.executable, str(SCRIPT), str(path), *extra], capture_output=True, text=True, check=True)
        return json.loads(proc.stdout)

    def test_aggregates_and_dedupes(self):
        report = self.run_cli()
        self.assertEqual(len(report["sessions"]), 1)
        session = report["sessions"][0]
        self.assertEqual(session["calls"], 2)  # duplicate response_id skipped
        self.assertEqual(session["turns"], 2)
        self.assertEqual(session["aborted_turns"], 1)
        self.assertEqual(session["usage"]["input_tokens"], 150)
        self.assertEqual(session["usage"]["output_tokens"], 17)
        self.assertEqual(session["usage"]["total_tokens"], 167)
        self.assertEqual(session["models"], ["gpt-6-luna"])
        self.assertEqual(session["skipped_lines"], 1)
        self.assertGreater(report["totals"]["total_tokens"], 0)

    def test_per_turn_breakdown(self):
        report = self.run_cli("--per-turn")
        turns = report["sessions"][0]["per_turn"]
        self.assertEqual(len(turns), 2)
        first = turns[0]
        self.assertEqual(first["turn_id"], "t1")
        self.assertEqual(first["calls"], 1)  # deduped
        self.assertEqual(first["usage"]["input_tokens"], 100)

    def test_since_filters_window(self):
        report = self.run_cli("--since", "2026-11-23T01:00:30Z")
        session = report["sessions"][0]
        self.assertEqual(session["calls"], 1)  # only r2 inside window
        self.assertEqual(session["usage"]["input_tokens"], 50)


    def test_no_timestamp_events_and_windowed_zero_sessions(self):
        empty_lines = [
            J({"type": "session_meta", "payload": {"id": "sess-empty", "cwd": "/tmp"}}),
        ]
        nts_lines = [
            J({"type": "token_usage_record", "payload": {"response_id": "r9", "turn_id": "t9", "usage": {"input_tokens": 5, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 1, "reasoning_output_tokens": 0, "total_tokens": 6}}}),  # no timestamp
        ]
        with tempfile.TemporaryDirectory() as tmp:
            empty_path = Path(tmp) / "rollout-2026-11-23T00-00-00-empty.jsonl"
            empty_path.write_text(NL.join(empty_lines) + NL, encoding="utf-8")
            nts_path = Path(tmp) / "rollout-2026-11-23T01-00-00-nts.jsonl"
            nts_path.write_text(NL.join(nts_lines) + NL, encoding="utf-8")
            # windowed report drops sessions with no in-window usage
            proc = subprocess.run([sys.executable, str(SCRIPT), str(empty_path), "--since", "2020-01-01"], capture_output=True, text=True, check=True)
            self.assertEqual(len(json.loads(proc.stdout)["sessions"]), 0)
            # no-timestamp usage event is counted separately and still included
            proc = subprocess.run([sys.executable, str(SCRIPT), str(nts_path), "--since", "2020-01-01"], capture_output=True, text=True, check=True)
            session = json.loads(proc.stdout)["sessions"][0]
            self.assertEqual(session["no_timestamp_events"], 1)
            self.assertEqual(session["calls"], 1)
    def test_cwd_filter(self):
        report = self.run_cli("--cwd-substr", "voice-x")
        self.assertEqual(len(report["sessions"]), 1)
        report = self.run_cli("--cwd-substr", "read-x")
        self.assertEqual(len(report["sessions"]), 0)


if __name__ == "__main__":
    unittest.main()