#!/usr/bin/env python3
"""Render a validated scoring_result as a Feishu CardKit 2.0 card."""
from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlparse


DIMENSIONS = (
    ("evidence_quality", "证据与论证"),
    ("insight_explanatory", "洞察解释"),
    ("transfer_durability", "长期迁移"),
)
ISSUE_REMEDIATION = {
    "source_status is not complete": "补充完整正文后重试",
    "isolated_retry_required": "按规则重新执行一次隔离重评",
    "relevance_context_unavailable": "恢复并校验相关性上下文后重试",
}


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


def _importance_text(result: dict) -> str:
    dimensions = result.get("importance_dimensions") or {}
    problem = dimensions.get("problem_significance_score")
    fmt = lambda value: f"{value:.1f}" if isinstance(value, (int, float)) else "不可用"
    return f"**大问题思考**  {fmt(problem)}"


def _compact(value: object, limit: int = 240) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _status_notes(result: dict) -> str:
    notes = []
    interest = result.get("interest_score")
    relevance_info = result.get("relevance_dimensions") or {}
    refresh = relevance_info.get("context_refresh")
    if isinstance(refresh, str):
        match = re.search(r"\d{4}-\d{2}-\d{2}", refresh)
        if match:
            try:
                if (date.today() - date.fromisoformat(match.group(0))).days > 8:
                    notes.append(f"相关性基于 {match.group(0)} 的旧版核心上下文（超过 8 天新鲜窗口），按旧版偏好评分")
            except ValueError:
                pass
    if isinstance(interest, (int, float)) and not isinstance(interest, bool) and float(interest) == 0:
        reason = "未命中受限上下文明确列出的领域兴趣"
        rationale = _compact(relevance_info.get("rationale"))
        if rationale:
            reason += f"；评分依据：{rationale}"
        notes.append(f"兴趣 +0.0（正常）：{reason}")
    elif interest is None:
        issues = result.get("issues") or []
        if result.get("interest_label") == "未计算（不影响本次路由）":
            notes.append("兴趣未计算：质量未达到相关性计算门槛，按规则跳过")
        elif "relevance_context_unavailable" in issues:
            notes.append("兴趣不可用：相关性上下文不可用，未把不可用误记为 0 分")
        else:
            notes.append(f"兴趣 {result.get('interest_label') or '不可用'}：没有可用的兴趣评分")

    return "**情况说明**\n" + "\n".join(f"- {note}" for note in notes) if notes else ""


def _failure_notes(result: dict) -> str:
    issues = [_compact(issue) for issue in (result.get("issues") or []) if _compact(issue)]
    if not issues:
        issues = ["评分没有产生可交付数字"]
    reason = "；".join(dict.fromkeys(issues[:4]))
    actions = [ISSUE_REMEDIATION[issue] for issue in issues if issue in ISSUE_REMEDIATION]
    action = actions[0] if actions else "已失败关闭，按上述原因修复后重试"
    if result.get("score_status") == "needs_full_text":
        action = "补充完整正文后重试"
    return f"**处理说明**\n- 原因：{reason}\n- 已处理：未编造数字，已停止当前评分\n- 下一步：{action}"


def render_card(result: dict, *, title: str, author: str, date: str, url: str, score_only: bool, quick_read: str | None = None) -> dict:
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
        elements = [_highlight("未产生数字评分", message, "yellow")]
        elements.append({"tag": "markdown", "content": _failure_notes(result)})
        elements.append({"tag": "markdown", "content": f"[查看原文]({url})"})
        return _card(title, subtitle, elements, "yellow", "需要完整正文" if status == "needs_full_text" else "评分待复核")

    relevance = result.get("relevance_score")
    relevance_value = f"+{relevance:.1f}" if isinstance(relevance, (int, float)) else result.get("priority_label", "-")
    relevance_label = "相关性"
    interest = result.get("interest_score")
    interest_value = f"+{interest:.1f}" if isinstance(interest, (int, float)) else result.get("interest_label", "-")
    interest_label = "兴趣"
    importance = result.get("importance_score")
    importance_value = f"{importance:.1f}" if isinstance(importance, (int, float)) and not isinstance(importance, bool) else "不可用"
    elements = [
        {
            "tag": "column_set",
            "flex_mode": "none",
            "horizontal_spacing": "8px",
            "margin": "0px 0px 12px 0px",
            "columns": [
                _metric("质量", f"{result['quality_score']:.1f}"),
                _metric("大问题思考", importance_value),
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
                _text_column(_dimension_text(result, DIMENSIONS[2:]) + "\n\n" + _importance_text(result)),
            ],
        },
        _highlight(f"{result['quality_label']} · {result['quality_score']:.1f}/10", result.get("conclusion") or "无结论"),
    ]
    if result.get("route") == "card" and result.get("quality_label") == "快速阅读" and not score_only:
        quick_read_text = (quick_read or "").strip()
        if not quick_read_text:
            raise ValueError("快速阅读 must provide --quick-read content file")
        elements.append({"tag": "markdown", "content": quick_read_text})
    notes = _status_notes(result)
    if notes:
        elements.insert(-1, {"tag": "markdown", "content": notes})
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
