#!/usr/bin/env python3
"""Quality output cache unit checks (run: python3 scripts/test_quality_cache.py)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quality_cache as qc


class QualityCacheTest(unittest.TestCase):
    SOURCE = "这是一段用于指纹计算的正文，长度超过二十四个字符，包含足够多的句子。第二句保证评分单元充足。"
    RUNTIME = "## 三维\n质量语义示例 rubric 正文。\n## 最终 JSON"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.db = self.tmp / "quality-cache.sqlite"

    def test_cache_key_deterministic(self):
        a = qc.cache_key(self.SOURCE, self.RUNTIME)
        b = qc.cache_key(self.SOURCE, self.RUNTIME)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)

    def test_source_change_changes_key(self):
        base = qc.cache_key(self.SOURCE, self.RUNTIME)
        changed = qc.cache_key(self.SOURCE.replace("正文", "正文化"), self.RUNTIME)
        self.assertNotEqual(base, changed)

    def test_runtime_change_changes_key(self):
        base = qc.cache_key(self.SOURCE, self.RUNTIME)
        changed = qc.cache_key(self.SOURCE, self.RUNTIME + "\n")
        self.assertNotEqual(base, changed)

    def test_put_then_get_roundtrip(self):
        key = qc.cache_key(self.SOURCE, self.RUNTIME)
        output = '{"schema_version":"v3.16","conclusion":"cached"}'
        qc.put(key, output, db_path=self.db)
        self.assertEqual(qc.get(key, db_path=self.db), output)
        self.assertEqual(os.stat(self.db).st_mode & 0o777, 0o600)

    def test_corrupt_db_returns_none(self):
        self.db.write_bytes(b"not a sqlite database at all" * 10)
        warnings = []
        self.assertIsNone(qc.get("k" * 64, db_path=self.db, warning=warnings.append))
        self.assertTrue(warnings)

    def test_put_unwritable_path_is_silent(self):
        warnings = []
        blocker = self.tmp / "blocker"
        blocker.write_text("x", encoding="utf-8")
        qc.put("k" * 64, "{}", db_path=blocker / "sub" / "db.sqlite", warning=warnings.append)
        self.assertTrue(warnings)


if __name__ == "__main__":
    unittest.main(verbosity=2)
