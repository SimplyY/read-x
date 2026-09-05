#!/usr/bin/env python3
"""Build the small public identity packet used by the authority search bridge."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlparse

NAME_RE = re.compile(r"\b[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){1,3}\b")
CN_NAME_RE = re.compile(r"(?:对话|采访|访谈)\s*([\u4e00-\u9fff]{2,6}(?:[·•][\u4e00-\u9fff]{1,6})?)")
CN_RELATION_NAME_RE = re.compile(r"(?<![\u4e00-\u9fff])(?:与|和)\s*([\u4e00-\u9fff]{2,6}(?:[·•][\u4e00-\u9fff]{1,6})?)")
CN_TITLE_NAME_RE = re.compile(r"^\s*([\u4e00-\u9fff]{2,6})\s*[：:]")
SOURCE_URL_RE = re.compile(r"^>\s*原始出处候选\s*[:：]\s*(https?://\S+)")
ALIASES = {
    "比尔·盖茨": ["Bill Gates"], "比尔盖茨": ["Bill Gates"],
    "林毅夫": ["Justin Yifu Lin", "Yifu Lin"],
}
GENERIC_SOURCE_LABELS = {"匿名", "佚名", "未知", "来源不明"}


def _metadata(source: str) -> tuple[str, str, str, list[str]]:
    lines = source.splitlines()
    title = lines[0][2:].strip() if lines and lines[0].startswith("# ") else ""
    author = publisher = ""
    candidates: list[str] = []
    for line in lines[:20]:
        match = re.match(r"^>\s*(公众号|作者|发布机构|发布时间)\s*[:：]\s*(.+)$", line)
        if match:
            if match.group(1) in {"公众号", "作者"}:
                author = match.group(2).strip()
            elif match.group(1) == "发布机构":
                publisher = match.group(2).strip()
        source_match = SOURCE_URL_RE.match(line)
        if source_match and urlparse(source_match.group(1)).scheme in {"http", "https"}:
            candidates.append(source_match.group(1).rstrip(".,;:!?，。；：！？"))
    return title, author, publisher, candidates


def build_identity(source: str, quality: dict | None = None) -> dict:
    title, author, publisher, candidates = _metadata(source)
    entities: list[dict] = []
    seen: set[str] = set()
    values = NAME_RE.findall(title) + NAME_RE.findall(author)
    values += CN_TITLE_NAME_RE.findall(title)
    if re.fullmatch(r"[\u4e00-\u9fff]{2,6}(?:[·•][\u4e00-\u9fff]{1,6})?", author):
        values.append(author)
    values += [match.group(1) for match in CN_NAME_RE.finditer(title)]
    values += [match.group(1) for match in CN_RELATION_NAME_RE.finditer(title)]
    if publisher:
        values.append(publisher)
    for value in values:
        value = value.strip()
        if value and value not in GENERIC_SOURCE_LABELS and value not in seen:
            seen.add(value)
            entities.append({"type": "organization" if value == publisher else "person", "name": value, "aliases": ALIASES.get(value, [])})
    if author and author not in seen and author not in GENERIC_SOURCE_LABELS:
        entities.append({"type": "organization", "name": author, "aliases": ALIASES.get(author, [])})
    domain = (quality or {}).get("detected_domain") or {}
    topic = {"primary": domain.get("primary", ""), "secondary": domain.get("secondary", "")}
    event_hint = title.split(":", 1)[-1].strip() if ":" in title else title
    return {
        "schema_version": "1", "title": title, "author": author, "publisher": publisher,
        "entities": entities[:8], "event_hint": event_hint[:200], "topic": topic,
        "source_candidates": candidates[:4],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--quality", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    quality = json.loads(args.quality.read_text(encoding="utf-8")) if args.quality else None
    result = build_identity(args.source.read_text(encoding="utf-8"), quality)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
