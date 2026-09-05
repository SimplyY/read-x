"""Contract checks for the shared ChatGPT Bridge adapter."""
from __future__ import annotations

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


def main():
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in sorted(tests, key=lambda item: item.__name__):
        test()
    print(f"{len(tests)} tests passed")


if __name__ == "__main__":
    main()
