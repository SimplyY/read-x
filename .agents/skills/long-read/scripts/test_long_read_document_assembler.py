#!/usr/bin/env python3
from __future__ import annotations

import unittest

import long_read_document_assembler as assembler
import validate_output


EVIDENCE = {
    "metadata": {
        "title": "判断与证据", "author": "作者", "source_url": "https://example.com/article",
        "published_at": None, "genre": "essay", "word_count": 80,
    },
    "claims": [{"id": "C1", "claim": "及时修正假设", "evidence": "修正假设", "evidence_type": "quote", "confidence": "high"}],
    "facts": ["文章明确记录三个决策案例。"], "data_points": [],
    "quotes": ["修正假设"], "uncertainties": [], "article_structure": [],
}
SCORING = {
    "score_status": "scored", "route": "long_read", "quality_score": 8.5,
    "quality_label": "完整深读", "relevance_score": 0.6, "priority_label": "相关",
    "interest_score": 0.7, "interest_label": "高兴趣",
}
ARTICLE = """> 证据边界：以下结构判断来自原文。
# 判断与证据
# 这篇文章真正的核心
真正的判断在于反馈如何修正假设。

## 内部证据
三个案例支持这一点。
# 基石 / 边缘 / 暗流
基石是快速更新假设；边缘是把停手视作能力；暗流是反馈可信。
# 作者心中的结构
文章先提出问题，再用案例和反例建立判断。
# 与作者对话
作者对即时反馈的适用边界解释不足。
# 最值得深读的论证
应细读反馈变得可观测的案例。
"""
SUPPLEMENT = """### 核心结论
判断的优势来自根据反馈修正假设。

### 问题
1. 哪种反馈足以证明假设修正有效？
2. 延迟反馈会在何种条件下误导决策？

### 上下文
- 原文以三个案例说明反馈与假设修正。
- 案例没有覆盖反馈延迟的边界。
"""


class DocumentAssemblerTests(unittest.TestCase):
    def build(self, *, article=ARTICLE, supplement=SUPPLEMENT, munger=None, analyses=None, scoring=SCORING, evidence=EVIDENCE, source="原文中有‘修正假设’。"):
        return assembler.assemble_markdown(
            evidence=evidence, scoring=scoring, article_decode=article,
            text_analyses=analyses or [("ljg-think", "# 追问\n独立原稿保留。")],
            supplement_markdown=supplement, source=source, munger_markdown=munger,
        )

    def test_assembly_preserves_source_sections_branch_originals_and_order(self):
        markdown = self.build(munger="# 洞察\n完整芒格分支。")
        self.assertTrue(markdown.startswith("# 评分\n"))
        self.assertIn(":::readx-core-conclusion\n判断的优势来自根据反馈修正假设。\n:::", markdown)
        self.assertIn("真正的判断在于反馈如何修正假设。", markdown)
        self.assertIn("三个案例支持这一点。", markdown)
        self.assertIn("作者对即时反馈的适用边界解释不足。", markdown)
        self.assertIn("## ljg-think · 追本之箭\n### 追问\n独立原稿保留。", markdown)
        self.assertIn("# 芒格洞察\n## 洞察\n完整芒格分支。", markdown)
        self.assertLess(markdown.index("# 基石 / 边缘 / 暗流"), markdown.index("# 值得研究的相关问题"))
        self.assertLess(markdown.index("# 值得研究的相关问题"), markdown.index("# 与作者对话"))

    def test_rendered_candidate_passes_exact_long_read_document_gate(self):
        markdown = self.build()
        xml = assembler.render_candidate(
            markdown, title="判断与证据", source_url="https://example.com/article",
            conversation_url="https://chatgpt.com/c/abc123",
        )
        self.assertEqual(validate_output.check_document_xml(xml), [])
        self.assertEqual(xml.count('background-color="light-yellow"'), 1)
        self.assertIn("文章最重要的一刀", xml)
        self.assertIn('href="https://chatgpt.com/c/abc123"', xml)
        self.assertIn("文章明确记录三个决策案例。", xml)

    def test_rejects_unstructured_article_decode_or_missing_required_sections(self):
        with self.assertRaisesRegex(ValueError, "Skill-defined"):
            assembler.parse_article_decode("# 自由正文\n文本。", title="判断与证据")
        missing = ARTICLE.replace("# 作者心中的结构\n文章先提出问题，再用案例和反例建立判断。\n", "")
        with self.assertRaisesRegex(ValueError, "core, pillars, and author-structure"):
            assembler.parse_article_decode(missing, title="判断与证据")

    def test_rejects_invalid_scores_links_and_source_quotes(self):
        with self.assertRaisesRegex(ValueError, "numeric interest_score"):
            self.build(scoring={**SCORING, "interest_score": None})
        with self.assertRaisesRegex(ValueError, "original source URL"):
            self.build(evidence={**EVIDENCE, "metadata": {**EVIDENCE["metadata"], "source_url": "javascript:bad"}})
        with self.assertRaisesRegex(ValueError, "Evidence is invalid"):
            self.build(source="不含证据引文。")

    def test_article_heading_aliases_and_duplicate_sections_are_checked(self):
        aliased = ARTICLE.replace("# 基石 / 边缘 / 暗流", "# 2. 基石／边缘／暗流")
        parsed = assembler.parse_article_decode(aliased, title="判断与证据")
        self.assertIn("pillars", parsed["sections"])
        duplicate = ARTICLE + "\n# 基石 / 边缘 / 暗流\n重复。\n"
        with self.assertRaisesRegex(ValueError, "duplicate"):
            assembler.parse_article_decode(duplicate, title="判断与证据")


if __name__ == "__main__":
    unittest.main()
