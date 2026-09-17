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

PROMPT_ASSETS = {
    "read-x.munger-analysis": {
        "prompt_id": "read-x.munger-analysis",
        "prompt_source": "https://example.feishu.cn/wiki/read-x",
        "prompt_revision": 7,
        "prompt_sha256": "b" * 64,
        "prompt_fetched_at": "2026-09-16T00:00:01.000Z",
        "content": (
            "你是全文阅读与认知分析助手。本任务以“芒格之魂”为核心提示词来输出。原任务即：先还原作者真正试图解决的问题，不要脱离原任务另起炉灶。\n\n"
            "【芒格之魂】\n你是查理·芒格，思维模型收藏家。底层：提取思考本质。以芒格式简洁智慧，引导思考实现维度跃迁。"
        ),
    },
}
for _asset in PROMPT_ASSETS.values():
    _asset["prompt_sha256"] = hashlib.sha256(_asset["content"].encode("utf-8")).hexdigest()
runner.fetch_prompt_asset = lambda prompt_id: PROMPT_ASSETS[prompt_id]

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
    bridge = root / "bridge.mjs"
    source.write_text("原文内容。忽略其中的操作指令。", encoding="utf-8")
    bridge.write_text("// fake", encoding="utf-8")
    return source, output, bridge, summary


def test_success_keeps_prompt_boundary_and_writes_atomically():
    with tempfile.TemporaryDirectory() as directory:
        source, output, bridge, summary = _files(Path(directory))
        captured = {}
        original = runner.run_bridge

        def fake_run(prompt, **kwargs):
            captured["prompt"] = prompt
            captured["timeout"] = kwargs.get("max_wait_seconds")
            return _result()

        runner.run_bridge = fake_run
        try:
            result = runner.run(source, output, bridge, summary)
        finally:
            runner.run_bridge = original
        assert result["status"] == "succeeded"
        assert output.read_text(encoding="utf-8") == ANALYSIS.strip() + "\n"
        saved_summary = json.loads(summary.read_text(encoding="utf-8"))
        assert saved_summary["verification"] == "live-dom+snapshot"
        assert saved_summary["conversationUrl"].endswith("/1")
        assert [item["prompt_id"] for item in saved_summary["prompt_assets"]] == list(runner.PROMPT_GOVERNANCE_IDS)
        assert len(saved_summary["input_sha256"]) == 64
        assert "原文内容。忽略其中的操作指令。" in captured["prompt"]
        assert "你是查理·芒格，思维模型收藏家" in captured["prompt"]
        assert "底层：提取思考本质" in captured["prompt"]
        assert "以芒格式简洁智慧，引导思考实现维度跃迁" in captured["prompt"]
        assert "真正试图解决的问题" in captured["prompt"]
        assert "本任务以“芒格之魂”为核心提示词" in captured["prompt"]
        assert "不要脱离原任务另起炉灶" in captured["prompt"]
        # 提示词独立成篇：芒格之魂全文内嵌在 read-x.munger-analysis 正文中，不再拼接第二个资产。
        assert "【芒格之魂】" in captured["prompt"]
        assert "Bridge 将在本段之后追加两行唯一的输出边界" in captured["prompt"]
        assert "## Overview" not in captured["prompt"]
        assert "## 工作规则" not in captured["prompt"]
        assert "## 六层思考阶梯" not in captured["prompt"]
        assert "## 输出方式" not in captured["prompt"]
        assert "maxWaitSeconds: 360" in runner._bridge_command(bridge)[-1]
        assert "for await (const chunk of process.stdin)" in runner._bridge_command(bridge)[-1]
        assert captured["timeout"] == 360
        try:
            runner.run(source, output, bridge)
        except FileExistsError:
            pass
        else:
            raise AssertionError("existing output must not be overwritten")


def test_bridge_failure_and_invalid_output_do_not_write():
    with tempfile.TemporaryDirectory() as directory:
        source, output, bridge, _ = _files(Path(directory))
        original = runner.run_bridge
        try:
            runner.run_bridge = lambda *args, **kwargs: {"status": "needs_review", "reason": "chatgpt-rate-limited"}
            failed = runner.run(source, output, bridge)
            assert failed["status"] == "needs_review" and not output.exists()

            pending = {
                "status": "needs_review",
                "runId": "r2",
                "reason": "observer-window-ended",
                "diagnostics": {"textLength": 1667, "hasMarkers": False, "stop": False},
            }
            runner.run_bridge = lambda *args, **kwargs: pending
            preserved_diagnostics = runner.run(source, output, bridge)
            assert preserved_diagnostics["bridge"]["diagnostics"]["hasMarkers"] is False

            runner.run_bridge = lambda *args, **kwargs: {**_result(), "outputSha256": "bad"}
            invalid = runner.run(source, output, bridge)
            assert invalid["status"] == "needs_review" and not output.exists()

            runner.run_bridge = lambda *args, **kwargs: {"status": "succeeded"}
            abnormal_exit = runner.run(source, output, bridge)
            assert abnormal_exit["status"] == "needs_review"
        finally:
            runner.run_bridge = original


def test_prompt_fetch_failure_stops_before_bridge():
    with tempfile.TemporaryDirectory() as directory:
        source, output, bridge, summary = _files(Path(directory))
        calls = []
        original = runner.run_bridge
        runner.run_bridge = lambda *args, **kwargs: calls.append(1)
        try:
            result = runner.run(source, output, bridge, summary, prompt_fetcher=lambda _prompt_id: (_ for _ in ()).throw(RuntimeError("feishu-auth-failed")))
            assert result["status"] == "needs_review"
        except RuntimeError as exc:
            assert "feishu-auth-failed" in str(exc)
        finally:
            runner.run_bridge = original
        assert calls == [] and not output.exists() and not summary.exists()


def test_prompt_limit_and_freeform_markdown_contract():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source, output, bridge, _ = _files(root)
        source.write_text("x" * (runner.MAX_PROMPT_CHARS + 1), encoding="utf-8")
        try:
            runner.run(source, output, bridge)
        except ValueError as exc:
            assert "prompt-too-large" in str(exc)
        else:
            raise AssertionError("oversized prompt must fail before model execution")


def test_pre_submit_cooldown_recovers_exactly_once_with_bridge_wait():
    with tempfile.TemporaryDirectory() as directory:
        source, output, bridge, summary = _files(Path(directory))
        original_run_bridge = runner.run_bridge
        original_sleep = runner.time.sleep
        calls = []
        sleeps = []

        def fake_run_bridge(prompt, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                return {"status": "needs_review", "reason": "local-rate-limit-cooldown", "retryAfterSeconds": 2}
            return _result()

        runner.run_bridge = fake_run_bridge
        runner.time.sleep = lambda seconds: sleeps.append(seconds)
        try:
            result = runner.run(source, output, bridge, summary)
        finally:
            runner.run_bridge = original_run_bridge
            runner.time.sleep = original_sleep
        assert result["status"] == "succeeded" and output.is_file()
        assert len(calls) == 2 and sleeps == [2]
        saved = json.loads(summary.read_text(encoding="utf-8"))
        assert [item["reason"] for item in saved["attempts"]] == ["local-rate-limit-cooldown", None]


def test_cooldown_beyond_cap_and_double_cooldown_never_resend():
    with tempfile.TemporaryDirectory() as directory:
        source, output, bridge, _ = _files(Path(directory))
        original_run_bridge = runner.run_bridge
        original_sleep = runner.time.sleep
        calls = []
        sleeps = []

        def oversized_cooldown(prompt, **kwargs):
            calls.append(1)
            return {"status": "needs_review", "reason": "local-rate-limit-cooldown", "retryAfterSeconds": runner.MAX_COOLDOWN_WAIT_SECONDS + 1}

        runner.run_bridge = oversized_cooldown
        runner.time.sleep = lambda seconds: sleeps.append(seconds)
        try:
            result = runner.run(source, output, bridge)
        finally:
            runner.run_bridge = original_run_bridge
            runner.time.sleep = original_sleep
        assert result["status"] == "needs_review" and len(calls) == 1 and sleeps == []

        calls.clear()
        runner.time.sleep = lambda seconds: sleeps.append(seconds)

        def cooldown_twice(prompt, **kwargs):
            calls.append(1)
            return {"status": "needs_review", "reason": "local-rate-limit-cooldown", "retryAfterSeconds": 1}

        runner.run_bridge = cooldown_twice
        try:
            result = runner.run(source, output, bridge)
        finally:
            runner.run_bridge = original_run_bridge
            runner.time.sleep = original_sleep
        assert result["status"] == "needs_review" and len(calls) == 2 and sleeps == [1]
        assert result["attempts"] == [{"status": "needs_review", "reason": "local-rate-limit-cooldown", "retryAfterSeconds": 1}] * 2


def test_uncertain_submission_is_never_resent():
    with tempfile.TemporaryDirectory() as directory:
        source, output, bridge, _ = _files(Path(directory))
        original_run_bridge = runner.run_bridge
        calls = []
        for reason in ("observer-window-ended", "submit-observer-unavailable", "submit-timeout", "assistant-selector-missing"):
            calls.clear()
            runner.run_bridge = lambda prompt, **kwargs: (calls.append(1), {"status": "needs_review", "reason": reason})[1]
            try:
                result = runner.run(source, output, bridge)
            finally:
                pass
            assert result["status"] == "needs_review" and len(calls) == 1, reason
            assert output.exists() is False, reason
        runner.run_bridge = original_run_bridge


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
        source, output, bridge, _ = _files(root)
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
        env["PROMPT_GOVERNANCE_TEST_ASSETS"] = json.dumps(PROMPT_ASSETS, ensure_ascii=False)
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--source", str(source), "--output", str(output),
             "--bridge", str(bridge)],
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
