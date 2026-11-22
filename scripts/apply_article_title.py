#!/usr/bin/env python3
"""Apply a browser-verified Chinese title to one isolated scoring run."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile

from article_titles import choose_article_title, has_han, is_article_title, normalize_title


def apply_title(run_dir: Path, source_url: str, title: str) -> dict:
    directory = run_dir.resolve(strict=True)
    if directory.name.startswith("readx-score.") is False or directory.parent != Path("/tmp").resolve():
        raise ValueError("run directory must be an isolated readx-score directory under /tmp")
    source = directory / "source.md"
    if source.is_symlink() or not source.is_file() or source.resolve().parent != directory:
        raise ValueError("unsafe source path")
    if source.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("source file exceeds size limit")
    new_title = choose_article_title([title], source_url)
    if not new_title or not has_han(new_title):
        raise ValueError("title must be a verified Chinese page title")
    content = source.read_text(encoding="utf-8")
    lines = content.splitlines(keepends=True)
    if not lines or not lines[0].startswith("# "):
        raise ValueError("source title heading missing")
    if source_url not in "".join(lines[:12]):
        raise ValueError("source URL does not match scoring run")
    old_title = normalize_title(lines[0][2:].strip())
    if old_title != new_title and is_article_title(old_title) and has_han(old_title):
        raise ValueError("source already has a different Chinese title")
    ending = "\r\n" if lines[0].endswith("\r\n") else "\n" if lines[0].endswith("\n") else ""
    lines[0] = f"# {new_title}{ending}"
    updated = "".join(lines).encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=".title-", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(updated)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_name, source)
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    check = source.read_text(encoding="utf-8")
    if not check.splitlines() or check.splitlines()[0] != f"# {new_title}":
        raise RuntimeError("title update readback mismatch")
    return {"status": "updated", "source": str(source), "title": new_title,
            "title_status": "ready", "title_language": "zh"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--title", required=True)
    args = parser.parse_args()
    print(json.dumps(apply_title(args.run_dir, args.url, args.title), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError, ValueError, RuntimeError) as error:
        raise SystemExit(str(error))
