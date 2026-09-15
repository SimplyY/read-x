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


def test_success_card_has_two_links_and_real_newline():
    value = card.render_card(title="标题", main_url="https://feishu.cn/docx/main", munger_url="https://feishu.cn/docx/munger")
    payload = json.loads(json.dumps(value, ensure_ascii=False))
    contents = [element["content"] for column in payload["body"]["elements"][0]["columns"] for element in column["elements"]]
    assert any("\n" in content for content in contents)
    assert all("\\n" not in content for content in contents)
    assert any("munger" in content for content in contents)
    assert any("ChatGPT 芒格洞察" in content for content in contents)


def test_failure_card_only_has_main_link():
    value = card.render_card(title="标题", main_url="https://feishu.cn/docx/main", failure_reason="bridge-output-unverified")
    content = json.dumps(value, ensure_ascii=False)
    assert "main" in content and "munger" not in content and "ChatGPT 芒格洞察待复核" in content


def test_skipped_card_shows_decision_score_reason():
    value = card.render_card(title="标题", main_url="https://feishu.cn/docx/main", decision_score=7.9, munger_threshold=8.5)
    content = json.dumps(value, ensure_ascii=False)
    assert "综合决策分 7.9" in content and "未达 ChatGPT 芒格门槛 8.5" in content


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
