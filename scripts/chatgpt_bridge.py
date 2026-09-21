#!/usr/bin/env python3
"""Small Python adapter for the installed Ego Lite ChatGPT Bridge."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse


BRIDGE_PROCESS_GRACE_SECONDS = 30
BUSY_RETRY_MAX_WAIT_SECONDS = 600


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


def bridge_command(
    bridge: Path | None = None,
    *,
    max_wait_seconds: int = 360,
    success_cooldown_seconds: int = 0,
    image: bool = False,
    conversation_url: str | None = None,
) -> list[str]:
    bridge_uri = (bridge or resolve_bridge()).resolve().as_uri()
    conversation = json.dumps(conversation_url) if conversation_url else "undefined"
    module = (
        f"import {{ runBridge }} from {json.dumps(bridge_uri)};"
        "process.stdin.setEncoding('utf8');"
        "let prompt = '';"
        "for await (const chunk of process.stdin) prompt += chunk;"
        f"const result = await runBridge({{ prompt, maxWaitSeconds: {int(max_wait_seconds)}, successCooldownSeconds: {int(success_cooldown_seconds)}, image: {'true' if image else 'false'}, conversationUrl: {conversation} }});"
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


def _run_bridge_once(prompt: str, *, bridge: Path | None, max_wait_seconds: int, image: bool, conversation_url: str | None = None) -> dict:
    try:
        completed = subprocess.run(
            bridge_command(bridge, max_wait_seconds=max_wait_seconds, image=image, conversation_url=conversation_url),
            input=prompt,
            text=True,
            capture_output=True,
            check=False,
            timeout=max(int(max_wait_seconds) + BRIDGE_PROCESS_GRACE_SECONDS, BRIDGE_PROCESS_GRACE_SECONDS),
        )
    except subprocess.TimeoutExpired:
        return {"status": "needs_review", "reason": "bridge-process-timeout"}
    try:
        result = _parse_stdout(completed.stdout)
    except Exception as exc:
        reason = str(exc)
        if completed.returncode != 0:
            reason = f"bridge-exit-{completed.returncode}: {reason}"
        return {"status": "needs_review", "reason": reason}
    if completed.returncode != 0 and result.get("status") == "succeeded":
        return {"status": "needs_review", "reason": f"bridge-exit-{completed.returncode}"}
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
) -> dict:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("bridge prompt must not be empty")
    started = time.monotonic()
    busy_retries: list[dict] = []
    while True:
        result = _run_bridge_once(prompt, bridge=bridge, max_wait_seconds=max_wait_seconds, image=image, conversation_url=conversation_url)
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
    if result.get("verification") != "live-dom+snapshot":
        raise RuntimeError("bridge output was not verified by live DOM and snapshot")
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
    if result.get("verification") != "live-dom+snapshot":
        raise RuntimeError("bridge output was not verified by live DOM and snapshot")
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
