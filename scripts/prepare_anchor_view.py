#!/usr/bin/env python3
"""Build the anonymous, leave-one-out anchor view consumed by the scorer."""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path
from urllib.parse import urlparse


ANCHORS = Path(__file__).parents[1] / ".agents/skills/content-scoring/references/anchors.md"
HEADING = re.compile(r"^## A\d+｜.*$", re.MULTILINE)


def article_key(url: str) -> str:
    parsed = urlparse(url.strip())
    parts = [part for part in parsed.path.split("/") if part]
    return parts[-1] if parsed.netloc.endswith("weixin.qq.com") and len(parts) >= 2 and parts[-2] == "s" else url.strip()


def build_view(target_url: str, source: str) -> tuple[str, bool]:
    source = source.split("\n## 使用方式", 1)[0]
    starts = [match.start() for match in HEADING.finditer(source)]
    blocks = [source[start:end].strip() for start, end in zip(starts, starts[1:] + [len(source)])]
    target = article_key(target_url)
    block_keys = []
    for block in blocks:
        match = re.search(r"^- URL：(\S+)\s*$", block, re.MULTILINE)
        block_keys.append(article_key(match.group(1)) if match else "")
    matching_index = next((index for index, key in enumerate(block_keys) if key == target), None)
    omitted_index = matching_index if matching_index is not None else hashlib.sha256(target.encode("utf-8")).digest()[0] % len(blocks)
    kept = []
    excluded = matching_index is not None

    for index, block in enumerate(blocks):
        if index == omitted_index:
            continue
        lines = []
        for line in block.splitlines()[1:]:
            if line.startswith("- URL：") or line.startswith("- 用户区间：") or line.startswith("- 相邻区别："):
                continue
            lines.append(line)
        anonymous = "\n".join(lines).strip()
        anonymous = re.sub(r"\n*核心主张：.*?\n+定级理由与上限：", "\n\n定级理由与上限：", anonymous, flags=re.DOTALL)
        anonymous = re.sub(r"（C\d+(?:[～、]\s*C?\d+)*）", "", anonymous)
        kept.append(anonymous)

    rendered = [
        "# 匿名质量锚点",
        "",
        "以下编号仅在本次评分中有效；标题、URL、目标分和原始编号均未提供。只比较论证结构与四维数值。",
    ]
    for index, block in enumerate(kept, 1):
        rendered.extend(["", f"## A{index}", "", block])
    return "\n".join(rendered).rstrip() + "\n", excluded


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target_url")
    parser.add_argument("--source", type=Path, default=ANCHORS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    view, excluded = build_view(args.target_url, args.source.read_text(encoding="utf-8"))
    if args.output:
        args.output.write_text(view, encoding="utf-8")
    else:
        sys.stdout.write(view)
    print(f"anchor-view: count={view.count(chr(10) + '## A')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
