#!/usr/bin/env python3
"""Small Python adapter for the installed Ego Lite ChatGPT Bridge."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse


BRIDGE_PROCESS_GRACE_SECONDS = 30
CONTEXT_PACK_BUDGET_SECONDS = 30
BUSY_RETRY_MAX_WAIT_SECONDS = 600
WORKFLOW_NEW_CHAT_URL = "https://chatgpt.com/"
READ_X_OUTCOME_CALLERS = {"read-x:long-read-text", "read-x:long-read-image", "read-x:long-read-task"}
OUTCOME_STATES = {"succeeded", "failed", "needs_review", "unknown"}
OUTCOME_FIELDS = {
    "source", "caller", "attemptId", "runId", "clientRequestId", "status",
    "deliveryStatus", "businessValidationStatus", "readbackStatus", "durationMs",
    "failureStage", "errorCode",
    "callerTaskId", "outcomeKind", "artifactKind", "resultVersion", "evidenceVersion", "deliveryClass", "humanConfirmed",
}


def _bridge_candidates() -> list[Path]:
    roots = []
    configured = os.environ.get("CODEX_HOME")
    if configured:
        roots.append(Path(configured) / "skills")
    roots.extend((Path.home() / ".codex/skills", Path.home() / ".agents/skills"))
    return [root / "chatgpt-web-bridge/scripts/bridge.mjs" for root in dict.fromkeys(roots)]


def resolve_bridge() -> Path:
    for path in _bridge_candidates():
        if path.is_file():
            return path
    raise FileNotFoundError("chatgpt-web-bridge/scripts/bridge.mjs is not installed")


def requested_bridge_result_version(result_version: int | None = None, observer: dict | None = None) -> int:
    if result_version is not None:
        if type(result_version) is not int or result_version not in {1, 2}:
            raise ValueError("bridge result version must be 1 or 2")
        return result_version
    if observer is not None:
        return 2
    configured = os.getenv("CHATGPT_WEB_BRIDGE_RESULT_VERSION")
    if configured is not None and configured not in {"1", "2"}:
        raise ValueError("bridge result version must be 1 or 2")
    return 1 if configured == "1" else 2


def bridge_command(
    bridge: Path | None = None,
    *,
    max_wait_seconds: int = 360,
    success_cooldown_seconds: int = 0,
    image: bool = False,
    conversation_url: str | None = None,
    caller_label: str = "read-x:chatgpt-bridge",
    request_id: str | None = None,
    observer: dict | None = None,
    result_version: int | None = None,
    context_pack_delivery: dict | None = None,
) -> list[str]:
    if not isinstance(caller_label, str) or not caller_label.strip() or len(caller_label.strip()) > 100:
        raise ValueError("bridge caller label must contain 1 to 100 characters")
    request_id = request_id or str(uuid.uuid4())
    try:
        uuid.UUID(request_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("bridge request id must be a UUID") from exc
    bridge_uri = (bridge or resolve_bridge()).resolve().as_uri()
    conversation = json.dumps(conversation_url) if conversation_url else "undefined"
    caller = json.dumps(caller_label.strip())
    correlation_id = json.dumps(request_id)
    requested_version = requested_bridge_result_version(result_version, observer)
    observer_option = f", observer: {json.dumps(observer)}" if observer is not None and requested_version == 2 else ""
    delivery_option = f", contextPackDelivery: {json.dumps(context_pack_delivery)}" if context_pack_delivery is not None else ""
    module = (
        f"import {{ runBridge }} from {json.dumps(bridge_uri)};"
        "process.stdin.setEncoding('utf8');"
        "let prompt = '';"
        "for await (const chunk of process.stdin) prompt += chunk;"
        f"const result = await runBridge({{ prompt, resultVersion: {requested_version}, maxWaitSeconds: {int(max_wait_seconds)}, successCooldownSeconds: {int(success_cooldown_seconds)}, image: {'true' if image else 'false'}, conversationUrl: {conversation}, callerLabel: {caller}, preallocatedRunId: {correlation_id}{observer_option}{delivery_option} }});"
        "process.stdout.write(JSON.stringify(result) + '\\n');"
        "process.exitCode = result.status === 'succeeded' ? 0 : 2;"
    )
    return ["node", "--input-type=module", "-e", module]


def observe_bridge_command(
    bridge: Path | None = None,
    *,
    original_run_id: str,
    conversation_url: str,
    caller_label: str,
    max_wait_seconds: int = 720,
    observer: dict | None = None,
    result_version: int | None = None,
) -> list[str]:
    try:
        uuid.UUID(original_run_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("original bridge run id must be a UUID") from exc
    if not isinstance(caller_label, str) or not caller_label.strip() or len(caller_label.strip()) > 100:
        raise ValueError("bridge caller label must contain 1 to 100 characters")
    parsed = urlparse(conversation_url)
    is_session = parsed.path.startswith("/c/") and parsed.hostname in {"chatgpt.com", "chat.openai.com"}
    is_new_chat_entry = parsed.scheme == "https" and parsed.hostname == "chatgpt.com" and parsed.path in {"", "/"}
    if parsed.scheme not in {"http", "https"} or not (is_session or is_new_chat_entry):
        raise ValueError("conversation URL must be a ChatGPT session or the exact new-chat entrypoint")
    if isinstance(max_wait_seconds, bool) or not isinstance(max_wait_seconds, int) or not 1 <= max_wait_seconds <= 3600:
        raise ValueError("observe wait must be between 1 and 3600 seconds")
    bridge_uri = (bridge or resolve_bridge()).resolve().as_uri()
    request_id = str(uuid.uuid4())
    requested_version = requested_bridge_result_version(result_version, observer)
    observer_option = f", observer: {json.dumps(observer)}" if observer is not None and requested_version == 2 else ""
    module = (
        f"import {{ observeBridge }} from {json.dumps(bridge_uri)};"
        f"const result = await observeBridge({{ resultVersion: {requested_version}, requestId: {json.dumps(request_id)}, originalRunId: {json.dumps(original_run_id)}, conversationUrl: {json.dumps(conversation_url)}, callerLabel: {json.dumps(caller_label.strip())}, maxWaitSeconds: {max_wait_seconds}{observer_option} }});"
        "process.stdout.write(JSON.stringify(result) + '\\n');"
        "process.exitCode = result.status === 'succeeded' ? 0 : 2;"
    )
    return ["node", "--input-type=module", "-e", module]


def _parse_stdout(stdout: str) -> dict:
    values = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("bridge output must be an object")
        values.append(value)
    if len(values) != 1:
        raise ValueError("bridge output must contain exactly one JSON object")
    return values[0]


def _run_bridge_once(prompt: str, *, bridge: Path | None, max_wait_seconds: int, image: bool, conversation_url: str | None = None, caller_label: str = "read-x:chatgpt-bridge", request_id: str | None = None, observer: dict | None = None, result_version: int | None = None, context_pack_delivery: dict | None = None) -> dict:
    request_id = request_id or str(uuid.uuid4())
    try:
        completed = subprocess.run(
            bridge_command(bridge, max_wait_seconds=max_wait_seconds, image=image, conversation_url=conversation_url, caller_label=caller_label, request_id=request_id, observer=observer, result_version=result_version, context_pack_delivery=context_pack_delivery),
            input=prompt,
            text=True,
            capture_output=True,
            check=False,
            timeout=max(int(max_wait_seconds) + BRIDGE_PROCESS_GRACE_SECONDS + CONTEXT_PACK_BUDGET_SECONDS, BRIDGE_PROCESS_GRACE_SECONDS),
        )
    except subprocess.TimeoutExpired:
        return {"status": "needs_review", "reason": "bridge-process-timeout", "runId": request_id, "callerLabel": caller_label}
    try:
        result = _parse_stdout(completed.stdout)
    except Exception as exc:
        reason = str(exc)
        if completed.returncode != 0:
            reason = f"bridge-exit-{completed.returncode}: {reason}"
        return {"status": "needs_review", "reason": reason, "runId": request_id, "callerLabel": caller_label}
    if completed.returncode != 0 and result.get("status") == "succeeded":
        return {**result, "status": "needs_review", "reason": f"bridge-exit-{completed.returncode}", "runId": request_id, "callerLabel": caller_label}
    if requested_bridge_result_version(result_version, observer) == 2 and result.get("status") == "succeeded" and result.get("resultVersion") != 2:
        return {**result,"status":"needs_review","reason":"bridge-v2-evidence-required","runId":request_id,"callerLabel":caller_label}
    return result


def observe_bridge(
    *,
    original_run_id: str,
    conversation_url: str,
    caller_label: str,
    bridge: Path | None = None,
    max_wait_seconds: int = 720,
    observer: dict | None = None,
    result_version: int | None = None,
) -> dict:
    """Read the original Bridge result by identity; this never submits a new prompt."""
    command = observe_bridge_command(
        bridge,
        original_run_id=original_run_id,
        conversation_url=conversation_url,
        caller_label=caller_label,
        max_wait_seconds=max_wait_seconds,
        observer=observer,
        result_version=result_version,
    )
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=max_wait_seconds + BRIDGE_PROCESS_GRACE_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {"status": "needs_review", "reason": "bridge-observe-process-timeout", "runId": original_run_id}
    try:
        result = _parse_stdout(completed.stdout)
    except Exception as exc:
        return {"status": "needs_review", "reason": f"bridge-observe-result-invalid: {exc}", "runId": original_run_id}
    if result.get("runId") != original_run_id:
        return {**result, "status": "needs_review", "reason": "bridge-observe-run-id-mismatch", "runId": original_run_id}
    if completed.returncode != 0 and result.get("status") == "succeeded":
        return {**result, "status": "needs_review", "reason": "bridge-observe-exit-status-mismatch", "runId": original_run_id}
    if requested_bridge_result_version(result_version, observer) == 2 and result.get("status") == "succeeded" and result.get("resultVersion") != 2:
        return {**result,"status":"needs_review","reason":"bridge-v2-evidence-required","runId":original_run_id}
    return result


def record_business_outcome(event: dict, *, bridge: Path | None = None, timeout_seconds: int = 30) -> dict:
    """Persist a content-free Read-X terminal outcome through the shared Bridge journal."""
    if not isinstance(event, dict) or set(event) - OUTCOME_FIELDS:
        raise ValueError("business outcome contains unsupported fields")
    if event.get("source") != "read-x" or event.get("caller") not in READ_X_OUTCOME_CALLERS:
        raise ValueError("business outcome source or caller is invalid")
    for key in ("attemptId", "runId", "clientRequestId"):
        value = event.get(key)
        if value is not None:
            try:
                uuid.UUID(value)
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError(f"business outcome {key} must be a UUID") from exc
    if not event.get("attemptId") or not (event.get("runId") or event.get("clientRequestId")
            or event.get("outcomeKind") == "task" and event.get("callerTaskId")):
        raise ValueError("business outcome identity is incomplete")
    for key in ("status", "deliveryStatus", "businessValidationStatus", "readbackStatus"):
        if event.get(key) not in OUTCOME_STATES:
            raise ValueError(f"business outcome {key} is invalid")
    if event.get("failureStage") not in {None, "output-validation"}:
        raise ValueError("business outcome failureStage is invalid")
    if event.get("errorCode") not in {None, "output-validation"}:
        raise ValueError("business outcome errorCode is invalid")
    if event.get("outcomeKind") not in {None, "stage", "task"} or event.get("artifactKind") not in {None, "text", "image", "task"}:
        raise ValueError("business outcome kind is invalid")
    if event.get("humanConfirmed") is not None and type(event["humanConfirmed"]) is not bool:
        raise ValueError("business outcome human confirmation must be boolean")
    if event.get("callerTaskId") is not None and (not isinstance(event["callerTaskId"], str) or len(event["callerTaskId"]) != 64
            or any(c not in "0123456789abcdef" for c in event["callerTaskId"])):
        raise ValueError("business outcome task identity must be a digest")
    duration_ms = event.get("durationMs")
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
        raise ValueError("business outcome duration is invalid")

    safe_event = {key: event[key] for key in OUTCOME_FIELDS if key in event}

    def failed_result():
        print(json.dumps({
            "event": "bridge-business-outcome-diagnostic-failed",
            "reason": "read-x-outcome-recorder-unavailable",
            "errorCode": "diagnostic-write-failed",
            "source": "read-x",
            "caller": safe_event["caller"],
            "attemptId": safe_event["attemptId"],
            **({"runId": safe_event["runId"]} if safe_event.get("runId") else {}),
            **({"clientRequestId": safe_event["clientRequestId"]} if safe_event.get("clientRequestId") else {}),
        }, separators=(",", ":")), file=sys.stderr)
        return {"status": "failed", "persistenceTarget": "unavailable", "errorCode": "diagnostic-write-failed"}

    try:
        bridge_uri = (bridge or resolve_bridge()).resolve().as_uri()
        module = (
            f"import {{ recordBridgeBusinessOutcome }} from {json.dumps(bridge_uri)};"
            "let input = ''; for await (const chunk of process.stdin) input += chunk;"
            "const result = await recordBridgeBusinessOutcome(JSON.parse(input));"
            "process.stdout.write(JSON.stringify(result) + '\\n');"
        )
        completed = subprocess.run(
            ["node", "--input-type=module", "-e", module],
            input=json.dumps(safe_event, separators=(",", ":")),
            text=True, capture_output=True, check=False, timeout=timeout_seconds,
        )
        result = _parse_stdout(completed.stdout)
    except Exception:
        return failed_result()
    if completed.returncode != 0 or result.get("status") != "recorded":
        return failed_result()
    return result


def _busy_retry_seconds(result: dict, started: float, budget: float) -> float | None:
    """All callers share the bridge slots; a busy rejection waits for a free slot."""
    if budget <= 0:
        return None
    if result.get("status") != "needs_review" or result.get("reason") != "bridge-busy":
        return None
    remaining = budget - (time.monotonic() - started)
    if remaining <= 0:
        return None
    retry_after = result.get("retryAfterSeconds")
    if isinstance(retry_after, bool) or not isinstance(retry_after, (int, float)) or retry_after <= 0:
        return None
    return min(float(retry_after), remaining)


def run_bridge(
    prompt: str,
    *,
    bridge: Path | None = None,
    max_wait_seconds: int = 360,
    image: bool = False,
    conversation_url: str | None = None,
    busy_retry_max_wait_seconds: int = BUSY_RETRY_MAX_WAIT_SECONDS,
    caller_label: str = "read-x:chatgpt-bridge",
    request_id: str | None = None,
    observer: dict | None = None,
    result_version: int | None = None,
    context_pack_delivery: dict | None = None,
) -> dict:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("bridge prompt must not be empty")
    if request_id is not None:
        # A durable caller has already journaled this exact ID before submission.
        # Do not hide a second submission inside the adapter; the workflow owns
        # admission retries and must allocate/journal a new ID for each one.
        return _run_bridge_once(
            prompt,
            bridge=bridge,
            max_wait_seconds=max_wait_seconds,
            image=image,
            conversation_url=conversation_url,
            caller_label=caller_label,
            request_id=request_id,
            **({"observer": observer} if observer is not None else {}),
            **({"result_version": result_version} if result_version is not None else {}),
            **({"context_pack_delivery": context_pack_delivery} if context_pack_delivery is not None else {}),
        )
    started = time.monotonic()
    busy_retries: list[dict] = []
    while True:
        request_id = str(uuid.uuid4())
        result = _run_bridge_once(prompt, bridge=bridge, max_wait_seconds=max_wait_seconds, image=image, conversation_url=conversation_url, caller_label=caller_label, request_id=request_id,
                                  **({"observer": observer} if observer is not None else {}), **({"result_version": result_version} if result_version is not None else {}),
                                  **({"context_pack_delivery": context_pack_delivery} if context_pack_delivery is not None else {}))
        wait = _busy_retry_seconds(result, started, busy_retry_max_wait_seconds)
        if wait is None:
            if busy_retries:
                result["busyRetries"] = busy_retries
            return result
        busy_retries.append({"reason": "bridge-busy", "waited_seconds": round(wait, 3)})
        time.sleep(wait)


def verified_text(result: dict) -> str:
    if result.get("status") != "succeeded":
        raise RuntimeError(result.get("reason") or f"bridge status: {result.get('status')}")
    if result.get("format") != "markdown":
        raise RuntimeError("bridge format must be markdown")
    verify_result_evidence(result)
    _verify_bound_session(result)
    parsed_url = urlparse(result.get("conversationUrl") or "")
    if parsed_url.scheme not in {"http", "https"} or "/c/" not in parsed_url.path:
        raise RuntimeError("bridge conversation URL is invalid")
    text = result.get("text")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("bridge returned empty text")
    if result.get("outputSha256") != hashlib.sha256(text.encode("utf-8")).hexdigest():
        raise RuntimeError("bridge output hash mismatch")
    return text.strip()


def verified_image(result: dict) -> bytes:
    if result.get("status") != "succeeded":
        raise RuntimeError(result.get("reason") or f"bridge status: {result.get('status')}")
    verify_result_evidence(result)
    _verify_bound_session(result)
    parsed_url = urlparse(result.get("conversationUrl") or "")
    if parsed_url.scheme not in {"http", "https"} or "/c/" not in parsed_url.path:
        raise RuntimeError("bridge conversation URL is invalid")
    data_url = result.get("imageBase64")
    if not isinstance(data_url, str) or not data_url.startswith("data:image/"):
        raise RuntimeError("bridge returned no image output")
    if result.get("outputSha256") != hashlib.sha256(data_url.encode("utf-8")).hexdigest():
        raise RuntimeError("bridge output hash mismatch")
    payload = data_url.split(",", 1)
    if len(payload) != 2 or not payload[1]:
        raise RuntimeError("bridge image payload is malformed")
    try:
        return base64.b64decode(payload[1], validate=True)
    except Exception as exc:
        raise RuntimeError(f"bridge image payload is not valid base64: {exc}") from exc


def verify_result_evidence(result: dict) -> None:
    """Check Bridge's deterministic evidence contract, never a visual confidence score."""
    if result.get("resultVersion") == 2 or result.get("evidenceVersion") == 2 or result.get("verification") == "bridge-evidence/v2":
        summary = result.get("evidenceSummary")
        digest = result.get("outputSha256")
        try:
            uuid.UUID(result.get("runId"))
        except (ValueError, TypeError, AttributeError) as exc:
            raise RuntimeError("bridge v2 run identity is invalid") from exc
        human_accepted = (result.get("humanConfirmed") is True and result.get("completionSource") == "user-manual"
                          and result.get("deliveryClass") == "usable_with_warnings"
                          and type(result.get("candidateRevision")) is int and result["candidateRevision"] > 0)
        if (result.get("resultVersion") != 2 or result.get("evidenceVersion") != 2
                or result.get("verification") != "bridge-evidence/v2"
                or result.get("deliveryClass") not in {"verified", "usable_with_warnings"}
                or not isinstance(summary, dict) or summary.get("identity") != "matched"
                or (summary.get("generation") != "complete" and not human_accepted) or summary.get("body") != "available"
                or (summary.get("truncation") not in {"absent", "unknown"} and not human_accepted)
                or not isinstance(digest, str) or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
                or result.get("candidateSha256") != digest or summary.get("candidateSha256") != digest):
            raise RuntimeError("bridge v2 delivery evidence is incomplete or mismatched")
        parsed = urlparse(result.get("conversationUrl") or "")
        if parsed.scheme != "https" or parsed.hostname not in {"chatgpt.com", "chat.openai.com"} or not parsed.path.startswith("/c/"):
            raise RuntimeError("bridge v2 conversation identity is invalid")
    elif result.get("verification") != "live-dom+snapshot":
        raise RuntimeError("bridge output was not verified by a supported evidence contract")


def _verify_bound_session(result: dict) -> None:
    actual = result.get("actualConversationUrl")
    returned = result.get("conversationUrl")
    parsed_actual = urlparse(actual or "")
    parsed_returned = urlparse(returned or "")
    if result.get("submissionConfirmed") is not True:
        raise RuntimeError("bridge submission was not confirmed")
    if (parsed_actual.scheme not in {"http", "https"} or "/c/" not in parsed_actual.path
            or (parsed_actual.scheme, parsed_actual.netloc, parsed_actual.path)
            != (parsed_returned.scheme, parsed_returned.netloc, parsed_returned.path)):
        raise RuntimeError("bridge actual conversation identity was not confirmed")


def json_text(result: dict, name: str) -> dict:
    text = verified_text(result)
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) < 3 or not lines[0].startswith("```") or lines[-1].strip() != "```":
            raise RuntimeError(f"{name} returned malformed JSON fence")
        text = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{name} returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{name} returned JSON that is not an object")
    return parsed
