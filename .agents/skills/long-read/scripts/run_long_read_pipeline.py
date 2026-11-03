#!/usr/bin/env python3
"""Retired non-durable long-read pipeline.

Direct execution now fails closed. New tasks must enter the persistent workflow,
which prepares manual handoffs and retains business validation and delivery gates.
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

STATE_CONTENT_READY = "content_ready"
STATE_CHATGPT_NEEDS_REVIEW = "needs_review_chatgpt"
STATE_DEGRADED = "degraded"
LABELS = {
    STATE_CONTENT_READY: "内容生成完成，等待文档与图片交付核验",
    STATE_CHATGPT_NEEDS_REVIEW: "内容生成待复核：芒格分支结果未确认",
    STATE_DEGRADED: "分析内容未齐，等待交付评估",
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
    """Content-stage state only; remote delivery is outside this pipeline."""
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
    analyses_completed = analyses.get("status") == "completed" and not missing_branches
    if analyses_completed and chatgpt_state in {"succeeded", "not_required"}:
        state = STATE_CONTENT_READY
    elif chatgpt_state == "needs_review":
        state = STATE_CHATGPT_NEEDS_REVIEW
    else:
        state = STATE_DEGRADED
    return {
        "state": state,
        "label": LABELS[state],
        "delivery_complete": False,
        "missing_branches": missing_branches,
        "chatgpt_state": chatgpt_state,
    }


def run(args: argparse.Namespace) -> dict:
    """Keep the retired, non-durable pipeline from submitting new Bridge runs."""
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "needs_review",
        "reason_code": "legacy-direct-entry-disabled",
        "manual_handoff_required": True,
        "message": (
            "旧临时流水线没有持久任务身份，不能安全创建人工交接或提交新的 ChatGPT 请求。"
            "请使用 long_read_workflow_cli.py submit 创建任务，再由 run-worker 准备交接包。"
        ),
        "next_step": "long_read_workflow_cli.py submit → run-worker",
        "delivery": {
            "state": STATE_CHATGPT_NEEDS_REVIEW,
            "label": "旧入口已停止新提交；请从持久工作流继续",
            "delivery_complete": False,
            "missing_branches": ["article-decode", "text-analysis", "chatgpt-munger"],
            "chatgpt_state": "needs_review",
        },
    }


def exit_code(summary: dict) -> int:
    """0 content stage ready; 1 incomplete content; 2 analysis preflight failed."""
    if summary.get("reason_code") == "legacy-direct-entry-disabled":
        return 2
    if summary["delivery"]["state"] == STATE_CONTENT_READY:
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
    parser.add_argument("--timeout", type=float, default=360)
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
    if summary.get("reason_code") != "legacy-direct-entry-disabled":
        args.summary_file.parent.mkdir(parents=True, exist_ok=True)
        isolated.atomic_write(args.summary_file, json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    return exit_code(summary)


if __name__ == "__main__":
    raise SystemExit(main())
