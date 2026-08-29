#!/usr/bin/env python3
"""Read-only original-source check for the v3.16 importance artifact."""
from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

SCORE_VERSION = "3.16"
_URL = re.compile(r"https?://[^\s<>()\[\]{}\"']+")
_PUBLISHER_ALIASES = {
    "麻省理工科技评论": ("MIT Technology Review", "Technology Review"),
    "麻省理工学院技术评论": ("MIT Technology Review", "Technology Review"),
}


def _visible_text(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def _clean_url(value: str) -> str:
    return value.rstrip(".,;:!?，。；：！？、）》」』】")


def original_url_from_source(source: str) -> str | None:
    """Find an explicitly labelled non-WeChat original URL without retaining article text."""
    lines = source.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^\s*>?\s*原文链接\s*[:：]?\s*(.*)$", line)
        if not match:
            continue
        candidates = [match.group(1)] + lines[index + 1:index + 4]
        for candidate in candidates:
            for raw_url in _URL.findall(candidate):
                url = _clean_url(raw_url)
                host = (urlparse(url).hostname or "").lower()
                if host and host != "mp.weixin.qq.com" and not host.endswith(".mp.weixin.qq.com"):
                    return url
    return None


def source_label_aliases(source: str, checks: list[tuple[str, str]]) -> dict[str, list[str]]:
    """Add only explicit parenthetical aliases and a tiny publisher translation map."""
    aliases: dict[str, list[str]] = {}
    for kind, label in checks:
        values = []
        for match in re.finditer(rf"{re.escape(label)}\s*[（(]([^（）()\n]{{2,80}})[）)]", source):
            values.append(match.group(1).strip())
        if kind == "publisher":
            values.extend(_PUBLISHER_ALIASES.get(re.sub(r"\s+", "", label), ()))
        if values:
            aliases[kind] = list(dict.fromkeys(values))
    return aliases


def verify(
    url: str,
    checks: list[tuple[str, str]],
    timeout: float = 20,
    label_aliases: dict[str, list[str]] | None = None,
) -> dict:
    """Fetch only the supplied URL; return evidence without retaining page content."""
    request = urllib.request.Request(url, headers={"User-Agent": "read-x-authority-check/3.16"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        text = _visible_text(response.read())
        status = getattr(response, "status", None) or 200
    haystack = text.lower()
    label_aliases = label_aliases or {}
    evidence = [
        {
            "kind": kind,
            "label": label,
            "url": url,
            "verified": status == 200 and any(candidate.lower() in haystack for candidate in [label, *label_aliases.get(kind, [])]),
        }
        for kind, label in checks
    ]
    verified = [item for item in evidence if item["verified"]]
    primary = any(item["kind"] in {"interview", "primary_source"} for item in verified)
    authority = 9.0 if len(verified) >= 2 and primary else 8.0 if verified else 4.0
    return {
        "schema_version": SCORE_VERSION,
        "authority_score": authority,
        "evidence": evidence,
        "confidence": "high" if authority >= 9 else "medium" if verified else "unavailable",
        "rationale": "原始出处可访问，且出版方/一手材料关键词核验完成" if verified else "原始出处未完成可核验匹配",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?")
    parser.add_argument("--source", type=Path, help="source.md containing an explicitly labelled original URL")
    parser.add_argument("--publisher", help="expected publisher text")
    parser.add_argument("--interview", help="expected interview subject or first-party text")
    parser.add_argument("--primary-source", help="expected first-party source text")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    url = args.url
    source_text = ""
    if not url and args.source:
        try:
            source_text = args.source.read_text(encoding="utf-8")
            url = original_url_from_source(source_text)
        except OSError:
            url = None
    checks = [("publisher", args.publisher), ("interview", args.interview), ("primary_source", args.primary_source)]
    try:
        if not url:
            raise ValueError("原始出处链接不可用")
        checks = [(kind, label) for kind, label in checks if label]
        result = verify(url, checks, label_aliases=source_label_aliases(source_text, checks))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        result = {
            "schema_version": SCORE_VERSION,
            "authority_score": 4.0,
            "evidence": [],
            "confidence": "unavailable",
            "rationale": f"原始出处核验不可用：{type(exc).__name__}",
        }
    except ValueError as exc:
        result = {
            "schema_version": SCORE_VERSION,
            "authority_score": 4.0,
            "evidence": [],
            "confidence": "unavailable",
            "rationale": str(exc),
        }
    args.output.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
