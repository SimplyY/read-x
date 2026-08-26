#!/usr/bin/env python3
"""Render the temporary canonical Markdown into safe Feishu document XML."""
from __future__ import annotations

import argparse
import html
import re
import tempfile
from pathlib import Path


MAX_TABLE_ROWS = 20
MAX_TABLE_COLUMNS = 8
MAX_PARAGRAPH_CHARS = 100
FENCE = "```"


def _inline(value: str) -> str:
    value = html.escape(value, quote=False)
    placeholders: list[str] = []

    def hold(rendered: str) -> str:
        placeholders.append(rendered)
        return f"\x00{len(placeholders) - 1}\x00"

    value = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", lambda m: hold(f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>'), value)
    value = re.sub(r"`([^`]+)`", lambda m: hold(f"<code>{m.group(1)}</code>"), value)
    value = re.sub(r"\*\*([^*]+)\*\*|__([^_]+)__", lambda m: f"<b>{m.group(1) or m.group(2)}</b>", value)
    value = re.sub(r"~~([^~]+)~~", r"<del>\1</del>", value)
    value = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)|(?<!_)_([^_]+)_(?!_)", lambda m: f"<em>{m.group(1) or m.group(2)}</em>", value)
    for index, rendered in enumerate(placeholders):
        value = value.replace(f"\x00{index}\x00", rendered)
    return value


def _split_paragraph(value: str) -> list[str]:
    value = value.strip()
    if not value:
        return []
    return [value[index:index + MAX_PARAGRAPH_CHARS] for index in range(0, len(value), MAX_PARAGRAPH_CHARS)]


def _table(lines: list[str]) -> str:
    rows = []
    for line in lines:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and not all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            rows.append(cells)
    width = max((len(row) for row in rows), default=0)
    if not rows or width > MAX_TABLE_COLUMNS or len(rows) > MAX_TABLE_ROWS:
        raw = html.escape("\n".join(lines), quote=False)
        return '<p>表格超出原生表格限制，保留 Markdown 原文：</p><pre><code>' + raw + '</code></pre>'
    head = rows[0] + [""] * (width - len(rows[0]))
    header = "<thead><tr>" + "".join(f"<th background-color=\"light-gray\">{_inline(cell)}</th>" for cell in head) + "</tr></thead>"
    body = []
    for row in rows[1:]:
        cells = row + [""] * (width - len(row))
        body.append("<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in cells) + "</tr>")
    return "<table>" + header + "<tbody>" + "".join(body) + "</tbody></table>"


def render_markdown(markdown: str, *, title: str, source_url: str, conversation_url: str) -> str:
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    output = [f"<title>{html.escape(title, quote=False)}</title>"]
    if source_url:
        output.append(f'<p>原文链接：<a href="{html.escape(source_url, quote=True)}">{html.escape(source_url, quote=False)}</a></p>')
    if conversation_url:
        output.append(f'<p>ChatGPT 会话：<a href="{html.escape(conversation_url, quote=True)}">{html.escape(conversation_url, quote=False)}</a></p>')
    output.append('<callout background-color="light-yellow"><p>以下内容由 ChatGPT 基于完整原文生成，并按芒格之魂提示词组织；事实、推断与未知应分别核对。</p></callout>')

    index = 0
    paragraph: list[str] = []

    def flush() -> None:
        if not paragraph:
            return
        raw = " ".join(item.strip() for item in paragraph).strip()
        paragraph.clear()
        for chunk in _split_paragraph(raw):
            output.append(f"<p>{_inline(chunk)}</p>")

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            flush(); index += 1; continue
        if stripped.startswith(FENCE):
            flush(); index += 1; code = []
            while index < len(lines) and not lines[index].strip().startswith(FENCE):
                code.append(lines[index]); index += 1
            if index < len(lines): index += 1
            output.append("<pre><code>" + html.escape("\n".join(code), quote=False) + "</code></pre>")
            continue
        heading = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            flush(); output.append(f"<h{len(heading.group(1))}>{_inline(heading.group(2))}</h{len(heading.group(1))}>"); index += 1; continue
        if line.lstrip().startswith("|") and index + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$", lines[index + 1]):
            flush(); table_lines = [line, lines[index + 1]]; index += 2
            while index < len(lines) and lines[index].lstrip().startswith("|"):
                table_lines.append(lines[index]); index += 1
            output.append(_table(table_lines)); continue
        if re.match(r"^\s*(?:[-*+] |\d+[.] )", line):
            flush(); ordered = bool(re.match(r"^\s*\d+[.] ", line)); items = []
            while index < len(lines) and re.match(r"^\s*(?:[-*+] |\d+[.] )", lines[index]):
                items.append(re.sub(r"^\s*(?:[-*+] |\d+[.] )", "", lines[index]).strip()); index += 1
            tag = "ol" if ordered else "ul"
            output.append(f"<{tag}>" + "".join(f"<li>{_inline(item)}</li>" for item in items) + f"</{tag}>"); continue
        if stripped.startswith(">"):
            flush(); quotes = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quotes.append(re.sub(r"^\s*>\s?", "", lines[index])); index += 1
            output.append("<blockquote>" + "".join(f"<p>{_inline(chunk)}</p>" for quote in quotes for chunk in _split_paragraph(quote)) + "</blockquote>"); continue
        paragraph.append(line); index += 1
    flush()
    return "\n".join(output) + "\n"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(text); handle.flush()
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--title", required=True)
    parser.add_argument("--source-url", default="")
    parser.add_argument("--conversation-url", default="")
    args = parser.parse_args()
    _atomic_write(args.output, render_markdown(args.input.read_text(encoding="utf-8"), title=args.title, source_url=args.source_url, conversation_url=args.conversation_url))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
