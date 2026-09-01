#!/usr/bin/env python3
"""Deterministically compare isolated long-read analysis artifacts."""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import tempfile
from pathlib import Path


RUNNER_PATH = Path(__file__).with_name("run_isolated_analyses.py")
SPEC = importlib.util.spec_from_file_location("long_read_runner_for_eval", RUNNER_PATH)
runner = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)

def task_name(path: Path) -> str | None:
    if path.name == "article-decode.md":
        return "article-decode"
    match = re.fullmatch(r"\d{2}-(ljg-[a-z]+)\.md", path.name)
    return match.group(1) if match else None


def paragraphs(text: str) -> set[str]:
    return {
        re.sub(r"\s+", "", paragraph)
        for paragraph in re.split(r"\n\s*\n", text)
        if len(re.sub(r"\s+", "", paragraph)) >= 40
    }


def inspect(directory: Path) -> dict:
    files = []
    paragraph_owners: dict[str, list[str]] = {}
    for path in sorted(directory.glob("*.md")):
        name = task_name(path)
        if not name:
            continue
        text = path.read_text(encoding="utf-8")
        visible_chars = len(re.sub(r"\s+", "", text))
        required = ("证据边界", "我的判断") if name == "article-decode" else runner.OUTPUT_MARKERS.get(name, ())
        missing = [marker for marker in required if marker not in text]
        tool_hits = [label for label, pattern in runner.TOOL_PATTERNS.items() if pattern.search(text)]
        if name == "ljg-word" and re.search(r"(?m)^\s*>", text):
            tool_hits.append("unsupported_quote_block")
        minimum = runner.MIN_OUTPUT_CHARS[name]
        files.append({
            "task": name,
            "path": str(path),
            "output_chars": visible_chars,
            "missing_form_markers": missing,
            "tool_artifacts": tool_hits,
            "passes": visible_chars >= minimum and not missing and not tool_hits,
        })
        for paragraph in paragraphs(text):
            paragraph_owners.setdefault(paragraph, []).append(name)
    duplicates = [owners for owners in paragraph_owners.values() if len(set(owners)) > 1]
    return {
        "directory": str(directory),
        "file_count": len(files),
        "passing_files": sum(item["passes"] for item in files),
        "tool_artifact_count": sum(len(item["tool_artifacts"]) for item in files),
        "form_failure_count": sum(bool(item["missing_form_markers"]) for item in files),
        "duplicate_paragraph_count": len(duplicates),
        "files": files,
    }


def load_summary(path: Path, expected_tasks: set[str]) -> dict:
    summary = json.loads(path.read_text(encoding="utf-8"))
    tasks = summary.get("tasks", [])
    task_names = {item.get("task") for item in tasks}
    if summary.get("status") != "completed" or task_names != expected_tasks or any(item.get("status") != "completed" for item in tasks):
        raise ValueError(f"summary is not a complete run for {sorted(expected_tasks)}: {path}")
    return summary


def skill_hashes(summary: dict) -> dict[str, str | None]:
    return {
        item["task"]: (
            item.get("skill_sha256"), item.get("instructions_sha256"), item.get("input_sha256"),
        )
        for item in summary["tasks"]
    }


def run_contract(summary: dict) -> tuple:
    return tuple(summary.get(key) for key in (
        "model", "store", "endpoint", "timeout_seconds", "max_output_tokens",
    ))


def compare(
    candidate: Path,
    effect_baseline: Path | None = None,
    candidate_summary: Path | None = None,
    performance_baseline_summary: Path | None = None,
    max_wall_ratio: float = 0.75,
) -> dict:
    candidate_result = inspect(candidate)
    candidate_tasks = {item["task"] for item in candidate_result["files"]}
    result = {
        "status": "pass" if candidate_result["file_count"] and candidate_result["passing_files"] == candidate_result["file_count"] else "fail",
        "candidate": candidate_result,
    }
    if effect_baseline:
        baseline_result = inspect(effect_baseline)
        result["effect_baseline"] = baseline_result
        result["delta"] = {
            key: candidate_result[key] - baseline_result[key]
            for key in ("tool_artifact_count", "form_failure_count", "duplicate_paragraph_count")
        }
        baseline_tasks = {item["task"] for item in baseline_result["files"]}
        result["effect_improved"] = (
            candidate_tasks == baseline_tasks
            and all(value <= 0 for value in result["delta"].values())
            and any(value < 0 for value in result["delta"].values())
        )
        if not result["effect_improved"]:
            result["status"] = "fail"
    if bool(candidate_summary) != bool(performance_baseline_summary):
        raise ValueError("candidate and performance baseline summaries must be provided together")
    if candidate_summary and performance_baseline_summary:
        candidate_run = load_summary(candidate_summary, candidate_tasks)
        baseline_run = load_summary(performance_baseline_summary, candidate_tasks)
        if skill_hashes(candidate_run) != skill_hashes(baseline_run) or run_contract(candidate_run) != run_contract(baseline_run):
            raise ValueError("candidate and performance baseline use different inputs or request contracts")
        if baseline_run.get("max_workers") != 1 or candidate_run.get("max_workers", 0) <= 1:
            raise ValueError("performance comparison requires max_workers=1 baseline and parallel candidate")
        ratio = candidate_run["wall_seconds"] / baseline_run["wall_seconds"]
        result["performance"] = {
            "candidate_wall_seconds": candidate_run["wall_seconds"],
            "baseline_wall_seconds": baseline_run["wall_seconds"],
            "wall_ratio": round(ratio, 4),
            "max_wall_ratio": max_wall_ratio,
            "passes": ratio <= max_wall_ratio,
        }
        if ratio > max_wall_ratio:
            result["status"] = "fail"
    return result


def self_check() -> bool:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        baseline = root / "baseline"
        candidate = root / "candidate"
        baseline.mkdir()
        candidate.mkdir()
        (baseline / "article-decode.md").write_text("普通输出" * 80, encoding="utf-8")
        (baseline / "01-ljg-think.md").write_text("```bash\ndate +%Y%m%d\n```\n" + "普通输出" * 80, encoding="utf-8")
        (candidate / "article-decode.md").write_text("证据边界 我的判断 " + "有效解码" * 80, encoding="utf-8")
        (candidate / "01-ljg-think.md").write_text("\n".join(f"## 层{i}\n" + "有效下钻" * 40 for i in range(4)), encoding="utf-8")
        baseline_summary = root / "baseline.json"
        candidate_summary = root / "candidate.json"
        tasks = [{"task": name, "status": "completed", "skill_sha256": name, "instructions_sha256": name, "input_sha256": name} for name in ("article-decode", "ljg-think")]
        contract = {"status": "completed", "model": "deepseek-v4-flash", "store": False, "endpoint": "local", "timeout_seconds": 240, "max_output_tokens": 8000, "tasks": tasks}
        baseline_summary.write_text(json.dumps({**contract, "max_workers": 1, "wall_seconds": 20}), encoding="utf-8")
        candidate_summary.write_text(json.dumps({**contract, "max_workers": 2, "wall_seconds": 10}), encoding="utf-8")
        result = compare(candidate, baseline, candidate_summary, baseline_summary)
        assert result["status"] == "pass"
        assert result["candidate"]["tool_artifact_count"] == 0
        assert result["effect_baseline"]["tool_artifact_count"] == 1
        assert result["delta"]["tool_artifact_count"] == -1
        assert result["performance"]["passes"]

        mismatched = root / "mismatched.json"
        mismatched_tasks = json.loads(json.dumps(tasks))
        mismatched_tasks[0]["input_sha256"] = "different"
        mismatched.write_text(json.dumps({**contract, "max_workers": 2, "wall_seconds": 10, "tasks": mismatched_tasks}), encoding="utf-8")
        try:
            compare(candidate, baseline, mismatched, baseline_summary)
        except ValueError as exc:
            assert "different inputs or request contracts" in str(exc)
        else:
            raise AssertionError("mismatched benchmark inputs must be rejected")

        wrong_workers = root / "wrong-workers.json"
        wrong_workers.write_text(json.dumps({**contract, "max_workers": 2, "wall_seconds": 20}), encoding="utf-8")
        try:
            compare(candidate, baseline, candidate_summary, wrong_workers)
        except ValueError as exc:
            assert "max_workers=1 baseline" in str(exc)
        else:
            raise AssertionError("non-sequential performance baseline must be rejected")

        incomplete = root / "incomplete"
        incomplete.mkdir()
        (incomplete / "article-decode.md").write_text("证据边界 我的判断 " + "有效解码" * 80, encoding="utf-8")
        assert compare(incomplete, baseline)["status"] == "fail"
        partial_summary = root / "partial.json"
        partial_summary.write_text(json.dumps({"status": "partial", "wall_seconds": 10, "tasks": tasks}), encoding="utf-8")
        try:
            compare(candidate, baseline, partial_summary, baseline_summary)
        except ValueError as exc:
            assert "not a complete run" in str(exc)
        else:
            raise AssertionError("partial summary must not pass comparison")
    print("self-check: ok")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--effect-baseline", type=Path)
    parser.add_argument("--candidate-summary", type=Path)
    parser.add_argument("--performance-baseline-summary", type=Path)
    parser.add_argument("--max-wall-ratio", type=float, default=0.75)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        return 0 if self_check() else 1
    if not args.candidate:
        parser.error("--candidate is required unless --self-check is used")
    result = compare(
        args.candidate, args.effect_baseline, args.candidate_summary,
        args.performance_baseline_summary, args.max_wall_ratio,
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
