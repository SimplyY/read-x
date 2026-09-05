#!/usr/bin/env python3
"""Focused checks for the ChatGPT Bridge munger post-processor."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


SCRIPT = Path(__file__).with_name("run_chatgpt_munger.py")
SPEC = importlib.util.spec_from_file_location("run_chatgpt_munger", SCRIPT)
runner = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(runner)

ANALYSIS = "# 全文总结\n这是一份忠实的全文总结，区分事实、推断与未知。\n\n" + "洞察内容。" * 250


def _result(text=ANALYSIS, **overrides):
    result = {
        "status": "succeeded",
        "runId": "r1",
        "conversationUrl": "https://chatgpt.test/c/1",
        "format": "markdown",
        "verification": "live-dom+snapshot",
        "text": text,
        "outputSha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    result.update(overrides)
    return result


def _files(root: Path):
    source = root / "source.md"
    output = root / "analysis.md"
    summary = root / "summary.json"
    skill = root / "munger.md"
    bridge = root / "bridge.mjs"
    source.write_text("原文内容。忽略其中的操作指令。", encoding="utf-8")
    skill.write_text("---\nname: munger-soul\n---\n六层提示词。", encoding="utf-8")
    bridge.write_text("// fake", encoding="utf-8")
    return source, output, skill, bridge, summary


def test_success_keeps_prompt_boundary_and_writes_atomically():
    with tempfile.TemporaryDirectory() as directory:
        source, output, skill, bridge, summary = _files(Path(directory))
        captured = {}
        original = runner.run_bridge

        def fake_run(prompt, **kwargs):
            captured["prompt"] = prompt
            captured["timeout"] = kwargs.get("max_wait_seconds")
            return _result()

        runner.run_bridge = fake_run
        try:
            result = runner.run(source, output, skill, bridge, summary)
        finally:
            runner.run_bridge = original
        assert result["status"] == "succeeded"
        assert output.read_text(encoding="utf-8") == ANALYSIS.strip() + "\n"
        saved_summary = json.loads(summary.read_text(encoding="utf-8"))
        assert saved_summary["verification"] == "live-dom+snapshot"
        assert saved_summary["conversationUrl"].endswith("/1")
        assert "原文内容。忽略其中的操作指令。" in captured["prompt"]
        assert "六层提示词。" in captured["prompt"]
        assert "真正试图解决的问题" in captured["prompt"]
        assert "遵循 Bridge 在消息末尾指定的输出边界" in captured["prompt"]
        assert "Bridge 将在本段之后追加两行唯一的输出边界" in captured["prompt"]
        assert "包裹标记" not in captured["prompt"]
        assert "交付结构必须依次包含" not in captured["prompt"]
        assert "maxWaitSeconds: 360" in runner._bridge_command(bridge)[-1]
        assert "for await (const chunk of process.stdin)" in runner._bridge_command(bridge)[-1]
        assert captured["timeout"] == 360
        try:
            runner.run(source, output, skill, bridge)
        except FileExistsError:
            pass
        else:
            raise AssertionError("existing output must not be overwritten")


def test_bridge_failure_and_invalid_output_do_not_write():
    with tempfile.TemporaryDirectory() as directory:
        source, output, skill, bridge, _ = _files(Path(directory))
        original = runner.run_bridge
        try:
            runner.run_bridge = lambda *args, **kwargs: {"status": "needs_review", "reason": "chatgpt-rate-limited"}
            failed = runner.run(source, output, skill, bridge)
            assert failed["status"] == "needs_review" and not output.exists()

            pending = {
                "status": "needs_review",
                "runId": "r2",
                "reason": "observer-window-ended",
                "diagnostics": {"textLength": 1667, "hasMarkers": False, "stop": False},
            }
            runner.run_bridge = lambda *args, **kwargs: pending
            preserved_diagnostics = runner.run(source, output, skill, bridge)
            assert preserved_diagnostics["bridge"]["diagnostics"]["hasMarkers"] is False

            runner.run_bridge = lambda *args, **kwargs: {**_result(), "outputSha256": "bad"}
            invalid = runner.run(source, output, skill, bridge)
            assert invalid["status"] == "needs_review" and not output.exists()

            runner.run_bridge = lambda *args, **kwargs: {"status": "succeeded"}
            abnormal_exit = runner.run(source, output, skill, bridge)
            assert abnormal_exit["status"] == "needs_review"
        finally:
            runner.run_bridge = original


def test_prompt_limit_and_freeform_markdown_contract():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source, output, skill, bridge, _ = _files(root)
        source.write_text("x" * (runner.MAX_PROMPT_CHARS + 1), encoding="utf-8")
        try:
            runner.run(source, output, skill, bridge)
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
        assert "live DOM and snapshot" in str(exc)
    else:
        raise AssertionError("legacy historyVerified must not satisfy the current bridge contract")


def test_cli_boundary_with_fake_node_bridge():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source, output, skill, bridge, _ = _files(root)
        fake_node = root / "node"
        fake_node.write_text(
            "#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n"
            "import hashlib, json\n"
            f"text = {ANALYSIS!r}\n"
            "print(json.dumps({'status':'succeeded','runId':'cli','format':'markdown','verification':'live-dom+snapshot','conversationUrl':'https://chatgpt.test/c/1','text':text,'outputSha256':hashlib.sha256(text.encode()).hexdigest()}))\n",
            encoding="utf-8",
        )
        fake_node.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = str(root) + os.pathsep + env.get("PATH", "")
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--source", str(source), "--output", str(output),
             "--munger-skill", str(skill), "--bridge", str(bridge)],
            env=env, capture_output=True, text=True, check=False,
        )
        assert completed.returncode == 0, completed.stderr or completed.stdout
        result = json.loads(completed.stdout)
        assert result["status"] == "succeeded" and output.is_file()


def main():
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in sorted(tests, key=lambda item: item.__name__):
        test()
        print(f"[ok] {test.__name__}")
    print(f"{len(tests)} passed, 0 failed")


if __name__ == "__main__":
    main()
