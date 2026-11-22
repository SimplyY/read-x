#!/usr/bin/env python3
"""Safely repair verified Read-X titles in the article Base table."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from archive_reading_context import BASE, TABLE, LarkBase
from article_titles import choose_article_title, has_han
from reading_intake import normalize_url


BOT_OPERATOR = "codex 小助手"


def read_record(base: LarkBase, record_id: str) -> dict:
    offset = 0
    while True:
        page = base.call("+record-list", "--limit", "200", "--offset", str(offset))
        names, rows, ids = page["fields"], page["data"], page["record_id_list"]
        if len(rows) != len(ids):
            raise ValueError("invalid Base record page")
        for rid, values in zip(ids, rows):
            if len(values) != len(names):
                raise ValueError("invalid Base row")
            if rid == record_id:
                return {"id": rid, "fields": dict(zip(names, values))}
        if not page.get("has_more"):
            raise ValueError("Base record no longer exists")
        if not rows:
            raise ValueError("Base pagination stalled")
        offset += len(rows)


def read_history(base: LarkBase, record_id: str) -> list[dict]:
    events = []
    cursor = None
    while True:
        args = ["--record-id", record_id, "--page-size", "50"]
        if cursor is not None:
            args += ["--max-version", str(cursor)]
        page = base.call("+record-history-list", *args)
        events.extend(page.get("items", []))
        if not page.get("has_more"):
            return events
        cursor = page.get("next_max_version")
        if cursor is None:
            raise ValueError("Base history pagination stalled")


def verify_machine_owned_title(base: LarkBase, record_id: str) -> None:
    events = read_history(base, record_id)
    creates = [event for event in events if event.get("activity_type") == "create"]
    if len(creates) != 1 or creates[0].get("operator") != BOT_OPERATOR:
        raise ValueError("record creator is not the verified Read-X bot")
    for event in events:
        changed_title = any(change.get("field_name") == "标题"
                            for change in event.get("field_changes") or [])
        if changed_title and event.get("operator") != BOT_OPERATOR:
            raise ValueError("title has a non-bot edit; preserve for manual review")


def repair_one(base: LarkBase, target: dict, *, execute: bool) -> dict:
    record_id = str(target.get("record_id") or "")
    url = str(target.get("url") or "")
    expected = str(target.get("expected_title") or "")
    proposed = choose_article_title([target.get("title")], url)
    if not record_id or not url or not expected or not proposed or not has_han(proposed):
        raise ValueError("target requires record_id, url, expected_title, and a Chinese title")
    normalize_url(url)
    record = read_record(base, record_id)
    fields = record["fields"]
    current_source = fields.get("原文链接")
    if not current_source or normalize_url(current_source) != normalize_url(url):
        raise ValueError("record source URL changed")
    if fields.get("标题") == proposed:
        return {"record_id": record_id, "status": "already_correct", "title": proposed}
    if fields.get("标题") != expected:
        raise ValueError("current title differs from the audited value")
    verify_machine_owned_title(base, record_id)
    if not execute:
        return {"record_id": record_id, "status": "dry_run", "title": proposed}
    base.call("+record-upsert", "--record-id", record_id,
              "--json", json.dumps({"标题": proposed}, ensure_ascii=False))
    for attempt in range(6):
        check = read_record(base, record_id)
        if check["fields"].get("标题") == proposed:
            break
        if attempt < 5:
            time.sleep(1)
    else:
        raise RuntimeError(f"Base title readback unresolved for record {record_id}")
    return {"record_id": record_id, "status": "updated", "title": proposed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.targets.is_symlink() or not args.targets.is_file() or args.targets.stat().st_size > 100_000:
        raise ValueError("unsafe title repair target file")
    targets = json.loads(args.targets.read_text(encoding="utf-8"))
    if not isinstance(targets, list) or len(targets) > 100:
        raise ValueError("invalid title repair target list")
    base = LarkBase(BASE, TABLE)
    base.verify_identity("cli_aab030a473f8dcd2")
    results = []
    for target in targets:
        if not isinstance(target, dict):
            raise ValueError("invalid title repair target")
        results.append(repair_one(base, target, execute=args.execute))
        time.sleep(0.2)
    print(json.dumps({"status": "updated" if args.execute else "dry_run",
                      "count": len(results), "results": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "needs_review", "reason": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
