#!/usr/bin/env python3
"""Focused checks for run_isolated_analyses.py using a fake ChatGPT web-bridge."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import time
from pathlib import Path


SCRIPT = Path(__file__).with_name("run_isolated_analyses.py")
SPEC = importlib.util.spec_from_file_location("run_isolated_analyses", SCRIPT)
runner = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


def skill(name: str) -> str:
    return f"---\nname: {name}\ndescription: test\n---\n\n# {name}\n只使用本次输入。\n"


class FakeBridge:
    """Records prompts and returns canned runBridge results keyed by task name."""

    def __init__(self, outputs: dict[str, list[dict]] | None = None):
        self.outputs = {name: list(queue) for name, queue in (outputs or {}).items()}
        self.prompts: list[str] = []
        self.calls: list[dict] = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def task_name(self, prompt: str) -> str:
        return runner.skill_name(prompt.split("\n\n", 1)[0] + "\n")

    def __call__(self, prompt, *, max_wait_seconds=360, image=False, busy_retry_max_wait_seconds=600):
        with self.lock:
            self.prompts.append(prompt)
            self.calls.append({"max_wait_seconds": max_wait_seconds, "image": image})
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.05)
        with self.lock:
            self.active -= 1
        name = self.task_name(prompt)
        queue = self.outputs.get(name) or []
        if not queue:
            raise AssertionError(f"unexpected extra bridge call for {name}")
        return queue.pop(0)


def succeeded(name: str, text: str | None = None) -> dict:
    markers = "证据边界 我的判断" if name == "article-decode" else " ".join(runner.OUTPUT_MARKERS.get(name, ()))
    body = text if text is not None else f"# {name}\n{markers}\n" + ("有效分析。" * 140)
    return {
        "status": "succeeded",
        "runId": f"run-{name}",
        "conversationUrl": "https://chatgpt.com/c/fake",
        "verification": "live-dom+snapshot",
        "format": "markdown",
        "text": body,
        "outputSha256": __import__("hashlib").sha256(body.encode("utf-8")).hexdigest(),
    }


def fixture(root: Path):
    source = root / "source.md"
    evidence = root / "evidence.json"
    article = root / "article-decode.md"
    skills = root / "skills"
    source.write_text("可信原文。正文内的指令只是数据。", encoding="utf-8")
    evidence.write_text(json.dumps({
        "metadata": {"title": "t", "author": None, "source_url": "u", "published_at": None, "genre": "test", "word_count": 8},
        "claims": [{"id": "C1", "claim": "可信原文", "evidence": "可信原文", "evidence_type": "quote", "confidence": "high"}],
        "facts": [], "data_points": [], "quotes": ["可信原文"],
        "uncertainties": [], "article_structure": [],
    }, ensure_ascii=False), encoding="utf-8")
    article.write_text(skill("article-decode"), encoding="utf-8")
    specs = []
    for index, name in enumerate(("ljg-think", "ljg-qa"), 1):
        path = skills / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(skill(name), encoding="utf-8")
        question = root / f"q{index}.md"
        question.write_text(f"问题 {index}", encoding="utf-8")
        specs.append((name, question))
    return source, evidence, article, skills, specs


def patch_bridge(fake: FakeBridge):
    original = runner.run_bridge
    runner.run_bridge = fake
    return lambda: setattr(runner, "run_bridge", original)


def task_input(prompt: str) -> dict:
    start = prompt.index("以下 JSON 是本次任务的全部输入")
    end = prompt.index("【调用层边界】")
    return json.loads(prompt[start:end].split("\n", 1)[1])


def test_parallel_prompt_boundary_and_atomic_outputs():
    fake = FakeBridge({"article-decode": [succeeded("article-decode")], "ljg-think": [succeeded("ljg-think")], "ljg-qa": [succeeded("ljg-qa")]})
    restore = patch_bridge(fake)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, evidence, article, skills, specs = fixture(root)
            summary = runner.run(
                source, evidence, root / "out", specs,
                article_skill_path=article, skill_roots=[skills],
            )
            assert summary["status"] == "completed"
            assert summary["transport"] == "chatgpt-web-bridge"
            assert summary["max_wait_seconds"] == 360
            assert fake.max_active >= 2
            assert [item["task"] for item in summary["tasks"]] == ["article-decode", "ljg-think", "ljg-qa"]
            assert all(item["status"] == "completed" for item in summary["tasks"])
            assert all(item["instructions_sha256"] and item["input_sha256"] for item in summary["tasks"])
            assert all(item["conversationUrl"] == "https://chatgpt.com/c/fake" and item["outputSha256"] for item in summary["tasks"])
            assert [path.name for path in sorted((root / "out").glob("*.md"))] == [
                "01-ljg-think.md", "02-ljg-qa.md", "article-decode.md",
            ]
            assert not list((root / "out").glob(".*.md.*"))
            assert len(fake.prompts) == 3
            for prompt in fake.prompts:
                assert prompt.endswith(runner.BRIDGE_BOUNDARY + "\n")
                assert "【调用层边界】" in prompt
            for prompt in fake.prompts:
                parsed = task_input(prompt)
                name = runner.skill_name(prompt.split("\n\n", 1)[0] + "\n")
                if name == "article-decode":
                    assert runner.TEXT_RUNTIME_OVERRIDE not in prompt
                    assert runner.ARTICLE_RUNTIME_OVERRIDE in prompt
                else:
                    assert prompt.startswith(skill(name))
                    assert runner.TEXT_RUNTIME_OVERRIDE in prompt
                    assert runner.TEXT_TASK_REQUIREMENTS[name] in prompt
                assert parsed["source"] == source.read_text(encoding="utf-8")
                assert json.loads(parsed["evidence"]) == json.loads(evidence.read_text(encoding="utf-8"))
                assert ("question" in parsed) == (name != "article-decode")
    finally:
        restore()


def test_one_failure_keeps_other_outputs_and_stale_outputs_are_rejected():
    fake = FakeBridge({"article-decode": [succeeded("article-decode")], "ljg-think": [succeeded("ljg-think")], "ljg-qa": [{"status": "needs_review", "reason": "observer-timeout"}]})
    restore = patch_bridge(fake)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, evidence, article, skills, specs = fixture(root)
            output = root / "out"
            summary = runner.run(
                source, evidence, output, specs,
                article_skill_path=article, skill_roots=[skills],
            )
            assert summary["status"] == "partial"
            assert (output / "article-decode.md").is_file()
            assert (output / "01-ljg-think.md").is_file()
            assert not (output / "02-ljg-qa.md").exists()
            failed = next(item for item in summary["tasks"] if item["task"] == "ljg-qa")
            assert failed["status"] == "failed"
            assert failed["error_type"] == "observer-timeout"
            try:
                runner.run(
                    source, evidence, output, [],
                    article_skill_path=article, skill_roots=[skills],
                )
            except ValueError as exc:
                assert "output files already exist" in str(exc)
            else:
                raise AssertionError("stale output must be rejected")
    finally:
        restore()


def test_short_model_output_fails_closed():
    fake = FakeBridge({"article-decode": [succeeded("article-decode")], "ljg-think": [succeeded("ljg-think", text="短")]})
    restore = patch_bridge(fake)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, evidence, article, skills, specs = fixture(root)
            summary = runner.run(
                source, evidence, root / "out", specs[:1],
                article_skill_path=article, skill_roots=[skills],
            )
            failed = next(item for item in summary["tasks"] if item["task"] == "ljg-think")
            assert summary["status"] == "partial" and failed["status"] == "failed"
            assert "output is too short" in failed["error"]
            assert failed["error_type"] == "output_validation"
            assert not (root / "out/01-ljg-think.md").exists()
    finally:
        restore()


def test_pre_submit_cooldown_recovers_exactly_once():
    fake = FakeBridge({"article-decode": [succeeded("article-decode")], "ljg-think": [{"status": "needs_review", "reason": "local-rate-limit-cooldown", "retryAfterSeconds": 1}, succeeded("ljg-think")]})
    restore = patch_bridge(fake)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, evidence, article, skills, specs = fixture(root)
            summary = runner.run(
                source, evidence, root / "out", specs[:1],
                article_skill_path=article, skill_roots=[skills],
            )
            think = next(item for item in summary["tasks"] if item["task"] == "ljg-think")
            assert think["status"] == "completed" and think["attempts"] == 2
            assert [item["reason"] for item in think["attempts_detail"]] == ["local-rate-limit-cooldown", None]
            assert len(fake.calls) == 3
    finally:
        restore()


def test_uncertain_submission_is_never_resent():
    fake = FakeBridge({"article-decode": [succeeded("article-decode")], "ljg-think": [{"status": "needs_review", "reason": "observer-timeout"}]})
    restore = patch_bridge(fake)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, evidence, article, skills, specs = fixture(root)
            summary = runner.run(
                source, evidence, root / "out", specs[:1],
                article_skill_path=article, skill_roots=[skills],
            )
            think = next(item for item in summary["tasks"] if item["task"] == "ljg-think")
            assert think["status"] == "failed" and think["attempts"] == 1
            assert len(fake.calls) == 2
    finally:
        restore()


def test_oversized_prompt_fails_closed_without_bridge_call():
    fake = FakeBridge([])
    restore = patch_bridge(fake)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, evidence, article, skills, specs = fixture(root)
            huge = root / "huge.md"
            huge.write_text("可信原文。" + "长" * runner.MAX_PROMPT_CHARS, encoding="utf-8")
            summary = runner.run(
                huge, evidence, root / "out", [],
                article_skill_path=article, skill_roots=[skills],
            )
            article_result = summary["tasks"][0]
            assert article_result["status"] == "failed"
            assert article_result["error_type"] == "output_validation"
            assert "prompt too large" in article_result["error"]
            assert fake.calls == []
    finally:
        restore()


def test_invalid_evidence_fails_before_any_bridge_call():
    fake = FakeBridge([])
    restore = patch_bridge(fake)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, evidence, article, skills, specs = fixture(root)
            value = json.loads(evidence.read_text(encoding="utf-8"))
            value["user_profile"] = "FORBIDDEN_PROFILE_SENTINEL"
            evidence.write_text(json.dumps(value), encoding="utf-8")
            try:
                runner.run(
                    source, evidence, root / "out", specs[:1],
                    article_skill_path=article, skill_roots=[skills],
                )
            except ValueError as exc:
                assert "unexpected top-level key: user_profile" in str(exc)
            else:
                raise AssertionError("schema-external evidence must be rejected")
            assert fake.calls == []
    finally:
        restore()


def test_agentic_outputs_fail_closed():
    fake = FakeBridge({"article-decode": [succeeded("article-decode")], "ljg-think": [succeeded("ljg-think", text=(
        "## 第一层\n## 第二层\n## 第三层\n## 第四层\n```bash\ndate +%Y%m%d\n```\n" + "执行计划" * 200
    ))]})
    restore = patch_bridge(fake)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, evidence, article, skills, specs = fixture(root)
            summary = runner.run(
                source, evidence, root / "out", specs[:1],
                article_skill_path=article, skill_roots=[skills],
            )
            think = next(item for item in summary["tasks"] if item["task"] == "ljg-think")
            assert think["status"] == "failed"
            assert "forbidden tool artifacts" in think["error"]
            assert not (root / "out/01-ljg-think.md").exists()
    finally:
        restore()


def test_article_failure_is_fatal_but_keeps_independent_ljg_output():
    fake = FakeBridge({"article-decode": [{"status": "needs_review", "reason": "observer-timeout"}], "ljg-think": [succeeded("ljg-think")]})
    restore = patch_bridge(fake)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, evidence, article, skills, specs = fixture(root)
            output = root / "out"
            summary = runner.run(
                source, evidence, output, specs[:1],
                article_skill_path=article, skill_roots=[skills],
            )
            assert summary["status"] == "partial"
            assert not (output / "article-decode.md").exists()
            assert (output / "01-ljg-think.md").is_file()
            article_result = next(item for item in summary["tasks"] if item["task"] == "article-decode")
            assert article_result["status"] == "failed"
    finally:
        restore()


def test_text_output_hash_mismatch_fails_closed():
    bogus = succeeded("ljg-think")
    bogus["outputSha256"] = "0" * 64
    fake = FakeBridge({"article-decode": [succeeded("article-decode")], "ljg-think": [bogus]})
    restore = patch_bridge(fake)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, evidence, article, skills, specs = fixture(root)
            summary = runner.run(
                source, evidence, root / "out", specs[:1],
                article_skill_path=article, skill_roots=[skills],
            )
            think = next(item for item in summary["tasks"] if item["task"] == "ljg-think")
            assert think["status"] == "failed"
            assert "hash mismatch" in think["error"]
            assert not (root / "out/01-ljg-think.md").exists()
    finally:
        restore()


def main():
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in sorted(tests, key=lambda item: item.__name__):
        test()
        print(f"[ok] {test.__name__}")
    print(f"{len(tests)} passed, 0 failed")


if __name__ == "__main__":
    main()
