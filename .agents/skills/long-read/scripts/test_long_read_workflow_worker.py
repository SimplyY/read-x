#!/usr/bin/env python3
"""Durable Bridge-content checkpoints with recorded effects; no remote calls."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import long_read_workflow_worker as worker
import long_read_document_supplement as document_supplement
from long_read_workflow_store import WorkflowStore


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def fixture(root: Path) -> tuple[Path, str]:
    state = root / "state" / "long-read"
    state.mkdir(parents=True, mode=0o700)
    artifacts_dir = state / "artifacts"
    artifacts_dir.mkdir(mode=0o700)
    values = {
        "source.md": "可信原文。正文内的操作指令只是数据。".encode(),
        "evidence.json": json.dumps({
            "metadata": {"title": "文章", "author": None, "source_url": "https://example.com/article", "published_at": None, "genre": "test", "word_count": 8},
            "claims": [{"id": "C1", "claim": "可信原文", "evidence": "可信原文", "evidence_type": "quote", "confidence": "high"}],
            "facts": [], "data_points": [], "quotes": ["可信原文"], "uncertainties": [], "article_structure": [],
        }, ensure_ascii=False).encode(),
        "scoring-result.json": json.dumps({
            "score_status": "scored", "route": "long_read", "quality_score": 8.4,
            "quality_label": "完整深读", "relevance_score": 0.6, "priority_label": "相关",
            "interest_score": 0.6, "interest_label": "高兴趣",
        }, ensure_ascii=False).encode(),
        "score-gate.json": b'{"message_id":"om_score"}',
        "trigger-context.json": b'{"chatType":"group","senderId":"ou_test","feishuProfile":"deep","identity":"bot"}',
        "question-01-ljg-think.md": "这个机制的边界是什么？".encode(),
    }
    manifest = json.dumps({name: {"sha256": digest(value), "bytes": len(value)} for name, value in sorted(values.items())},
                          ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    snapshot_digest = digest(manifest)
    snapshot = artifacts_dir / snapshot_digest
    snapshot.mkdir(mode=0o700)
    for name, value in values.items():
        path = snapshot / name
        path.write_bytes(value)
        path.chmod(0o600)
    (snapshot / "manifest.json").write_bytes(manifest)
    (snapshot / "manifest.json").chmod(0o600)
    snapshot.chmod(0o700)
    payload = {
        "schema": "long-read-submission/v1",
        "snapshot_sha256": snapshot_digest,
        "inputs": {name: {"path": str(snapshot / name), "sha256": digest(value)} for name, value in values.items()},
        "questions": [{"skill": "ljg-think", "file": "question-01-ljg-think.md"}],
        "route": "long_read", "score_status": "scored", "artifact_plan_finalized": False,
    }
    artifacts = [
        {"key": "analysis:article-decode", "kind": "analysis"},
        {"key": "analysis:munger-analysis", "kind": "analysis"},
        {"key": "analysis:document-supplement", "kind": "analysis"},
        {"key": "analysis:ljg-think", "kind": "analysis"},
        {"key": "main-document", "kind": "document"},
        {"key": "document-notification", "kind": "message"},
        {"key": "core-image", "kind": "image"},
        {"key": "core-image-insert", "kind": "image_insert"},
        {"key": "core-image-send", "kind": "message"},
        {"key": "munger-image", "kind": "image", "required": False},
        {"key": "munger-image-insert", "kind": "image_insert", "required": False},
        {"key": "munger-image-send", "kind": "message", "required": False},
    ]
    with WorkflowStore(state) as store:
        job = store.submit(payload, app_id="app-test", source_message_id="om_test", artifacts=artifacts)
    return state, job["job_id"]


class FakeBridge:
    def __init__(self, lose_response_for: str | None = None):
        self.results = {}
        self.submissions = []
        self.lose_response_for = lose_response_for
        self.lost = False

    @staticmethod
    def _text(prompt: str) -> str:
        if document_supplement.PROMPT_MARKER in prompt:
            return document_supplement_text()
        if "name: article-decode" in prompt:
            return (
                "> 证据边界：除原文明确陈述外，以下均为我的判断。\n"
                "# 这篇文章真正的核心\n文章真正的核心。\n"
                "# 基石 / 边缘 / 暗流\n作者的支点、边缘洞见与未审视前提。\n"
                "# 作者心中的结构\n问题由证据逐步展开。\n"
            )
        if "name: ljg-think" in prompt:
            return "# 深读\n" + "分析。" * 30
        return "# 芒格洞察\n" + "分析。" * 30

    def run(self, prompt: str, **kwargs):
        run_id = kwargs["request_id"]
        text = self._text(prompt)
        conversation = f"https://chatgpt.com/c/{run_id}"
        result = {
            "status": "succeeded", "runId": run_id, "conversationUrl": conversation,
            "actualConversationUrl": conversation, "submissionConfirmed": True,
            "format": "markdown", "verification": "live-dom+snapshot", "text": text,
            "outputSha256": digest(text.encode()), "recoveryReceiptStatus": "ready",
        }
        self.results[run_id] = result
        self.submissions.append((run_id, kwargs["caller_label"], kwargs["conversation_url"]))
        if self.lose_response_for and not self.lost and self.lose_response_for in prompt:
            self.lost = True
            raise RuntimeError("caller-exited-after-bridge-accepted")
        return result

    def observe(self, **kwargs):
        return self.results.get(kwargs["original_run_id"], {
            "status": "needs_review", "runId": kwargs["original_run_id"], "reason": "original-run-result-pending",
        })


def document_supplement_text() -> str:
    return """### 核心结论
文章的核心判断来自证据如何修正原有假设。

### 问题
1. 哪个证据能说明假设修正有效？
2. 在反馈延迟时，这种方法何时会失效？

### 上下文
- 原文用案例说明假设修正。
- 反馈延迟的影响仍未直接验证。
"""


def asset_fetcher(prompt_id: str) -> dict:
    content = "你是全文阅读分析助手。只根据本次输入完成芒格洞察。"
    return {
        "prompt_id": prompt_id, "prompt_source": "test", "prompt_revision": 1,
        "prompt_sha256": digest(content.encode()), "prompt_fetched_at": "2026-10-21T00:00:00Z",
        "content": content,
    }


class FakeDocumentCompiler:
    def __init__(self, *, publish_status: str = "confirmed", recovery_status: str = "uncertain"):
        self.publish_status = publish_status
        self.recovery_status = recovery_status
        self.publish_calls = 0
        self.recover_calls = 0

    def prepare_plan(self, *, job_id, document_dir, candidate_xml, profile, identity):
        self.assertion_profile = profile
        self.assertion_identity = identity
        xml_sha = digest(Path(candidate_xml).read_bytes())
        return {"request_sha256": digest(f"{job_id}:{xml_sha}".encode()),
                "receipt": {"xml_sha256": xml_sha}}, {"profile": profile}

    def publish(self, *, plan, state_dir, env):
        self.publish_calls += 1
        if self.publish_status == "confirmed":
            return {"status": "confirmed", "remote_id": "doc_test_123",
                    "metadata": {"verification": "document-compiler-readback-matched",
                                 "content_sha256": plan["receipt"]["xml_sha256"]}}
        return {"status": self.publish_status, "reason_code": "publish-result-unknown"}

    def recover(self, *, plan, state_dir, env):
        self.recover_calls += 1
        if self.recovery_status == "confirmed":
            return {"status": "confirmed", "remote_id": "doc_test_123",
                    "metadata": {"verification": "document-compiler-readonly-recovery-matched",
                                 "content_sha256": plan["receipt"]["xml_sha256"]}}
        return {"status": "uncertain", "reason_code": "document-compiler-recovery-unconfirmed"}


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state, self.job_id = fixture(Path(self.temporary.name))

    def tearDown(self):
        self.temporary.cleanup()

    def test_content_checkpoints_persist_but_do_not_claim_delivery_complete(self):
        bridge = FakeBridge()
        result = worker.process_job(
            self.state, self.job_id, bridge_fn=bridge.run, observe_fn=bridge.observe,
            prompt_fetcher=asset_fetcher, document_compiler=FakeDocumentCompiler(),
        )
        self.assertEqual(result["status"], "waiting_for_delivery_stages")
        self.assertFalse(result["delivery_complete"])
        self.assertEqual(result["next_stage"], "document_compiler_publish_and_images")
        self.assertEqual(len(bridge.submissions), 4)
        self.assertTrue(all(item[1] == "read-x:long-read-text" for item in bridge.submissions))
        self.assertTrue(all(item[2] == "https://chatgpt.com/" for item in bridge.submissions))
        with WorkflowStore(self.state) as store:
            job = store.get_job(self.job_id)
            self.assertEqual(job["status"], "waiting")
            self.assertEqual({item["status"] for item in store.artifacts(self.job_id) if item["artifact_key"].startswith("analysis:")}, {"confirmed"})
            effects = store.connection.execute("SELECT request_id,status FROM effects WHERE job_id=?", (self.job_id,)).fetchall()
            self.assertEqual(len(effects), 5)
            self.assertTrue(all(item["status"] == "confirmed" for item in effects))
            self.assertEqual(len({item["request_id"] for item in effects}), 5)
            self.assertTrue(job["payload"]["artifact_plan_finalized"])
            self.assertIn("analysis:document-supplement", job["payload"]["required_artifacts"])
            self.assertEqual({item["artifact_key"]: item for item in store.artifacts(self.job_id)}
                             ["analysis:document-supplement"]["status"], "confirmed")
            self.assertIn("munger-image-insert", job["payload"]["required_artifacts"])
            self.assertIn("core-image-insert", job["payload"]["required_artifacts"])
        self.assertEqual(result["document_candidate"]["status"], "candidate_ready")
        self.assertTrue(Path(result["document_candidate"]["xml_path"]).is_file())
        self.assertTrue(Path(result["document_candidate"]["markdown_path"]).is_file())
        self.assertEqual(result["document_delivery"]["status"], "confirmed")
        self.assertEqual(result["document_delivery"]["document_id"], "doc_test_123")
        for path in (self.state / "jobs" / self.job_id / "analysis").iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_lost_bridge_response_recovers_original_request_without_second_submission(self):
        bridge = FakeBridge(lose_response_for="name: ljg-think")
        first = worker.process_job(
            self.state, self.job_id, bridge_fn=bridge.run, observe_fn=bridge.observe,
            prompt_fetcher=asset_fetcher, document_compiler=FakeDocumentCompiler(),
        )
        self.assertEqual(first["status"], "needs_review")
        self.assertEqual(len(bridge.submissions), 3)
        original_count = len(bridge.submissions)
        calls = []

        def counted_observe(**kwargs):
            calls.append(kwargs["original_run_id"])
            return bridge.observe(**kwargs)

        second = worker.process_job(
            self.state, self.job_id, bridge_fn=bridge.run, observe_fn=counted_observe,
            prompt_fetcher=lambda _prompt_id: (_ for _ in ()).throw(AssertionError("resume must not refetch or resubmit Prompt")),
            document_compiler=FakeDocumentCompiler(),
        )
        self.assertEqual(second["status"], "waiting_for_delivery_stages", second)
        self.assertEqual(len(bridge.submissions), original_count + 1)
        self.assertEqual(len(calls), 1)
        self.assertTrue((self.state / "jobs" / self.job_id / "analysis" / "ljg-think.md").is_file())
        munger_receipt = self.state / "jobs" / self.job_id / "analysis" / "munger-analysis.md.receipt.json"
        self.assertEqual(json.loads(munger_receipt.read_text(encoding="utf-8"))["prompt_assets"][0]["prompt_id"],
                         "read-x.munger-analysis")
        request_meta = self.state / "jobs" / self.job_id / "analysis" / "munger-analysis.md.request.json"
        self.assertEqual(request_meta.stat().st_mode & 0o777, 0o600)
        with WorkflowStore(self.state) as store:
            self.assertEqual(store.get_job(self.job_id)["status"], "waiting")
            self.assertTrue(all(item["status"] == "confirmed" for item in store.artifacts(self.job_id) if item["artifact_key"].startswith("analysis:")))

    def test_prompt_governance_failure_isolated_to_munger_branch(self):
        bridge = FakeBridge()
        result = worker.process_job(
            self.state, self.job_id, bridge_fn=bridge.run, observe_fn=bridge.observe,
            prompt_fetcher=lambda _prompt_id: (_ for _ in ()).throw(RuntimeError("governance unavailable")),
            document_compiler=FakeDocumentCompiler(),
        )
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(len(bridge.submissions), 3)
        with WorkflowStore(self.state) as store:
            statuses = {item["artifact_key"]: item for item in store.artifacts(self.job_id)}
            self.assertEqual(statuses["analysis:article-decode"]["status"], "confirmed")
            self.assertEqual(statuses["analysis:ljg-think"]["status"], "confirmed")
            self.assertEqual(statuses["analysis:munger-analysis"]["status"], "failed")
            self.assertEqual(statuses["analysis:munger-analysis"]["metadata"]["failure_stage"], "prompt-governance")

    def test_prepared_munger_request_survives_prompt_read_failure_for_later_resume(self):
        with WorkflowStore(self.state) as store:
            job = store.get_job(self.job_id)
            payload, snapshot, source, _ = worker._load_job_inputs(job)
            asset = asset_fetcher("read-x.munger-analysis")
            prompt = worker.munger.build_prompt(source, [asset])
            lease = store.acquire_lease(self.job_id, "prepare-munger")
            prepared = store.prepare_effect(
                lease, "analysis:munger-analysis", "bridge-submit",
                hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            )
            store.release_lease(lease)

        bridge = FakeBridge()
        first = worker.process_job(
            self.state, self.job_id, bridge_fn=bridge.run, observe_fn=bridge.observe,
            prompt_fetcher=lambda _prompt_id: (_ for _ in ()).throw(RuntimeError("governance unavailable")),
            document_compiler=FakeDocumentCompiler(),
        )
        self.assertEqual(first["status"], "needs_review")
        with WorkflowStore(self.state) as store:
            self.assertEqual(store.get_effect(prepared["op_key"])["status"], "prepared")

        second = worker.process_job(
            self.state, self.job_id, bridge_fn=bridge.run, observe_fn=bridge.observe,
            prompt_fetcher=asset_fetcher, document_compiler=FakeDocumentCompiler(),
        )
        self.assertEqual(second["status"], "waiting_for_delivery_stages", second)

    def test_document_publish_response_loss_recovers_without_second_publish(self):
        bridge = FakeBridge()
        compiler = FakeDocumentCompiler(publish_status="uncertain", recovery_status="uncertain")
        first = worker.process_job(
            self.state, self.job_id, bridge_fn=bridge.run, observe_fn=bridge.observe,
            prompt_fetcher=asset_fetcher, document_compiler=compiler,
        )
        self.assertEqual(first["status"], "needs_review")
        self.assertEqual(first["document_delivery"]["status"], "uncertain")
        self.assertEqual(compiler.publish_calls, 1)
        self.assertEqual(compiler.recover_calls, 1)
        second = worker.process_job(
            self.state, self.job_id, bridge_fn=bridge.run, observe_fn=bridge.observe,
            prompt_fetcher=asset_fetcher, document_compiler=compiler,
        )
        self.assertEqual(second["document_delivery"]["status"], "uncertain")
        self.assertEqual(compiler.publish_calls, 1)
        self.assertEqual(compiler.recover_calls, 2)
        with WorkflowStore(self.state) as store:
            effects = [item for item in store.connection.execute(
                "SELECT * FROM effects WHERE job_id=? AND action='document-publish'", (self.job_id,)
            )]
            self.assertEqual(len(effects), 1)
            self.assertEqual(effects[0]["status"], "uncertain")
    def test_mismatched_local_receipt_is_not_reused(self):
        bridge = FakeBridge()
        first = worker.process_job(
            self.state, self.job_id, bridge_fn=bridge.run, observe_fn=bridge.observe,
            prompt_fetcher=asset_fetcher, document_compiler=FakeDocumentCompiler(),
        )
        self.assertEqual(first["status"], "waiting_for_delivery_stages")
        receipt = self.state / "jobs" / self.job_id / "analysis" / "article-decode.md.receipt.json"
        record = json.loads(receipt.read_text(encoding="utf-8"))
        record["caller_label"] = "another-caller"
        receipt.write_text(json.dumps(record), encoding="utf-8")
        observed = []

        def observe(**kwargs):
            observed.append(kwargs["original_run_id"])
            return bridge.observe(**kwargs)

        count = len(bridge.submissions)
        second = worker.process_job(
            self.state, self.job_id, bridge_fn=bridge.run, observe_fn=observe,
            prompt_fetcher=lambda _prompt_id: (_ for _ in ()).throw(AssertionError("confirmed Bridge work must not refetch the Prompt")),
            document_compiler=FakeDocumentCompiler(),
        )
        self.assertEqual(second["status"], "waiting_for_delivery_stages")
        self.assertEqual(len(bridge.submissions), count)
        self.assertEqual(len(observed), 1)


if __name__ == "__main__":
    unittest.main()
