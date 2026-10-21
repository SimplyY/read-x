#!/usr/bin/env python3
"""The analysis-only pipeline must never claim end-to-end delivery."""
from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_long_read_pipeline as pipeline


class PipelineStatusTests(unittest.TestCase):
    def test_successful_content_branches_wait_for_delivery_verification(self):
        result = pipeline.delivery_state(
            {"status": "completed", "tasks": [{"task": "article-decode", "status": "completed"}]},
            {"status": "succeeded"},
        )
        self.assertEqual(result["state"], "content_ready")
        self.assertFalse(result["delivery_complete"])
        self.assertIn("等待", result["label"])
        self.assertNotIn("精读完成", result["label"])

    def test_unverified_munger_and_failed_analysis_are_not_final_success(self):
        munger = pipeline.delivery_state(
            {"status": "completed", "tasks": [{"task": "article-decode", "status": "completed"}]},
            {"status": "needs_review"},
        )
        self.assertEqual(munger["state"], "needs_review_chatgpt")
        self.assertFalse(munger["delivery_complete"])

        analysis = pipeline.delivery_state(
            {"status": "completed", "tasks": [{"task": "article-decode", "status": "failed"}]},
            {"status": "succeeded"},
        )
        self.assertEqual(analysis["state"], "degraded")
        self.assertFalse(analysis["delivery_complete"])


if __name__ == "__main__":
    unittest.main()
