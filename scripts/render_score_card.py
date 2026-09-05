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
)
IMPORTANCE_LABEL = "权威性与大问题思考"

AUTHORITY_STATUS_LABELS = {
    "verified": "已核验",
    "corroborated": "搜索交叉",
    "inferred": "基于常识推断（上限 8）",
    "source_missing": "未提供出处",
    "fetch_failed": "暂不可达",
    "mismatch": "未匹配",
    "rejected": "已拒绝",
}

AUTHORITY_REASON_DEFAULTS = {
    "entity_missing": "身份包没有可消歧实体",
    "entity_or_topic_mismatch": "搜索证据显示实体或主题不匹配",
    "insufficient_authority_evidence": "身份或主题信息存在，但核验证据不足",
    "search_unavailable": "搜索桥不可用，未产生可核验证据",
    "invalid_identity_packet": "身份包不符合受控契约",
    "invalid_search_observation": "搜索观察不符合受控契约",
    "authority_artifact_missing": "未提供权威核验产物",
    "invalid_artifact": "权威核验产物不合法",
    "model_knowledge_inferred": "只有常识推断，没有可核验出处",
}

AUTHORITY_REMEDIATION = {
    "entity_missing": "已尝试从来源标题、作者和发布机构提取身份；未臆造实体，权威分保持不可用",
    "entity_or_topic_mismatch": "已完成受限核验；不采用不匹配结果",
    "insufficient_authority_evidence": "已完成受限核验；不把证据不足当作权威",
    "search_unavailable": "已尝试搜索；服务恢复后可重试",
    "invalid_identity_packet": "已失败关闭；需重新生成身份包",
    "invalid_search_observation": "已失败关闭；需重新生成搜索观察",
    "authority_artifact_missing": "已阻止交付；需先完成权威核验",
    "invalid_artifact": "已失败关闭；需重新生成合法核验产物",
    "model_knowledge_inferred": "已使用知识兜底；按规则保持低置信度并限制上限",
}

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
    authority = dimensions.get("authority_score")
    problem = dimensions.get("problem_significance_score")
    fmt = lambda value: f"{value:.1f}" if isinstance(value, (int, float)) else "不可用"
    if isinstance(authority, (int, float)) and not isinstance(authority, bool):
        authority_text = fmt(authority)
    else:
        status = dimensions.get("authority_status") or result.get("authority_status")
        authority_text = AUTHORITY_STATUS_LABELS.get(status, "待核验")
    status = dimensions.get("authority_status") or result.get("authority_status")
    status_text = AUTHORITY_STATUS_LABELS.get(status, "待核验")
    suffix = "" if authority_text == status_text else f"（{status_text}）"
    return f"**权威性**  {authority_text}{suffix}\n**大问题思考**  {fmt(problem)}"


def _importance_label(result: dict) -> str:
    return IMPORTANCE_LABEL if isinstance((result.get("importance_dimensions") or {}).get("authority_score"), (int, float)) else "大问题思考"


def _compact(value: object, limit: int = 240) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _status_notes(result: dict) -> str:
    notes = []
    interest = result.get("interest_score")
    relevance_info = result.get("relevance_dimensions") or {}
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

    dimensions = result.get("importance_dimensions") or {}
    status = dimensions.get("authority_status") or result.get("authority_status")
    if status not in {"verified", "corroborated"}:
        code = dimensions.get("authority_reason_code") or result.get("authority_reason_code") or ""
        reason = _compact(dimensions.get("authority_rationale") or result.get("authority_rationale"))
        reason = reason or AUTHORITY_REASON_DEFAULTS.get(code, "权威核验证据不足")
        remediation = AUTHORITY_REMEDIATION.get(code, "已失败关闭；需补充证据后重试")
        observation = dimensions.get("search_observation") or {}
        attempt = ""
        if isinstance(observation, dict) and isinstance(observation.get("query_count"), int) and isinstance(observation.get("result_count"), int):
            attempt = f"；核验记录：搜索 {observation['query_count']} 次，得到 {observation['result_count']} 条结果"
        notes.append(f"权威性 {AUTHORITY_STATUS_LABELS.get(status, '待核验')}：{reason}；{remediation}{attempt}")

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


def render_card(result: dict, *, title: str, author: str, date: str, url: str, score_only: bool) -> dict:
    if urlparse(url).scheme not in {"http", "https"}:
        raise ValueError("url must use http or https")
    status = result.get("score_status")
    if status == "needs_relevance":
        raise ValueError("needs_relevance is internal and cannot be rendered")
    if status not in {"scored", "needs_full_text", "needs_review"}:
        raise ValueError(f"unsupported score_status: {status}")
    if status == "scored" and result.get("authority_reason_code") == "authority_artifact_missing":
        raise ValueError("authority verification artifact is required before rendering a score card")

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
                _metric(_importance_label(result), importance_value),
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
