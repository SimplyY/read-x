#!/usr/bin/env python3
"""Compatibility entrypoint for the shared Feishu Markdown renderer."""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path


_CODEX_HOME = Path(os.environ["CODEX_HOME"]) if os.environ.get("CODEX_HOME") else Path.home() / ".codex"
_SHARED_PATH = _CODEX_HOME / "skills" / "feishu-doc-renderer" / "scripts" / "markdown_to_feishu_xml.py"
if not _SHARED_PATH.is_file():
    raise RuntimeError(f"shared renderer is not installed: {_SHARED_PATH}")

_SPEC = importlib.util.spec_from_file_location("feishu_doc_renderer_shared", _SHARED_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"cannot load shared renderer: {_SHARED_PATH}")
_SHARED = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_SHARED)

for _name in (
    "MAX_TABLE_ROWS",
    "MAX_TABLE_COLUMNS",
    "MAX_PARAGRAPH_CHARS",
    "_inline",
    "_split_rendered",
    "_rendered_paragraph",
    "_paragraph_text",
    "_table_row",
    "_table",
    "_indent_width",
    "_is_ordered",
    "_render_list",
    "_fence_closes",
    "_heading_policy",
    "_atomic_write",
):
    globals()[_name] = getattr(_SHARED, _name)


def render_markdown(markdown: str, *, title: str, source_url: str, conversation_url: str) -> str:
    """Keep read-x's provenance notice while delegating layout to the shared layer."""
    xml = _SHARED.render_markdown(
        markdown,
        title=title,
        source_url=source_url,
        conversation_url=conversation_url,
    )
    blocks = xml.rstrip("\n").split("\n")
    insertion = 1
    if len(blocks) > 1 and blocks[1].startswith("<p>溯源："):
        insertion = 2
    blocks.insert(
        insertion,
        '<callout emoji="⭐" background-color="light-yellow" border-color="yellow"><p>以下内容由 DeepSeek 基于完整原文生成，并按芒格之魂提示词组织；事实、推断与未知应分别核对。</p></callout>',
    )
    return "\n".join(blocks) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--title", required=True)
    parser.add_argument("--source-url", default="")
    parser.add_argument("--conversation-url", default="")
    args = parser.parse_args()
    _atomic_write(
        args.output,
        render_markdown(
            args.input.read_text(encoding="utf-8"),
            title=args.title,
            source_url=args.source_url,
            conversation_url=args.conversation_url,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
