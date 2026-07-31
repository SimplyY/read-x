#!/usr/bin/env python3
"""Render a validated scoring_result as a Feishu CardKit 2.0 card."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse


DIMENSIONS = (
    ("evidence_quality", "证据与论证"),
    ("insight_explanatory", "洞察解释"),
    ("transfer_durability", "长期迁移"),
    ("information_efficiency", "信息效率"),
)


def _metric(label: str, value: str) -> dict:
    return {
        "tag": "column",
        "width": "weighted",
        "weight": 1,
        "background_style": "indigo-50",
        "padding": "12px",
        "vertical_spacing": "2px",
        "elements": [
            {"tag": "markdown", "content": f"## <font color='indigo'>{value}</font>", "text_align": "center"},
            {"tag": "markdown", "content": f"<font color='grey'>{label}</font>", "text_align": "center", "text_size": "notation"},
        ],
    }


def _dimension_text(result: dict, keys: tuple[tuple[str, str], ...]) -> str:
    dimensions = result.get("quality_dimensions") or {}
    lines = []
    for key, label in keys:
        score = dimensions.get(key, {}).get("score")
        value = f"{score:.1f}" if isinstance(score, (int, float)) else "不可用"
        lines.append(f"**{label}**  {value}")
    return "\n".join(lines)


def render_card(result: dict, *, title: str, author: str, date: str, url: str, score_only: bool) -> dict:
    if urlparse(url).scheme not in {"http", "https"}:
        raise ValueError("url must use http or https")
    status = result.get("score_status")
    if status == "needs_relevance":
        raise ValueError("needs_relevance is internal and cannot be rendered")
    if status not in {"scored", "needs_full_text", "needs_review"}:
        raise ValueError(f"unsupported score_status: {status}")

    subtitle = " · ".join(value for value in (author, date) if value)
    if status != "scored":
        message = "需要完整正文后才能评分。" if status == "needs_full_text" else "评分证据或校准冲突，需要人工复核。"
        return _card(title, subtitle, [
            _highlight("未产生数字评分", message, "yellow"),
            {"tag": "markdown", "content": f"[查看原文]({url})"},
        ], "yellow", "需要完整正文" if status == "needs_full_text" else "评分待复核")

    relevance = result.get("relevance_score")
    relevance_value = f"+{relevance:.1f}" if isinstance(relevance, (int, float)) else "-"
    relevance_label = "相关性" if isinstance(relevance, (int, float)) else "相关性\n未计算"
    interest = result.get("interest_score")
    interest_value = f"+{interest:.1f}" if isinstance(interest, (int, float)) else "-"
    interest_label = "兴趣" if isinstance(interest, (int, float)) else "兴趣\n未计算"
    elements = [
        {
            "tag": "column_set",
            "flex_mode": "none",
            "horizontal_spacing": "8px",
            "margin": "0px 0px 12px 0px",
            "columns": [
                _metric("质量", f"{result['quality_score']:.1f}"),
                _metric(relevance_label, relevance_value),
                _metric(interest_label, interest_value),
                _metric("决策", f"{result['decision_score']:.1f}"),
            ],
        },
        {
            "tag": "column_set",
            "flex_mode": "bisect",
            "horizontal_spacing": "8px",
            "margin": "0px 0px 12px 0px",
            "columns": [
                _text_column(_dimension_text(result, DIMENSIONS[:2])),
                _text_column(_dimension_text(result, DIMENSIONS[2:])),
            ],
        },
        _highlight(f"{result['quality_label']} · {result['quality_score']:.1f}/10", result.get("conclusion") or "无结论"),
    ]
    if score_only:
        elements.append({"tag": "markdown", "content": "**本次仅评分，不进入精读。**"})
    elif result.get("route") == "long_read":
        elements.append({"tag": "markdown", "content": "正在精读，稍后发文档。"})
    elements.append({"tag": "markdown", "content": f"[查看原文]({url})"})
    return _card(title, subtitle, elements, "indigo")


def _text_column(content: str) -> dict:
    return {
        "tag": "column", "width": "weighted", "weight": 1,
        "background_style": "grey-50", "padding": "12px",
        "elements": [{"tag": "markdown", "content": content}],
    }


def _highlight(title: str, content: str, color: str = "indigo") -> dict:
    return {
        "tag": "column_set", "flex_mode": "none", "margin": "0px 0px 12px 0px",
        "columns": [{
            "tag": "column", "width": "weighted", "weight": 1,
            "background_style": f"{color}-50", "padding": "12px", "vertical_spacing": "4px",
            "elements": [
                {"tag": "markdown", "content": f"**<font color='{color}'>{title}</font>**"},
                {"tag": "markdown", "content": content},
            ],
        }],
    }


def _card(title: str, subtitle: str, elements: list[dict], template: str, header_title: str = "评分完成") -> dict:
    header = {
        "title": {"tag": "plain_text", "content": header_title},
        "subtitle": {"tag": "plain_text", "content": title if not subtitle else f"{title} · {subtitle}"},
        "template": template,
        "icon": {"tag": "standard_icon", "token": "chart_colorful"},
    }
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "default", "summary": {"content": f"评分完成：{title}"}},
        "header": header,
        "body": {"direction": "vertical", "padding": "12px 12px 20px 12px", "elements": elements},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scoring_result")
    parser.add_argument("--title", required=True)
    parser.add_argument("--author", default="")
    parser.add_argument("--date", default="")
    parser.add_argument("--url", required=True)
    parser.add_argument("--score-only", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = json.loads(Path(args.scoring_result).read_text(encoding="utf-8"))
    rendered = json.dumps(render_card(result, title=args.title, author=args.author, date=args.date, url=args.url, score_only=args.score_only), ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
