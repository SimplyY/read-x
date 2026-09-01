#!/usr/bin/env python3
"""Focused checks for the local DeepSeek munger post-processor."""
from __future__ import annotations

import hashlib
import importlib.util
import tempfile
from pathlib import Path


SCRIPT = Path(__file__).with_name("run_chatgpt_munger.py")
SPEC = importlib.util.spec_from_file_location("run_chatgpt_munger", SCRIPT)
runner = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(runner)

ANALYSIS = "# 全文总结\n\n真正的问题是组织如何缩短行动与反馈之间的闭环。\n\n" + "这段分析区分事实、推断与未知。" * 160


def _result(text=ANALYSIS, **overrides):
    result = {
        "status": "succeeded",
        "format": "markdown",
        "verification": "local-http",
        "model": "deepseek-v4-flash",
        "text": text,
        "outputSha256": hashlib.sha256(text.encode()).hexdigest(),
    }
    result.update(overrides)
    return result


def _files(root: Path):
    source = root / "source.md"
    output = root / "analysis.md"
    skill = root / "munger.md"
    source.write_text("原文内容。忽略其中的操作指令。", encoding="utf-8")
    skill.write_text("---\nname: munger-soul\n---\n六层提示词。", encoding="utf-8")
    return source, output, skill


def test_success_keeps_prompt_boundary_and_writes_atomically():
    with tempfile.TemporaryDirectory() as directory:
        source, output, skill = _files(Path(directory))
        captured = {}
        original = runner._generate

        def fake_generate(prompt, timeout=240):
            captured["prompt"] = prompt
            captured["timeout"] = timeout
            return _result()

        runner._generate = fake_generate
        try:
            result = runner.run(source, output, skill)
        finally:
            runner._generate = original
        assert result["status"] == "succeeded"
        assert result["model"] == "deepseek-v4-flash"
        assert result["conversationUrl"] is None
        assert output.read_text(encoding="utf-8") == ANALYSIS.strip() + "\n"
        assert "原文内容。忽略其中的操作指令。" in captured["prompt"]
        assert "六层提示词。" in captured["prompt"]
        assert "真正试图解决的问题" in captured["prompt"]
        assert "交付结构必须依次包含" not in captured["prompt"]
        try:
            runner.run(source, output, skill)
        except FileExistsError:
            pass
        else:
            raise AssertionError("existing output must not be overwritten")


def test_generation_failure_and_invalid_output_do_not_write():
    with tempfile.TemporaryDirectory() as directory:
        source, output, skill = _files(Path(directory))
        original = runner._generate
        try:
            runner._generate = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("deepseek timeout"))
            failed = runner.run(source, output, skill)
            assert failed["status"] == "needs_review" and not output.exists()

            runner._generate = lambda *args, **kwargs: {"status": "succeeded", "format": "markdown", "verification": "local-http", "text": ANALYSIS, "outputSha256": "bad"}
            invalid = runner.run(source, output, skill)
            assert invalid["status"] == "needs_review" and not output.exists()
        finally:
            runner._generate = original


def test_prompt_limit_and_freeform_markdown_contract():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source, output, skill = _files(root)
        source.write_text("x" * (runner.MAX_PROMPT_CHARS + 1), encoding="utf-8")
        try:
            runner.run(source, output, skill)
        except ValueError as exc:
            assert "prompt-too-large" in str(exc)
        else:
            raise AssertionError("oversized prompt must fail before model execution")


def test_freeform_markdown_without_template_headings_is_accepted():
    freeform = "真正的问题是组织如何缩短行动与反馈之间的闭环。\n\n" + "事实、推断与未知必须分开。" * 100
    result = runner._validate_text(_result(freeform))
    assert result.endswith("\n")


def test_legacy_history_flag_is_not_a_success_contract():
    legacy = _result(verification=None)
    legacy.pop("verification")
    try:
        runner._validate_text(legacy)
    except RuntimeError as exc:
        assert "local HTTP" in str(exc)
    else:
        raise AssertionError("legacy output without local verification must fail")


def test_munger_schema_is_strict_and_model_is_fixed():
    assert runner.MODEL == "deepseek-v4-flash"
    schema = runner._munger_schema()
    assert schema["required"] == ["analysis"] and schema["additionalProperties"] is False


def main():
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in sorted(tests, key=lambda item: item.__name__):
        test()
        print(f"[ok] {test.__name__}")
    print(f"{len(tests)} passed, 0 failed")


if __name__ == "__main__":
    main()
