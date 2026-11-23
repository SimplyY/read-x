#!/usr/bin/env python3
"""Tests for codex_model.extract_turn_usage (no real model calls)."""
from __future__ import annotations

import unittest

import codex_model

NL = chr(10)


class ExtractTurnUsageTests(unittest.TestCase):
    def test_sums_turn_completed_events(self):
        stdout = NL.join([
            "noise line",
            "{\"type\":\"thread.started\"}",
            "{\"type\":\"turn.completed\",\"usage\":{\"input_tokens\":10,\"cached_input_tokens\":8,\"cache_write_input_tokens\":0,\"output_tokens\":2,\"reasoning_output_tokens\":1,\"total_tokens\":12}}",
            "{\"type\":\"turn.completed\",\"usage\":{\"input_tokens\":5,\"cached_input_tokens\":3,\"cache_write_input_tokens\":0,\"output_tokens\":1,\"reasoning_output_tokens\":0,\"total_tokens\":6}}",
            "{\"type\":\"turn.completed\"}",
        ])
        usage = codex_model.extract_turn_usage(stdout)
        self.assertIsNotNone(usage)
        self.assertEqual(usage["turn_completed_events"], 2)
        self.assertEqual(usage["input_tokens"], 15)
        self.assertEqual(usage["output_tokens"], 3)
        self.assertEqual(usage["total_tokens"], 18)

    def test_no_events_returns_none(self):
        self.assertIsNone(codex_model.extract_turn_usage("no json here" + NL))
        self.assertIsNone(codex_model.extract_turn_usage(""))
        self.assertIsNone(codex_model.extract_turn_usage(None))


if __name__ == "__main__":
    unittest.main()