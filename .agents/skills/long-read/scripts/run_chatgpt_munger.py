#!/usr/bin/env python3
"""Legacy munger runner; its direct CLI no longer submits new ChatGPT requests."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))
from chatgpt_bridge import WORKFLOW_NEW_CHAT_URL, bridge_command, run_bridge, verified_text
from long_read_prompt_contract import BRIDGE_BOUNDARY, assert_no_hard_total_length, load_concision_prompt


MAX_PROMPT_CHARS = 120_000
# 提交前冷却最多按 Bridge 给出的等待时间安全恢复一次；超过 15 分钟的冷却不挂起编排。
MAX_COOLDOWN_WAIT_SECONDS = 900

# 提示词独立成篇：read-x.munger-analysis 正文内嵌芒格之魂全文，一次只读取这一个资产。
PROMPT_GOVERNANCE_IDS = ("read-x.munger-analysis",)


def _skill_candidates(name: str) -> list[Path]:
    roots = []
    configured = os.environ.get("CODEX_HOME")
    if configured:
        roots.append(Path(configured) / "skills")
    roots.extend((Path.home() / ".codex/skills", Path.home() / ".agents/skills"))
    return [root / name / "SKILL.md" for root in dict.fromkeys(roots)]


def resolve_skill(name: str) -> Path:
    for path in _skill_candidates(name):
        if path.is_file():
            return path
    raise FileNotFoundError(f"skill is not installed: {name}")


def _read_regular(path: Path, label: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular file: {path}")
    value = path.read_text(encoding="utf-8")
    if not value.strip():
        raise ValueError(f"{label} must not be empty: {path}")
    return value


def _governance_candidates() -> list[Path]:
    roots = []
    configured = os.environ.get("CODEX_HOME")
    if configured:
        roots.append(Path(configured) / "skills")
    roots.extend((Path.home() / ".codex/skills", Path.home() / ".agents/skills"))
    return [root / "prompt-governance/scripts/fetch-prompt.mjs" for root in dict.fromkeys(roots)]


def resolve_prompt_governance() -> Path:
    for path in _governance_candidates():
        if path.is_file():
            return path
    raise FileNotFoundError("prompt-governance is not installed")


def fetch_prompt_asset(prompt_id: str) -> dict:
    test_assets = os.environ.get("PROMPT_GOVERNANCE_TEST_ASSETS")
    if test_assets:
        try:
            asset = json.loads(test_assets)[prompt_id]
        except (KeyError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Prompt 测试资产无效（{prompt_id}）") from exc
        return asset
    script = resolve_prompt_governance()
    try:
        completed = subprocess.run(
            ["node", str(script), prompt_id],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as exc:
        raise RuntimeError(f"prompt-governance 执行失败：{exc}") from exc
    if completed.returncode != 0:
        raise RuntimeError(f"Prompt 读取失败（{prompt_id}）：{completed.stderr.strip() or completed.stdout.strip()}")
    try:
        asset = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Prompt 读取返回坏 JSON（{prompt_id}）") from exc
    required = ("prompt_id", "prompt_source", "prompt_revision", "prompt_sha256", "prompt_fetched_at", "content")
    if any(key not in asset for key in required) or asset["prompt_id"] != prompt_id or not str(asset["content"]).strip():
        raise RuntimeError(f"Prompt 读取结果不完整（{prompt_id}）")
    if hashlib.sha256(str(asset["content"]).encode("utf-8")).hexdigest() != str(asset["prompt_sha256"]).lower():
        raise RuntimeError(f"Prompt 哈希不匹配（{prompt_id}）")
    return asset


def build_business_prompt(source: str, prompt_assets: list[dict], concision_prompt: str | None = None) -> str:
    if not prompt_assets:
        raise ValueError("prompt-assets-empty")
    asset_text = "\n\n".join(str(asset["content"]).strip() for asset in prompt_assets)
    assert_no_hard_total_length(asset_text, context="munger Prompt asset")
    concision = concision_prompt if concision_prompt is not None else load_concision_prompt()
    prompt = f"""{asset_text}

{concision}

【待分析全文】
{source}

【本次交付要求】
输出独立成篇的最终 Markdown；按信息价值保留必要论证，删除低价值冗余。"""
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"prompt-too-large: {len(prompt)} > {MAX_PROMPT_CHARS}")
    return prompt


def build_prompt(source: str, prompt_assets: list[dict], concision_prompt: str | None = None) -> str:
    prompt = build_business_prompt(source, prompt_assets, concision_prompt) + BRIDGE_BOUNDARY
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"prompt-too-large: {len(prompt)} > {MAX_PROMPT_CHARS}")
    return prompt


def _bridge_command(bridge: Path) -> list[str]:
    return bridge_command(bridge, max_wait_seconds=360)


def _validate_text(result: dict) -> str:
    return verified_text(result).strip() + "\n"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        if temporary:
            temporary.unlink(missing_ok=True)
        raise


def _write_summary(path: Path, value: dict) -> None:
    _atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


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


def run(source_path: Path, output_path: Path, bridge_path: Path | None = None, summary_path: Path | None = None, prompt_fetcher=None, request_id: str | None = None, *, _allow_legacy_bridge_submit: bool = False) -> dict:
    if not _allow_legacy_bridge_submit:
        return {
            "status": "needs_review",
            "reason_code": "legacy-direct-entry-disabled",
            "manual_handoff_required": True,
            "message": "独立芒格脚本不创建持久任务或网页请求；请从 long_read_workflow_cli.py submit 进入人工交接。旧 Run 通过原任务 resume 观察。",
        }
    if output_path.exists():
        raise FileExistsError(f"output already exists: {output_path}")
    source = _read_regular(source_path, "source")
    prompt_fetcher = prompt_fetcher or fetch_prompt_asset
    prompt_assets = [prompt_fetcher(prompt_id) for prompt_id in PROMPT_GOVERNANCE_IDS]
    prompt = build_prompt(source, prompt_assets)
    bridge = bridge_path or resolve_skill("chatgpt-web-bridge").parent / "scripts/bridge.mjs"
    if not bridge.is_file():
        raise FileNotFoundError(f"chatgpt bridge is not installed: {bridge}")
    started = time.monotonic()
    attempts: list[dict] = []
    # The Bridge owns its submit/observe/cleanup deadlines. A shorter parent
    bridge_args = {
        "bridge": bridge,
        "max_wait_seconds": 360,
        "caller_label": "read-x:long-read-text",
        "conversation_url": WORKFLOW_NEW_CHAT_URL,
    }
    if request_id is not None:
        bridge_args["request_id"] = request_id
    result = run_bridge(prompt, **bridge_args)
    _record_attempt(attempts, result)
    cooldown_wait = _cooldown_wait_seconds(result) if request_id is None else None
    if cooldown_wait is not None:
        # 提交前冷却：还没有任何内容被提交，按 Bridge 给出的等待时间安全恢复一次。
        time.sleep(cooldown_wait)
        result = run_bridge(prompt, **bridge_args)
        _record_attempt(attempts, result)
    try:
        text = _validate_text(result)
    except Exception as exc:
        # 可能已经提交但无法确认（observer-window-ended、submit-* 等）时到达这里：
        # 状态必须是 needs_review，禁止自动重发。
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
        "request_id": request_id or result.get("runId"),
        "sourceSha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "prompt_assets": [{key: asset.get(key) for key in ("prompt_id", "prompt_source", "prompt_revision", "prompt_sha256", "prompt_fetched_at")} for asset in prompt_assets],
        "input_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "attempts": attempts,
    }
    _atomic_write(output_path, text)
    if summary_path:
        _write_summary(summary_path, metadata)
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--bridge", type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--request-id", help="旧请求标识；新请求不再从此入口提交，旧任务请用持久工作流 resume")
    args = parser.parse_args()
    result = {
        "status": "needs_review",
        "reason_code": "legacy-direct-entry-disabled",
        "manual_handoff_required": True,
        "message": (
            "该独立脚本不再提交新的 ChatGPT 请求，也不能创建带任务身份的交接包。"
            "请使用 long_read_workflow_cli.py submit 后运行 Worker；已有 Run 通过原任务 resume 只读观察。"
        ),
        "next_step": "long_read_workflow_cli.py submit → run-worker → resume --job-id <id>",
        "request_id": args.request_id,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
