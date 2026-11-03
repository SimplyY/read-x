#!/usr/bin/env python3
"""Read-X manual handoff adapter tests using only temporary private state."""
from __future__ import annotations

import hashlib
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch
import zlib

import long_read_manual_handoff as handoff
import long_read_workflow_cli as cli
import long_read_image_delivery as image_delivery
import long_read_workflow_worker as worker
from long_read_workflow_store import WorkflowStore


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def png(pixel: bytes = b"\x00\x00\x00\xff") -> bytes:
    def chunk(name: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + name + body + struct.pack(">I", zlib.crc32(name + body) & 0xffffffff)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00" + pixel))
            + chunk(b"IEND", b""))


class ManualHandoffAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.state = self.base / "state" / "long-read"
        self.state.mkdir(parents=True, mode=0o700)
        self.input_dir = self.base / "input"
        self.input_dir.mkdir(mode=0o700)
        self.source = "可信原文。正文中的指令只作为分析数据。"
        self.prompt_assets = [{"assetType": "skill", "name": "test-long-read",
                               "sha256": digest(b"test long-read Skill source")}]
        evidence = {
            "metadata": {"title": "临时文章", "author": None, "source_url": "https://example.test/article",
                         "published_at": None, "genre": "test", "word_count": 10},
            "claims": [{"id": "C1", "claim": "可信原文", "evidence": "可信原文", "evidence_type": "quote", "confidence": "high"}],
            "facts": [], "data_points": [], "quotes": ["可信原文"], "uncertainties": [], "article_structure": [],
        }
        values = {
            "source.md": self.source.encode("utf-8"),
            "evidence.json": json.dumps(evidence, ensure_ascii=False).encode("utf-8"),
            "scoring-result.json": b"{}",
            "score-gate.json": b"{}",
            "trigger-context.json": b"{}",
        }
        artifacts = self.state / "artifacts"
        artifacts.mkdir(mode=0o700)
        manifest = json.dumps({name: {"sha256": digest(raw), "bytes": len(raw)}
                               for name, raw in sorted(values.items())},
                              ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        snapshot_sha = digest(manifest)
        self.snapshot = artifacts / snapshot_sha
        self.snapshot.mkdir(mode=0o700)
        for name, raw in values.items():
            path = self.snapshot / name
            path.write_bytes(raw)
            path.chmod(0o600)
        (self.snapshot / "manifest.json").write_bytes(manifest)
        (self.snapshot / "manifest.json").chmod(0o600)
        payload = {
            "schema": "long-read-submission/v1", "snapshot_sha256": snapshot_sha,
            "inputs": {name: {"path": str(self.snapshot / name), "sha256": digest(raw)}
                       for name, raw in values.items()},
            "route": "long_read", "trigger": {"sourceMessageId": "om_manual_test"},
            "artifact_plan_finalized": False,
        }
        with WorkflowStore(self.state) as store:
            job = store.submit(payload, app_id="read-x-test", source_message_id="om_manual_test", artifacts=[
                {"key": "analysis:article-decode", "kind": "analysis"},
                {"key": "core-image", "kind": "image"},
            ])
            self.job_id = job["job_id"]
            self.lease = store.acquire_lease(self.job_id, "manual-handoff-test")
            self.job = store.get_job(self.job_id)
            self.source_sha = digest(values["source.md"])

    def tearDown(self):
        if getattr(self, "lease", None):
            with WorkflowStore(self.state) as store:
                try:
                    store.release_lease(self.lease)
                except Exception:
                    pass
        self.temporary.cleanup()

    def test_retention_sweep_does_not_create_store_and_rejects_invalid_core_result(self):
        self.assertEqual(handoff.prune_handoffs(state_dir=self.state), {"pruned": 0, "skipped": 0})
        invalid_state = self.base / "not-a-directory"
        invalid_state.write_text("not a state directory", encoding="utf-8")
        with self.assertRaisesRegex(handoff.ManualHandoffError, "retention-store-unavailable"):
            handoff.prune_handoffs(state_dir=invalid_state)
        root = handoff.handoff_root(self.state)
        root.mkdir(mode=0o700)
        with patch.object(handoff, "_invoke", return_value={"pruned": 2, "skipped": 1}) as invoke:
            self.assertEqual(handoff.prune_handoffs(state_dir=self.state), {"pruned": 2, "skipped": 1})
        invoke.assert_called_once_with("prune", root=root)
        with patch.object(handoff, "_invoke", return_value={"errorCode": "handoff-root-invalid"}):
            with self.assertRaisesRegex(handoff.ManualHandoffError, "retention-sweep-failed"):
                handoff.prune_handoffs(state_dir=self.state)

    def test_idle_worker_sweeps_handoffs_and_continues_if_sweep_fails(self):
        state = self.base / "idle-worker"
        with patch.object(handoff, "prune_handoffs", return_value={"pruned": 1, "skipped": 0}) as sweep:
            result = worker.run_once(state)
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["retention_status"], "complete")
        self.assertEqual(result["retention_pruned"], 1)
        sweep.assert_called_once_with(state_dir=state.absolute())

        with patch.object(handoff, "prune_handoffs", return_value={"pruned": 0, "skipped": 2}):
            result = worker.run_once(state)
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["retention_status"], "incomplete")
        self.assertEqual(result["retention_skipped"], 2)

        with patch.object(handoff, "prune_handoffs", side_effect=RuntimeError("private detail")):
            stderr = StringIO()
            with redirect_stderr(stderr):
                result = worker.run_once(state)
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["retention_status"], "failed")
        self.assertIn("manual-handoff-retention-sweep-failed", stderr.getvalue())
        self.assertNotIn("private detail", stderr.getvalue())

    def test_explicit_job_recovery_sweeps_and_exposes_retention_state(self):
        with patch.object(handoff, "prune_handoffs", return_value={"pruned": 1, "skipped": 0}) as sweep, \
                patch.object(worker, "process_job", return_value={"status": "waiting", "delivery_complete": False}) as process:
            result = worker.run_once(self.state, job_id=self.job_id)
        sweep.assert_called_once_with(state_dir=self.state.absolute())
        process.assert_called_once()
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["retention_status"], "complete")
        self.assertEqual(result["retention_pruned"], 1)

    def test_text_candidate_with_original_image_stays_untrusted_until_worker_validation(self):
        output = self.state / "jobs" / self.job_id / "analysis" / "article-decode.md"
        prompt = "完整业务 Prompt\n" + self.source
        with WorkflowStore(self.state) as store:
            prepared = worker._run_text_branch(
                store=store, lease=self.lease, lock=threading.RLock(),
                artifact_key="analysis:article-decode", output=output,
                source_sha=self.source_sha, prompt=prompt,
                validate=lambda text: text.strip() + "\n", state_dir=self.state,
                prompt_assets=self.prompt_assets,
            )
        self.assertEqual(prepared["status"], "awaiting_manual")
        self.assertEqual(prepared["source"], "manual-handoff")
        self.assertEqual(prepared["history_dependency"]["required"], False)
        self.assertTrue(prepared["history_dependency"]["verified"])
        self.assertTrue(prepared["full_flow"])
        self.assertTrue(prepared["continuation_choice"]["finalConfirmationRequired"])
        record = handoff.read_handoff(state_dir=self.state, handoff_id=prepared["handoff_id"])["record"]
        self.assertEqual(record["promptIdentity"], {
            "promptId": "read-x.skill/test-long-read",
            "revision": self.prompt_assets[0]["sha256"],
            "sha256": digest(prompt.encode("utf-8")),
        })
        prompt_path = Path(prepared["prompt_path"])
        self.assertEqual(prompt_path.read_bytes(), prompt.encode("utf-8"))
        self.assertEqual(prompt_path.stat().st_mode & 0o777, 0o600)

        returned = self.base / "returned.md"
        returned.write_text("原始 ChatGPT 文本候选。", encoding="utf-8")
        original_image = self.base / "candidate.png"
        image_bytes = b"\x89PNG\r\n\x1a\noriginal image bytes"
        original_image.write_bytes(image_bytes)
        stdout = StringIO()
        with patch("long_read_workflow_worker.run_once", return_value={
                "status": "needs_review", "awaiting_manual": True,
                "delivery_complete": False, "content_branches": [],
        }) as resume_worker, redirect_stdout(stdout):
            exit_code = cli.main([
                "--state-dir", str(self.state), "import-manual-result",
                "--job-id", self.job_id, "--artifact", "analysis:article-decode",
                "--result", str(returned), "--file", str(original_image),
                "--event-id", "codex-import:message-1",
            ])
        resume_worker.assert_called_once()
        self.assertEqual(exit_code, 75)
        first_import = json.loads(stdout.getvalue())["candidate_import"]
        self.assertEqual(first_import["status"], "candidate_received")
        self.assertFalse(first_import["business_completed"])
        imported_record = handoff.read_handoff(state_dir=self.state, handoff_id=prepared["handoff_id"])["record"]
        candidate = imported_record["candidates"][0]
        self.assertEqual(candidate["intakeChannel"], "codex")
        self.assertEqual(candidate["operatorBasis"]["kind"], "codex-user-message")
        self.assertRegex(candidate["operatorBasis"]["referenceSha256"], r"^[a-f0-9]{64}$")
        self.assertEqual(candidate["evidenceLevel"], "user-asserted")
        duplicate = cli.import_manual_result(
            state_dir=self.state, job_id=self.job_id, artifact_key="analysis:article-decode",
            result_path=returned, file_paths=[original_image], event_id="codex-import:message-1",
        )
        self.assertEqual(duplicate["status"], "candidate_received")
        self.assertEqual(duplicate["candidate_id"], first_import["candidate_id"])

        with WorkflowStore(self.state) as store:
            effect = store.find_effect(self.job_id, "analysis:article-decode", handoff.MANUAL_ACTION)
            artifact = next(item for item in store.artifacts(self.job_id)
                            if item["artifact_key"] == "analysis:article-decode")
            self.assertEqual(effect["status"], "prepared")
            self.assertEqual(artifact["status"], "pending")
            resumed = worker._run_text_branch(
                store=store, lease=self.lease, lock=threading.RLock(),
                artifact_key="analysis:article-decode", output=output,
                source_sha=self.source_sha, prompt=None,
                validate=lambda text: text.strip() + "\n", prompt_sha=effect["request_sha256"],
                state_dir=self.state,
            )
            self.assertEqual(resumed["status"], "confirmed")
            self.assertEqual(resumed["source"], "manual-handoff")
            self.assertEqual(resumed["evidence_level"], "user-asserted")
            self.assertEqual(output.read_text(encoding="utf-8"), "原始 ChatGPT 文本候选。\n")
            receipt = json.loads(output.with_suffix(".md.manual.receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["rawSha256"], digest("原始 ChatGPT 文本候选。".encode("utf-8")))
            self.assertEqual(receipt["normalizedSha256"], digest(output.read_bytes()))
            self.assertNotIn("verification", receipt)
            self.assertNotIn("conversationUrl", receipt)
            saved_image = Path(receipt["files"][0]["path"])
            self.assertEqual(saved_image.read_bytes(), image_bytes)
            self.assertEqual(saved_image.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(store.find_effect(self.job_id, "analysis:article-decode", handoff.MANUAL_ACTION)["status"], "confirmed")

            later_upload = cli.import_manual_result(
                state_dir=self.state, job_id=self.job_id, artifact_key="analysis:article-decode",
                result_path=returned, file_paths=[original_image], event_id="codex-import:late-duplicate",
            )
            self.assertEqual(later_upload["status"], "needs_review")
            reused = worker._run_text_branch(
                store=store, lease=self.lease, lock=threading.RLock(),
                artifact_key="analysis:article-decode", output=output,
                source_sha=self.source_sha, prompt=None,
                validate=lambda text: text.strip() + "\n", prompt_sha=effect["request_sha256"],
                state_dir=self.state,
            )
            self.assertTrue(reused["reused"])
            self.assertEqual(output.read_text(encoding="utf-8"), "原始 ChatGPT 文本候选。\n")

    def test_wrong_input_version_and_expired_handoff_fail_closed(self):
        prompt = b"prompt with immutable source"
        prepared = handoff.prepare_branch(
            state_dir=self.state, job=self.job, artifact_key="analysis:article-decode",
            prompt_bytes=prompt, source_sha=self.source_sha,
            prompt_assets=[{"assetType": "skill", "name": "test-long-read",
                            "sha256": digest(b"test long-read Skill source")}],
        )
        binding = handoff.binding_for_job(
            self.job, "analysis:article-decode", prompt_sha=digest(prompt),
        )
        wrong = dict(binding, inputVersion=binding["inputVersion"] + ":old")
        imported = handoff.import_result(
            state_dir=self.state, handoff_id=prepared["handoffId"], binding=wrong,
            event_id="codex-import:wrong-version", source_claim="user claimed result",
            result_bytes=b"stale result", files=[],
        )
        self.assertEqual(imported["status"], "needs_review")
        with self.assertRaises(handoff.ManualHandoffError):
            handoff.read_candidate(
                state_dir=self.state, handoff_id=prepared["handoffId"],
                candidate_id=imported["candidate"]["candidateId"], binding=binding,
            )

        manifest_path = self.state / "manual-handoffs" / prepared["handoffId"] / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["expiresAt"] = "2000-01-01T00:00:00.000Z"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        manifest_path.chmod(0o600)
        with self.assertRaisesRegex(handoff.ManualHandoffError, "handoff-expired"):
            handoff.read_handoff(
                state_dir=self.state, handoff_id=prepared["handoffId"], binding=binding,
            )

    def test_raw_image_only_candidate_is_retained_and_waits_for_image_stage_adapter(self):
        output = self.state / "jobs" / self.job_id / "analysis" / "article-decode.md"
        with WorkflowStore(self.state) as store:
            prepared = worker._run_text_branch(
                store=store, lease=self.lease, lock=threading.RLock(),
                artifact_key="analysis:article-decode", output=output,
                source_sha=self.source_sha, prompt="prompt requiring text analysis",
                validate=lambda text: text, state_dir=self.state, prompt_assets=self.prompt_assets,
            )
        image_path = self.base / "returned-original.png"
        image_bytes = b"original image candidate bytes"
        image_path.write_bytes(image_bytes)
        imported = cli.import_manual_result(
            state_dir=self.state, job_id=self.job_id, artifact_key="analysis:article-decode",
            result_path=Path("-"), file_paths=[image_path], event_id="codex-import:image-only",
        )
        self.assertEqual(imported["status"], "candidate_received")
        self.assertEqual(imported["files"][0]["sha256"], digest(image_bytes))
        with WorkflowStore(self.state) as store:
            effect = store.find_effect(self.job_id, "analysis:article-decode", handoff.MANUAL_ACTION)
            result = worker._run_text_branch(
                store=store, lease=self.lease, lock=threading.RLock(),
                artifact_key="analysis:article-decode", output=output,
                source_sha=self.source_sha, prompt=None, prompt_sha=effect["request_sha256"],
                validate=lambda text: text, state_dir=self.state,
            )
            self.assertEqual(result["status"], "awaiting_manual")
            self.assertEqual(result["reason"], "raw-image-result-awaiting-image-stage-adapter")
            self.assertFalse(output.exists())
            self.assertEqual(store.find_effect(self.job_id, "analysis:article-decode", handoff.MANUAL_ACTION)["status"], "prepared")
        candidate = handoff.read_candidate(
            state_dir=self.state, handoff_id=prepared["handoff_id"],
            candidate_id=imported["candidate_id"],
            binding=handoff.binding_for_job(self.job, "analysis:article-decode",
                                            prompt_sha=prepared["prompt_sha256"]),
        )
        self.assertEqual(candidate["resultBytes"], b"")
        self.assertEqual(candidate["files"][0]["bytes"], image_bytes)

    def _prepare_image(self):
        source = self.base / "main-doc.md"
        source.write_text("# 完整精读文档\n" + ("可信正文与充分上下文。" * 80), encoding="utf-8")
        content = "请依据完整精读文档生成一张核心内容图。"
        asset = {
            "prompt_id": image_delivery.core_image.PROMPT_GOVERNANCE_ID,
            "prompt_source": "https://example.test/prompt",
            "prompt_revision": 7,
            "prompt_sha256": digest(content.encode("utf-8")),
            "prompt_fetched_at": "2026-11-02T00:00:00.000Z",
            "content": content,
        }
        root = self.state / "jobs" / self.job_id
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        with patch.object(image_delivery.core_image, "run", side_effect=AssertionError("manual path attempted Bridge submission")):
            with WorkflowStore(self.state) as store:
                result = image_delivery._prepare_generation(
                    store=store, lease=self.lease, image_key="core-image", image_name="core-image",
                    source_path=source,
                    root=root,
                    round_number=0, conversation_url=None,
                    prompt_fetcher=lambda _prompt_id: asset, image_runner=image_delivery.core_image,
                )
        return root, source, asset, result

    def test_image_handoff_keeps_original_minimum_source_length_gate(self):
        source = self.base / "short-main-doc.md"
        source.write_text("# 太短\n" + self.source, encoding="utf-8")
        prompt = "请依据完整精读文档生成一张核心内容图。"
        asset = {
            "prompt_id": image_delivery.core_image.PROMPT_GOVERNANCE_ID,
            "prompt_source": "https://example.test/prompt",
            "prompt_revision": 7,
            "prompt_sha256": digest(prompt.encode("utf-8")),
            "prompt_fetched_at": "2026-11-02T00:00:00.000Z",
            "content": prompt,
        }
        root = self.state / "jobs" / self.job_id
        with patch.object(image_delivery.core_image, "run", side_effect=AssertionError("short input attempted Bridge submission")):
            with WorkflowStore(self.state) as store:
                result = image_delivery._prepare_generation(
                    store=store, lease=self.lease, image_key="core-image", image_name="core-image",
                    source_path=source, root=root, round_number=0, conversation_url=None,
                    prompt_fetcher=lambda _prompt_id: asset, image_runner=image_delivery.core_image,
                )
                effect = store.find_effect(self.job_id, "core-image", handoff.MANUAL_ACTION)
            self.assertEqual(result["status"], "needs_review")
            self.assertEqual(result["reason_code"], "manual-image-source-too-short")
            self.assertIsNone(effect)

    def _resume_image_candidate(self, *, root, source, asset):
        with patch.object(image_delivery.core_image, "run", side_effect=AssertionError("manual resume attempted Bridge submission")):
            with WorkflowStore(self.state) as store:
                return image_delivery._prepare_generation(
                    store=store, lease=self.lease, image_key="core-image", image_name="core-image",
                    source_path=source, root=root, round_number=0, conversation_url=None,
                    prompt_fetcher=lambda _prompt_id: asset, image_runner=image_delivery.core_image,
                )

    def test_image_handoff_import_resume_validates_png_and_preserves_manual_source(self):
        root, source, asset, prepared = self._prepare_image()
        self.assertEqual(prepared["status"], "awaiting_manual")
        self.assertEqual(prepared["source"], "manual-handoff")
        prompt_path = Path(prepared["prompt_path"])
        expected_prompt = image_delivery.core_image.build_prompt(
            source.read_text(encoding="utf-8"), asset["content"],
        ).encode("utf-8")
        self.assertEqual(prompt_path.read_bytes(), expected_prompt)
        self.assertEqual(prompt_path.stat().st_mode & 0o777, 0o600)

        candidate_path = self.base / "candidate.png"
        candidate_bytes = png()
        image_delivery.core_image._validate_png(candidate_bytes)
        candidate_path.write_bytes(candidate_bytes)
        imported = cli.import_manual_result(
            state_dir=self.state, job_id=self.job_id, artifact_key="core-image",
            result_path=candidate_path, event_id="codex-image-import:1",
        )
        self.assertEqual(imported["status"], "candidate_received")
        self.assertFalse(imported["business_completed"])

        resumed = self._resume_image_candidate(root=root, source=source, asset=asset)
        self.assertEqual(resumed["status"], "confirmed")
        self.assertEqual(resumed["source"], "manual-handoff")
        output = root / "images" / "core-image.png"
        self.assertEqual(output.read_bytes(), candidate_bytes)
        receipt = json.loads(output.with_suffix(".png.manual.receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["schema"], "read-x-manual-image-receipt/v1")
        self.assertEqual(receipt["evidenceLevel"], "user-asserted")
        self.assertEqual(receipt["imageSha256"], digest(candidate_bytes))
        self.assertNotIn("verification", receipt)
        self.assertNotIn("submissionConfirmed", receipt)
        self.assertNotIn("conversationUrl", receipt)
        with WorkflowStore(self.state) as store:
            effect = store.find_effect(self.job_id, "core-image", handoff.MANUAL_ACTION)
            self.assertEqual(effect["status"], "confirmed")
            self.assertEqual(effect["metadata"]["source"], "manual-handoff")
            self.assertIsNone(store.find_effect(self.job_id, "core-image", "image-generate"))

    def test_missing_image_candidate_keeps_handoff_waiting(self):
        root, source, asset, prepared = self._prepare_image()
        self.assertEqual(prepared["status"], "awaiting_manual")
        resumed = self._resume_image_candidate(root=root, source=source, asset=asset)
        self.assertEqual(resumed["status"], "awaiting_manual")
        self.assertEqual(resumed["reason_code"], "manual-image-candidate-missing")
        with WorkflowStore(self.state) as store:
            effect = store.find_effect(self.job_id, "core-image", handoff.MANUAL_ACTION)
            self.assertEqual(effect["status"], "prepared")
            self.assertEqual(next(row for row in store.artifacts(self.job_id)
                                 if row["artifact_key"] == "core-image")["status"], "pending")

    def test_stale_candidate_binding_is_rejected_without_output(self):
        root, source, asset, prepared = self._prepare_image()
        candidate_bytes = png()
        with WorkflowStore(self.state) as store:
            effect = store.find_effect(self.job_id, "core-image", handoff.MANUAL_ACTION)
            binding = handoff.binding_for_job(
                self.job, "core-image", prompt_sha=effect["request_sha256"],
            )
        stale_binding = dict(binding, inputVersion=binding["inputVersion"] + ":old")
        imported = handoff.import_result(
            state_dir=self.state, handoff_id=prepared["handoff_id"], binding=stale_binding,
            event_id="codex-image-stale", source_claim="user reports ChatGPT returned an image",
            result_bytes=candidate_bytes,
        )
        self.assertEqual(imported["status"], "needs_review")
        resumed = self._resume_image_candidate(root=root, source=source, asset=asset)
        self.assertEqual(resumed["status"], "awaiting_manual")
        self.assertFalse((root / "images" / "core-image.png").exists())

    def test_same_candidate_duplicate_is_idempotent(self):
        root, source, asset, prepared = self._prepare_image()
        first_png = self.base / "candidate-1.png"
        first_png.write_bytes(png())
        first = cli.import_manual_result(
            state_dir=self.state, job_id=self.job_id, artifact_key="core-image",
            result_path=first_png, event_id="codex-image-import:same",
        )
        duplicate = cli.import_manual_result(
            state_dir=self.state, job_id=self.job_id, artifact_key="core-image",
            result_path=first_png, event_id="codex-image-import:same",
        )
        self.assertEqual(first["candidate_id"], duplicate["candidate_id"])
        self.assertFalse(duplicate["candidate_selection_required"])
        resumed = self._resume_image_candidate(root=root, source=source, asset=asset)
        self.assertEqual(resumed["status"], "confirmed")
        # Simulate a crash after the verified manual effect was stored but
        # before the artifact row was committed. Recovery must retain the
        # manual provenance instead of labelling it as Bridge verification.
        with WorkflowStore(self.state) as store:
            store.set_artifact(self.lease, "core-image", "pending", metadata={})
        resumed_again = self._resume_image_candidate(root=root, source=source, asset=asset)
        self.assertEqual(resumed_again["status"], "confirmed")
        self.assertTrue(resumed_again["reused"])
        with WorkflowStore(self.state) as store:
            artifact = next(item for item in store.artifacts(self.job_id)
                            if item["artifact_key"] == "core-image")
            self.assertEqual(artifact["status"], "confirmed")
            self.assertEqual(artifact["metadata"]["source"], "manual-handoff")
            self.assertNotIn("verification", artifact["metadata"])

    def test_distinct_image_candidates_wait_for_explicit_selection(self):
        root, source, asset, prepared = self._prepare_image()
        first_png2 = self.base / "candidate-a.png"
        second_png2 = self.base / "candidate-b.png"
        first_png2.write_bytes(png(b"\xff\x00\x00\xff"))
        second_png2.write_bytes(png(b"\x00\xff\x00\xff"))
        cli.import_manual_result(
            state_dir=self.state, job_id=self.job_id, artifact_key="core-image",
            result_path=first_png2, event_id="codex-image-import:a",
        )
        second = cli.import_manual_result(
            state_dir=self.state, job_id=self.job_id, artifact_key="core-image",
            result_path=second_png2, event_id="codex-image-import:b",
        )
        self.assertTrue(second["candidate_selection_required"])
        self.assertGreaterEqual(len(second["candidate_ids"]), 2)
        waiting = self._resume_image_candidate(root=root, source=source, asset=asset)
        self.assertEqual(waiting["status"], "awaiting_manual")
        self.assertEqual(waiting["reason_code"], "manual-image-candidate-selection-required")

        chosen_id = second["candidate_ids"][-1]
        selection = cli.select_manual_candidate(
            state_dir=self.state, job_id=self.job_id, artifact_key="core-image", candidate_id=chosen_id,
        )
        self.assertEqual(selection["status"], "candidate_selected")
        selected = self._resume_image_candidate(root=root, source=source, asset=asset)
        self.assertEqual(selected["status"], "confirmed")
        self.assertEqual((root / "images" / "core-image.png").read_bytes(), second_png2.read_bytes())

    def test_tampered_candidate_bytes_are_rejected_and_not_materialized(self):
        root, source, asset, prepared = self._prepare_image()
        candidate_path = self.base / "candidate-tampered.png"
        candidate_path.write_bytes(png())
        imported = cli.import_manual_result(
            state_dir=self.state, job_id=self.job_id, artifact_key="core-image",
            result_path=candidate_path, event_id="codex-image-import:tamper",
        )
        candidate_file = (self.state / "manual-handoffs" / imported["handoff_id"]
                          / "candidates" / imported["candidate_id"] / "result.bin")
        candidate_file.write_bytes(png(b"\xff\x00\x00\xff"))
        resumed = self._resume_image_candidate(root=root, source=source, asset=asset)
        self.assertEqual(resumed["status"], "needs_review")
        self.assertIn("content-conflict", resumed["reason_code"])
        self.assertFalse((root / "images" / "core-image.png").exists())
        with WorkflowStore(self.state) as store:
            self.assertEqual(store.find_effect(self.job_id, "core-image", handoff.MANUAL_ACTION)["status"], "prepared")

    def test_invalid_png_candidate_stays_unvalidated_for_correction(self):
        root, source, asset, prepared = self._prepare_image()
        candidate_path = self.base / "not-a-png.png"
        candidate_path.write_bytes(b"\x89PNG\r\n\x1a\ninvalid image payload")
        cli.import_manual_result(
            state_dir=self.state, job_id=self.job_id, artifact_key="core-image",
            result_path=candidate_path, event_id="codex-image-import:invalid",
        )
        resumed = self._resume_image_candidate(root=root, source=source, asset=asset)
        self.assertEqual(resumed["status"], "needs_review")
        self.assertEqual(resumed["reason_code"], "manual-image-png-validation-failed")
        self.assertFalse((root / "images" / "core-image.png").exists())
        with WorkflowStore(self.state) as store:
            self.assertEqual(store.find_effect(self.job_id, "core-image", handoff.MANUAL_ACTION)["status"], "prepared")

if __name__ == "__main__":
    unittest.main()
