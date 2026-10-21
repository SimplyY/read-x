#!/usr/bin/env python3
"""Render the single long-read document delivery Card 2.0 payload."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse


def _load_score_evidence(path: Path, scoring_result: Path) -> dict:
    evidence = json.loads(path.read_text(encoding="utf-8"))
    message_id = evidence.get("message_id")
    result_hash = evidence.get("scoring_result_sha256")
    valid = (
        evidence.get("schema_version") == "1"
        and isinstance(message_id, str)
        and message_id.startswith("om_")
        and evidence.get("sent_at_command") is True
        and isinstance(result_hash, str)
        and len(result_hash) == 64
    )
    if not valid:
        raise ValueError("score evidence is missing or invalid")
    actual_hash = hashlib.sha256(scoring_result.read_bytes()).hexdigest()
    if result_hash != actual_hash:
        raise ValueError("score evidence does not match scoring result")
    return evidence


def _url(value: str) -> str:
    if urlparse(value).scheme not in {"http", "https"}:
        raise ValueError("document URL must use http or https")
    return value


DELIVERY_RECEIPT_SCHEMA = "read-x-delivery-receipt/v1"
REQUIRED_SUCCESS_ARTIFACTS = {
    "analysis:article-decode", "analysis:munger-analysis", "main-document",
    "document-notification", "core-image", "core-image-insert", "core-image-send",
}
DELIVERY_TITLES = {
    "processing": "部分交付｜恢复中",
    "needs_review": "交付结果待核实",
    "failed": "精读交付失败",
    "succeeded": "长文精读完成",
}


def _delivery_state(receipt: dict | None, main_url: str) -> str:
    if receipt is None:
        return "processing"
    if not isinstance(receipt, dict) or receipt.get("schema") != DELIVERY_RECEIPT_SCHEMA:
        raise ValueError("delivery receipt is missing or invalid")
    status = receipt.get("status")
    if status not in DELIVERY_TITLES:
        raise ValueError("delivery receipt has an invalid workflow status")
    if status != "succeeded":
        return status
    if receipt.get("main_url") != main_url:
        raise ValueError("delivery receipt main URL does not match the card")
    if receipt.get("munger_embedded") is not True:
        raise ValueError("successful delivery receipt must confirm the required Munger section")
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, dict) or receipt.get("unresolved_actions") != []:
        raise ValueError("successful delivery receipt has unresolved or missing artifacts")
    missing = REQUIRED_SUCCESS_ARTIFACTS - set(artifacts)
    if missing:
        raise ValueError("successful delivery receipt is missing required artifact records")
    for key, artifact in artifacts.items():
        if not isinstance(artifact, dict):
            raise ValueError("delivery receipt artifact record is invalid")
        required = artifact.get("required") is True
        state = artifact.get("status")
        if required and state != "confirmed":
            raise ValueError("successful delivery receipt contains an unconfirmed required artifact")
        if state not in {"confirmed", "not_required"}:
            raise ValueError("successful delivery receipt contains an unknown artifact state")
        if state == "not_required" and required:
            raise ValueError("required artifact cannot be marked not_required")
    if not all(artifacts[key].get("required") is True for key in REQUIRED_SUCCESS_ARTIFACTS):
        raise ValueError("successful delivery receipt marks a required artifact optional")
    return status


def render_card(*, title: str, main_url: str, munger_embedded: bool = False,
                failure_reason: str | None = None, image_note: str | None = None,
                delivery_receipt: dict | None = None) -> dict:
    _url(main_url)
    delivery_status = _delivery_state(delivery_receipt, main_url)
    success = delivery_status == "succeeded"
    subtitle = "主精读文档（含芒格洞察）" if success and munger_embedded else "主精读文档"
    elements = [{
        "tag": "column_set", "flex_mode": "bisect", "horizontal_spacing": "8px",
        "columns": [{
            "tag": "column", "width": "weighted", "weight": 1,
            "background_style": "indigo-50", "padding": "12px",
            "elements": [{
                "tag": "markdown",
                "content": (
                    "**主精读文档（含芒格洞察）**\n"
                    f"[打开主文档]({main_url})" if success else
                    f"**主精读文档**\n[打开主文档]({main_url})"
                ),
            }],
        }],
    }]
    if not success:
        state_text = {
            "processing": "主文档链接已提供；仍有必需交付阶段在运行或等待确认。",
            "needs_review": "至少一项远端结果无法确认，尚未报告最终成功。",
            "failed": f"自动交付已结束：{failure_reason or '存在未完成的必需产物'}。",
        }[delivery_status]
        elements.append({"tag": "markdown", "content": state_text})
    if not munger_embedded and failure_reason:
        elements.append({"tag": "markdown", "content": f"芒格分析：{failure_reason}"})
    if image_note:
        elements.append({"tag": "markdown", "content": f"核心内容图：{image_note}"})
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "default", "summary": {"content": f"{DELIVERY_TITLES[delivery_status]}：{title}"}},
        "header": {
            "title": {"tag": "plain_text", "content": DELIVERY_TITLES[delivery_status]},
            "subtitle": {"tag": "plain_text", "content": f"{title} · {subtitle}"},
            "template": "indigo",
            "icon": {"tag": "standard_icon", "token": "chart_colorful"},
        },
        "body": {"direction": "vertical", "padding": "12px 12px 20px 12px", "elements": elements},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", required=True)
    parser.add_argument("--main-url", required=True)
    parser.add_argument("--munger-embedded", action="store_true")
    parser.add_argument("--failure-reason")
    parser.add_argument("--image-note")
    parser.add_argument("--delivery-receipt", type=Path,
                        help="verified workflow receipt; without it the card cannot report final success")
    parser.add_argument("--score-evidence", type=Path, required=True)
    parser.add_argument("--scoring-result", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    _load_score_evidence(args.score_evidence, args.scoring_result)
    receipt = json.loads(args.delivery_receipt.read_text(encoding="utf-8")) if args.delivery_receipt else None
    rendered = json.dumps(render_card(title=args.title, main_url=args.main_url,
                                      munger_embedded=args.munger_embedded,
                                      failure_reason=args.failure_reason, image_note=args.image_note,
                                      delivery_receipt=receipt), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
