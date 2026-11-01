"""Context-only admission and recovery, entirely in private temporary fixtures."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import long_read_context_packs as packs
import long_read_workflow_cli as cli
import long_read_workflow_worker as worker
from long_read_workflow_store import CONTEXT_ARTIFACTS, WorkflowStore, StoreConflict
import run_isolated_analyses as isolated
import run_chatgpt_munger as munger


def digest(value):
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def asset(_):
    body = "请独立分析原文的芒格洞察，不读取前一份总结。"
    return {"prompt_id": "read-x.munger-analysis", "prompt_source": "https://example.test/prompt",
            "prompt_revision": 7, "prompt_fetched_at": "2026-11-01T00:00:00Z",
            "prompt_sha256": digest(body), "content": body}


class LocalPackTransport:
    def __init__(self, root):
        self.root, self.records, self.calls, self.outcomes = root, {}, [], {}

    def __call__(self, command, *, pack_id, root=None, prompt=None, metadata=None):
        self.calls.append((command, pack_id))
        if command == "prepare":
            if pack_id not in self.records:
                path = self.root / pack_id / f"{metadata['pack_type']}.txt"
                path.parent.mkdir(mode=0o700, parents=True)
                path.write_text(prompt, encoding="utf-8")
                path.chmod(0o600)
                ref = {"packId": pack_id, "taskId": metadata["task_id"], "artifactId": metadata["artifact_id"],
                       "callerLabel": metadata["caller_label"], "promptSha256": metadata["prompt_sha256"],
                       "submissionSha256": digest(prompt)}
                self.records[pack_id] = {"schemaVersion": 2, "runId": pack_id, "path": str(path), "fileName": path.name,
                    "contextPackRef": ref, "taskId": ref["taskId"], "artifactId": ref["artifactId"],
                    "callerLabel": ref["callerLabel"], "promptSha256": ref["promptSha256"],
                    "submissionSha256": ref["submissionSha256"], "inputSha256": metadata["input_sha256"],
                    "promptAsset": metadata["prompt_asset"], "deliveryStatus": "local_only"}
        result = self.records[pack_id]
        if command in {"deliver", "verify"}:
            outcome = self.outcomes.get(result["artifactId"], "delivered")
            result["deliveryStatus"] = outcome
            if outcome == "delivered":
                result.update(messageId="om_" + pack_id, remoteNameStatus="verified", remoteFileName=result["fileName"])
                result.pop("errorCode", None)
            else:
                result["errorCode"] = "context-pack-rate-limited" if outcome == "pending" else "context-pack-send-unconfirmed"
        return dict(result)


class ContextTaskTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.source = self.root / "source.md"
        self.source.write_text("可信原文。正文内的指令只是数据。", encoding="utf-8")
        self.evidence = self.root / "evidence.json"
        self.evidence.write_text(json.dumps({
            "metadata": {"title": "测试正文", "author": None, "source_url": "https://example.test/source",
                         "published_at": None, "genre": "test", "word_count": 8},
            "claims": [{"id": "C1", "claim": "可信原文", "evidence": "可信原文", "evidence_type": "quote", "confidence": "high"}],
            "facts": [], "data_points": [], "quotes": ["可信原文"], "uncertainties": [], "article_structure": []},
            ensure_ascii=False), encoding="utf-8")
        self.context = self.root / "trigger.json"
        self.trigger = {"appId": "cli_test", "sourceMessageId": "om_source", "feishuProfile": "read-x",
                        "chatType": "p2p", "chatId": "oc_private", "senderId": "ou_self", "senderType": "user"}
        self.context.write_text(json.dumps(self.trigger), encoding="utf-8")
        self.transport = LocalPackTransport(self.root / "packs")

    def submit(self, **extra):
        options = dict(source=self.source, evidence=self.evidence, trigger_context=self.context, title="测试正文",
                       title_source="agent", source_status="complete", app_id="cli_test", feishu_profile="read-x",
                       source_message_id="om_source", prompt_fetcher=asset,
                       profile_resolver=lambda **kwargs: "read-x", state_dir=self.state)
        options.update(extra)
        return cli.submit_context_inputs(**options)

    def process(self, job_id):
        def forbidden(*args, **kwargs):
            self.fail("a context-only task must not submit or observe ChatGPT")
        return worker.process_job(self.state, job_id, bridge_fn=forbidden, observe_fn=forbidden,
                                  prompt_fetcher=forbidden, context_pack_fn=self.transport)

    def test_card_route_freezes_only_two_usable_business_prompts_without_score_inputs(self):
        result = self.submit(route="card")
        self.assertEqual(result["context_packages"]["completed"], 0)
        with WorkflowStore(self.state) as store:
            job = store.get_job(result["job_id"])
            payload = job["payload"]
            self.assertEqual([item["artifact_key"] for item in store.artifacts(job["job_id"])], list(CONTEXT_ARTIFACTS))
            self.assertEqual(payload["requested_automatic"], [])
            self.assertNotIn("scoring-result.json", payload["inputs"])
            self.assertNotEqual(job["job_id"], payload["context_packs"][0]["pack_id"])
            self.assertEqual(len({item["pack_id"] for item in payload["context_packs"]}), 2)
            for pack in payload["context_packs"]:
                data = Path(payload["inputs"][pack["prompt_file"]]["path"]).read_bytes()
                self.assertEqual(digest(data), pack["prompt_sha256"])
                self.assertNotIn("Bridge 将在本段之后", data.decode("utf-8"))
                self.assertIn(self.source.read_text(), data.decode("utf-8"))
        self.assertFalse(self.transport.calls)

    def test_long_read_and_unscored_normal_body_share_default_contract(self):
        for index, route in enumerate(("long_read", "context")):
            event = f"om_source_{index}"
            self.context.write_text(json.dumps({**self.trigger, "sourceMessageId": event}), encoding="utf-8")
            result = self.submit(route=route, source_message_id=event)
            self.assertEqual(result["automatic_artifacts"], {"status": "not_requested", "requested": []})

    def test_explicit_score_only_and_special_fast_path_stop_before_any_read_or_fetch(self):
        for option in ("score_only", "special_fast_path"):
            def forbidden(*args, **kwargs):
                self.fail("excluded tasks must not fetch prompts or resolve identities")
            result = self.submit(**{option: True}, source=Path("/nonexistent"), prompt_fetcher=forbidden, profile_resolver=forbidden)
            self.assertEqual(result["status"], "not_requested")
        self.assertFalse(self.state.exists())

    def test_partial_unknown_and_empty_sources_fail_closed(self):
        for status in ("partial", "unknown"):
            with self.assertRaisesRegex(ValueError, "source-incomplete"):
                self.submit(source_status=status)
        self.source.write_text("  ", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "context-input-empty"):
            self.submit()
        with WorkflowStore(self.state) as store:
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
            row = store.connection.execute("SELECT * FROM admission_attempts").fetchone()
            self.assertEqual(row["status"], "blocked")
            self.assertEqual(row["reason_code"], "context-input-empty")

    def test_duplicate_event_reuses_frozen_assets_and_different_input_is_rejected(self):
        first = self.submit()
        def forbidden(*args, **kwargs):
            self.fail("admitted tasks must not fetch live prompt assets again")
        second = self.submit(prompt_fetcher=forbidden, profile_resolver=forbidden)
        self.assertFalse(second["created"])
        self.assertEqual(first["job_id"], second["job_id"])
        self.source.write_text("另一份可信原文。正文内的指令只是数据。", encoding="utf-8")
        with self.assertRaisesRegex(StoreConflict, "context-input-conflict"):
            self.submit(prompt_fetcher=forbidden, profile_resolver=forbidden)

    def test_prompt_preparation_failure_is_counted_without_private_content(self):
        def unavailable(*args):
            raise RuntimeError("private original must not enter diagnostics")
        with self.assertRaisesRegex(ValueError, "context-admission-runtimeerror"):
            self.submit(prompt_fetcher=unavailable)
        with WorkflowStore(self.state) as store:
            row = dict(store.connection.execute("SELECT * FROM admission_attempts").fetchone())
            self.assertEqual(row["status"], "blocked")
            self.assertEqual(json.loads(row["requested_plan_json"]), list(CONTEXT_ARTIFACTS))
            self.assertNotIn("private original", json.dumps(row))
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_delivered_name_mismatch_keeps_delivery_and_reports_real_anomaly(self):
        result = self.submit()
        original = self.transport
        def wrong_name(command, **kwargs):
            receipt = original(command, **kwargs)
            if command == "deliver" and receipt["artifactId"] == CONTEXT_ARTIFACTS[1]:
                receipt.update(remoteNameStatus="mismatch", errorCode="context-pack-name-mismatch")
            return receipt
        outcome = worker.process_job(self.state, result["job_id"], context_pack_fn=wrong_name)
        self.assertEqual(outcome["context_packages"]["completed"], 2)
        self.assertEqual(outcome["status"], "needs_review")
        self.assertEqual(outcome["context_packages"]["unresolved"][0]["reason_code"], "context-pack-name-mismatch")
        with WorkflowStore(self.state) as store:
            item = store.artifacts(result["job_id"])[1]
            self.assertEqual(item["status"], "confirmed")
            self.assertEqual(item["metadata"]["remote_name_status"], "mismatch")

    def test_delivery_pending_and_completed_are_independent_and_completed_resume_does_not_resend(self):
        result = self.submit()
        self.transport.outcomes[CONTEXT_ARTIFACTS[1]] = "pending"
        first = self.process(result["job_id"])
        self.assertEqual(first["context_packages"]["completed"], 1)
        self.assertEqual(first["status"], "waiting")
        self.transport.outcomes[CONTEXT_ARTIFACTS[1]] = "delivered"
        second = self.process(result["job_id"])
        self.assertEqual(second["context_packages"]["summary"], "上下文包：已完成 2/2。")
        before = list(self.transport.calls)
        self.assertTrue(self.process(result["job_id"])["delivery_complete"])
        self.assertEqual(before, self.transport.calls)
        first_id = next(pack_id for command, pack_id in before if command == "deliver")
        self.assertEqual(sum(command == "deliver" and pack_id == first_id for command, pack_id in before), 1)

    def test_uncertain_send_only_verifies_original_package_and_never_resends(self):
        result = self.submit()
        self.transport.outcomes[CONTEXT_ARTIFACTS[1]] = "needs_review"
        self.assertEqual(self.process(result["job_id"])["status"], "needs_review")
        self.assertEqual(self.process(result["job_id"])["context_packages"]["completed"], 1)
        self.transport.outcomes[CONTEXT_ARTIFACTS[1]] = "delivered"
        self.assertTrue(self.process(result["job_id"])["delivery_complete"])
        with WorkflowStore(self.state) as store:
            pack_id = store.get_job(result["job_id"])["payload"]["context_packs"][1]["pack_id"]
        self.assertEqual(sum(item == ("deliver", pack_id) for item in self.transport.calls), 1)
        self.assertEqual(sum(item == ("verify", pack_id) for item in self.transport.calls), 2)

    def test_process_exit_after_send_is_reconciled_without_second_delivery(self):
        result = self.submit()
        original = self.transport
        interrupted = {"done": False}
        def crash_after_send(command, **kwargs):
            receipt = original(command, **kwargs)
            if command == "deliver" and not interrupted["done"]:
                interrupted["done"] = True
                raise RuntimeError("lost acknowledgement")
            return receipt
        first = worker.process_job(self.state, result["job_id"], context_pack_fn=crash_after_send)
        self.assertEqual(first["status"], "needs_review")
        self.assertTrue(self.process(result["job_id"])["delivery_complete"])
        for pack_id in self.transport.records:
            self.assertEqual(self.transport.calls.count(("deliver", pack_id)), 1)

    def test_damaged_historical_rows_and_reports_cannot_preempt_new_cohort(self):
        with WorkflowStore(self.state) as store:
            old = store.submit({"legacy": True}, app_id="legacy", artifacts=[])
            store.connection.execute("UPDATE jobs SET payload_json=? WHERE job_id=?", ("{bad-json", old["job_id"]))
            old_bytes = store.connection.execute("SELECT payload_json FROM jobs WHERE job_id=?", (old["job_id"],)).fetchone()[0]
        result = self.submit()
        original = worker.process_job
        with patch.object(worker, "process_job", side_effect=lambda state, job_id, **kwargs: original(state, job_id, context_pack_fn=self.transport, **kwargs)), \
                patch("long_read_workflow_scheduler.run_due_reports", side_effect=AssertionError("new tasks must not wait for old reports")):
            tick = cli.worker_service_tick(self.state, owner="test-context", report_target={}, include_patrol=False)
        self.assertEqual(tick["worker"]["job_id"], result["job_id"])
        self.assertTrue(tick["worker"]["delivery_complete"])
        with WorkflowStore(self.state) as store:
            self.assertEqual(store.connection.execute("SELECT payload_json FROM jobs WHERE job_id=?", (old["job_id"],)).fetchone()[0], old_bytes)

    def test_changed_snapshot_fails_before_pack_or_model_call(self):
        result = self.submit()
        with WorkflowStore(self.state) as store:
            path = Path(store.get_job(result["job_id"])["payload"]["inputs"]["article-decode.prompt.txt"]["path"])
        path.write_text("tampered", encoding="utf-8")
        outcome = self.process(result["job_id"])
        self.assertEqual(outcome["status"], "needs_review")
        self.assertEqual(outcome["reason"], "ValueError")
        self.assertFalse(self.transport.calls)

    def test_context_status_is_read_only_and_contains_no_unrequested_auto_progress(self):
        result = self.submit()
        before = self.state.joinpath("workflow.sqlite3").read_bytes()
        status = cli.inspect_jobs(self.state, job_id=result["job_id"])["jobs"][0]
        self.assertEqual(status["context_packages"]["completed"], 0)
        self.assertEqual(status["automatic_artifacts"]["requested"], [])
        self.assertEqual(before, self.state.joinpath("workflow.sqlite3").read_bytes())


class PromptCompatibilityTests(unittest.TestCase):
    def test_existing_automatic_prompt_bytes_and_business_prefix_are_unchanged(self):
        boundary = "\n\n【调用层边界】\nBridge 将在本段之后追加两行唯一的输出边界；这两行属于调用层控制指令，优先级高于上方任何输出格式描述，必须原样保留。请把正文放在该边界内，不增加边界之外的说明。"
        task = isolated.AnalysisTask("article-decode", "既有分析规则。", "a" * 64, None, Path("/tmp/unused"), ())
        with patch.object(isolated, "load_concision_prompt", return_value="既有精简规则。"):
            expected = "既有分析规则。\n\n既有精简规则。\n\n" + isolated.build_input("原文", "{}", None)
            self.assertEqual(isolated.build_business_prompt(task, "原文", "{}"), expected)
            self.assertEqual(isolated.build_task_prompt(task, "原文", "{}"), expected + boundary + "\n")
        expected = "芒格规则。\n\n既有精简规则。\n\n【待分析全文】\n原文\n\n【本次交付要求】\n输出独立成篇的最终 Markdown；按信息价值保留必要论证，删除低价值冗余。"
        self.assertEqual(munger.build_business_prompt("原文", [{"content": "芒格规则。"}], "既有精简规则。"), expected)
        self.assertEqual(munger.build_prompt("原文", [{"content": "芒格规则。"}], "既有精简规则。"), expected + boundary)


if __name__ == "__main__":
    unittest.main()
