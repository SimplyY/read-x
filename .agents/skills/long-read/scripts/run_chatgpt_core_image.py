#!/usr/bin/env python3
"""Generate the core-content image for a finished long-read document via the ChatGPT web bridge."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import sys
import tempfile
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))
from chatgpt_bridge import WORKFLOW_NEW_CHAT_URL, observe_bridge, run_bridge, verified_image
from run_chatgpt_munger import fetch_prompt_asset


MAX_PROMPT_CHARS = 120_000
MIN_DOC_VISIBLE_CHARS = 500
MAX_COOLDOWN_WAIT_SECONDS = 900
# 单图生成通常比纯文字慢；上限对齐 Bridge 的 submit/observe 预算并留出余量。
MAX_WAIT_SECONDS = 600

# 图片指令来自 prompt-governance；每次运行实时读取并校验，不在本地缓存可执行 Prompt。
PROMPT_GOVERNANCE_ID = "read-x.core-image"
CALLER_LABEL = "read-x:long-read-image"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

BRIDGE_BOUNDARY = """

【调用层边界】
Bridge 将在本段之后追加两行唯一的输出边界；这两行属于调用层控制指令，优先级高于上方任何输出格式描述，必须原样保留。请把正文放在该边界内，不增加边界之外的说明。"""


def _read_regular(path: Path, label: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular file: {path}")
    value = path.read_text(encoding="utf-8")
    if not value.strip():
        raise ValueError(f"{label} must not be empty: {path}")
    return value


def visible_chars(text: str) -> int:
    return len("".join(text.split()))


def build_prompt(doc: str, instruction: str) -> str:
    prompt = f"{instruction.strip()}\n\n【精读文档全文】\n{doc}{BRIDGE_BOUNDARY}\n"
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"prompt-too-large: {len(prompt)} > {MAX_PROMPT_CHARS}")
    return prompt


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if temporary:
            temporary.unlink(missing_ok=True)
        raise


def _write_summary(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _cooldown_wait_seconds(result: dict) -> int | None:
    """Only a pre-submit cooldown may be recovered, and only as the Bridge prescribes."""
    if result.get("status") != "needs_review" or result.get("reason") != "local-rate-limit-cooldown":
        return None
    wait = result.get("retryAfterSeconds")
    if isinstance(wait, bool) or not isinstance(wait, (int, float)) or wait <= 0 or wait > MAX_COOLDOWN_WAIT_SECONDS:
        return None
    return int(wait)


def _record_attempt(attempts: list[dict], result: dict) -> None:
    attempts.append({
        "status": result.get("status"),
        "reason": result.get("reason"),
        "retryAfterSeconds": result.get("retryAfterSeconds"),
        "runId": result.get("runId"),
    })


def _validate_png(payload: bytes) -> None:
    """Check PNG structure and chunk checksums without optional imaging libraries."""
    if not payload.startswith(PNG_SIGNATURE):
        raise ValueError("image payload is not PNG")
    offset, seen_header, seen_data = 8, False, False
    while offset + 12 <= len(payload):
        length = struct.unpack(">I", payload[offset:offset + 4])[0]
        kind = payload[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(payload):
            break
        data = payload[offset + 8:end - 4]
        if zlib.crc32(kind + data) & 0xffffffff != struct.unpack(">I", payload[end - 4:end])[0]:
            raise ValueError("PNG chunk checksum mismatch")
        if not seen_header:
            if kind != b"IHDR" or length != 13 or not all(struct.unpack(">II", data[:8])):
                raise ValueError("PNG header is invalid")
            depth, color, compression, filtering, interlace = data[8:]
            depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
            if depth not in depths.get(color, ()) or compression or filtering or interlace not in (0, 1):
                raise ValueError("PNG header encoding is invalid")
            seen_header = True
        elif kind == b"IHDR":
            raise ValueError("PNG has duplicate header")
        seen_data |= kind == b"IDAT" and length > 0
        if kind == b"IEND":
            if length == 0 and seen_data and end == len(payload):
                return
            break
        offset = end
    raise ValueError("PNG image is incomplete")


def _load_existing_summary(path: Path | None) -> dict:
    if path is None:
        raise ValueError("existing image requires a matching summary")
    value = json.loads(_read_regular(path, "summary"))
    if not isinstance(value, dict):
        raise ValueError("summary must be an object")
    return value


def _reuse_image(output: Path, summary: dict, source_hash: str, request_id: str | None, conversation_url: str | None) -> dict:
    if output.is_symlink() or not output.is_file():
        raise ValueError("existing image must be a regular non-symlink PNG")
    if summary.get("status") != "succeeded" or summary.get("source_sha256") != source_hash:
        raise ValueError("existing image summary does not confirm the same source")
    if summary.get("prompt_id") != PROMPT_GOVERNANCE_ID or not summary.get("runId") or not summary.get("conversationUrl"):
        raise ValueError("existing image summary is missing request provenance")
    if request_id is not None and summary.get("runId") != request_id:
        raise ValueError("existing image request_id mismatch")
    if conversation_url is not None and conversation_url not in (summary.get("conversationUrl"), summary.get("conversation_url_requested")):
        raise ValueError("existing image conversation mismatch")
    if summary.get("submissionConfirmed") is not True or summary.get("verification") != "live-dom+snapshot":
        raise ValueError("existing image submission was not verified")
    for key in ("prompt_sha256", "input_sha256", "outputSha256", "image_sha256"):
        if not isinstance(summary.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", summary[key]):
            raise ValueError(f"existing image summary is missing {key}")
    payload = output.read_bytes()
    _validate_png(payload)
    if summary["image_sha256"] != hashlib.sha256(payload).hexdigest() or summary.get("image_bytes") != len(payload):
        raise ValueError("existing image bytes hash mismatch")
    return {**summary, "reused": True}


def run(source_path: Path, output_path: Path, bridge_path: Path | None = None, summary_path: Path | None = None, conversation_url: str | None = None, prompt_fetcher=None, request_id: str | None = None) -> dict:
    doc = _read_regular(source_path, "source")
    source_hash = hashlib.sha256(doc.encode("utf-8")).hexdigest()
    prepared_summary = None
    if output_path.exists() or output_path.is_symlink():
        return _reuse_image(output_path, _load_existing_summary(summary_path), source_hash, request_id, conversation_url)
    if request_id is not None and summary_path is not None and (summary_path.exists() or summary_path.is_symlink()):
        previous = _load_existing_summary(summary_path)
        if previous.get("runId") == request_id:
            if previous.get("source_sha256") != source_hash:
                raise ValueError("existing request_id source mismatch")
            if previous.get("status") == "needs_review":
                return {**previous, "reused": True}
            if previous.get("status") != "prepared":
                raise ValueError("existing request summary has no reusable PNG; observe the original request")
            if previous.get("caller_label") != CALLER_LABEL:
                raise ValueError("prepared image request caller identity mismatch")
            if conversation_url is not None and previous.get("conversation_url_requested") != conversation_url:
                raise ValueError("prepared image request conversation mismatch")
            prepared_summary = previous
    if visible_chars(doc) < MIN_DOC_VISIBLE_CHARS:
        raise ValueError(f"document too short for an image: {visible_chars(doc)} < {MIN_DOC_VISIBLE_CHARS}")
    try:
        prompt_assets = [(prompt_fetcher or fetch_prompt_asset)(PROMPT_GOVERNANCE_ID)]
    except Exception as exc:
        failure = {"status": "needs_review", "reason": str(exc), "runId": request_id, "source_sha256": source_hash, "attempts": []}
        if summary_path:
            _write_summary(summary_path, failure)
        return failure
    prompt = build_prompt(doc, prompt_assets[0]["content"])
    started = time.monotonic()
    attempts: list[dict] = []
    requested_conversation_url = conversation_url or WORKFLOW_NEW_CHAT_URL
    call_args = {"bridge": bridge_path, "max_wait_seconds": MAX_WAIT_SECONDS, "image": True, "conversation_url": requested_conversation_url, "caller_label": CALLER_LABEL}
    if request_id is not None:
        call_args.update(request_id=request_id, busy_retry_max_wait_seconds=0)
    context = {
        "runId": request_id, "run_id": request_id, "caller_label": CALLER_LABEL,
        "source_sha256": source_hash, "conversation_url_requested": requested_conversation_url,
        "prompt_id": PROMPT_GOVERNANCE_ID,
        **{key: prompt_assets[0][key] for key in ("prompt_source", "prompt_revision", "prompt_sha256", "prompt_fetched_at")},
        "input_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    }
    if request_id is not None and summary_path is not None:
        # Make the request identity and source binding durable before the
        # Bridge can submit anything; a killed caller can then observe this ID.
        if prepared_summary is not None:
            for key in ("prompt_id", "prompt_sha256", "input_sha256", "conversation_url_requested"):
                if prepared_summary.get(key) != context.get(key):
                    raise ValueError(f"prepared image request {key} mismatch")
        _write_summary(summary_path, {**context, "status": "attempting", "attempts": []})
    result = {}
    try:
        result = run_bridge(prompt, **call_args)
        _record_attempt(attempts, result)
        cooldown_wait = _cooldown_wait_seconds(result) if request_id is None else None
        if cooldown_wait is not None:
            time.sleep(cooldown_wait)
            result = run_bridge(prompt, **call_args)
            _record_attempt(attempts, result)
        if request_id is not None and result.get("runId") != request_id:
            raise ValueError("bridge request_id mismatch")
        payload = verified_image(result)
        _validate_png(payload)
    except Exception as exc:
        # 可能已经提交但无法确认时到达这里：状态必须是 needs_review，禁止自动重发。
        failure = {
            **context,
            "status": "needs_review",
            "reason": str(exc),
            **{key: result.get(key) for key in ("conversationUrl", "actualConversationUrl", "submissionConfirmed", "outputSha256", "verification")},
            "runId": request_id or result.get("runId"),
            "run_id": request_id or result.get("runId"),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "attempts": attempts,
            "bridge": {k: result.get(k) for k in ("status", "runId", "reason", "retryAfterSeconds", "diagnostics")},
        }
        if summary_path:
            _write_summary(summary_path, failure)
        return failure
    metadata = {
        **context,
        "status": "succeeded",
        "output": str(output_path),
        "runId": result.get("runId"),
        "run_id": result.get("runId"),
        "conversationUrl": result.get("conversationUrl"),
        "conversation_url": result.get("conversationUrl"),
        "actualConversationUrl": result.get("actualConversationUrl"),
        "verification": result.get("verification"),
        "submissionConfirmed": result.get("submissionConfirmed"),
        "outputSha256": result.get("outputSha256"),
        "output_sha256": result.get("outputSha256"),
        "image_bytes": len(payload),
        "image_sha256": hashlib.sha256(payload).hexdigest(),
        "reason": None,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "attempts": attempts,
    }
    _atomic_write_bytes(output_path, payload)
    if summary_path:
        _write_summary(summary_path, metadata)
    return metadata


def recover(source_path: Path, output_path: Path, summary_path: Path, *, request_id: str,
            observe_fn=observe_bridge) -> dict:
    """Observe and materialize only the original Bridge image request."""
    doc = _read_regular(source_path, "source")
    source_hash = hashlib.sha256(doc.encode("utf-8")).hexdigest()
    previous = _load_existing_summary(summary_path)
    if previous.get("runId") != request_id or previous.get("source_sha256") != source_hash:
        raise ValueError("image recovery identity or source hash mismatch")
    if previous.get("caller_label") != CALLER_LABEL:
        raise ValueError("image recovery caller identity mismatch")
    if output_path.exists() or output_path.is_symlink():
        if output_path.is_symlink():
            raise ValueError("existing image must not be a symlink")
        if previous.get("status") == "succeeded":
            return _reuse_image(output_path, previous, source_hash, request_id,
                                previous.get("conversation_url_requested"))
        # A PNG without a verified receipt may be a partial write. Re-observe
        # the original request and replace it only with verified bytes.
    requested_url = previous.get("conversation_url_requested") or WORKFLOW_NEW_CHAT_URL
    if requested_url not in {WORKFLOW_NEW_CHAT_URL} and "/c/" not in requested_url:
        raise ValueError("image recovery URL is not bound to a known ChatGPT entrypoint or session")
    result = observe_fn(
        original_run_id=request_id,
        conversation_url=requested_url,
        caller_label=CALLER_LABEL,
        max_wait_seconds=720,
    )
    if not isinstance(result, dict) or result.get("runId") != request_id:
        recovery = {**previous, "status": "needs_review", "reason": "observer-run-id-mismatch", "reused": True}
        _write_summary(summary_path, recovery)
        return recovery
    if result.get("status") != "succeeded":
        recovery = {**previous, "status": "needs_review",
                    "reason": result.get("reason") or result.get("status") or "observer-unknown",
                    "observer_status": result.get("status"), "reused": True}
        _write_summary(summary_path, recovery)
        return recovery
    payload = verified_image(result)
    _validate_png(payload)
    metadata = {
        **previous,
        "status": "succeeded", "output": str(output_path),
        "runId": request_id, "run_id": request_id,
        "conversationUrl": result.get("conversationUrl"),
        "conversation_url": result.get("conversationUrl"),
        "actualConversationUrl": result.get("actualConversationUrl"),
        "verification": result.get("verification"),
        "submissionConfirmed": result.get("submissionConfirmed"),
        "outputSha256": result.get("outputSha256"),
        "output_sha256": result.get("outputSha256"),
        "image_bytes": len(payload),
        "image_sha256": hashlib.sha256(payload).hexdigest(),
        "reason": None, "recovered_from_observation": True,
    }
    _atomic_write_bytes(output_path, payload)
    _write_summary(summary_path, metadata)
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="完整精读文档 markdown（run_dir/main-doc.md）")
    parser.add_argument("--output", required=True, type=Path, help="核心内容图 PNG 输出路径")
    parser.add_argument("--bridge", type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--conversation-url", type=str, help="复用已有 ChatGPT 会话链接，不传则开新窗口")
    parser.add_argument("--request-id", type=str, help="已持久化的 Bridge 请求 UUID；禁用隐藏重试")
    args = parser.parse_args()
    try:
        result = run(args.source, args.output, args.bridge, args.summary, args.conversation_url, request_id=args.request_id)
    except Exception as exc:
        result = {"status": "needs_review", "reason": str(exc)}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "succeeded" else 2


if __name__ == "__main__":
    raise SystemExit(main())
