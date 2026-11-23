#!/usr/bin/env python3
"""Aggregate Codex token usage from rollout session JSONL files.

Read-only and privacy-safe: only numeric usage fields, ids, timestamps,
model/effort and cwd are extracted; message content is never read or printed.
Schema verified against codex-cli 0.157.1 (token_usage_record / turn_context /
turn_aborted / session_meta). Unknown fields are ignored on purpose.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "codex-cli-0.157.1"
USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
MAX_LINE_BYTES = 8 * 1024 * 1024  # tolerate truncated/corrupt giant lines


def parse_timestamp(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def file_start_epoch(path: Path):
    """Cheap filename pre-filter: rollout-YYYY-MM-DDTHH-MM-SS-..."""
    name = path.name
    if not name.startswith("rollout-"):
        return None
    stamp = name[len("rollout-"):][:19]
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H-%M-%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def empty_usage():
    return {key: 0 for key in USAGE_KEYS}


def add_usage(target, usage):
    for key in USAGE_KEYS:
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            target[key] += int(value)


def analyze_file(path: Path, *, since=None, until=None, cwd_substr=None, per_turn=False):
    session = {
        "file": str(path), "session_id": None, "cwd": None, "models": [],
        "turns": 0, "calls": 0, "aborted_turns": 0, "skipped_lines": 0,
        "no_timestamp_events": 0,
        "usage": empty_usage(), "first_ts": None, "last_ts": None,
    }
    turn_ctx = {}
    turn_usage = defaultdict(empty_usage)
    turn_calls = defaultdict(int)
    turn_first, turn_last = {}, {}
    seen_responses = set()

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            if len(raw_line) > MAX_LINE_BYTES:
                session["skipped_lines"] += 1
                continue
            line = raw_line.strip()
            if not line:
                continue
            if not line.startswith("{"):
                session["skipped_lines"] += 1
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                session["skipped_lines"] += 1
                continue
            if not isinstance(event, dict):
                continue
            ts = parse_timestamp(event.get("timestamp"))
            if ts is not None:
                if session["first_ts"] is None or ts < session["first_ts"]:
                    session["first_ts"] = ts
                if session["last_ts"] is None or ts > session["last_ts"]:
                    session["last_ts"] = ts
                if (since is not None and ts < since) or (until is not None and ts > until):
                    continue
            etype = event.get("type")
            payload = event.get("payload")
            if etype == "session_meta" and isinstance(payload, dict):
                if session["session_id"] is None:
                    session["session_id"] = payload.get("id") or payload.get("session_id")
                if session["cwd"] is None:
                    session["cwd"] = payload.get("cwd")
                continue
            if etype == "turn_context" and isinstance(payload, dict):
                turn_id = payload.get("turn_id")
                if turn_id:
                    turn_ctx[turn_id] = {"model": payload.get("model"), "effort": payload.get("effort")}
                continue
            if etype == "turn_aborted":
                session["aborted_turns"] += 1
                continue
            if etype != "token_usage_record" or not isinstance(payload, dict):
                continue
            response_id = payload.get("response_id")
            if response_id:
                if response_id in seen_responses:
                    continue
                seen_responses.add(response_id)
            if ts is None:
                session["no_timestamp_events"] += 1  # window cannot apply; counted separately
            usage = payload.get("usage")
            if not isinstance(usage, dict):
                continue
            add_usage(session["usage"], usage)
            session["calls"] += 1
            turn_id = payload.get("turn_id") or "unknown"
            add_usage(turn_usage[turn_id], usage)
            turn_calls[turn_id] += 1
            if ts is not None:
                turn_first.setdefault(turn_id, ts)
                if turn_id not in turn_last or ts > turn_last[turn_id]:
                    turn_last[turn_id] = ts

    session["turns"] = len(turn_calls)
    session["models"] = sorted({ctx.get("model") for ctx in turn_ctx.values() if ctx.get("model")})
    if per_turn:
        session["per_turn"] = [
            {
                "turn_id": turn_id,
                "model": turn_ctx.get(turn_id, {}).get("model"),
                "calls": turn_calls[turn_id],
                "usage": turn_usage[turn_id],
                "first_ts": turn_first.get(turn_id),
                "last_ts": turn_last.get(turn_id),
            }
            for turn_id in sorted(turn_calls, key=lambda tid: (turn_first.get(tid) or 0))
        ]
    if cwd_substr and not (isinstance(session["cwd"], str) and cwd_substr in session["cwd"]):
        return None
    return session


def collect_files(paths, codex_home: Path, until=None):
    if paths:
        files = list(paths)
    else:
        files = sorted(codex_home.rglob("rollout-*.jsonl"))
    result = []
    for path in files:
        if until is not None:
            start = file_start_epoch(path)
            if start is not None and start > until:
                continue  # whole session starts after the window
        result.append(path)
    return result


def parse_time_arg(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        raise SystemExit("invalid --since/--until value: " + str(value))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="session JSONL files (default: scan codex home)")
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex" / "sessions")
    parser.add_argument("--since", default=None, help="ISO datetime or YYYY-MM-DD (UTC)")
    parser.add_argument("--until", default=None, help="ISO datetime or YYYY-MM-DD (UTC)")
    parser.add_argument("--cwd-substr", default=None, help="only sessions whose cwd contains this substring")
    parser.add_argument("--per-turn", action="store_true", help="include per-turn breakdown")
    args = parser.parse_args()
    since = parse_time_arg(args.since)
    until = parse_time_arg(args.until)
    totals = empty_usage()
    sessions = []
    for path in collect_files(args.paths, args.codex_home, until):
        result = analyze_file(path, since=since, until=until, cwd_substr=args.cwd_substr, per_turn=args.per_turn)
        if result is None:
            continue
        if (since is not None or until is not None) and result["calls"] == 0:
            continue  # windowed report: drop sessions with no in-window usage
        add_usage(totals, result["usage"])
        sessions.append(result)
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {"since": args.since, "until": args.until, "cwd_substr": args.cwd_substr},
        "sessions": sessions,
        "totals": totals,
    }
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
