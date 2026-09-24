#!/usr/bin/env python3
"""Render, send, and record the mandatory score-card delivery gate."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from render_score_card import render_card


def _subprocess_output(command: list[str]) -> str:
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise RuntimeError(f"lark-cli failed: {detail}")
    return completed.stdout


def _send(args: argparse.Namespace, card: dict, output_fn=_subprocess_output) -> dict:
    cli = shutil.which("lark-cli")
    if not cli:
        raise RuntimeError("lark-cli is not available")
    content = json.dumps(card, ensure_ascii=False)
    command = [
        cli,
        "im",
        "+messages-send",
        "--as",
        "bot",
        "--msg-type",
        "interactive",
        "--content",
        content,
    ]
    if args.chat_id:
        command += ["--chat-id", args.chat_id]
    else:
        command += ["--user-id", args.user_id]
    command += ["--idempotency-key", args.idempotency_key, "--json"]
    response = json.loads(output_fn(command))
    if not response.get("ok"):
        raise RuntimeError(f"lark-cli rejected score card: {response}")
    message_id = response.get("data", {}).get("message_id")
    if not isinstance(message_id, str) or not message_id.startswith("om_"):
        raise RuntimeError("score-card message_id is missing")
    return {
        "schema_version": "1",
        "message_id": message_id,
        "scoring_result_sha256": hashlib.sha256(
            args.scoring_result.read_bytes()
        ).hexdigest(),
        "chat_id": args.chat_id,
        "user_id": args.user_id,
        "idempotency_key": args.idempotency_key,
        "card_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "sent_at_command": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scoring_result", type=Path)
    parser.add_argument("--title", required=True)
    parser.add_argument("--author", default="")
    parser.add_argument("--date", default="")
    parser.add_argument("--url", required=True)
    parser.add_argument("--score-only", action="store_true")
    parser.add_argument("--quick-read", type=Path)
    parser.add_argument("--idempotency-key", required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--chat-id")
    target.add_argument("--user-id")
    parser.add_argument("--evidence-output", type=Path, required=True)
    args = parser.parse_args()

    result = json.loads(args.scoring_result.read_text(encoding="utf-8"))
    quick_read = args.quick_read.read_text(encoding="utf-8") if args.quick_read else None
    card = render_card(
        result,
        title=args.title,
        author=args.author,
        date=args.date,
        url=args.url,
        score_only=args.score_only,
        quick_read=quick_read,
    )
    evidence = _send(args, card)
    args.evidence_output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(evidence, ensure_ascii=False))


if __name__ == "__main__":
    raise SystemExit(main())
