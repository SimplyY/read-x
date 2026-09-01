#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).parents[3]
SPEC = importlib.util.spec_from_file_location("markdown_to_feishu_xml", Path(__file__).with_name("markdown_to_feishu_xml.py"))
renderer = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(renderer)
VALIDATOR_SPEC = importlib.util.spec_from_file_location("validate_output", Path(__file__).with_name("validate_output.py"))
validator = importlib.util.module_from_spec(VALIDATOR_SPEC)
assert VALIDATOR_SPEC and VALIDATOR_SPEC.loader
VALIDATOR_SPEC.loader.exec_module(validator)


def test_common_markdown_is_native_xml():
    markdown = """## 全文总结
这是 **重点**，见 [原文](https://example.com)。

- 一
- 二

> 引用

`code`

```python
print('ok')
```

| A | B |
| --- | --- |
| 1 | 2 |
"""
    xml = renderer.render_markdown(markdown, title="标题", source_url="https://source.test", conversation_url="https://chatgpt.com/c/1")
    ET.fromstring(f"<root>{xml}</root>")
    assert "<h1>全文总结</h1>" in xml
    assert "<b>重点</b>" in xml and "<a href=\"https://example.com\">原文</a>" in xml
    assert "<ul>" in xml and "<blockquote>" in xml and '<pre lang="python"><code>' in xml and "<table>" in xml
    assert xml.count('background-color="light-yellow"') == 1
    assert 'emoji="⭐"' in xml and 'border-color="yellow"' in xml


def test_research_questions_section_survives_render_and_validation():
    markdown = """## 基石 / 边缘 / 暗流
文章的核心结构。

## 值得研究的相关问题
1. **问题：**因果链在哪里断裂？ **上下文：**文章只证明相关性。
2. **问题：**这个判断何时失效？ **上下文：**边界条件没有展开。

## 与作者对话
保留具体对话。
"""
    xml = renderer.render_markdown(
        markdown,
        title="标题",
        source_url="https://example.com/article",
        conversation_url="https://chatgpt.com/c/1",
    )
    assert validator.check_document_xml(xml) == []


def test_unsafe_html_is_escaped_and_large_tables_are_preserved():
    markdown = "<script>alert(1)</script>\n\n| " + " | ".join(f"c{i}" for i in range(9)) + " |\n| " + " | ".join("---" for _ in range(9)) + " |\n| " + " | ".join("x" for _ in range(9)) + " |"
    xml = renderer.render_markdown(markdown, title="标题", source_url="", conversation_url="")
    ET.fromstring(f"<root>{xml}</root>")
    assert "<script>" not in xml
    assert "表格超出原生表格限制" in xml and '<pre lang="markdown"><code>' in xml


def test_local_deepseek_output_gets_provenance_callout_without_session_url():
    xml = renderer.render_markdown("正文。", title="标题", source_url="https://example.com/article", conversation_url="")
    assert 'emoji="⭐"' in xml
    assert "DeepSeek 基于完整原文生成" in xml


def test_metadata_rejects_unsafe_urls():
    for unsafe_url in ("javascript:alert(1)", "data:text/html,unsafe", "//example.com/article"):
        try:
            renderer.render_markdown("正文。", title="标题", source_url=unsafe_url, conversation_url="")
        except ValueError as error:
            assert "absolute http(s)" in str(error)
        else:
            raise AssertionError(f"unsafe metadata URL was accepted: {unsafe_url}")


def test_semantic_blocks_keep_native_structure():
    markdown = """## 排版
- [x] 已完成
- [ ] 待办
  1. 子步骤
  2. 下一步

---

```python
print(1 < 2)
```

第一行\\
第二行。
"""
    xml = renderer.render_markdown(markdown, title="标题", source_url="", conversation_url="")
    ET.fromstring(f"<root>{xml}</root>")
    assert '<checkbox done="true">已完成</checkbox>' in xml
    assert '<checkbox done="false">待办<ol>' not in xml
    assert '<checkbox done="false">待办</checkbox><ol>' in xml
    assert '<ol><li seq="auto">子步骤</li><li seq="auto">下一步</li></ol>' in xml
    assert "<hr/>" in xml and '<pre lang="python"><code>' in xml
    assert "第一行<br/>第二行。" in xml


def test_mixed_task_and_regular_items_stay_as_block_level_structures():
    markdown = "- [x] 已完成\n- 普通事项\n- [ ] 待处理"
    xml = renderer.render_markdown(markdown, title="标题", source_url="", conversation_url="")
    ET.fromstring(f"<root>{xml}</root>")
    assert '<checkbox done="true">已完成</checkbox>' in xml
    assert '<checkbox done="false">待处理</checkbox>' in xml
    assert "<ul><li>普通事项</li></ul>" in xml
    assert "<ul><li><checkbox" not in xml


def test_long_paragraphs_break_at_readable_boundaries():
    markdown = "这是第一句。" + ("这是后续内容，保持连贯。" * 20)
    xml = renderer.render_markdown(markdown, title="标题", source_url="", conversation_url="")
    root = ET.fromstring(f"<root>{xml}</root>")
    paragraphs = [node for node in root if node.tag == "p"]
    assert len(paragraphs) > 1
    texts = ["".join(node.itertext()) for node in paragraphs]
    assert all(len(text) <= renderer.MAX_PARAGRAPH_CHARS for text in texts)
    assert texts[0].endswith("。") and len(texts[0]) < renderer.MAX_PARAGRAPH_CHARS


def test_long_inline_styles_survive_paragraph_splitting():
    markdown = "**" + ("重点" * 80) + "**，并保留 [来源](https://example.com)。"
    xml = renderer.render_markdown(markdown, title="标题", source_url="", conversation_url="")
    root = ET.fromstring(f"<root>{xml}</root>")
    paragraphs = [node for node in root if node.tag == "p"]
    assert len(paragraphs) > 1
    assert all(len("".join(node.itertext())) <= renderer.MAX_PARAGRAPH_CHARS for node in paragraphs)
    assert sum(len(node.findall(".//b")) for node in paragraphs) == len(paragraphs)
    assert '<a href="https://example.com">来源</a>' in xml


def test_headings_are_continuous_and_top_level_sections_are_separated():
    markdown = """## 第一章
正文。

### 子章节
子内容。

## 第二章
第二段。
"""
    xml = renderer.render_markdown(markdown, title="标题", source_url="", conversation_url="")
    assert "<h1>第一章</h1>" in xml
    assert "<h2>子章节</h2>" in xml
    assert "<h1>第二章</h1>" in xml
    assert xml.count("<hr/>") == 1


def test_explicit_divider_is_not_duplicated_and_metadata_uses_short_links():
    markdown = "## 第一章\n正文。\n\n---\n\n## 第二章\n第二段。"
    xml = renderer.render_markdown(
        markdown,
        title="标题",
        source_url="https://example.com/article",
        conversation_url="https://chatgpt.com/c/123",
    )
    assert xml.count("<hr/>") == 1
    assert "<p>溯源：<a href=\"https://example.com/article\">查看原文</a> · <a href=\"https://chatgpt.com/c/123\">查看 ChatGPT 会话</a></p>" in xml
    assert ">https://example.com/article</a>" not in xml


def test_visible_content_is_preserved_while_layout_changes():
    markdown = """## 第一章
这是 **重点**，见 [来源](https://example.com)。

> 一句引用。

- 一个并列项
- 另一个并列项
"""
    xml = renderer.render_markdown(markdown, title="标题", source_url="", conversation_url="")
    visible = "".join(ET.fromstring(f"<root>{xml}</root>").itertext())
    for text in ("第一章", "这是 重点，见 来源。", "一句引用。", "一个并列项", "另一个并列项"):
        assert text in visible


def main():
    for name, value in sorted(globals().items()):
        if name.startswith("test_"):
            value(); print(f"[ok] {name}")


if __name__ == "__main__":
    main()
