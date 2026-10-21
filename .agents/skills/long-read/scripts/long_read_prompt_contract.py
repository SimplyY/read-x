"""Shared Read-X long-read Prompt rules applied before ChatGPT submission."""
from __future__ import annotations

from pathlib import Path
import re


CONCISION_PROMPT_PATH = Path(__file__).resolve().parents[1] / "references/single-branch-concision.md"
HARD_TOTAL_LENGTH = re.compile(
    r"(?:\d{2,5}\s*[-~～—–到至]\s*\d{2,5}\s*(?:字|words?)|"
    r"(?:总字数|总量|篇幅|输出.{0,8}|全文.{0,8}).{0,16}(?:不超过|至少|不少于|至多|最多)?\s*\d{2,5}\s*(?:字|words?)|"
    r"(?:不超过|至少|不少于|至多|最多)\s*\d{2,5}\s*(?:字|words?))",
    re.IGNORECASE,
)


def load_concision_prompt(path: Path = CONCISION_PROMPT_PATH) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("single-branch concision prompt must be a regular file")
    value = path.read_text(encoding="utf-8").strip()
    if (not value or "不设置统一的硬性总字数要求。" not in value
            or "本指令作为实际发送给 ChatGPT 的生成约束" not in value):
        raise ValueError("single-branch concision prompt is incomplete")
    if HARD_TOTAL_LENGTH.search(value):
        raise ValueError("single-branch concision prompt contains a conflicting hard total-length requirement")
    return value


def assert_no_hard_total_length(instructions: str, *, context: str) -> None:
    if HARD_TOTAL_LENGTH.search(instructions):
        raise ValueError(f"{context} contains a conflicting hard total-length requirement")
