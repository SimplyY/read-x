#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


SPEC = importlib.util.spec_from_file_location(
    "send_score_card", Path(__file__).with_name("send_score_card.py")
)
module = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(module)


def _args(**overrides):
    defaults = {
        "scoring_result": Path("/dev/null"),
        "chat_id": "oc_chat",
        "user_id": None,
        "idempotency_key": "test-key",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_send_records_verified_message():
    seen = {}

    def fake_output(command):
        seen["command"] = command
        return json.dumps(
            {"ok": True, "data": {"message_id": "om_score_card"}}
        )

    evidence = module._send(_args(), {"schema": "2.0"}, output_fn=fake_output)
    assert evidence["message_id"] == "om_score_card"
    assert len(evidence["scoring_result_sha256"]) == 64
    assert evidence["chat_id"] == "oc_chat"
    assert evidence["idempotency_key"] == "test-key"
    assert seen["command"][0].endswith("lark-cli")
    assert "--user-id" not in seen["command"]


def test_send_rejects_missing_message_id():
    bad_output = lambda command: json.dumps({"ok": True, "data": {}})
    try:
        module._send(_args(), {"schema": "2.0"}, output_fn=bad_output)
    except RuntimeError as error:
        assert "message_id" in str(error)
    else:
        raise AssertionError("missing message_id was accepted")


if __name__ == "__main__":
    test_send_records_verified_message()
    test_send_rejects_missing_message_id()
    print("[ok] send score card tests")
