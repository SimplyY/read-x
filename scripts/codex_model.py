#!/usr/bin/env python3
"""Run one schema-bound model request through the current Codex CLI."""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path


MODEL = "codex-current"


def parse_json_output(raw: str, name: str) -> dict:
    raw = raw.strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError(f"{name} codex returned invalid JSON")
        try:
            parsed = json.loads(raw[start:end + 1])
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{name} codex returned invalid JSON") from exc
    return parsed


def call_model(input_text: str, schema: dict, name: str, timeout: float) -> dict:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="readx-codex-") as temp_dir:
        temp_path = Path(temp_dir)
        schema_path = temp_path / "schema.json"
        output_path = temp_path / "output.json"
        schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
        command = [
            "codex", "exec", "--ephemeral", "--skip-git-repo-check", "--ignore-rules",
            "--sandbox", "read-only", "--cd", temp_dir,
            "--output-schema", str(schema_path), "--output-last-message", str(output_path), "-",
        ]
        try:
            completed = subprocess.run(
                command, input=input_text, capture_output=True, text=True, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"{name} codex request timed out") from exc
        except OSError as exc:
            raise RuntimeError(f"{name} codex command unavailable") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(f"{name} codex request failed: {detail[:300]}")
        if not output_path.exists():
            raise RuntimeError(f"{name} codex request returned no output file")
        raw = output_path.read_text(encoding="utf-8").strip()
        parsed = parse_json_output(raw, name)
    if not isinstance(parsed, dict) or set(schema.get("required", [])) - set(parsed):
        raise RuntimeError(f"{name} codex returned invalid fields")
    print(json.dumps({
        "event": "codex_generation_completed", "name": name, "model": MODEL,
        "elapsed_seconds": round(time.perf_counter() - started, 3), "output_chars": len(raw),
    }, ensure_ascii=False, separators=(",", ":")), flush=True)
    return parsed
