#!/usr/bin/env python3
"""Thin deterministic orchestration entry for the long-read analysis branches.

并行独立启动三条分支；任何前置失败都不得阻断或丢弃其他分支：

- article-decode + 文字 ljg（run_isolated_analyses，独立 MoonBridge HTTP 请求）；
- ChatGPT Bridge 芒格精读（run_chatgpt_munger，仅当 scoring_result.chatgpt_munger_doc=true）。

阈值与触发条件只来自 content_scoring 的 scoring_result；本脚本不复制阈值、不重算路由。
article-decode 失败不阻断 ChatGPT，也不丢弃已成功的 ljg 输出；汇总逐分支记录状态与错误，
交付状态只能是：精读完成 / 主精读完成，ChatGPT 待复核 / 主文档降级交付。
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))

import run_isolated_analyses as isolated
import run_chatgpt_munger as munger


SCHEMA_VERSION = "1"

STATE_COMPLETE = "complete"
STATE_CHATGPT_NEEDS_REVIEW = "needs_review_chatgpt"
STATE_DEGRADED = "degraded"
LABELS = {
    STATE_COMPLETE: "精读完成",
    STATE_CHATGPT_NEEDS_REVIEW: "主精读完成，ChatGPT 待复核",
    STATE_DEGRADED: "主文档降级交付",
}


def load_scoring_result(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"scoring result must be a regular file: {path}")
    scoring = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(scoring, dict):
        raise ValueError("scoring result must be an object")
    if scoring.get("score_status") != "scored" or scoring.get("route") != "long_read":
        raise ValueError(
            f"pipeline requires score_status=scored and route=long_read, got "
            f"{scoring.get('score_status')}/{scoring.get('route')}"
        )
    return scoring


def _run_analyses(args, results: dict) -> None:
    planned = ["article-decode"] + [name for name, _ in args.task]
    try:
        results["analyses"] = isolated.run(
            args.source,
            args.evidence,
            args.output_dir,
            [(name, Path(question)) for name, question in args.task],
            args.max_workers,
            args.timeout,
            args.max_output_tokens,
        )
    except Exception as exc:
        # 输入校验等前置失败：记录计划中的任务后仍不阻断 ChatGPT 分支。
        results["analyses"] = {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "tasks": [],
            "planned_tasks": planned,
        }


def _run_chatgpt(args, results: dict) -> None:
    try:
        results["chatgpt_munger"] = munger.run(args.source, args.munger_output, summary_path=args.munger_summary)
    except FileExistsError as exc:
        results["chatgpt_munger"] = {"status": "needs_review", "reason": str(exc), "attempts": []}
    except Exception as exc:
        results["chatgpt_munger"] = {"status": "needs_review", "reason": f"{type(exc).__name__}: {exc}", "attempts": []}


def delivery_state(analyses: dict, chatgpt: dict) -> dict:
    """Deterministic delivery state; ChatGPT 成功前整轮不得标记为“精读完成”。"""
    failed_tasks = [item.get("task") for item in analyses.get("tasks", []) if item.get("status") != "completed"]
    missing_branches = list(failed_tasks)
    if analyses.get("status") == "failed" and not analyses.get("tasks"):
        # 分支未运行（输入校验失败等）：计划任务全部视为缺失，必须显式可见。
        missing_branches.extend(analyses.get("planned_tasks", ["article-decode"]))
    chatgpt_state = chatgpt.get("status")
    if chatgpt_state not in {"succeeded", "needs_review"}:
        chatgpt_state = "not_required" if chatgpt.get("status") == "not_required" else "needs_review"
    if chatgpt_state == "needs_review":
        missing_branches.append("chatgpt-munger")
    analyses_completed = analyses.get("status") == "completed"
    if analyses_completed and chatgpt_state in {"succeeded", "not_required"}:
        state = STATE_COMPLETE
    elif chatgpt_state == "needs_review":
        state = STATE_CHATGPT_NEEDS_REVIEW
    else:
        state = STATE_DEGRADED
    return {
        "state": state,
        "label": LABELS[state],
        "missing_branches": missing_branches,
        "chatgpt_state": chatgpt_state,
    }


def run(args: argparse.Namespace) -> dict:
    started = time.monotonic()
    scoring = load_scoring_result(args.scoring_result)
    chatgpt_required = bool(scoring.get("chatgpt_munger_doc"))
    results: dict = {"analyses": None, "chatgpt_munger": {"status": "not_required"}}
    threads = [threading.Thread(target=_run_analyses, args=(args, results), daemon=True)]
    if chatgpt_required:
        threads.append(threading.Thread(target=_run_chatgpt, args=(args, results), daemon=True))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    chatgpt = results["chatgpt_munger"]
    delivery = delivery_state(results["analyses"], chatgpt)
    return {
        "schema_version": SCHEMA_VERSION,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "scoring": {
            "score_status": scoring.get("score_status"),
            "route": scoring.get("route"),
            "decision_score": scoring.get("decision_score"),
            "chatgpt_munger_doc": chatgpt_required,
        },
        "branches": {
            "analyses": results["analyses"],
            "chatgpt_munger": chatgpt,
        },
        "delivery": delivery,
    }


def exit_code(summary: dict) -> int:
    """0 精读完成；1 降级/待复核但主文档仍可交付；2 分析分支未运行（输入失败）。"""
    if summary["delivery"]["state"] == STATE_COMPLETE:
        return 0
    analyses = summary["branches"]["analyses"]
    if analyses.get("status") == "failed" and not analyses.get("tasks"):
        return 2
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--scoring-result", required=True, type=Path)
    parser.add_argument("--task", action="append", nargs=2, metavar=("SKILL", "QUESTION_FILE"), default=[])
    parser.add_argument("--max-workers", type=int, default=isolated.MAX_TASKS)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--max-output-tokens", type=int, default=8000)
    parser.add_argument("--munger-output", type=Path)
    parser.add_argument("--munger-summary", type=Path)
    parser.add_argument("--summary-file", type=Path)
    args = parser.parse_args()
    args.munger_output = args.munger_output or (args.run_dir / "chatgpt-munger.md")
    args.munger_summary = args.munger_summary or (args.run_dir / "chatgpt-munger-summary.json")
    args.summary_file = args.summary_file or (args.run_dir / "pipeline-summary.json")
    try:
        summary = run(args)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 2
    args.summary_file.parent.mkdir(parents=True, exist_ok=True)
    isolated.atomic_write(args.summary_file, json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    return exit_code(summary)


if __name__ == "__main__":
    raise SystemExit(main())
