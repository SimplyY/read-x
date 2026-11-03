from __future__ import annotations

import argparse
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import run_chatgpt_core_image as image
import run_chatgpt_munger as munger
import run_isolated_analyses as isolated
import run_long_read_pipeline as pipeline


class LegacyChatGPTEntryTests(unittest.TestCase):
    def _main(self, module, argv):
        with patch.object(sys, "argv", [module.__file__, *argv]), contextlib.redirect_stdout(io.StringIO()):
            return module.main()

    def test_pipeline_is_closed_without_calling_generation_helpers(self):
        with patch.object(pipeline.isolated, "run", side_effect=AssertionError("must not submit")), \
                patch.object(pipeline.munger, "run", side_effect=AssertionError("must not submit")):
            result = pipeline.run(argparse.Namespace())
        self.assertEqual(result["reason_code"], "legacy-direct-entry-disabled")
        self.assertFalse(result["delivery"]["delivery_complete"])
        self.assertEqual(pipeline.exit_code(result), 2)

    def test_text_clis_fail_closed_without_running_bridge(self):
        with patch.object(munger, "run_bridge", side_effect=AssertionError("must not submit")):
            self.assertEqual(self._main(munger, ["--source", "/missing", "--output", "/missing"]), 2)
            self.assertEqual(munger.run(Path("/missing"), Path("/missing"))["reason_code"], "legacy-direct-entry-disabled")
        with patch.object(isolated, "run", side_effect=AssertionError("must not submit")):
            self.assertEqual(self._main(isolated, ["--source", "/missing", "--evidence", "/missing", "--output-dir", "/missing"]), 2)
        self.assertEqual(isolated.run(Path("/missing"), Path("/missing"), Path("/missing"), [])["reason_code"], "legacy-direct-entry-disabled")

    def test_image_cli_blocks_new_generation_but_observes_explicit_bound_run(self):
        with patch.object(image, "run", side_effect=AssertionError("must not submit")):
            self.assertEqual(self._main(image, ["--source", "/missing", "--output", "/missing"]), 2)
        with patch.object(image, "run", side_effect=AssertionError("must not submit")), \
                patch.object(image, "recover", return_value={"status": "succeeded", "runId": "old-run"}) as recover:
            self.assertEqual(self._main(image, ["--source", "source.md", "--output", "out.png", "--summary", "old.json", "--request-id", "old-run"]), 0)
            recover.assert_called_once()

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "doc.md"
            source.write_text("正文" * 400, encoding="utf-8")
            with patch.object(image, "run_bridge", side_effect=AssertionError("must not submit")), \
                    patch.object(image, "fetch_prompt_asset", side_effect=AssertionError("must not fetch")):
                result = image.run(source, Path(directory) / "image.png")
            self.assertEqual(result["reason_code"], "legacy-direct-entry-disabled")


if __name__ == "__main__":
    unittest.main()
