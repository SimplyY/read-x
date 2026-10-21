#!/usr/bin/env python3
from __future__ import annotations

import unittest

import long_read_document_supplement as supplement


GOOD = """### 核心结论
作者把判断优势放在修正假设的速度，而非预测无误。

### 问题
1. 哪个可观测信号能区分假设修正与事后解释？
2. 当反馈延迟时，作者的方法如何避免过度交易？

### 上下文
- 文章用多个历史案例支持及时修正假设。
- 反馈延迟带来的边界仍未得到直接验证。
"""


class DocumentSupplementTests(unittest.TestCase):
    def test_prompt_contains_frozen_concision_rules_and_hashes_inputs(self):
        prompt, digest = supplement.build_prompt(
            source="完整原文。", evidence={"quotes": []},
            analyses={"article-decode": "文章分析。", "ljg-think": "深读原稿。"},
        )
        self.assertIn(supplement.PROMPT_MARKER, prompt)
        self.assertIn("本指令作为实际发送给 ChatGPT 的生成约束", prompt)
        self.assertNotRegex(prompt, r"500\s*[-~～—–到至]\s*1500\s*字")
        self.assertEqual(len(digest), 64)

    def test_prompt_rejects_missing_article_decode_and_oversized_input(self):
        with self.assertRaisesRegex(ValueError, "article-decode"):
            supplement.build_prompt(source="原文", evidence={}, analyses={"ljg-think": "分析"})
        with self.assertRaisesRegex(ValueError, "too large"):
            supplement.build_prompt(source="x" * supplement.MAX_PROMPT_CHARS, evidence={},
                                    analyses={"article-decode": "分析"})

    def test_valid_supplement_is_preserved_and_research_section_is_renderable(self):
        result = supplement.validate_supplement(GOOD)
        self.assertEqual(result, GOOD)
        rendered = supplement.render_research_section(GOOD)
        self.assertIn("# 值得研究的相关问题", rendered)
        self.assertIn("## 问题\n1. 哪个可观测信号", rendered)
        self.assertIn("## 上下文\n- 文章用多个历史案例", rendered)

    def test_rejects_missing_or_reordered_schema_headings(self):
        for bad in (
            GOOD.replace("### 问题", "### 追问"),
            GOOD.replace("### 核心结论", "## 核心结论"),
            GOOD + "\n### 其他\n额外内容。\n",
        ):
            with self.subTest(bad=bad[:30]), self.assertRaisesRegex(ValueError, "headings"):
                supplement.validate_supplement(bad)

    def test_rejects_bad_question_count_labels_or_context(self):
        cases = (
            GOOD.replace("2. 当反馈延迟时，作者的方法如何避免过度交易？\n", ""),
            GOOD.replace("1. 哪个可观测信号", "问题：哪个可观测信号"),
            GOOD.replace("2. 当反馈延迟时，作者的方法如何避免过度交易？", "2. 当反馈延迟时如何调整"),
            GOOD.replace("- 文章用多个历史案例", "文章用多个历史案例"),
        )
        for bad in cases:
            with self.subTest(bad=bad[:40]), self.assertRaises(ValueError):
                supplement.validate_supplement(bad)

    def test_rejects_fragment_and_combined_context_overflow(self):
        long_core = GOOD.replace(
            "作者把判断优势放在修正假设的速度，而非预测无误。", "长" * 101)
        with self.assertRaisesRegex(ValueError, "core conclusion"):
            supplement.validate_supplement(long_core)
        long_context = GOOD.replace("反馈延迟带来的边界仍未得到直接验证。", "边界" * 140)
        with self.assertRaisesRegex(ValueError, "300"):
            supplement.validate_supplement(long_context)


if __name__ == "__main__":
    unittest.main()
