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
    assert "<h2>全文总结</h2>" in xml
    assert "<b>重点</b>" in xml and "<a href=\"https://example.com\">原文</a>" in xml
    assert "<ul>" in xml and "<blockquote>" in xml and "<pre><code>" in xml and "<table>" in xml
    assert xml.count('background-color="light-yellow"') == 1


def test_unsafe_html_is_escaped_and_large_tables_are_preserved():
    markdown = "<script>alert(1)</script>\n\n| " + " | ".join(f"c{i}" for i in range(9)) + " |\n| " + " | ".join("---" for _ in range(9)) + " |\n| " + " | ".join("x" for _ in range(9)) + " |"
    xml = renderer.render_markdown(markdown, title="标题", source_url="", conversation_url="")
    ET.fromstring(f"<root>{xml}</root>")
    assert "<script>" not in xml
    assert "表格超出原生表格限制" in xml and "<pre><code>" in xml


def main():
    for name, value in sorted(globals().items()):
        if name.startswith("test_"):
            value(); print(f"[ok] {name}")


if __name__ == "__main__":
    main()
