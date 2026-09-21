#!/usr/bin/env python3
"""Focused checks for the thin long-read pipeline orchestrator."""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
from pathlib import Path


SCRIPT = Path(__file__).with_name("run_long_read_pipeline.py")
SPEC = importlib.util.spec_from_file_location("run_long_read_pipeline", SCRIPT)
pipeline = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = pipeline
SPEC.loader.exec_module(pipeline)


def _args(root: Path, *, chatgpt: bool) -> argparse.Namespace:
    run_dir = root / "run"
    return argparse.Namespace(
        source=root / "source.md",
        evidence=root / "evidence.json",
        output_dir=run_dir / "analyses",
        run_dir=run_dir,
        scoring_result=root / "scoring-result.json",
        task=[],
        max_workers=3,
        timeout=1,
        munger_output=run_dir / "chatgpt-munger.md",
        munger_summary=run_dir / "chatgpt-munger-summary.json",
        summary_file=run_dir / "pipeline-summary.json",
        _chatgpt=chatgpt,
    )


def _scoring(root: Path, chatgpt: bool) -> Path:
    path = root / "scoring-result.json"
    path.write_text(json.dumps({
        "score_status": "scored", "route": "long_read", "decision_score": 8.5 if chatgpt else 7.5,
        "chatgpt_munger_doc": chatgpt,
    }, ensure_ascii=False), encoding="utf-8")
    return path


def _analyses(article: str, ljg: str | None) -> dict:
    tasks = [
        {"task": "article-decode", "status": article},
        {"task": "ljg-think", "status": "completed"},
    ]
    if ljg is not None:
        tasks.append({"task": "ljg-qa", "status": ljg})
    return {"status": "completed" if all(item["status"] == "completed" for item in tasks) else "partial" if any(item["status"] == "completed" for item in tasks) else "failed", "tasks": tasks}


def _patch(monkey: dict):
    original = (pipeline.isolated.run, pipeline.munger.run)
    pipeline.isolated.run = monkey.get("isolated", original[0])
    pipeline.munger.run = monkey.get("munger", original[1])
    return original


def _restore(original):
    pipeline.isolated.run, pipeline.munger.run = original


def test_munger_runs_even_when_score_is_below_gate():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        args = _args(root, chatgpt=False)
        _scoring(root, chatgpt=False)
        calls = {"isolated": 0, "munger": 0}

        def fake_munger(source, output, **kwargs):
            calls["munger"] += 1
            return {"status": "succeeded", "output": str(output)}

        original = _patch({
            "isolated": lambda *a, **k: (calls.__setitem__("isolated", calls["isolated"] + 1), _analyses("completed", "completed"))[1],
            "munger": fake_munger,
        })
        try:
            summary = pipeline.run(args)
        finally:
            _restore(original)
        # route=long_read 即启动芒格分支：评分里的 chatgpt_munger_doc=false 不再阻止启动
        assert calls == {"isolated": 1, "munger": 1}
        assert summary["scoring"]["chatgpt_munger_doc"] is False
        assert summary["branches"]["chatgpt_munger"]["status"] == "succeeded"
        assert summary["delivery"]["state"] == "complete"
        assert summary["delivery"]["label"] == "精读完成"
        assert summary["delivery"]["missing_branches"] == []


def test_article_decode_failure_does_not_block_chatgpt_or_discard_ljg():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        args = _args(root, chatgpt=True)
        _scoring(root, chatgpt=True)
        calls = {"isolated": 0, "munger": 0}

        def fake_isolated(*a, **k):
            calls["isolated"] += 1
            return _analyses("failed", None)

        def fake_munger(source, output, **kwargs):
            calls["munger"] += 1
            return {"status": "succeeded", "output": str(output)}

        original = _patch({"isolated": fake_isolated, "munger": fake_munger})
        try:
            summary = pipeline.run(args)
        finally:
            _restore(original)
        # ChatGPT 分支必须与 article-decode 失败无关地启动并成功
        assert calls == {"isolated": 1, "munger": 1}
        assert summary["branches"]["chatgpt_munger"]["status"] == "succeeded"
        assert summary["delivery"]["state"] == "degraded"
        assert summary["delivery"]["label"] == "主文档降级交付"
        assert summary["delivery"]["missing_branches"] == ["article-decode"]
        # ljg-think 成功结果不被丢弃
        ljg = next(item for item in summary["branches"]["analyses"]["tasks"] if item["task"] == "ljg-think")
        assert ljg["status"] == "completed"


def test_chatgpt_failure_keeps_main_doc_and_requires_review():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        args = _args(root, chatgpt=True)
        _scoring(root, chatgpt=True)
        original = _patch({
            "isolated": lambda *a, **k: _analyses("completed", "completed"),
            "munger": lambda *a, **k: {"status": "needs_review", "reason": "observer-window-ended", "attempts": [{"reason": "observer-window-ended"}]},
        })
        try:
            summary = pipeline.run(args)
        finally:
            _restore(original)
        assert summary["delivery"]["state"] == "needs_review_chatgpt"
        assert summary["delivery"]["label"] == "主精读完成，ChatGPT 待复核"
        assert summary["delivery"]["missing_branches"] == ["chatgpt-munger"]
        assert summary["branches"]["analyses"]["status"] == "completed"


def test_partial_ljg_failure_and_mixed_attempts_are_preserved():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        args = _args(root, chatgpt=True)
        _scoring(root, chatgpt=True)
        analyses = _analyses("completed", "failed")
        analyses["tasks"][2]["attempts_detail"] = [
            {"attempt": 1, "error_type": "timeout", "error": "ceiling", "elapsed_seconds": 1.0},
            {"attempt": 2, "error_type": "output_validation", "error": "too short", "elapsed_seconds": 0.2},
        ]
        original = _patch({
            "isolated": lambda *a, **k: analyses,
            "munger": lambda *a, **k: {"status": "succeeded"},
        })
        try:
            summary = pipeline.run(args)
        finally:
            _restore(original)
        assert summary["delivery"]["state"] == "degraded"
        assert summary["delivery"]["missing_branches"] == ["ljg-qa"]
        detail = summary["branches"]["analyses"]["tasks"][2]["attempts_detail"]
        assert [item["error_type"] for item in detail] == ["timeout", "output_validation"]


def test_non_long_read_scoring_result_fails_closed():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        args = _args(root, chatgpt=False)
        path = root / "scoring-result.json"
        path.write_text(json.dumps({"score_status": "scored", "route": "card", "chatgpt_munger_doc": False}), encoding="utf-8")
        original = _patch({})
        try:
            try:
                pipeline.run(args)
            except ValueError as exc:
                assert "route=long_read" in str(exc)
            else:
                raise AssertionError("non long_read scoring result must fail closed")
        finally:
            _restore(original)


def test_analyses_crash_records_planned_tasks_as_missing():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        args = _args(root, chatgpt=False)
        args.task = [("ljg-think", root / "q.md")]
        _scoring(root, chatgpt=False)

        def crash(*a, **k):
            raise ValueError("invalid evidence: quotes[0] not a substring of source")

        original = _patch({"isolated": crash, "munger": lambda *a, **k: {"status": "succeeded"}})
        try:
            summary = pipeline.run(args)
        finally:
            _restore(original)
        assert summary["branches"]["analyses"]["status"] == "failed"
        assert "invalid evidence" in summary["branches"]["analyses"]["error"]
        assert summary["branches"]["analyses"]["planned_tasks"] == ["article-decode", "ljg-think"]
        assert summary["delivery"]["missing_branches"] == ["article-decode", "ljg-think"]
        assert pipeline.exit_code(summary) == 2


def test_exit_code_mapping_is_deterministic():
    assert pipeline.exit_code({"delivery": {"state": "complete"}, "branches": {"analyses": {"status": "completed"}}}) == 0
    assert pipeline.exit_code({"delivery": {"state": "needs_review_chatgpt"}, "branches": {"analyses": {"status": "completed"}}}) == 1
    assert pipeline.exit_code({"delivery": {"state": "degraded"}, "branches": {"analyses": {"status": "partial", "tasks": [{"task": "article-decode", "status": "failed"}]}}}) == 1
    assert pipeline.exit_code({"delivery": {"state": "degraded"}, "branches": {"analyses": {"status": "failed", "error": "invalid evidence", "tasks": []}}}) == 2


def main():
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in sorted(tests, key=lambda item: item.__name__):
        test()
        print(f"[ok] {test.__name__}")
    print(f"{len(tests)} passed, 0 failed")


if __name__ == "__main__":
    main()
