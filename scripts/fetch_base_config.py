#!/usr/bin/env python3
"""Read the live Read-X Base policy into one run-local JSON snapshot."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from policy_sync import DEFAULT_BASE_TOKEN, DEFAULT_TABLE_ID, _dump, fetch_records, rebuild_policy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--base-token", default=DEFAULT_BASE_TOKEN)
    parser.add_argument("--table-id", default=DEFAULT_TABLE_ID)
    args = parser.parse_args()
    try:
        columns, rows = fetch_records(args.base_token, args.table_id)
        policy = rebuild_policy([dict(zip(columns, row)) for row in rows])
        args.output.write_text(_dump(policy), encoding="utf-8")
    except Exception as exc:
        print(f"Base 配置读取失败: {str(exc)[:240]}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "output": str(args.output), "policy_source": "base"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
