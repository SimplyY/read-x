import json
import unittest
from pathlib import Path
import tempfile

import apply_article_title
import repair_reading_titles
from article_titles import (
    TITLE_PENDING,
    choose_article_title,
    can_update_base_title,
    has_han,
    is_article_title,
    is_source_url_title,
    is_url_title,
)
import wx_fast


class ArticleTitleTests(unittest.TestCase):
    def test_url_placeholders_are_rejected(self):
        self.assertTrue(is_url_title("https://mp.weixin.qq.com/s/example"))
        self.assertTrue(is_url_title("www.example.com/article"))
        self.assertFalse(is_article_title("https://example.com/article"))
        self.assertFalse(is_article_title(TITLE_PENDING))
        self.assertIsNone(choose_article_title([
            "https://example.com/article", "www.example.com/article", ""
        ], "https://example.com/article"))

    def test_chinese_candidate_wins_over_english_and_url_metadata(self):
        title = choose_article_title([
            "https://example.com/article", "English SEO title", "中文文章标题"
        ], "https://example.com/article")
        self.assertEqual(title, "中文文章标题")
        self.assertTrue(has_han(title))

    def test_original_english_title_is_preserved_when_no_chinese_source_exists(self):
        self.assertEqual(choose_article_title(["What to Do"], "https://example.com/do"), "What to Do")

    def test_only_the_article_url_itself_is_treated_as_a_replaceable_placeholder(self):
        self.assertTrue(is_source_url_title("https://example.com/a?from=share", "https://example.com/a"))
        self.assertFalse(is_source_url_title("https://example.com/other", "https://example.com/a"))
        self.assertTrue(can_update_base_title("https://example.com/a", "中文标题", "https://example.com/a"))
        self.assertFalse(can_update_base_title("https://example.com/other", "中文标题", "https://example.com/a"))

    def test_wechat_parser_uses_chinese_fallback_after_bad_og_title(self):
        source = '''
        <title>English page title</title>
        <meta property="og:title" content="https://mp.weixin.qq.com/s/example">
        <meta name="twitter:title" content="English SEO title">
        <script>var msg_title = "中文文章标题";</script>
        <div id="js_content"><p>''' + ("正文内容" * 60) + '''</p></div>
        '''
        markdown = wx_fast.parse_article(source, "https://mp.weixin.qq.com/s/example")
        self.assertTrue(markdown.startswith("# 中文文章标题\n"))
        self.assertNotIn("# https://", markdown)

    def test_wechat_parser_uses_document_title_when_og_and_js_titles_are_urls(self):
        source = '''
        <title>中文页面标题</title>
        <meta property="og:title" content="https://mp.weixin.qq.com/s/example">
        <script>var msg_title = "https://mp.weixin.qq.com/s/example";</script>
        <div id="js_content"><p>''' + ("正文内容" * 60) + '''</p></div>
        '''
        markdown = wx_fast.parse_article(source, "https://mp.weixin.qq.com/s/example")
        self.assertTrue(markdown.startswith("# 中文页面标题\n"))

    def test_wechat_parser_marks_title_pending_if_all_candidates_are_urls(self):
        source = '''
        <meta property="og:title" content="https://mp.weixin.qq.com/s/example">
        <script>var msg_title = "https://mp.weixin.qq.com/s/example";</script>
        <div id="js_content"><p>''' + ("正文内容" * 60) + '''</p></div>
        '''
        markdown = wx_fast.parse_article(source, "https://mp.weixin.qq.com/s/example")
        self.assertTrue(markdown.startswith("# 标题待补采\n"))
        self.assertNotIn("# https://", markdown)

    def test_apply_ego_title_updates_only_the_scoring_source_heading(self):
        with tempfile.TemporaryDirectory(prefix="readx-score.", dir="/tmp") as directory:
            path = Path(directory) / "source.md"
            url = "https://mp.weixin.qq.com/s/example"
            path.write_text(f"# 标题待补采\n> 原文链接: {url}\n\n正文保真。\n", encoding="utf-8")
            result = apply_article_title.apply_title(Path(directory), url, "EGO 核实的中文标题")
            self.assertEqual(result["title"], "EGO 核实的中文标题")
            self.assertTrue(path.read_text(encoding="utf-8").startswith("# EGO 核实的中文标题\n"))
            self.assertIn("正文保真。", path.read_text(encoding="utf-8"))

    def test_apply_ego_title_rejects_mismatched_url_and_existing_chinese_title(self):
        with tempfile.TemporaryDirectory(prefix="readx-score.", dir="/tmp") as directory:
            path = Path(directory) / "source.md"
            path.write_text("# 已有中文标题\n> 原文链接: https://example.com/a\n\n正文。\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source URL"):
                apply_article_title.apply_title(Path(directory), "https://example.com/b", "新的中文标题")
            with self.assertRaisesRegex(ValueError, "different Chinese title"):
                apply_article_title.apply_title(Path(directory), "https://example.com/a", "新的中文标题")

    def test_base_title_repair_updates_only_after_bot_history_check(self):
        class FakeBase:
            record = {"id": "rec1", "fields": {"标题": "Old English", "原文链接": "https://example.com/a"}}
            events = [
                {"activity_type": "create", "operator": repair_reading_titles.BOT_OPERATOR, "field_changes": None}
            ]

            def call(self, command, *args):
                if command == "+record-list":
                    fields = ["标题", "原文链接"]
                    return {"fields": fields, "data": [[self.record["fields"].get(name) for name in fields]],
                            "record_id_list": [self.record["id"]], "has_more": False}
                if command == "+record-history-list":
                    return {"items": self.events, "has_more": False}
                if command == "+record-upsert":
                    self.record["fields"].update(json.loads(args[3]))
                    return {}
                raise AssertionError(command)

        base = FakeBase()
        target = {"record_id": "rec1", "url": "https://example.com/a",
                  "expected_title": "Old English", "title": "新中文标题"}
        result = repair_reading_titles.repair_one(base, target, execute=True)
        self.assertEqual(result["status"], "updated")
        self.assertEqual(base.record["fields"], {"标题": "新中文标题", "原文链接": "https://example.com/a"})

    def test_base_title_repair_refuses_a_human_title_change(self):
        class FakeBase:
            record = {"id": "rec1", "fields": {"标题": "Old English", "原文链接": "https://example.com/a"}}
            events = [
                {"activity_type": "create", "operator": repair_reading_titles.BOT_OPERATOR, "field_changes": None},
                {"activity_type": "update", "operator": "用户", "field_changes": [
                    {"field_name": "标题", "before": "Older English", "after": "Old English"}
                ]},
            ]

            def call(self, command, *args):
                if command == "+record-list":
                    fields = ["标题", "原文链接"]
                    return {"fields": fields, "data": [[self.record["fields"].get(name) for name in fields]],
                            "record_id_list": [self.record["id"]], "has_more": False}
                if command == "+record-history-list":
                    return {"items": self.events, "has_more": False}
                raise AssertionError(command)

        target = {"record_id": "rec1", "url": "https://example.com/a",
                  "expected_title": "Old English", "title": "新中文标题"}
        with self.assertRaisesRegex(ValueError, "non-bot edit"):
            repair_reading_titles.repair_one(FakeBase(), target, execute=True)


if __name__ == "__main__":
    unittest.main()
