#!/usr/bin/env python3
"""Render the single long-read document delivery Card 2.0 payload."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse


def _url(value: str) -> str:
    if urlparse(value).scheme not in {"http", "https"}:
        raise ValueError("document URL must use http or https")
    return value


def render_card(*, title: str, main_url: str, munger_url: str | None = None, failure_reason: str | None = None) -> dict:
    _url(main_url)
    if munger_url:
        _url(munger_url)
    success = bool(munger_url) and not failure_reason
    subtitle = "主精读文档 + 芒格洞察" if success else "主精读文档"
    columns = [{
        "tag": "column", "width": "weighted", "weight": 1,
        "background_style": "indigo-50", "padding": "12px",
        "elements": [{"tag": "markdown", "content": f"**主精读文档**\n[打开主文档]({main_url})"}],
    }]
    if success:
        columns.append({
            "tag": "column", "width": "weighted", "weight": 1,
            "background_style": "purple-50", "padding": "12px",
            "elements": [{"tag": "markdown", "content": f"**ChatGPT 芒格洞察**\n[打开芒格文档]({munger_url})"}],
        })
    elements = [{"tag": "column_set", "flex_mode": "bisect", "horizontal_spacing": "8px", "columns": columns}]
    if not success:
        elements.append({"tag": "markdown", "content": f"ChatGPT 芒格洞察待复核：{failure_reason or '未生成'}"})
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "default", "summary": {"content": f"长文精读完成：{title}"}},
        "header": {
            "title": {"tag": "plain_text", "content": "长文精读完成"},
            "subtitle": {"tag": "plain_text", "content": f"{title} · {subtitle}"},
            "template": "indigo",
            "icon": {"tag": "standard_icon", "token": "chart_colorful"},
        },
        "body": {"direction": "vertical", "padding": "12px 12px 20px 12px", "elements": elements},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", required=True)
    parser.add_argument("--main-url", required=True)
    parser.add_argument("--munger-url")
    parser.add_argument("--failure-reason")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rendered = json.dumps(render_card(title=args.title, main_url=args.main_url, munger_url=args.munger_url, failure_reason=args.failure_reason), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
