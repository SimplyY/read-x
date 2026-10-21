#!/usr/bin/env python3
"""Durable Read-X adapter for the existing Document Compiler XML bridge.

Filesystem work stays private to the Read-X job. The only network write is
dc publish; uncertain outcomes are handled with read-only dc recover.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Callable


DOCUMENT_COMPILER_ROOT = Path("/Users/yuwei/code/document-compiler")
DEFAULT_PROFILE_ROOT = Path("/Users/yuwei/.lark-channel/profiles")
_PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}\Z")
_DOC_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")
_SHA_RE = re.compile(r"[a-f0-9]{64}\Z")


class DocumentCompilerError(RuntimeError):
    pass


class DocumentCompilerUnknown(DocumentCompilerError):
    pass


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_regular(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise DocumentCompilerError(f"required private file is missing or unsafe: {path.name}")
    return path.read_bytes()


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise DocumentCompilerError("private Document Compiler path must not traverse symlinks")
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=".readx-dc-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def profile_environment(profile: str, *, profile_root: Path = DEFAULT_PROFILE_ROOT,
                        base_environment: dict | None = None) -> dict:
    """Bind the worker to the Feishu profile recorded in trusted admission data."""
    if not isinstance(profile, str) or not _PROFILE_RE.fullmatch(profile):
        raise DocumentCompilerError("trusted Feishu profile is missing or invalid")
    profile_root = Path(profile_root).absolute()
    if profile_root.is_symlink():
        raise DocumentCompilerError("Feishu profile root must not be a symlink")
    profile_dir = profile_root / profile
    cli_config = profile_dir / "lark-cli"
    source_config = profile_dir / "lark-cli-source" / "config.json"
    if (profile_dir.is_symlink() or cli_config.is_symlink() or source_config.is_symlink()
            or not cli_config.is_dir() or not source_config.is_file()):
        raise DocumentCompilerError("recorded Feishu profile is unavailable")
    env = dict(os.environ if base_environment is None else base_environment)
    env.update({
        "LARK_CHANNEL": "1",
        "LARK_CHANNEL_HOME": str(profile_root.parent),
        "LARK_CHANNEL_PROFILE": profile,
        "LARKSUITE_CLI_CONFIG_DIR": str(cli_config),
        "LARK_CHANNEL_CONFIG": str(source_config),
        "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
        "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
    })
    return env


class DocumentCompilerAdapter:
    def __init__(self, *, compiler_root: Path = DOCUMENT_COMPILER_ROOT,
                 profile_root: Path = DEFAULT_PROFILE_ROOT,
                 runner: Callable = subprocess.run, timeout_seconds: float = 300):
        self.compiler_root = Path(compiler_root).absolute()
        self.profile_root = Path(profile_root).absolute()
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def _run(self, args: list[str], *, env: dict, remote_write: bool = False):
        try:
            return self.runner(
                [sys.executable, "-m", "compiler.cli", *args],
                cwd=self.compiler_root, env=env, capture_output=True, text=True,
                timeout=self.timeout_seconds, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            if remote_write:
                raise DocumentCompilerUnknown("Document Compiler publish timed out; outcome requires read-only recovery") from exc
            raise DocumentCompilerError("local Document Compiler preparation timed out") from exc
        except OSError as exc:
            raise DocumentCompilerError("Document Compiler process could not be started") from exc

    @staticmethod
    def _json_result(result, *, label: str) -> dict:
        try:
            payload = json.loads(getattr(result, "stdout", "") or "")
        except (ValueError, TypeError) as exc:
            raise DocumentCompilerError(f"Document Compiler {label} returned malformed JSON") from exc
        if not isinstance(payload, dict):
            raise DocumentCompilerError(f"Document Compiler {label} returned a non-object result")
        return payload

    def prepare_plan(self, *, job_id: str, document_dir: Path, candidate_xml: Path,
                     profile: str, identity: str = "bot") -> tuple[dict, dict]:
        env = profile_environment(profile, profile_root=self.profile_root)
        return self._prepare_plan(job_id=job_id, document_dir=Path(document_dir),
                                  candidate_xml=Path(candidate_xml), profile=profile,
                                  identity=identity, env=env), env

    def _prepare_plan(self, *, job_id: str, document_dir: Path, candidate_xml: Path,
                      profile: str, identity: str, env: dict) -> dict:
        if identity != "bot":
            raise DocumentCompilerError("long-read document publication requires the admitted bot identity")
        if not isinstance(profile, str) or not _PROFILE_RE.fullmatch(profile):
            raise DocumentCompilerError("trusted Feishu profile is missing or invalid")
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", job_id):
            raise DocumentCompilerError("job id is invalid")
        publish_dir = document_dir / "document-compiler"
        if publish_dir.is_symlink():
            raise DocumentCompilerError("Document Compiler plan directory must not be a symlink")
        publish_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(publish_dir, 0o700)
        packet_path = publish_dir / "packet.json"
        xml_path = publish_dir / "candidate.xml"
        review_path = publish_dir / "review.json"
        receipt_path = publish_dir / "plan.receipt.json"

        packet = {
            "schema": "ContentPacket/v1",
            "job_id": f"read-x-longread-{job_id}",
            "caller": "read-x/long-read",
            "business_key": f"read-x-longread-{job_id}",
            "operation": "create",
            "target": {},
            "identity": identity,
        }

        if receipt_path.exists() or receipt_path.is_symlink():
            receipt = json.loads(_read_regular(receipt_path).decode("utf-8"))
            for path, expected_key in ((packet_path, "packet_sha256"), (xml_path, "xml_sha256"),
                                       (review_path, "review_sha256")):
                if _sha(_read_regular(path)) != receipt.get(expected_key):
                    raise DocumentCompilerError("persisted Document Compiler plan failed its hash receipt")
            if (receipt.get("schema") != "read-x-document-compiler-plan/v1"
                    or receipt.get("job_id") != job_id
                    or receipt.get("business_key") != packet["business_key"]
                    or receipt.get("profile") != profile or receipt.get("identity") != identity
                    or not _SHA_RE.fullmatch(str(receipt.get("request_sha256") or ""))):
                raise DocumentCompilerError("persisted Document Compiler plan identity changed")
            return {"directory": publish_dir, "packet": packet_path, "xml": xml_path,
                    "review": review_path, "receipt": receipt,
                    "request_sha256": receipt["request_sha256"]}

        if any(path.exists() or path.is_symlink() for path in (packet_path, xml_path, review_path)):
            raise DocumentCompilerError("incomplete pre-existing Document Compiler plan requires review")
        original_xml = _read_regular(Path(candidate_xml))
        _write_private(packet_path, _canonical(packet) + b"\n")
        _write_private(xml_path, original_xml)
        tableplan = self._run(["tableplan", "--xml", str(xml_path), "--out", str(xml_path), "--json"], env=env)
        if getattr(tableplan, "returncode", 1) != 0:
            raise DocumentCompilerError("Document Compiler tableplan blocked the candidate")
        tableplan_result = self._json_result(tableplan, label="tableplan")
        if tableplan_result.get("status") == "blocked":
            raise DocumentCompilerError("Document Compiler tableplan blocked the candidate")

        fingerprint_run = self._run(["fingerprint", "--packet", str(packet_path), "--xml", str(xml_path)], env=env)
        if getattr(fingerprint_run, "returncode", 1) != 0:
            raise DocumentCompilerError("Document Compiler could not fingerprint the candidate")
        fingerprint = self._json_result(fingerprint_run, label="fingerprint")
        packet_bytes, xml_bytes = _read_regular(packet_path), _read_regular(xml_path)
        packet_sha, xml_sha = _sha(packet_bytes), _sha(xml_bytes)
        if (not _SHA_RE.fullmatch(str(fingerprint.get("fingerprint") or ""))
                or fingerprint.get("packet_sha256") != packet_sha
                or fingerprint.get("xml_sha256") != xml_sha
                or not str(fingerprint.get("release_id") or "")):
            raise DocumentCompilerError("Document Compiler fingerprint does not bind the exact packet and XML")
        review = {
            "status": "PASS",
            "author": "read-x long-read (validated XML bridge candidate)",
            "release_id": fingerprint["release_id"],
            "fingerprint": fingerprint["fingerprint"],
            "xml_sha256": xml_sha,
            "packet_sha256": packet_sha,
            "confirmation": {
                "source": "read-x-validate-output",
                "confirmed_by": "Read-X long-read candidate validation passed",
                "sha256": xml_sha,
                "run_id": job_id,
            },
        }
        review_bytes = _canonical(review) + b"\n"
        _write_private(review_path, review_bytes)
        request = {
            "packet_sha256": packet_sha, "xml_sha256": xml_sha,
            "review_sha256": _sha(review_bytes), "profile": profile,
            "identity": identity, "compiler_root": str(self.compiler_root),
        }
        request_sha = _sha(_canonical(request))
        receipt = {
            "schema": "read-x-document-compiler-plan/v1", "job_id": job_id,
            "business_key": packet["business_key"], "profile": profile, "identity": identity,
            "packet_sha256": packet_sha, "xml_sha256": xml_sha,
            "review_sha256": _sha(review_bytes), "fingerprint": fingerprint["fingerprint"],
            "release_id": fingerprint["release_id"], "request_sha256": request_sha,
        }
        _write_private(receipt_path, _canonical(receipt) + b"\n")
        return {"directory": publish_dir, "packet": packet_path, "xml": xml_path,
                "review": review_path, "receipt": receipt, "request_sha256": request_sha}

    def publish(self, *, plan: dict, state_dir: Path, env: dict) -> dict:
        cmd = ["publish", "--packet", str(plan["packet"]), "--xml", str(plan["xml"]),
               "--review", str(plan["review"]), "--state-dir", str(Path(state_dir).absolute()), "--json"]
        result = self._run(cmd, env=env, remote_write=True)
        code = getattr(result, "returncode", -1)
        try:
            payload = self._json_result(result, label="publish")
        except DocumentCompilerError as exc:
            raise DocumentCompilerUnknown("Document Compiler publish result is unreadable; outcome requires read-only recovery") from exc
        status = payload.get("status")
        readback = payload.get("readback")
        doc = payload.get("doc")
        if (code == 0 and status == "published" and isinstance(readback, dict)
                and readback.get("matched") is True and isinstance(doc, str) and _DOC_ID_RE.fullmatch(doc)):
            return {"status": "confirmed", "remote_id": doc,
                    "metadata": {"verification": "document-compiler-readback-matched",
                                 "content_sha256": plan["receipt"]["xml_sha256"]},
                    "plan": plan["receipt"]}
        if code == 1 and status in {"blocked", "pending"}:
            return {"status": "failed", "reason_code": "document-compiler-blocked",
                    "plan": plan["receipt"]}
        if code == 0 and status == "dry_run":
            return {"status": "failed", "reason_code": "document-compiler-dry-run",
                    "plan": plan["receipt"]}
        if code == 3 or status == "unknown":
            return {"status": "uncertain", "reason_code": "document-compiler-publish-unknown",
                    "plan": plan["receipt"]}
        raise DocumentCompilerUnknown("Document Compiler publish response does not confirm a safe terminal outcome")

    def recover(self, *, plan: dict, state_dir: Path, env: dict) -> dict:
        cmd = ["recover", "--packet", str(plan["packet"]), "--state-dir", str(Path(state_dir).absolute()), "--json"]
        try:
            result = self._run(cmd, env=env, remote_write=False)
            payload = self._json_result(result, label="recover")
        except (DocumentCompilerError, DocumentCompilerUnknown):
            return {"status": "uncertain", "reason_code": "document-compiler-recovery-unreadable",
                    "plan": plan["receipt"]}
        doc = payload.get("doc")
        if (payload.get("status") == "published" and payload.get("matched") is True
                and payload.get("read_only") is True and isinstance(doc, str)
                and _DOC_ID_RE.fullmatch(doc)):
            return {"status": "confirmed", "remote_id": doc,
                    "metadata": {"verification": "document-compiler-readonly-recovery-matched",
                                 "content_sha256": plan["receipt"]["xml_sha256"]},
                    "plan": plan["receipt"], "recovered": True}
        return {"status": "uncertain", "reason_code": "document-compiler-recovery-unconfirmed",
                "plan": plan["receipt"], "recovery_status": str(payload.get("status") or "unknown")}
