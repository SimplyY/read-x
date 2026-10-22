#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path


SPEC = importlib.util.spec_from_file_location("delivery_card", Path(__file__).with_name("render_long_read_delivery_card.py"))
card = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(card)


def successful_receipt():
    required = card.REQUIRED_SUCCESS_ARTIFACTS
    return {
        "schema": card.DELIVERY_RECEIPT_SCHEMA,
        "status": "succeeded",
        "main_url": "https://feishu.cn/docx/main",
        "munger_embedded": True,
        "unresolved_actions": [],
        "artifacts": {key: {"required": True, "status": "confirmed"} for key in required},
    }


def test_success_card_has_single_link_and_real_newline():
    value = card.render_card(title="标题", main_url="https://feishu.cn/docx/main",
                             munger_embedded=True, delivery_receipt=successful_receipt())
    payload = json.loads(json.dumps(value, ensure_ascii=False))
    contents = [element["content"] for column in payload["body"]["elements"][0]["columns"] for element in column["elements"]]
    assert any("\n" in content for content in contents)
    assert all("\\n" not in content for content in contents)
    assert all("munger" not in content for content in contents)
    assert any("主精读文档（含芒格洞察）" in content for content in contents)
    links = [content for content in contents if "https://feishu.cn/docx/main" in content]
    assert len(links) == 1


def test_failure_card_never_uses_success_title():
    receipt = successful_receipt()
    receipt["status"] = "failed"
    value = card.render_card(title="标题", main_url="https://feishu.cn/docx/main",
                             failure_reason="image-send-failed", delivery_receipt=receipt)
    content = json.dumps(value, ensure_ascii=False)
    assert "main" in content and "长文精读完成" not in content and "自动交付已结束" in content
    assert "失败详情：image-send-failed" in content
    assert "芒格分析：image-send-failed" not in content


def test_failure_without_a_document_has_no_fake_feishu_home_link():
    receipt = {
        "schema": card.DELIVERY_RECEIPT_SCHEMA,
        "status": "failed", "main_url": None, "munger_embedded": False,
        "artifacts": {}, "unresolved_actions": [],
    }
    value = card.render_card(title="标题", main_url=None,
                             failure_reason="main-document-unavailable",
                             delivery_receipt=receipt)
    content = json.dumps(value, ensure_ascii=False)
    assert "尚未创建或无法确认" in content
    assert "打开主文档" not in content
    assert "https://feishu.cn" not in content


def test_missing_receipt_is_processing_not_success():
    value = card.render_card(title="标题", main_url="https://feishu.cn/docx/main")
    content = json.dumps(value, ensure_ascii=False)
    assert "部分交付｜恢复中" in content
    assert "长文精读完成" not in content


def test_image_note_is_appended_without_affecting_munger_state():
    value = card.render_card(title="标题", main_url="https://feishu.cn/docx/main", munger_embedded=True,
                             image_note="待插入", delivery_receipt=successful_receipt())
    content = json.dumps(value, ensure_ascii=False)
    assert "待插入" in content
    assert "主精读文档（含芒格洞察）" in content


def test_success_requires_all_required_artifacts_and_no_unknown_actions():
    receipt = successful_receipt()
    del receipt["artifacts"]["core-image-insert"]
    try:
        card.render_card(title="标题", main_url="https://feishu.cn/docx/main",
                         munger_embedded=True, delivery_receipt=receipt)
    except ValueError as exc:
        assert "missing required" in str(exc)
    else:
        raise AssertionError("incomplete required artifacts must not render success")

    receipt = successful_receipt()
    receipt["unresolved_actions"] = ["core-image-send"]
    try:
        card.render_card(title="标题", main_url="https://feishu.cn/docx/main",
                         munger_embedded=True, delivery_receipt=receipt)
    except ValueError as exc:
        assert "unresolved" in str(exc)
    else:
        raise AssertionError("unknown remote actions must not render success")


def test_cli_requires_verified_score_evidence():
    script = Path(__file__).with_name("render_long_read_delivery_card.py")
    with tempfile.TemporaryDirectory() as temp:
        run_dir = Path(temp)
        scoring_result = run_dir / "scoring-result.json"
        scoring_result.write_text('{"score_status":"scored"}', encoding="utf-8")
        result_hash = hashlib.sha256(scoring_result.read_bytes()).hexdigest()
        evidence = run_dir / "score-gate.json"
        output = run_dir / "delivery-card.json"
        command = [
            sys.executable, str(script), "--title", "标题",
            "--main-url", "https://feishu.cn/docx/main",
            "--score-evidence", str(evidence),
            "--scoring-result", str(scoring_result), "--output", str(output),
        ]
        result = subprocess.run(command, text=True, capture_output=True)
        assert result.returncode != 0 and not output.exists()

        evidence.write_text(json.dumps({
            "schema_version": "1",
            "message_id": "om_score_card",
            "scoring_result_sha256": result_hash,
            "sent_at_command": True,
        }), encoding="utf-8")
        result = subprocess.run(command, text=True, capture_output=True)
        assert result.returncode == 0 and json.loads(output.read_text())["schema"] == "2.0"

        evidence.write_text(json.dumps({
            "schema_version": "1",
            "message_id": "bad",
            "scoring_result_sha256": "a" * 64,
            "sent_at_command": True,
        }), encoding="utf-8")
        output.unlink()
        result = subprocess.run(command, text=True, capture_output=True)
        assert result.returncode != 0 and not output.exists()

        for bad in (
            {"schema_version": "2"},
            {"message_id": None},
            {"sent_at_command": False},
            {"scoring_result_sha256": None},
        ):
            payload = {
                "schema_version": "1",
                "message_id": "om_score_card",
                "scoring_result_sha256": result_hash,
                "sent_at_command": True,
            }
            payload.update(bad)
            evidence.write_text(json.dumps(payload), encoding="utf-8")
            output.unlink(missing_ok=True)
            result = subprocess.run(command, text=True, capture_output=True)
            assert result.returncode != 0 and not output.exists()

        evidence.write_text(json.dumps({
            "schema_version": "1",
            "message_id": "om_score_card",
            "scoring_result_sha256": "b" * 64,
            "sent_at_command": True,
        }), encoding="utf-8")
        result = subprocess.run(command, text=True, capture_output=True)
        assert result.returncode != 0 and not output.exists()


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_"):
            value(); print(f"[ok] {name}")
