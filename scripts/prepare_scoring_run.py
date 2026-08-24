#!/usr/bin/env python3
"""Fetch one WeChat article and prepare the deterministic scoring inputs.

While the article is being fetched, Base configuration is pulled in parallel
so it's ready by the time content_scoring.py runs. If the Base fetch fails,
the caller falls back to scoring-policy.json and the result records the reason.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import prepare_anchor_view


SAVED_PREFIX = "✅ 已保存: "
METADATA = re.compile(r"^>\s*(公众号|发布时间|原文链接)\s*[:：]\s*(.+)$")
BLIND_PART_BYTES = 20_000
SCRIPTS_DIR = Path(__file__).parent


def saved_path(output: str) -> Path:
    matches = [line[len(SAVED_PREFIX):].strip() for line in output.splitlines() if line.startswith(SAVED_PREFIX)]
    if len(matches) != 1:
        raise ValueError("fetch output must contain exactly one saved path")
    path = Path(matches[0])
    if not path.is_file():
        raise ValueError("saved article path does not exist")
    return path


def article_metadata(source: str) -> dict[str, str]:
    lines = source.splitlines()
    title = lines[0][2:].strip() if lines and lines[0].startswith("# ") else ""
    values = {}
    for line in lines[:12]:
        match = METADATA.match(line)
        if match:
            values[match.group(1)] = match.group(2).strip()
    return {"title": title, "author": values.get("公众号", ""), "date": values.get("发布时间", "")}


def blind_parts(path: Path, text: str) -> list[str]:
    """Keep each model-visible tool result small without changing article text."""
    if len(text.encode("utf-8")) <= BLIND_PART_BYTES:
        return [str(path)]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for char in text:
        char_size = len(char.encode("utf-8"))
        if current and size + char_size > BLIND_PART_BYTES:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += char_size
    if current:
        chunks.append("".join(current))
    paths = []
    for index, chunk in enumerate(chunks, 1):
        part = path.with_name(f"blind-source.part-{index:02d}.md")
        part.write_text(chunk, encoding="utf-8")
        paths.append(str(part))
    return paths


def _fetch_base_config_async(run_dir: Path) -> threading.Thread:
    """Start a daemon thread to fetch Base config in parallel.

    Returns the thread; thread.result_path is set to the output file path.
    If the fetch fails, result_path remains None and the caller falls back to policy.json.
    """
    config_path = run_dir / "base-config.json"

    def _fetch():
        try:
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "fetch_base_config.py"),
                 "--output", str(config_path)],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip()[:200] if proc.stderr else "unknown error")
        except Exception as exc:
            thread.error = str(exc)[:200]  # type: ignore[attr-defined]

    thread = threading.Thread(target=_fetch, daemon=True)
    thread.result_path = config_path  # type: ignore[attr-defined]
    thread.error = None  # type: ignore[attr-defined]
    thread.start()
    return thread


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    args = parser.parse_args()

    run_dir = Path(tempfile.mkdtemp(prefix="readx-score.", dir="/tmp"))

    # Start Base config fetch in parallel with article fetch
    config_thread = _fetch_base_config_async(run_dir)

    fetched_path = run_dir / "fetched.md"
    fetched = subprocess.run(
        [str(SCRIPTS_DIR / "wx_fast.py"), args.url, "--output", str(fetched_path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    (run_dir / "fetch.log").write_text(fetched.stdout, encoding="utf-8")
    if fetched.returncode:
        raise SystemExit(f"fetch failed with exit code {fetched.returncode}")

    source_path = run_dir / "source.md"
    shutil.copyfile(saved_path(fetched.stdout), source_path)
    source = source_path.read_text(encoding="utf-8")
    if args.url not in source:
        raise SystemExit("source URL mismatch")

    blind_path = run_dir / "blind-source.md"
    blind_source = prepare_anchor_view.blind_article_source(source)
    blind_path.write_text(blind_source, encoding="utf-8")

    # Wait for Base config fetch to complete (it should already be done by now)
    config_thread.join()

    # Check if Base config was successfully fetched
    base_config_path = None
    config_error = getattr(config_thread, "error", None)
    if not config_error and hasattr(config_thread, "result_path") and config_thread.result_path.is_file():
        # Validate the config is valid JSON before passing it through
        try:
            json.loads(config_thread.result_path.read_text(encoding="utf-8"))
            base_config_path = str(config_thread.result_path)
        except (json.JSONDecodeError, OSError) as exc:
            config_error = str(exc)[:200]

    result = {
        "run_dir": str(run_dir),
        "source": str(source_path),
        "blind_source": str(blind_path),
        "blind_source_parts": blind_parts(blind_path, blind_source),
        "url": args.url,
        "policy_source": "base" if base_config_path else "local",
        **article_metadata(source),
    }
    if base_config_path:
        result["base_config"] = base_config_path
    elif config_error:
        result["config_error"] = config_error
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
