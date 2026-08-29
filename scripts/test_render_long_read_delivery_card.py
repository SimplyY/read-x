#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SPEC = importlib.util.spec_from_file_location("delivery_card", Path(__file__).with_name("render_long_read_delivery_card.py"))
card = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(card)


def test_success_card_has_two_links_and_real_newline():
    value = card.render_card(title="标题", main_url="https://feishu.cn/docx/main", munger_url="https://feishu.cn/docx/munger")
    payload = json.loads(json.dumps(value, ensure_ascii=False))
    contents = [element["content"] for column in payload["body"]["elements"][0]["columns"] for element in column["elements"]]
    assert any("\n" in content for content in contents)
    assert all("\\n" not in content for content in contents)
    assert any("munger" in content for content in contents)


def test_failure_card_only_has_main_link():
    value = card.render_card(title="标题", main_url="https://feishu.cn/docx/main", failure_reason="bridge-output-unverified")
    content = json.dumps(value, ensure_ascii=False)
    assert "main" in content and "munger" not in content and "待复核" in content


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_"):
            value(); print(f"[ok] {name}")
