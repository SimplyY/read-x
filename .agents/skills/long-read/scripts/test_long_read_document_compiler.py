#!/usr/bin/env python3
"""Document Compiler adapter contract tests; all CLI effects are faked."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from long_read_document_compiler import (
    DocumentCompilerAdapter, DocumentCompilerError, DocumentCompilerUnknown,
    profile_environment,
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.publish_result = (0, {
            "status": "published", "doc": "doc_test_123",
            "readback": {"matched": True},
        })
        self.recover_result = (0, {
            "status": "published", "doc": "doc_test_123",
            "matched": True, "read_only": True,
        })

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        command = argv[3]
        if command == "tableplan":
            return SimpleNamespace(returncode=0, stdout='{"status":"ready"}', stderr="")
        if command == "fingerprint":
            packet = Path(argv[argv.index("--packet") + 1]).read_bytes()
            xml = Path(argv[argv.index("--xml") + 1]).read_bytes()
            value = {
                "fingerprint": "f" * 64, "packet_sha256": sha(packet),
                "xml_sha256": sha(xml), "release_id": "release-test",
            }
            return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="")
        if command == "publish":
            code, payload = self.publish_result
            return SimpleNamespace(returncode=code, stdout=json.dumps(payload), stderr="")
        if command == "recover":
            code, payload = self.recover_result
            return SimpleNamespace(returncode=code, stdout=json.dumps(payload), stderr="")
        raise AssertionError(f"unexpected command {command}")


class DocumentCompilerAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.profile_root = self.root / "profiles"
        profile_dir = self.profile_root / "deep"
        (profile_dir / "lark-cli").mkdir(parents=True)
        source = profile_dir / "lark-cli-source" / "config.json"
        source.parent.mkdir()
        source.write_text("{}", encoding="utf-8")
        self.document_dir = self.root / "job" / "document"
        self.document_dir.mkdir(parents=True)
        self.candidate = self.document_dir / "main-doc.wx_doc.xml"
        self.candidate.write_text("<title>Read-X</title><heading>Core</heading>", encoding="utf-8")
        self.runner = FakeRunner()
        self.adapter = DocumentCompilerAdapter(
            compiler_root=self.root / "compiler", profile_root=self.profile_root,
            runner=self.runner,
        )
        self.job_id = "12345678-1234-4234-8234-123456789abc"
        self.state_dir = self.root / "dc-state"

    def tearDown(self):
        self.temporary.cleanup()

    def plan(self):
        return self.adapter.prepare_plan(
            job_id=self.job_id, document_dir=self.document_dir,
            candidate_xml=self.candidate, profile="deep", identity="bot",
        )

    def test_profile_is_explicit_and_invalid_or_missing_profile_fails_closed(self):
        env = profile_environment("deep", profile_root=self.profile_root, base_environment={})
        self.assertEqual(env["LARK_CHANNEL_PROFILE"], "deep")
        self.assertEqual(env["LARKSUITE_CLI_CONFIG_DIR"], str(self.profile_root / "deep" / "lark-cli"))
        self.assertEqual(env["LARK_CHANNEL_CONFIG"], str(self.profile_root / "deep" / "lark-cli-source" / "config.json"))
        with self.assertRaises(DocumentCompilerError):
            profile_environment("../deep", profile_root=self.profile_root, base_environment={})
        with self.assertRaises(DocumentCompilerError):
            self.adapter.prepare_plan(
                job_id=self.job_id, document_dir=self.document_dir,
                candidate_xml=self.candidate, profile="deep", identity="user",
            )

    def test_publish_plan_binds_candidate_packet_profile_and_review(self):
        plan, env = self.plan()
        self.assertEqual(plan["receipt"]["business_key"], f"read-x-longread-{self.job_id}")
        self.assertEqual(plan["receipt"]["profile"], "deep")
        self.assertEqual(plan["receipt"]["identity"], "bot")
        packet = json.loads(plan["packet"].read_text(encoding="utf-8"))
        review = json.loads(plan["review"].read_text(encoding="utf-8"))
        self.assertEqual(packet["operation"], "create")
        self.assertEqual(packet["target"], {})
        self.assertEqual(packet["identity"], "bot")
        self.assertEqual(review["status"], "PASS")
        self.assertEqual(review["fingerprint"], "f" * 64)
        self.assertEqual(review["xml_sha256"], sha(plan["xml"].read_bytes()))
        self.assertEqual(plan["directory"].stat().st_mode & 0o777, 0o700)
        self.assertEqual(plan["packet"].stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(self.runner.calls), 2)
        self.assertTrue(all(call[1]["env"]["LARK_CHANNEL_PROFILE"] == "deep" for call in self.runner.calls))

    def test_verified_publish_requires_readback_match_and_document_id(self):
        plan, env = self.plan()
        result = self.adapter.publish(plan=plan, state_dir=self.state_dir, env=env)
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["remote_id"], "doc_test_123")
        self.assertIn("--state-dir", self.runner.calls[-1][0])

        self.runner.publish_result = (0, {
            "status": "published", "doc": "doc_test_123", "readback": {"matched": False},
        })
        with self.assertRaises(DocumentCompilerUnknown):
            self.adapter.publish(plan=plan, state_dir=self.state_dir, env=env)

    def test_unknown_publish_is_not_reported_as_failure_or_retried(self):
        plan, env = self.plan()
        self.runner.publish_result = (3, {"status": "unknown", "doc": None})
        result = self.adapter.publish(plan=plan, state_dir=self.state_dir, env=env)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(sum(call[0][3] == "publish" for call in self.runner.calls), 1)

    def test_read_only_recovery_requires_explicit_match(self):
        plan, env = self.plan()
        result = self.adapter.recover(plan=plan, state_dir=self.state_dir, env=env)
        self.assertEqual(result["status"], "confirmed")
        self.assertTrue(result["recovered"])
        self.runner.recover_result = (3, {
            "status": "published", "doc": "doc_test_123",
            "matched": True, "read_only": False,
        })
        uncertain = self.adapter.recover(plan=plan, state_dir=self.state_dir, env=env)
        self.assertEqual(uncertain["status"], "uncertain")
        self.assertEqual(sum(call[0][3] == "recover" for call in self.runner.calls), 2)

    def test_changed_plan_receipt_fails_closed_and_never_republishes(self):
        plan, _ = self.plan()
        plan["xml"].write_text("<title>changed</title>", encoding="utf-8")
        with self.assertRaises(DocumentCompilerError):
            self.plan()


if __name__ == "__main__":
    unittest.main()
