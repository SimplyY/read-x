"""Contract checks for the shared ChatGPT Bridge adapter."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile

import chatgpt_bridge as bridge


def result(text="{}", **extra):
    value = {"status": "succeeded", "runId": "r1", "conversationUrl": "https://chatgpt.test/c/1",
             "actualConversationUrl": "https://chatgpt.test/c/1", "submissionConfirmed": True,
             "format": "markdown", "verification": "live-dom+snapshot", "text": text,
             "outputSha256": hashlib.sha256(text.encode()).hexdigest()}
    value.update(extra)
    return value


def image_result(**extra):
    payload = base64.b64encode(b"\x89PNG-fake").decode()
    data_url = f"data:image/png;base64,{payload}"
    value = {"status": "succeeded", "runId": "r2", "conversationUrl": "https://chatgpt.test/c/2",
             "actualConversationUrl": "https://chatgpt.test/c/2", "submissionConfirmed": True,
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
                result("正文", conversationUrl="https://chatgpt.test/share/1"), result("正文", outputSha256="bad"),
                result("正文", submissionConfirmed=False),
                result("正文", actualConversationUrl="https://chatgpt.test/c/other")):
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
    command = bridge.bridge_command(Path("/tmp/bridge.mjs"), max_wait_seconds=321,
                                    caller_label="read-x:weekly", request_id="12345678-1234-5678-1234-567812345678")[-1]
    assert "for await (const chunk of process.stdin)" in command
    assert "maxWaitSeconds: 321" in command
    assert "successCooldownSeconds: 0" in command
    assert "image: false" in command
    assert 'callerLabel: "read-x:weekly"' in command
    assert 'preallocatedRunId: "12345678-1234-5678-1234-567812345678"' in command
    image_command = bridge.bridge_command(Path("/tmp/bridge.mjs"), image=True)[-1]
    assert "image: true" in image_command
    assert "resultVersion: 1" in image_command
    enabled = bridge.bridge_command(Path("/tmp/bridge.mjs"), image=True, observer={"mode": "single", "order": ["C"]})[-1]
    assert "resultVersion: 2" in enabled


def test_observe_command_uses_original_identity_and_never_submits_a_prompt():
    command = bridge.observe_bridge_command(
        Path("/tmp/bridge.mjs"),
        original_run_id="12345678-1234-5678-1234-567812345678",
        conversation_url="https://chatgpt.com/c/1",
        caller_label="read-x:long-read-image",
        max_wait_seconds=420,
    )[-1]
    assert "import { observeBridge }" in command
    assert 'originalRunId: "12345678-1234-5678-1234-567812345678"' in command
    assert 'callerLabel: "read-x:long-read-image"' in command
    assert "maxWaitSeconds: 420" in command
    assert "runBridge" not in command
    assert "resultVersion: 1" in command
    assert command != bridge.observe_bridge_command(
        Path("/tmp/bridge.mjs"), original_run_id="12345678-1234-5678-1234-567812345678",
        conversation_url="https://chatgpt.com/c/1", caller_label="read-x:long-read-image", max_wait_seconds=420,
    )[-1], "each recovery attempt has its own client request identity"
    entrypoint_command = bridge.observe_bridge_command(
        Path("/tmp/bridge.mjs"),
        original_run_id="12345678-1234-5678-1234-567812345678",
        conversation_url="https://chatgpt.com/",
        caller_label="read-x:long-read-text",
    )[-1]
    assert "observeBridge" in entrypoint_command
    try:
        bridge.observe_bridge_command(
            Path("/tmp/bridge.mjs"),
            original_run_id="12345678-1234-5678-1234-567812345678",
            conversation_url="https://example.com/",
            caller_label="read-x:long-read-text",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("untrusted observer URL must fail closed")


def test_observe_adapter_returns_only_matching_original_run():
    original = bridge.subprocess.run
    run_id = "12345678-1234-5678-1234-567812345678"

    def fake(command, **kwargs):
        assert "observeBridge" in command[-1]
        return bridge.subprocess.CompletedProcess(command, 0, json.dumps({"status": "needs_review", "runId": run_id, "reason": "original-run-result-pending"}), "")

    bridge.subprocess.run = fake
    try:
        result = bridge.observe_bridge(
            original_run_id=run_id,
            conversation_url="https://chatgpt.com/c/1",
            caller_label="read-x:long-read-text",
            bridge=Path("/tmp/bridge.mjs"),
        )
    finally:
        bridge.subprocess.run = original
    assert result["runId"] == run_id and result["status"] == "needs_review"


def test_verified_image_requires_current_contract():
    assert bridge.verified_image(image_result()) == b"\x89PNG-fake"
    for bad in (image_result(verification="historyVerified"),
                image_result(conversationUrl="https://chatgpt.test/share/2"),
                image_result(actualConversationUrl="https://chatgpt.test/c/other"),
                image_result(submissionConfirmed=False),
                image_result(outputSha256="bad"),
                image_result(imageBase64="data:text/plain;base64,SGk="),
                image_result(imageBase64="data:image/png;base64,%%%not-base64%%%")):
        try:
            bridge.verified_image(bad)
        except RuntimeError:
            pass
        else:
            raise AssertionError("legacy or unverifiable image result must fail")


def test_v2_evidence_accepts_degraded_format_but_rejects_unsafe_or_visual_only_results():
    text = "完整且业务可解析的正文"
    digest = hashlib.sha256(text.encode()).hexdigest()
    value = result(text, runId="12345678-1234-5678-1234-567812345678",
                   conversationUrl="https://chatgpt.com/c/v2", actualConversationUrl="https://chatgpt.com/c/v2",
                   resultVersion=2, evidenceVersion=2, verification="bridge-evidence/v2",
                   deliveryClass="usable_with_warnings", candidateSha256=digest,
                   evidenceSummary={"identity": "matched", "generation": "complete", "body": "available",
                                    "truncation": "unknown", "candidateSha256": digest})
    assert bridge.verified_text(value) == text
    for change in ({"candidateSha256": "0" * 64}, {"outputSha256": "0" * 64}, {"runId": "bad"},
                   {"resultVersion": 1}, {"evidenceVersion": 1}, {"evidenceSummary": {"confidence": .99}},
                   {"deliveryClass": "requires_user_review"}, {"submissionConfirmed": False}):
        try:
            bridge.verified_text({**value, **change})
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"unsafe v2 delivery was accepted: {list(change)}")
    for field, state in (("identity", "unknown"), ("generation", "generating"), ("truncation", "possible")):
        try:
            bridge.verified_text({**value, "evidenceSummary": {**value["evidenceSummary"], field: state}})
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"unsafe {field} evidence was accepted")


def test_observer_configuration_is_forwarded_without_changing_submission_identity():
    observer = {"mode": "single", "order": ["C"]}
    command = bridge.bridge_command(Path("/tmp/bridge.mjs"), observer=observer,
                                    request_id="12345678-1234-5678-1234-567812345678")[-1]
    assert f"observer: {json.dumps(observer)}" in command
    assert 'preallocatedRunId: "12345678-1234-5678-1234-567812345678"' in command


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
    assert result["status"] == "needs_review"
    assert result["reason"] == "bridge-process-timeout"
    assert result["runId"] and result["callerLabel"] == "read-x:chatgpt-bridge"


def test_busy_rejection_waits_for_a_free_slot_once_then_succeeds():
    attempts = []
    sleeps = []
    original_once = bridge._run_bridge_once
    original_sleep = bridge.time.sleep

    def fake_once(prompt, *, bridge, max_wait_seconds, image, conversation_url=None, caller_label=None, request_id=None):
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

    def fake_once(prompt, *, bridge, max_wait_seconds, image, conversation_url=None, caller_label=None, request_id=None):
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


def test_preallocated_request_id_is_single_submission_even_when_bridge_is_busy():
    attempts = []
    original_once = bridge._run_bridge_once

    def fake_once(prompt, *, bridge, max_wait_seconds, image, conversation_url=None, caller_label=None, request_id=None):
        attempts.append(request_id)
        return {"status": "needs_review", "reason": "bridge-busy", "retryAfterSeconds": 30}

    bridge._run_bridge_once = fake_once
    try:
        result = bridge.run_bridge(
            "prompt",
            bridge=Path("/tmp/bridge.mjs"),
            request_id="12345678-1234-5678-1234-567812345678",
        )
    finally:
        bridge._run_bridge_once = original_once
    assert result["reason"] == "bridge-busy"
    assert attempts == ["12345678-1234-5678-1234-567812345678"]
    assert "busyRetries" not in result


def test_conversation_url_is_passed_to_bridge_once():
    original_once = bridge._run_bridge_once
    seen = []

    def fake_once(prompt, *, bridge, max_wait_seconds, image, conversation_url=None, caller_label=None, request_id=None):
        seen.append(conversation_url)
        return {"status": "succeeded"}

    bridge._run_bridge_once = fake_once
    try:
        bridge.run_bridge("prompt", bridge=Path("/tmp/bridge.mjs"),
                          conversation_url="https://chatgpt.test/c/1")
    finally:
        bridge._run_bridge_once = original_once
    assert seen == ["https://chatgpt.test/c/1"]


def test_business_outcome_uses_shared_bridge_with_only_allowlisted_metadata():
    event = {
        "source": "read-x", "caller": "read-x:long-read-text",
        "attemptId": "12345678-1234-5678-1234-567812345678",
        "runId": "12345678-1234-5678-1234-567812345678",
        "clientRequestId": "12345678-1234-5678-1234-567812345678",
        "status": "succeeded", "deliveryStatus": "succeeded",
        "businessValidationStatus": "succeeded", "readbackStatus": "unknown", "durationMs": 42,
    }
    original = bridge.subprocess.run

    def fake(command, **kwargs):
        assert "recordBridgeBusinessOutcome" in command[-1]
        assert json.loads(kwargs["input"]) == event
        assert not any(secret in kwargs["input"] for secret in ("prompt", "response", "conversationUrl"))
        return bridge.subprocess.CompletedProcess(command, 0, '{"status":"recorded","persistenceTarget":"primary"}\n', "")

    bridge.subprocess.run = fake
    try:
        result = bridge.record_business_outcome(event, bridge=Path("/tmp/bridge.mjs"))
    finally:
        bridge.subprocess.run = original
    assert result == {"status": "recorded", "persistenceTarget": "primary"}


def test_business_outcome_writes_to_an_isolated_shared_journal():
    event = {
        "source": "read-x", "caller": "read-x:long-read-text",
        "attemptId": "12345678-1234-5678-1234-567812345678",
        "runId": "12345678-1234-5678-1234-567812345678",
        "clientRequestId": "12345678-1234-5678-1234-567812345678",
        "status": "succeeded", "deliveryStatus": "succeeded",
        "businessValidationStatus": "succeeded", "readbackStatus": "unknown", "durationMs": 42,
    }
    bridge_module = (Path(__file__).resolve().parents[1].parent / "browser-runtime-governance"
                     / "skills/chatgpt-web-bridge/scripts/bridge.mjs")
    original = {key: os.environ.get(key) for key in ("EGO_RUNTIME_DIR", "CHATGPT_WEB_BRIDGE_CLIENT_RUNTIME_DIR")}
    with tempfile.TemporaryDirectory(prefix="readx-bridge-outcome-") as directory:
        runtime_dir = Path(directory) / "runtime"
        client_dir = Path(directory) / "client"
        os.environ["EGO_RUNTIME_DIR"] = str(runtime_dir)
        os.environ["CHATGPT_WEB_BRIDGE_CLIENT_RUNTIME_DIR"] = str(client_dir)
        try:
            result = bridge.record_business_outcome(event, bridge=bridge_module)
            assert result == {"status": "recorded", "persistenceTarget": "primary"}
            shards = list((runtime_dir / "journal").glob("*.jsonl"))
            assert len(shards) == 1
            persisted = json.loads(shards[0].read_text(encoding="utf-8").splitlines()[0])
            assert persisted["source"] == "read-x"
            assert persisted["attemptId"] == event["attemptId"]
            assert persisted["deliveryStatus"] == "succeeded"
            assert not ({"prompt", "response", "conversationUrl"} & set(persisted))
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def test_business_outcome_failure_is_visible_without_raw_exception_details():
    event = {
        "source": "read-x", "caller": "read-x:long-read-text",
        "attemptId": "12345678-1234-5678-1234-567812345678",
        "runId": "12345678-1234-5678-1234-567812345678",
        "status": "needs_review", "deliveryStatus": "needs_review",
        "businessValidationStatus": "unknown", "readbackStatus": "unknown", "durationMs": 7,
    }
    original_run, original_stderr = bridge.subprocess.run, bridge.sys.stderr
    captured = io.StringIO()
    bridge.subprocess.run = lambda command, **kwargs: bridge.subprocess.CompletedProcess(
        command, 1, '{"status":"failed","errorCode":"diagnostic-write-failed"}\n', "PRIVATE PATH SENTINEL",
    )
    bridge.sys.stderr = captured
    try:
        result = bridge.record_business_outcome(event, bridge=Path("/tmp/bridge.mjs"))
    finally:
        bridge.subprocess.run, bridge.sys.stderr = original_run, original_stderr
    assert result["status"] == "failed"
    assert "bridge-business-outcome-diagnostic-failed" in captured.getvalue()
    assert "diagnostic-write-failed" in captured.getvalue()
    assert "PRIVATE PATH SENTINEL" not in captured.getvalue()


def test_business_outcome_bridge_resolution_failure_is_visible_without_path_details():
    event = {
        "source": "read-x", "caller": "read-x:long-read-text",
        "attemptId": "12345678-1234-5678-1234-567812345678",
        "runId": "12345678-1234-5678-1234-567812345678",
        "status": "needs_review", "deliveryStatus": "needs_review",
        "businessValidationStatus": "unknown", "readbackStatus": "unknown", "durationMs": 7,
    }
    original_resolve, original_stderr = bridge.resolve_bridge, bridge.sys.stderr
    captured = io.StringIO()
    bridge.resolve_bridge = lambda: (_ for _ in ()).throw(OSError("PRIVATE PATH SENTINEL"))
    bridge.sys.stderr = captured
    try:
        result = bridge.record_business_outcome(event)
    finally:
        bridge.resolve_bridge, bridge.sys.stderr = original_resolve, original_stderr
    assert result["status"] == "failed"
    assert "bridge-business-outcome-diagnostic-failed" in captured.getvalue()
    assert "diagnostic-write-failed" in captured.getvalue()
    assert "PRIVATE PATH SENTINEL" not in captured.getvalue()


def test_business_outcome_rejects_content_fields_before_running_node():
    event = {
        "source": "read-x", "caller": "read-x:long-read-text",
        "attemptId": "12345678-1234-5678-1234-567812345678",
        "runId": "12345678-1234-5678-1234-567812345678",
        "status": "succeeded", "deliveryStatus": "succeeded",
        "businessValidationStatus": "succeeded", "readbackStatus": "unknown", "durationMs": 1,
        "prompt": "PRIVATE PROMPT SENTINEL",
    }
    original = bridge.subprocess.run
    bridge.subprocess.run = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must reject before Node"))
    try:
        try:
            bridge.record_business_outcome(event, bridge=Path("/tmp/bridge.mjs"))
        except ValueError as exc:
            assert "unsupported fields" in str(exc)
        else:
            raise AssertionError("content-bearing event must be rejected")
    finally:
        bridge.subprocess.run = original


def test_manual_candidate_acceptance_keeps_hash_and_identity_gates():
    value = result("accepted partial", runId="123e4567-e89b-42d3-a456-426614174000", resultVersion=2,evidenceVersion=2,verification="bridge-evidence/v2",deliveryClass="usable_with_warnings",humanConfirmed=True,completionSource="user-manual",candidateRevision=1,conversationUrl="https://chatgpt.com/c/target",actualConversationUrl="https://chatgpt.com/c/target")
    value["candidateSha256"] = value["outputSha256"]
    value["evidenceSummary"] = {"identity":"matched","generation":"unknown","body":"available","truncation":"present","candidateSha256":value["outputSha256"]}
    bridge.verify_result_evidence(value)
    for field, bad in [("humanConfirmed",False),("candidateSha256","0"*64),("candidateRevision",True),("candidateRevision",0)]:
        changed = dict(value); changed[field] = bad
        try: bridge.verify_result_evidence(changed)
        except RuntimeError: pass
        else: raise AssertionError("manual acceptance must retain safety gates")


def main():
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in sorted(tests, key=lambda item: item.__name__):
        test()
    print(f"{len(tests)} tests passed")


if __name__ == "__main__":
    main()
