#!/usr/bin/env python3
"""Focused checks for run_chatgpt_core_image.py using a fake ChatGPT web-bridge."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import struct
import sys
import tempfile
import zlib
from pathlib import Path


SCRIPT = Path(__file__).with_name("run_chatgpt_core_image.py")
SPEC = importlib.util.spec_from_file_location("run_chatgpt_core_image", SCRIPT)
image_runner = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = image_runner
SPEC.loader.exec_module(image_runner)


REQUEST_ID = "123e4567-e89b-42d3-a456-426614174000"


def png_bytes() -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")) + chunk(b"IEND", b""))


def image_result(**extra) -> dict:
    payload = base64.b64encode(png_bytes()).decode()
    data_url = f"data:image/png;base64,{payload}"
    value = {
        "status": "succeeded", "runId": "run-img", "conversationUrl": "https://chatgpt.com/c/img",
        "actualConversationUrl": "https://chatgpt.com/c/img", "submissionConfirmed": True,
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

    def __call__(self, prompt, *, bridge=None, max_wait_seconds=600, image=False, conversation_url=None, busy_retry_max_wait_seconds=600, caller_label=None, request_id=None):
        self.prompts.append(prompt)
        self.calls.append({"max_wait_seconds": max_wait_seconds, "image": image, "conversation_url": conversation_url, "caller_label": caller_label, "request_id": request_id, "busy_retry_max_wait_seconds": busy_retry_max_wait_seconds})
        if not self.results:
            raise AssertionError("unexpected extra bridge call")
        return self.results.pop(0)


def patch(fake: FakeBridge):
    original_bridge = image_runner.run_bridge
    original_sleep = image_runner.time.sleep
    image_runner.run_bridge = fake
    image_runner.time.sleep = fake.sleeps.append
    return lambda: (setattr(image_runner, "run_bridge", original_bridge), setattr(image_runner, "time.sleep", original_sleep))


def prompt_asset():
    content = "# 任务\n\n生成核心信息图。\n\n# 内容\n\n图中所有可见文字必须与上下文内容一致。\n\n# 视觉\n\n干净可读。"
    return {
        "prompt_id": image_runner.PROMPT_GOVERNANCE_ID,
        "prompt_source": "https://example.invalid/wiki/prompt",
        "prompt_revision": 1,
        "prompt_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "prompt_fetched_at": "2026-01-01T00:00:00.000Z",
        "content": content,
    }


def test_success_fetches_governance_prompt_writes_png_and_summary():
    fake = FakeBridge([image_result()])
    restore = patch(fake)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "main-doc.md"
            source.write_text(doc_text(), encoding="utf-8")
            output = root / "core-image.png"
            summary_path = root / "core-image-summary.json"
            fetched_assets = []

            def fetcher(prompt_id):
                fetched_assets.append(prompt_id)
                return prompt_asset()

            result = image_runner.run(source, output, summary_path=summary_path, prompt_fetcher=fetcher)
            assert result["status"] == "succeeded"
            assert output.is_file() and output.read_bytes() == png_bytes()
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            assert summary["status"] == "succeeded" and summary["image_bytes"] == len(png_bytes())
            assert summary["image_sha256"] == hashlib.sha256(png_bytes()).hexdigest()
            assert summary["submissionConfirmed"] is True
            assert summary["conversationUrl"] == "https://chatgpt.com/c/img"
            assert fetched_assets == [image_runner.PROMPT_GOVERNANCE_ID]
            assert summary["prompt_id"] == image_runner.PROMPT_GOVERNANCE_ID
            assert summary["prompt_revision"] == 1
            assert summary["prompt_sha256"] == prompt_asset()["prompt_sha256"]
            assert summary["source_sha256"] == hashlib.sha256(doc_text().encode()).hexdigest()
            assert len(fake.calls) == 1
            assert fake.calls[0] == {"max_wait_seconds": 600, "image": True, "conversation_url": image_runner.WORKFLOW_NEW_CHAT_URL, "caller_label": "read-x:long-read-image", "request_id": None, "busy_retry_max_wait_seconds": 600}
            prompt = fake.prompts[0]
            assert "# 任务" in prompt
            assert "图中所有可见文字必须与上下文内容一致" in prompt
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
            result = image_runner.run(source, output, summary_path=summary_path, prompt_fetcher=lambda _: prompt_asset())
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
            result = image_runner.run(source, root / "core-image.png", prompt_fetcher=lambda _: prompt_asset())
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
            result = image_runner.run(source, root / "core-image.png", conversation_url=url, prompt_fetcher=lambda _: prompt_asset())
            assert result["status"] == "succeeded"
            assert all(call["conversation_url"] == url for call in fake.calls)
            assert len(fake.calls) == 2
    finally:
        restore()


def test_governance_prompt_failure_stops_before_bridge():
    fake = FakeBridge([image_result()])
    restore = patch(fake)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "main-doc.md"
            source.write_text(doc_text(), encoding="utf-8")

            def fetcher(prompt_id):
                raise RuntimeError("prompt-fetch-failed")

            result = image_runner.run(source, root / "core-image.png", prompt_fetcher=fetcher)
            assert result["status"] == "needs_review" and "prompt-fetch-failed" in result["reason"]
            assert not (root / "core-image.png").exists()
            assert len(fake.calls) == 0
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
            result = image_runner.run(source, root / "core-image.png", prompt_fetcher=lambda _: prompt_asset())
            assert result["status"] == "needs_review" and "hash mismatch" in result["reason"]
            assert not (root / "core-image.png").exists()

            short = root / "short.md"
            short.write_text("太短。", encoding="utf-8")
            try:
                image_runner.run(short, root / "core-image.png", prompt_fetcher=lambda _: prompt_asset())
            except ValueError as exc:
                assert "document too short" in str(exc)
            else:
                raise AssertionError("short document must fail before any bridge call")
            assert len(fake.calls) == 1
    finally:
        restore()


def test_restart_reuses_verified_png_without_prompt_fetch_or_bridge():
    fake = FakeBridge([image_result(runId=REQUEST_ID)])
    restore = patch(fake)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, summary = root / "source.md", root / "image.png", root / "image.json"
            source.write_text(doc_text(), encoding="utf-8")
            first = image_runner.run(source, output, summary_path=summary, prompt_fetcher=lambda _: prompt_asset(), request_id=REQUEST_ID)

            def must_not_fetch(_):
                raise AssertionError("reusing a verified image must not fetch a new prompt")

            second = image_runner.run(source, output, summary_path=summary, prompt_fetcher=must_not_fetch, request_id=REQUEST_ID)
            assert second["status"] == "succeeded" and second["reused"] is True
            assert second["image_sha256"] == first["image_sha256"]
            assert len(fake.calls) == 1
            assert fake.calls[0]["request_id"] == REQUEST_ID
            assert fake.calls[0]["busy_retry_max_wait_seconds"] == 0
    finally:
        restore()


def test_existing_png_requires_summary_and_matching_bytes_source_and_request():
    fake = FakeBridge([image_result(runId=REQUEST_ID)])
    restore = patch(fake)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, summary = root / "source.md", root / "image.png", root / "image.json"
            source.write_text(doc_text(), encoding="utf-8")
            image_runner.run(source, output, summary_path=summary, prompt_fetcher=lambda _: prompt_asset(), request_id=REQUEST_ID)
            saved = summary.read_text()
            cases = [
                (None, None),
                ({**json.loads(saved), "image_sha256": "0" * 64}, None),
                ({**json.loads(saved), "source_sha256": "0" * 64}, None),
                (json.loads(saved), "223e4567-e89b-42d3-a456-426614174000"),
            ]
            for metadata, request_id in cases:
                if metadata is None:
                    summary.unlink()
                else:
                    summary.write_text(json.dumps(metadata))
                try:
                    image_runner.run(source, output, summary_path=summary, request_id=request_id)
                except (ValueError, FileNotFoundError):
                    pass
                else:
                    raise AssertionError("unverified existing image must fail closed")
            assert len(fake.calls) == 1
    finally:
        restore()


def test_existing_symlink_and_non_png_are_not_reused():
    fake = FakeBridge([image_result()])
    restore = patch(fake)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, summary = root / "source.md", root / "image.png", root / "image.json"
            source.write_text(doc_text(), encoding="utf-8")
            image_runner.run(source, output, summary_path=summary, prompt_fetcher=lambda _: prompt_asset())
            original = root / "original.png"
            output.rename(original)
            output.symlink_to(original)
            for invalid in ("symlink", "invalid-png"):
                if invalid == "invalid-png":
                    output.unlink()
                    output.write_bytes(b"not a png")
                    metadata = json.loads(summary.read_text())
                    metadata.update(image_sha256=hashlib.sha256(output.read_bytes()).hexdigest(), image_bytes=output.stat().st_size)
                    summary.write_text(json.dumps(metadata))
                try:
                    image_runner.run(source, output, summary_path=summary)
                except ValueError:
                    pass
                else:
                    raise AssertionError("unsafe or invalid PNG must not be reused")
            assert len(fake.calls) == 1
    finally:
        restore()


def test_stable_request_never_hides_cooldown_busy_or_unknown_resubmission():
    for reason in ("local-rate-limit-cooldown", "bridge-busy", "observer-timeout", "bridge-process-timeout"):
        fake = FakeBridge([{"status": "needs_review", "reason": reason, "retryAfterSeconds": 1, "runId": REQUEST_ID, "submissionConfirmed": reason == "observer-timeout"}])
        restore = patch(fake)
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, output, summary = root / "source.md", root / "image.png", root / "image.json"
                source.write_text(doc_text(), encoding="utf-8")
                first = image_runner.run(source, output, summary_path=summary, prompt_fetcher=lambda _: prompt_asset(), request_id=REQUEST_ID)
                second = image_runner.run(source, output, summary_path=summary, prompt_fetcher=lambda _: prompt_asset(), request_id=REQUEST_ID)
                assert first["status"] == second["status"] == "needs_review"
                assert second["reason"] == reason and second["reused"] is True
                assert second["runId"] == REQUEST_ID
                assert second["source_sha256"] and second["prompt_sha256"] and second["input_sha256"]
                assert len(fake.calls) == 1 and fake.sleeps == []
                assert not output.exists()
        finally:
            restore()


def test_durable_request_identity_is_written_before_bridge_submission():
    calls = []
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source, output, summary = root / "source.md", root / "image.png", root / "image.json"
        source.write_text(doc_text(), encoding="utf-8")

        def inspect_before_submit(prompt, **kwargs):
            saved = json.loads(summary.read_text(encoding="utf-8"))
            assert saved["status"] == "attempting"
            assert saved["runId"] == REQUEST_ID
            assert saved["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
            calls.append(kwargs["request_id"])
            return image_result(runId=REQUEST_ID)

        original_bridge = image_runner.run_bridge
        image_runner.run_bridge = inspect_before_submit
        try:
            result = image_runner.run(source, output, summary_path=summary,
                                      prompt_fetcher=lambda _: prompt_asset(), request_id=REQUEST_ID)
        finally:
            image_runner.run_bridge = original_bridge
        assert result["status"] == "succeeded"
        assert calls == [REQUEST_ID]


def test_recovery_observes_original_request_and_materializes_verified_image():
    fake = FakeBridge([{"status": "needs_review", "reason": "bridge-process-timeout", "runId": REQUEST_ID}])
    restore = patch(fake)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, summary = root / "source.md", root / "image.png", root / "image.json"
            source.write_text(doc_text(), encoding="utf-8")
            initial = image_runner.run(source, output, summary_path=summary,
                                       prompt_fetcher=lambda _: prompt_asset(), request_id=REQUEST_ID)
            assert initial["status"] == "needs_review"
            observed = []

            def observer(**kwargs):
                observed.append(kwargs)
                return image_result(runId=REQUEST_ID)

            recovered = image_runner.recover(source, output, summary, request_id=REQUEST_ID, observe_fn=observer)
            assert recovered["status"] == "succeeded" and recovered["recovered_from_observation"] is True
            assert observed == [{"original_run_id": REQUEST_ID, "conversation_url": "https://chatgpt.com/",
                                 "caller_label": image_runner.CALLER_LABEL, "max_wait_seconds": 720}]
            assert output.read_bytes() == png_bytes()
            saved = json.loads(summary.read_text(encoding="utf-8"))
            assert saved["status"] == "succeeded" and saved["runId"] == REQUEST_ID
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
