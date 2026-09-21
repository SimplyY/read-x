#!/usr/bin/env python3
"""Generate the core-content image for a finished long-read document via the ChatGPT web bridge."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))
from chatgpt_bridge import run_bridge, verified_image


MAX_PROMPT_CHARS = 120_000
MIN_DOC_VISIBLE_CHARS = 500
MAX_COOLDOWN_WAIT_SECONDS = 900
# 单图生成通常比纯文字慢；上限对齐 Bridge 的 submit/observe 预算并留出余量。
MAX_WAIT_SECONDS = 600

# 图片指令作为仓库内版本化真源；每次运行的 sha256 记入 summary，便于追溯。
CORE_IMAGE_INSTRUCTION = """你是一名信息图设计师。请基于下方的长文精读文档全文，生成一张「核心内容图」：

- 一张图，16:9 横版，信息图风格；
- 只表达文档真正核心的判断与结构（核心结论、关键机制或关系），不做全文摘要；
- 图内文字使用简体中文，短语化，总量克制（不超过 40 个词），不得出现长段落；
- 构图自上而下：顶部一行点题的大标题短语，中部是核心结构或机制的可视化，底部一行出处小字（原文标题）；
- 平面矢量插画风格，干净背景，最多三种主色，不用照片素材，不加水印、边框和装饰性图标堆砌；
- 不得出现文档中不存在的事实；推断与原文判断在视觉上不得混淆。"""

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


def build_prompt(doc: str) -> str:
    prompt = f"{CORE_IMAGE_INSTRUCTION}\n\n【精读文档全文】\n{doc}{BRIDGE_BOUNDARY}\n"
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
    })


def run(source_path: Path, output_path: Path, bridge_path: Path | None = None, summary_path: Path | None = None, conversation_url: str | None = None) -> dict:
    if output_path.exists():
        raise FileExistsError(f"output already exists: {output_path}")
    doc = _read_regular(source_path, "source")
    if visible_chars(doc) < MIN_DOC_VISIBLE_CHARS:
        raise ValueError(f"document too short for an image: {visible_chars(doc)} < {MIN_DOC_VISIBLE_CHARS}")
    prompt = build_prompt(doc)
    started = time.monotonic()
    attempts: list[dict] = []
    result = run_bridge(prompt, bridge=bridge_path, max_wait_seconds=MAX_WAIT_SECONDS, image=True, conversation_url=conversation_url)
    _record_attempt(attempts, result)
    cooldown_wait = _cooldown_wait_seconds(result)
    if cooldown_wait is not None:
        # 提交前冷却：还没有任何内容被提交，按 Bridge 给出的等待时间安全恢复一次。
        time.sleep(cooldown_wait)
        result = run_bridge(prompt, bridge=bridge_path, max_wait_seconds=MAX_WAIT_SECONDS, image=True, conversation_url=conversation_url)
        _record_attempt(attempts, result)
    try:
        payload = verified_image(result)
        if not payload:
            raise RuntimeError("bridge image payload is empty")
    except Exception as exc:
        # 可能已经提交但无法确认时到达这里：状态必须是 needs_review，禁止自动重发。
        failure = {
            "status": "needs_review",
            "reason": str(exc),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "attempts": attempts,
            "bridge": {k: result.get(k) for k in ("status", "runId", "reason", "retryAfterSeconds", "diagnostics")},
        }
        if summary_path:
            _write_summary(summary_path, failure)
        return failure
    metadata = {
        "status": "succeeded",
        "output": str(output_path),
        "runId": result.get("runId"),
        "run_id": result.get("runId"),
        "conversationUrl": result.get("conversationUrl"),
        "conversation_url": result.get("conversationUrl"),
        "verification": result.get("verification"),
        "outputSha256": result.get("outputSha256"),
        "output_sha256": result.get("outputSha256"),
        "image_bytes": len(payload),
        "source_sha256": hashlib.sha256(doc.encode("utf-8")).hexdigest(),
        "conversation_url_requested": conversation_url,
        "instruction_sha256": hashlib.sha256(CORE_IMAGE_INSTRUCTION.encode("utf-8")).hexdigest(),
        "input_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "attempts": attempts,
    }
    _atomic_write_bytes(output_path, payload)
    if summary_path:
        _write_summary(summary_path, metadata)
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="完整精读文档 markdown（run_dir/main-doc.md）")
    parser.add_argument("--output", required=True, type=Path, help="核心内容图 PNG 输出路径")
    parser.add_argument("--bridge", type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--conversation-url", type=str, help="复用已有 ChatGPT 会话链接，不传则开新窗口")
    args = parser.parse_args()
    try:
        result = run(args.source, args.output, args.bridge, args.summary, args.conversation_url)
    except Exception as exc:
        result = {"status": "needs_review", "reason": str(exc)}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "succeeded" else 2


if __name__ == "__main__":
    raise SystemExit(main())
