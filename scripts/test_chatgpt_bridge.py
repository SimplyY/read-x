"""Contract checks for the shared ChatGPT Bridge adapter."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import chatgpt_bridge as bridge


def result(text="{}", **extra):
    value = {"status": "succeeded", "runId": "r1", "conversationUrl": "https://chatgpt.test/c/1",
             "format": "markdown", "verification": "live-dom+snapshot", "text": text,
             "outputSha256": hashlib.sha256(text.encode()).hexdigest()}
    value.update(extra)
    return value


def image_result(**extra):
    payload = base64.b64encode(b"\x89PNG-fake").decode()
    data_url = f"data:image/png;base64,{payload}"
    value = {"status": "succeeded", "runId": "r2", "conversationUrl": "https://chatgpt.test/c/2",
             "verification": "live-dom+snapshot", "imageBase64": data_url,
             "outputSha256": hashlib.sha256(data_url.encode()).hexdigest()}
    value.update(extra)
    return value


def test_stdout_is_one_object():
    assert bridge._parse_stdout('{"status":"failed"}\n') == {"status": "failed"}
    for value in ("", "{}\n{}\n", "[]\n"):
        try:
            bridge._parse_stdout(value)
        except (ValueError, json.JSONDecodeError):
            pass
        else:
            raise AssertionError("ambiguous bridge stdout must fail")


def test_verified_text_requires_current_contract():
    assert bridge.verified_text(result("正文")) == "正文"
    for bad in (result("正文", verification="historyVerified"), result("正文", format="text"),
                result("正文", conversationUrl="https://chatgpt.test/share/1"), result("正文", outputSha256="bad")):
        try:
            bridge.verified_text(bad)
        except RuntimeError:
            pass
        else:
            raise AssertionError("legacy or unverifiable bridge result must fail")


def test_json_text_accepts_only_raw_or_fenced_object():
    assert bridge.json_text(result('{"ok":true}'), "x") == {"ok": True}
    assert bridge.json_text(result("```json\n{\"ok\":true}\n```"), "x") == {"ok": True}
    try:
        bridge.json_text(result("说明\n{\"ok\":true}"), "x")
    except RuntimeError:
        pass
    else:
        raise AssertionError("prose around JSON must fail")


def test_command_streams_prompt_and_disables_only_success_cooldown():
    command = bridge.bridge_command(Path("/tmp/bridge.mjs"), max_wait_seconds=321)[-1]
    assert "for await (const chunk of process.stdin)" in command
    assert "maxWaitSeconds: 321" in command
    assert "successCooldownSeconds: 0" in command
    assert "image: false" in command
    image_command = bridge.bridge_command(Path("/tmp/bridge.mjs"), image=True)[-1]
    assert "image: true" in image_command


def test_verified_image_requires_current_contract():
    assert bridge.verified_image(image_result()) == b"\x89PNG-fake"
    for bad in (image_result(verification="historyVerified"),
                image_result(conversationUrl="https://chatgpt.test/share/2"),
                image_result(outputSha256="bad"),
                image_result(imageBase64="data:text/plain;base64,SGk="),
                image_result(imageBase64="data:image/png;base64,%%%not-base64%%%")):
        try:
            bridge.verified_image(bad)
        except RuntimeError:
            pass
        else:
            raise AssertionError("legacy or unverifiable image result must fail")


def test_process_timeout_fails_closed():
    original = bridge.subprocess.run

    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 330
        raise bridge.subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    bridge.subprocess.run = timeout
    try:
        result = bridge.run_bridge("prompt", bridge=Path("/tmp/bridge.mjs"), max_wait_seconds=300)
    finally:
        bridge.subprocess.run = original
    assert result == {"status": "needs_review", "reason": "bridge-process-timeout"}


def test_busy_rejection_waits_for_a_free_slot_once_then_succeeds():
    attempts = []
    sleeps = []
    original_once = bridge._run_bridge_once
    original_sleep = bridge.time.sleep

    def fake_once(prompt, *, bridge, max_wait_seconds, image, conversation_url=None):
        attempts.append(1)
        if len(attempts) == 1:
            return {"status": "needs_review", "reason": "bridge-busy", "retryAfterSeconds": 30}
        return {"status": "succeeded"}

    bridge._run_bridge_once = fake_once
    bridge.time.sleep = sleeps.append
    try:
        result = bridge.run_bridge("prompt", bridge=Path("/tmp/bridge.mjs"))
    finally:
        bridge._run_bridge_once = original_once
        bridge.time.sleep = original_sleep
    assert result["status"] == "succeeded"
    assert result["busyRetries"] == [{"reason": "bridge-busy", "waited_seconds": 30.0}]
    assert sleeps == [30.0]


def test_busy_rejection_honors_wait_budget():
    attempts = []
    original_once = bridge._run_bridge_once

    def fake_once(prompt, *, bridge, max_wait_seconds, image, conversation_url=None):
        attempts.append(1)
        return {"status": "needs_review", "reason": "bridge-busy", "retryAfterSeconds": 30}

    bridge._run_bridge_once = fake_once
    try:
        result = bridge.run_bridge("prompt", bridge=Path("/tmp/bridge.mjs"), busy_retry_max_wait_seconds=0)
    finally:
        bridge._run_bridge_once = original_once
    assert result["reason"] == "bridge-busy"
    assert len(attempts) == 1
    assert "busyRetries" not in result


def test_conversation_url_is_passed_to_bridge_once():
    original_once = bridge._run_bridge_once
    seen = []

    def fake_once(prompt, *, bridge, max_wait_seconds, image, conversation_url=None):
        seen.append(conversation_url)
        return {"status": "succeeded"}

    bridge._run_bridge_once = fake_once
    try:
        bridge.run_bridge("prompt", bridge=Path("/tmp/bridge.mjs"),
                          conversation_url="https://chatgpt.test/c/1")
    finally:
        bridge._run_bridge_once = original_once
    assert seen == ["https://chatgpt.test/c/1"]


def main():
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in sorted(tests, key=lambda item: item.__name__):
        test()
    print(f"{len(tests)} tests passed")


if __name__ == "__main__":
    main()
