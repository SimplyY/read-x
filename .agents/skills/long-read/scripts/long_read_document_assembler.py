#!/usr/bin/env python3
"""Assemble verified long-read branch originals into a validated local candidate."""
from __future__ import annotations

import re
from urllib.parse import urlparse

import markdown_to_feishu_xml
import validate_output
import long_read_document_supplement as supplement


SECTION_ALIASES = {
    "这篇文章真正的核心": "core",
    "文章真正的核心": "core",
    "基石/边缘/暗流": "pillars",
    "作者为什么要写这篇文章": "author_why",
    "作者心中的结构": "structure",
    "与作者对话": "dialogue",
    "最值得深读的论证": "deep_reading",
    "最值得深读之处": "deep_reading",
    "引导追问": "followups",
}
LJG_TITLES = {
    "ljg-learn": "思维解剖",
    "ljg-qa": "信息提问机",
    "ljg-roundtable": "圆桌交锋",
    "ljg-think": "追本之箭",
    "ljg-word": "英语词语深读",
    "ljg-writes": "层层推进的写作",
}
HEADING_RE = re.compile(r"(?m)^[ \t]{0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")


def _normalize_heading(value: str) -> str:
    value = re.sub(r"^\s*\d+\s*[.)、．]\s*", "", value)
    value = re.sub(r"[*_`]", "", value)
    value = re.sub(r"\s+", "", value)
    return value.replace("／", "/").replace("、", "/")


def _demote_headings(markdown: str, *, by: int) -> str:
    def replace(match: re.Match) -> str:
        level = min(6, len(match.group(1)) + by)
        return "#" * level + " " + match.group(2)
    return HEADING_RE.sub(replace, markdown)


def parse_article_decode(markdown: str, *, title: str) -> dict:
    """Split only Skill-defined top-level sections; preserve each section body."""
    if not isinstance(markdown, str) or not markdown.strip():
        raise ValueError("article-decode output is empty")
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    # Drop a duplicated source-title heading only; it is represented by <title>.
    lines = [
        line for line in lines
        if not ((match := HEADING_RE.match(line)) and match.group(1) == "#"
                and match.group(2).strip() == title.strip())
    ]

    starts: list[tuple[int, str, str]] = []
    preamble = []
    for index, line in enumerate(lines):
        match = HEADING_RE.match(line)
        if not match or match.group(1) != "#":
            continue
        normalized = _normalize_heading(match.group(2))
        key = SECTION_ALIASES.get(normalized)
        if key:
            starts.append((index, key, match.group(2).strip()))

    if not starts:
        raise ValueError("article-decode is missing Skill-defined top-level section headings")
    keys = [item[1] for item in starts]
    if len(keys) != len(set(keys)):
        raise ValueError("article-decode contains duplicate Skill-defined section headings")
    if "core" not in keys or "pillars" not in keys or "structure" not in keys:
        raise ValueError("article-decode must include core, pillars, and author-structure sections")
    if keys.index("core") > keys.index("pillars") or keys.index("pillars") > keys.index("structure"):
        raise ValueError("article-decode required sections are out of order")

    sections = {}
    for position, (start, key, original_heading) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        body = "\n".join(lines[start + 1:end]).strip()
        if not body:
            raise ValueError(f"article-decode section is empty: {original_heading}")
        sections[key] = body
    first = starts[0][0]
    preamble = "\n".join(lines[:first]).strip()
    # Unrecognized H1 headings remain in the source segment; make them nested
    # when the body is placed under the program-owned section headings.
    sections = {key: _demote_headings(body, by=1) for key, body in sections.items()}
    return {"preamble": _demote_headings(preamble, by=1), "sections": sections}


def _score_text(value, label: str, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"scoring result requires numeric {field}")
    if isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")}):
        raise ValueError(f"scoring result contains invalid {field}")
    if not isinstance(label, str) or not label.strip():
        raise ValueError(f"scoring result requires {field} label")
    rendered = f"{value:g}" if isinstance(value, float) else str(value)
    return f"{rendered}/10 · {label.strip()}"


def validate_scoring_fields(scoring: dict) -> None:
    """Validate the score fields required by the assembled document."""
    for value_key, label_key, field in (
        ("quality_score", "quality_label", "quality_score"),
        ("relevance_score", "priority_label", "relevance_score"),
        ("interest_score", "interest_label", "interest_score"),
    ):
        _score_text(scoring.get(value_key), scoring.get(label_key), field)


def _validate_url(value: str, label: str) -> str:
    parsed = urlparse(value or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{label} must be an absolute http(s) URL")
    return value


def _quote_block(value: str) -> str:
    return "\n".join("> " + line for line in value.splitlines())


def _append_section(chunks: list[str], heading: str, body: str) -> None:
    chunks.extend((f"# {heading}", body.strip(), ""))


def assemble_markdown(*, evidence: dict, scoring: dict, article_decode: str,
                      text_analyses: list[tuple[str, str]], supplement_markdown: str,
                      source: str, munger_markdown: str | None = None) -> str:
    if not isinstance(evidence, dict) or not isinstance(scoring, dict):
        raise ValueError("document assembly requires Evidence and scoring objects")
    metadata = evidence.get("metadata")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("title"), str) or not metadata["title"].strip():
        raise ValueError("document assembly requires the Evidence title")
    source_url = _validate_url(metadata.get("source_url"), "original source URL")
    if scoring.get("score_status") != "scored" or scoring.get("route") != "long_read":
        raise ValueError("document assembly requires a scored long_read route")
    validate_scoring_fields(scoring)
    title = metadata["title"].strip()
    evidence_findings = validate_output.check_structure(evidence) + validate_output.check_quotes_substring(evidence, source)
    if evidence_findings:
        raise ValueError("document assembly Evidence is invalid: " + "; ".join(evidence_findings))
    questions_fragment = supplement.validate_supplement(supplement_markdown)
    parsed = parse_article_decode(article_decode, title=title)
    sections = parsed["sections"]
    if not isinstance(text_analyses, list):
        raise ValueError("text analyses must be an ordered list")

    chunks = [
        "# 评分",
        "| 维度 | 判断 |",
        "| --- | --- |",
        f"| 质量 | {_score_text(scoring.get('quality_score'), scoring.get('quality_label'), 'quality_score')} |",
        f"| 相关性 | {_score_text(scoring.get('relevance_score'), scoring.get('priority_label'), 'relevance_score')} |",
        f"| 兴趣 | {_score_text(scoring.get('interest_score'), scoring.get('interest_label'), 'interest_score')} |",
        "",
    ]
    quotes = evidence.get("quotes")
    if not isinstance(quotes, list):
        raise ValueError("Evidence quotes must be a list")
    if quotes:
        chunks.extend(("# 原文金句", "\n\n".join(_quote_block(item) for item in quotes), ""))

    supplement_lines = questions_fragment.splitlines()
    core_heading = supplement_lines.index("### 核心结论")
    question_heading = supplement_lines.index("### 问题")
    conclusion_lines = [line.strip() for line in supplement_lines[core_heading + 1:question_heading] if line.strip()]
    if len(conclusion_lines) != 1:
        raise ValueError("document supplement must contain exactly one core conclusion")
    conclusion = conclusion_lines[0]
    if conclusion.startswith(("文章最重要的一刀：", "核心结论：")):
        conclusion = conclusion.split("：", 1)[1].strip()
    chunks.extend((":::readx-core-conclusion", conclusion, ":::", ""))

    _append_section(chunks, "文章真正的核心", "\n\n".join(part for part in (parsed["preamble"], sections["core"]) if part))
    _append_section(chunks, "基石 / 边缘 / 暗流", sections["pillars"])
    for key, heading in (("author_why", "作者为什么要写这篇文章"), ("structure", "作者心中的结构")):
        if key in sections:
            _append_section(chunks, heading, sections[key])
    chunks.append(supplement.render_research_section(questions_fragment).rstrip())
    chunks.append("")
    dialogue = sections.get("dialogue", "目前没有可由原文确认、且能独立增加判断的作者对话点。")
    _append_section(chunks, "与作者对话", dialogue)
    if "deep_reading" in sections:
        _append_section(chunks, "最值得深读之处", sections["deep_reading"])
    if "followups" in sections:
        _append_section(chunks, "解码追问", sections["followups"])
    if munger_markdown is not None:
        if not isinstance(munger_markdown, str) or not munger_markdown.strip():
            raise ValueError("Munger branch is marked successful but has no content")
        _append_section(chunks, "芒格洞察", _demote_headings(munger_markdown.strip(), by=1))
    if text_analyses:
        chunks.extend(("# 附录：独立深度分析", ""))
        seen = set()
        for skill, body in text_analyses:
            if skill not in LJG_TITLES or skill in seen or not isinstance(body, str) or not body.strip():
                raise ValueError("text analysis list contains an unsupported, duplicated, or empty branch")
            seen.add(skill)
            chunks.extend((f"## {skill} · {LJG_TITLES[skill]}", _demote_headings(body.strip(), by=2), ""))
    facts = evidence.get("facts")
    if not isinstance(facts, list):
        raise ValueError("Evidence facts must be a list")
    if facts:
        if any(not isinstance(item, str) or not item.strip() for item in facts):
            raise ValueError("Evidence facts must contain non-empty source strings")
        chunks.extend(("# 必要事实", "\n".join(f"- {item}" for item in facts), ""))
    return "\n".join(chunks).rstrip() + "\n"


def render_candidate(markdown: str, *, title: str, source_url: str, conversation_url: str) -> str:
    xml = markdown_to_feishu_xml.render_markdown(
        markdown, title=title, source_url=source_url, conversation_url=conversation_url,
    )
    findings = validate_output.check_document_xml(xml)
    if findings:
        raise ValueError("candidate failed long-read validation: " + "; ".join(findings))
    return xml
