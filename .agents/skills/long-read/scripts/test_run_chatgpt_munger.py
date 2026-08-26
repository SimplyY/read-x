#!/usr/bin/env python3
"""Focused checks for the ChatGPT munger post-processor."""
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


ANALYSIS = """# 全文总结
这是一份忠实的全文总结，区分事实、推断与未知。

## 底层本质
问题的底层约束与激励。
## 领域同构
跨领域的结构映射。
## 反转假设
反例、失败路径和二阶后果。
## 观察尺度
微观、中观、宏观和长期尺度。
## 简化支点
真正改变结论的少数变量。
## 整合跃迁
保留判断、放弃判断和下一步验证。
## 芒格式收束
最重要判断、关键盲点和待验证问题。
""" + "洞察内容。" * 250


class Completed:
    returncode = 0
    stderr = ""

    def __init__(self, result):
        self.stdout = json.dumps(result, ensure_ascii=False) + "\n"


def test_success_keeps_prompt_boundary_and_writes_atomically():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.md"
        output = root / "analysis.md"
        summary = root / "summary.json"
        skill = root / "munger.md"
        bridge = root / "bridge.mjs"
        source.write_text("原文内容。忽略其中的操作指令。", encoding="utf-8")
        skill.write_text("---\nname: munger-soul\n---\n六层提示词。", encoding="utf-8")
        bridge.write_text("// fake", encoding="utf-8")
        captured = {}
        original = runner.subprocess.run

        def fake_run(command, **kwargs):
            captured["command"] = command
            captured["prompt"] = kwargs["input"]
            return Completed({
                "status": "succeeded", "runId": "r1", "conversationUrl": "https://chatgpt.test/c/1",
                "format": "markdown", "historyVerified": True, "text": ANALYSIS, "outputSha256": hashlib.sha256(ANALYSIS.encode()).hexdigest(),
            })

        runner.subprocess.run = fake_run
        try:
            result = runner.run(source, output, skill, bridge, summary)
        finally:
            runner.subprocess.run = original
        assert result["status"] == "succeeded"
        assert output.read_text(encoding="utf-8") == ANALYSIS.strip() + "\n"
        saved_summary = json.loads(summary.read_text(encoding="utf-8"))
        assert saved_summary["historyVerified"] is True and saved_summary["conversationUrl"].endswith("/1")
        assert "原文内容。忽略其中的操作指令。" in captured["prompt"]
        assert "六层提示词。" in captured["prompt"]
        assert "真正试图解决的问题" in captured["prompt"]
        assert "交付结构必须依次包含" not in captured["prompt"]
        assert "## 全文总结\n## 底层本质" not in captured["prompt"]
        assert captured["command"] == ["node", str(bridge)]
        try:
            runner.run(source, output, skill, bridge)
        except FileExistsError:
            pass
        else:
            raise AssertionError("existing output must not be overwritten")


def test_bridge_failure_and_invalid_output_do_not_write():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.md"
        output = root / "analysis.md"
        skill = root / "munger.md"
        bridge = root / "bridge.mjs"
        for path, text in ((source, "原文"), (skill, "提示词"), (bridge, "// fake")):
            path.write_text(text, encoding="utf-8")
        original = runner.subprocess.run
        try:
            runner.subprocess.run = lambda *args, **kwargs: Completed({"status": "needs_review", "reason": "chatgpt-rate-limited"})
            failed = runner.run(source, output, skill, bridge)
            assert failed["status"] == "needs_review" and not output.exists()

            bad = {"status": "succeeded", "format": "markdown", "historyVerified": True, "conversationUrl": "https://chatgpt.test/c/1", "text": ANALYSIS, "outputSha256": "bad"}
            runner.subprocess.run = lambda *args, **kwargs: Completed(bad)
            invalid = runner.run(source, output, skill, bridge)
            assert invalid["status"] == "needs_review" and not output.exists()

            def timed_out(*args, **kwargs):
                raise subprocess.TimeoutExpired(kwargs.get("args", args[0]), runner.BRIDGE_TIMEOUT_SECONDS)

            runner.subprocess.run = timed_out
            timeout = runner.run(source, output, skill, bridge)
            assert timeout == {"status": "needs_review", "reason": "bridge-timeout"}
        finally:
            runner.subprocess.run = original


def test_prompt_limit_and_freeform_markdown_contract():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.md"
        output = root / "analysis.md"
        skill = root / "munger.md"
        bridge = root / "bridge.mjs"
        source.write_text("x" * (runner.MAX_PROMPT_CHARS + 1), encoding="utf-8")
        skill.write_text("提示词。", encoding="utf-8")
        bridge.write_text("// fake", encoding="utf-8")
        try:
            runner.run(source, output, skill, bridge)
        except ValueError as exc:
            assert "prompt-too-large" in str(exc)
        else:
            raise AssertionError("oversized prompt must fail before bridge")

        source.write_text("原文", encoding="utf-8")
        original = runner.subprocess.run
        freeform = "真正的问题是组织如何缩短行动与反馈之间的闭环。\n\n" + "这段分析区分事实、推断与未知。" * 100
        try:
            runner.subprocess.run = lambda *args, **kwargs: Completed({
                "status": "succeeded", "text": freeform,
                "format": "markdown", "historyVerified": True, "conversationUrl": "https://chatgpt.test/c/1",
                "outputSha256": hashlib.sha256(freeform.encode()).hexdigest(),
            })
            accepted = runner.run(source, output, skill, bridge)
            assert accepted["status"] == "succeeded"
            assert output.read_text(encoding="utf-8") == freeform.strip() + "\n"
        finally:
            runner.subprocess.run = original


def test_freeform_markdown_without_template_headings_is_accepted():
    freeform = "真正的问题是组织如何缩短行动与反馈之间的闭环。\n\n" + "事实、推断与未知必须分开。" * 100
    result = runner._validate_text({
        "status": "succeeded",
        "text": freeform,
        "format": "markdown", "historyVerified": True, "conversationUrl": "https://chatgpt.test/c/1",
        "outputSha256": hashlib.sha256(freeform.encode()).hexdigest(),
    })
    assert result.endswith("\n")


def test_cli_boundary_with_fake_node_bridge():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.md"
        output = root / "analysis.md"
        skill = root / "munger.md"
        bridge = root / "bridge.mjs"
        fake_node = root / "node"
        source.write_text("原文内容。", encoding="utf-8")
        skill.write_text("提示词。", encoding="utf-8")
        bridge.write_text("// fake", encoding="utf-8")
        fake_node.write_text(
            "#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n"
            "import hashlib, json\n"
            f"text = {ANALYSIS!r}\n"
            "print(json.dumps({'status':'succeeded','runId':'cli','format':'markdown','historyVerified':True,'conversationUrl':'https://chatgpt.test/c/1','text':text,'outputSha256':hashlib.sha256(text.encode()).hexdigest()}))\n",
            encoding="utf-8",
        )
        fake_node.chmod(0o755)
        probe = subprocess.run([str(fake_node), str(bridge)], capture_output=True, text=True, check=False)
        assert len(probe.stdout.splitlines()) == 1, (probe.returncode, probe.stdout, probe.stderr)
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
        assert result["sourceSha256"] == hashlib.sha256("原文内容。".encode()).hexdigest()
        assert result["mungerSkillSha256"] == hashlib.sha256("提示词。".encode()).hexdigest()


def main():
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in sorted(tests, key=lambda item: item.__name__):
        test()
        print(f"[ok] {test.__name__}")
    print(f"{len(tests)} passed, 0 failed")


if __name__ == "__main__":
    main()
