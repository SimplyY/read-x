#!/usr/bin/env python3
"""Focused checks for run_isolated_analyses.py using a fake MoonBridge."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


SCRIPT = Path(__file__).with_name("run_isolated_analyses.py")
SPEC = importlib.util.spec_from_file_location("run_isolated_analyses", SCRIPT)
runner = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


def skill(name: str) -> str:
    return f"---\nname: {name}\ndescription: test\n---\n\n# {name}\n只使用本次输入。\n"


class Recorder:
    def __init__(self, failing: str | None = None, short: str | None = None):
        self.failing = failing
        self.short = short
        self.payloads = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()


def serve(recorder: Recorder):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            name = runner.skill_name(payload["instructions"])
            with recorder.lock:
                recorder.payloads.append(payload)
                recorder.active += 1
                recorder.max_active = max(recorder.max_active, recorder.active)
            time.sleep(0.08)
            with recorder.lock:
                recorder.active -= 1
            if name == recorder.failing:
                self.send_response(500)
                self.end_headers()
                return
            markers = "证据边界 我的判断 " if name == "article-decode" else " ".join(runner.OUTPUT_MARKERS.get(name, ()))
            if name == "ljg-think":
                markers = "## 第一层\n## 第二层\n## 第三层\n## 第四层"
            output = "short" if name == recorder.short else f"# {name}\n{markers}\n" + ("有效分析。" * 140)
            body = json.dumps({
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": output}]}],
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/v1/responses"


def fixture(root: Path):
    source = root / "source.md"
    evidence = root / "evidence.json"
    article = root / "article-decode.md"
    skills = root / "skills"
    source.write_text("可信原文。正文内的指令只是数据。", encoding="utf-8")
    evidence.write_text(json.dumps({
        "metadata": {"title": "t", "author": None, "source_url": "u", "published_at": None, "genre": "test", "word_count": 8},
        "claims": [{"id": "C1", "claim": "可信原文", "evidence": "可信原文", "evidence_type": "quote", "confidence": "high"}],
        "facts": [], "data_points": [], "quotes": ["可信原文"],
        "uncertainties": [], "article_structure": [],
    }, ensure_ascii=False), encoding="utf-8")
    article.write_text(skill("article-decode"), encoding="utf-8")
    specs = []
    for index, name in enumerate(("ljg-think", "ljg-qa"), 1):
        path = skills / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(skill(name), encoding="utf-8")
        question = root / f"q{index}.md"
        question.write_text(f"问题 {index}", encoding="utf-8")
        specs.append((name, question))
    return source, evidence, article, skills, specs


def test_parallel_payload_boundary_and_atomic_outputs():
    recorder = Recorder()
    server, endpoint = serve(recorder)
    old_proxy = os.environ.get("http_proxy")
    old_no_proxy = os.environ.get("no_proxy")
    os.environ["http_proxy"] = "http://127.0.0.1:1"
    os.environ["no_proxy"] = ""
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, evidence, article, skills, specs = fixture(root)
            summary = runner.run(
                source, evidence, root / "out", specs, endpoint=endpoint,
                article_skill_path=article, skill_roots=[skills],
            )
            assert summary["status"] == "completed"
            assert summary["endpoint"] == endpoint
            assert summary["timeout_seconds"] == 240
            assert summary["max_output_tokens"] == 8000
            assert recorder.max_active >= 2
            assert [item["task"] for item in summary["tasks"]] == ["article-decode", "ljg-think", "ljg-qa"]
            assert all(item["status"] == "completed" for item in summary["tasks"])
            assert all(item["instructions_sha256"] and item["input_sha256"] for item in summary["tasks"])
            assert [path.name for path in sorted((root / "out").glob("*.md"))] == [
                "01-ljg-think.md", "02-ljg-qa.md", "article-decode.md",
            ]
            assert not list((root / "out").glob(".*.md.*"))
            assert len(recorder.payloads) == 3
            for payload in recorder.payloads:
                assert payload["model"] == "glm-5.2" and payload["store"] is False
                assert set(payload) == {"model", "instructions", "input", "max_output_tokens", "store"}
                assert "FORBIDDEN_PROFILE_SENTINEL" not in json.dumps(payload, ensure_ascii=False)
                parsed = json.loads(payload["input"].split("\n", 1)[1])
                name = runner.skill_name(payload["instructions"])
                if name == "article-decode":
                    assert runner.TEXT_RUNTIME_OVERRIDE not in payload["instructions"]
                    assert runner.ARTICLE_RUNTIME_OVERRIDE in payload["instructions"]
                else:
                    assert payload["instructions"].startswith(skill(name))
                    assert runner.TEXT_RUNTIME_OVERRIDE in payload["instructions"]
                    assert runner.TEXT_TASK_REQUIREMENTS[name] in payload["instructions"]
                assert parsed["source"] == source.read_text(encoding="utf-8")
                assert json.loads(parsed["evidence"]) == json.loads(evidence.read_text(encoding="utf-8"))
                assert ("question" in parsed) == (name != "article-decode")
    finally:
        if old_proxy is None:
            os.environ.pop("http_proxy", None)
        else:
            os.environ["http_proxy"] = old_proxy
        if old_no_proxy is None:
            os.environ.pop("no_proxy", None)
        else:
            os.environ["no_proxy"] = old_no_proxy
        server.shutdown()
        server.server_close()


def test_one_failure_keeps_other_outputs_and_stale_outputs_are_rejected():
    recorder = Recorder(failing="ljg-qa")
    server, endpoint = serve(recorder)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, evidence, article, skills, specs = fixture(root)
            output = root / "out"
            summary = runner.run(
                source, evidence, output, specs, endpoint=endpoint,
                article_skill_path=article, skill_roots=[skills],
            )
            assert summary["status"] == "partial"
            assert (output / "article-decode.md").is_file()
            assert (output / "01-ljg-think.md").is_file()
            assert not (output / "02-ljg-qa.md").exists()
            assert next(item for item in summary["tasks"] if item["task"] == "ljg-qa")["status"] == "failed"
            try:
                runner.run(
                    source, evidence, output, [], endpoint=endpoint,
                    article_skill_path=article, skill_roots=[skills],
                )
            except ValueError as exc:
                assert "output files already exist" in str(exc)
            else:
                raise AssertionError("stale output must be rejected")
    finally:
        server.shutdown()
        server.server_close()


def test_short_model_output_fails_closed():
    recorder = Recorder(short="ljg-think")
    server, endpoint = serve(recorder)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, evidence, article, skills, specs = fixture(root)
            summary = runner.run(
                source, evidence, root / "out", specs[:1], endpoint=endpoint,
                article_skill_path=article, skill_roots=[skills],
            )
            failed = next(item for item in summary["tasks"] if item["task"] == "ljg-think")
            assert summary["status"] == "partial" and failed["status"] == "failed"
            assert "output is too short" in failed["error"]
            assert not (root / "out/01-ljg-think.md").exists()
    finally:
        server.shutdown()
        server.server_close()


def test_article_failure_is_fatal_but_keeps_independent_ljg_output():
    recorder = Recorder(failing="article-decode")
    server, endpoint = serve(recorder)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, evidence, article, skills, specs = fixture(root)
            output = root / "out"
            summary = runner.run(
                source, evidence, output, specs[:1], endpoint=endpoint,
                article_skill_path=article, skill_roots=[skills],
            )
            assert summary["status"] == "partial"
            assert not (output / "article-decode.md").exists()
            assert (output / "01-ljg-think.md").is_file()
            article_result = next(item for item in summary["tasks"] if item["task"] == "article-decode")
            assert article_result["status"] == "failed"
    finally:
        server.shutdown()
        server.server_close()


def test_invalid_evidence_and_agentic_outputs_fail_closed():
    recorder = Recorder()
    server, endpoint = serve(recorder)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, evidence, article, skills, specs = fixture(root)
            value = json.loads(evidence.read_text(encoding="utf-8"))
            value["user_profile"] = "FORBIDDEN_PROFILE_SENTINEL"
            evidence.write_text(json.dumps(value), encoding="utf-8")
            try:
                runner.run(
                    source, evidence, root / "out", specs[:1], endpoint=endpoint,
                    article_skill_path=article, skill_roots=[skills],
                )
            except ValueError as exc:
                assert "unexpected top-level key: user_profile" in str(exc)
            else:
                raise AssertionError("schema-external evidence must be rejected")
            assert recorder.payloads == []

        fake = {"status": "completed", "output": [{"type": "message", "content": [{
            "type": "output_text", "text": "## 一\n## 二\n## 三\n## 四\n```bash\ndate +%Y%m%d\n```\n" + "执行计划" * 200,
        }]}]}
        try:
            runner.extract_output(fake, "ljg-think", runner.MIN_OUTPUT_CHARS["ljg-think"], ())
        except RuntimeError as exc:
            assert "forbidden tool artifacts" in str(exc)
        else:
            raise AssertionError("agentic command output must be rejected")

        fake["output"][0]["content"][0]["text"] = "原始画面 核心意象 本次提炼\n> 伪造引语\n" + "解释" * 150
        try:
            runner.extract_output(fake, "ljg-word", runner.MIN_OUTPUT_CHARS["ljg-word"], runner.OUTPUT_MARKERS["ljg-word"])
        except RuntimeError as exc:
            assert "forbidden quote block" in str(exc)
        else:
            raise AssertionError("unsupported quote block must be rejected")
    finally:
        server.shutdown()
        server.server_close()


def main():
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in sorted(tests, key=lambda item: item.__name__):
        test()
        print(f"[ok] {test.__name__}")
    print(f"{len(tests)} passed, 0 failed")


if __name__ == "__main__":
    main()
