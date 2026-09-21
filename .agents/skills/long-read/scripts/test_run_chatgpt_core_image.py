#!/usr/bin/env python3
"""Focused checks for run_chatgpt_core_image.py using a fake ChatGPT web-bridge."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
import tempfile
from pathlib import Path


SCRIPT = Path(__file__).with_name("run_chatgpt_core_image.py")
SPEC = importlib.util.spec_from_file_location("run_chatgpt_core_image", SCRIPT)
image_runner = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = image_runner
SPEC.loader.exec_module(image_runner)


def image_result(**extra) -> dict:
    payload = base64.b64encode(b"\x89PNG-fake-image").decode()
    data_url = f"data:image/png;base64,{payload}"
    value = {
        "status": "succeeded", "runId": "run-img", "conversationUrl": "https://chatgpt.com/c/img",
        "verification": "live-dom+snapshot", "imageBase64": data_url,
        "outputSha256": hashlib.sha256(data_url.encode("utf-8")).hexdigest(),
    }
    value.update(extra)
    return value


def doc_text(chars: int = 600) -> str:
    return ("核心结论与机制。" * (chars // 7 + 1))[:chars]


class FakeBridge:
    def __init__(self, results: list[dict]):
        self.results = list(results)
        self.prompts: list[str] = []
        self.calls: list[dict] = []
        self.sleeps: list[float] = []

    def __call__(self, prompt, *, bridge=None, max_wait_seconds=600, image=False, conversation_url=None, busy_retry_max_wait_seconds=600):
        self.prompts.append(prompt)
        self.calls.append({"max_wait_seconds": max_wait_seconds, "image": image, "conversation_url": conversation_url})
        if not self.results:
            raise AssertionError("unexpected extra bridge call")
        return self.results.pop(0)


def patch(fake: FakeBridge):
    original_bridge = image_runner.run_bridge
    original_sleep = image_runner.time.sleep
    image_runner.run_bridge = fake
    image_runner.time.sleep = fake.sleeps.append
    return lambda: (setattr(image_runner, "run_bridge", original_bridge), setattr(image_runner, "time.sleep", original_sleep))


def test_success_writes_png_and_summary_metadata():
    fake = FakeBridge([image_result()])
    restore = patch(fake)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "main-doc.md"
            source.write_text(doc_text(), encoding="utf-8")
            output = root / "core-image.png"
            summary_path = root / "core-image-summary.json"
            result = image_runner.run(source, output, summary_path=summary_path)
            assert result["status"] == "succeeded"
            assert output.is_file() and output.read_bytes() == b"\x89PNG-fake-image"
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            assert summary["status"] == "succeeded" and summary["image_bytes"] == len(b"\x89PNG-fake-image")
            assert summary["conversationUrl"] == "https://chatgpt.com/c/img"
            assert summary["instruction_sha256"] == hashlib.sha256(image_runner.CORE_IMAGE_INSTRUCTION.encode()).hexdigest()
            assert summary["source_sha256"] == hashlib.sha256(doc_text().encode()).hexdigest()
            assert fake.calls == [{"max_wait_seconds": 600, "image": True, "conversation_url": None}]
            prompt = fake.prompts[0]
            assert image_runner.CORE_IMAGE_INSTRUCTION in prompt
            assert doc_text() in prompt
            assert prompt.endswith(image_runner.BRIDGE_BOUNDARY + "\n")
    finally:
        restore()


def test_failure_is_recorded_and_never_resent():
    fake = FakeBridge([{"status": "needs_review", "reason": "observer-timeout"}])
    restore = patch(fake)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "main-doc.md"
            source.write_text(doc_text(), encoding="utf-8")
            output = root / "core-image.png"
            summary_path = root / "core-image-summary.json"
            result = image_runner.run(source, output, summary_path=summary_path)
            assert result["status"] == "needs_review" and result["reason"] == "observer-timeout"
            assert len(result["attempts"]) == 1
            assert not output.exists()
            assert json.loads(summary_path.read_text(encoding="utf-8"))["status"] == "needs_review"
            assert len(fake.calls) == 1
    finally:
        restore()


def test_pre_submit_cooldown_recovers_exactly_once():
    fake = FakeBridge([
        {"status": "needs_review", "reason": "local-rate-limit-cooldown", "retryAfterSeconds": 2},
        image_result(),
    ])
    restore = patch(fake)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "main-doc.md"
            source.write_text(doc_text(), encoding="utf-8")
            result = image_runner.run(source, root / "core-image.png")
            assert result["status"] == "succeeded"
            assert fake.sleeps == [2]
            assert len(fake.calls) == 2
    finally:
        restore()


def test_conversation_url_passthrough_and_cooldown_reuse():
    url = "https://chatgpt.com/c/existing-session"
    fake = FakeBridge([
        {"status": "needs_review", "reason": "local-rate-limit-cooldown", "retryAfterSeconds": 1},
        image_result(),
    ])
    restore = patch(fake)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "main-doc.md"
            source.write_text(doc_text(), encoding="utf-8")
            result = image_runner.run(source, root / "core-image.png", conversation_url=url)
            assert result["status"] == "succeeded"
            assert all(call["conversation_url"] == url for call in fake.calls)
            assert len(fake.calls) == 2
    finally:
        restore()


def test_hash_mismatch_and_short_doc_fail_closed():
    fake = FakeBridge([image_result(outputSha256="0" * 64)])
    restore = patch(fake)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "main-doc.md"
            source.write_text(doc_text(), encoding="utf-8")
            result = image_runner.run(source, root / "core-image.png")
            assert result["status"] == "needs_review" and "hash mismatch" in result["reason"]
            assert not (root / "core-image.png").exists()

            short = root / "short.md"
            short.write_text("太短。", encoding="utf-8")
            try:
                image_runner.run(short, root / "core-image.png")
            except ValueError as exc:
                assert "document too short" in str(exc)
            else:
                raise AssertionError("short document must fail before any bridge call")
            assert len(fake.calls) == 1
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
