#!/usr/bin/env python3
"""Durable long-read content worker. Delivery stages remain separate and gated."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import uuid
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from chatgpt_bridge import WORKFLOW_NEW_CHAT_URL, observe_bridge, run_bridge, verified_text
from long_read_workflow_store import (
    Lease, WorkflowStore, workflow_payload_issue,
)
from long_read_document_compiler import (
    DocumentCompilerAdapter, DocumentCompilerError, DocumentCompilerUnknown,
)
import run_chatgpt_munger as munger
import run_isolated_analyses as isolated
import long_read_document_supplement as document_supplement
import long_read_document_assembler as document_assembler
import long_read_workflow_notifications as workflow_notifications


CALLER = "read-x:long-read-text"
MAX_BRANCHES = 3
LONG_EXTERNAL_WAIT_LEASE_SECONDS = 900
DOCUMENT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")
SAFE_PRE_SUBMIT_FAILURES = {
    "bridge-busy", "local-rate-limit-cooldown", "recovery-capability-unavailable",
    "invalid-bridge-transport", "invalid-mode", "invalid-request-id",
}


def _renew_for_external_wait(store: WorkflowStore, lease: Lease,
                             lock: threading.RLock | None = None) -> None:
    """Keep the job fenced while one bounded external wait is in flight."""
    remaining = max(0.0, lease.expires_at - float(store.clock()))
    ttl_seconds = max(float(LONG_EXTERNAL_WAIT_LEASE_SECONDS), remaining)
    if lock is None:
        store.renew_lease(lease, ttl_seconds)
    else:
        with lock:
            store.renew_lease(lease, ttl_seconds)


def _safe(path: Path) -> None:
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() and part not in {Path("/tmp"), Path("/var")}:
            raise ValueError(f"workflow artifact path must not traverse symlinks: {path}")


def _read_bytes(path: Path) -> bytes:
    _safe(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    return path.read_bytes()


def _atomic_bytes(path: Path, payload: bytes) -> None:
    _safe(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _artifact_root(state_dir: Path, job_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f-]{36}", job_id):
        raise ValueError("invalid workflow job id")
    root = state_dir / "jobs" / job_id
    _safe(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    return root


def _load_job_inputs(job: dict) -> tuple[dict, Path, str, str]:
    payload = job["payload"]
    snapshot = Path(payload["inputs"]["source.md"]["path"]).parent
    _safe(snapshot)
    manifest_raw = _read_bytes(snapshot / "manifest.json")
    snapshot_digest = hashlib.sha256(manifest_raw).hexdigest()
    if payload.get("snapshot_sha256") != snapshot_digest or snapshot.name != snapshot_digest:
        raise ValueError("private input snapshot manifest identity mismatch")
    manifest = json.loads(manifest_raw.decode("utf-8"))
    if {path.name for path in snapshot.iterdir()} != {*payload["inputs"], "manifest.json"}:
        raise ValueError("private input snapshot file set changed")
    expected_manifest = {}
    for name, reference in payload["inputs"].items():
        expected_path = (snapshot / name).absolute()
        if Path(reference["path"]).absolute() != expected_path:
            raise ValueError(f"private input path was rebound: {name}")
        raw = _read_bytes(expected_path)
        expected_manifest[name] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    if manifest != expected_manifest:
        raise ValueError("private input snapshot manifest content mismatch")
    source_raw = _read_bytes(snapshot / "source.md")
    source_digest = hashlib.sha256(source_raw).hexdigest()
    for name, reference in payload["inputs"].items():
        raw = _read_bytes(Path(reference["path"]))
        if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
            raise ValueError(f"private input snapshot changed: {name}")
    source = source_raw.decode("utf-8")
    evidence = _read_bytes(snapshot / "evidence.json").decode("utf-8")
    isolated.validate_evidence(evidence, source)
    return payload, snapshot, source, source_digest


def _receipt_paths(output: Path) -> tuple[Path, Path]:
    return output, output.with_suffix(output.suffix + ".receipt.json")


def _request_metadata_path(output: Path) -> Path:
    return output.with_suffix(output.suffix + ".request.json")


def _read_local_receipt(output: Path, receipt_path: Path, request_id: str, prompt_sha: str, source_sha: str) -> tuple[str, dict] | None:
    try:
        body = _read_bytes(output).decode("utf-8")
        record = json.loads(_read_bytes(receipt_path).decode("utf-8"))
    except (FileNotFoundError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if (record.get("status") != "succeeded" or record.get("request_id") != request_id
            or record.get("caller_label") != CALLER
            or record.get("conversation_url_requested") != WORKFLOW_NEW_CHAT_URL
            or _conversation_id(record.get("conversation_url")) is None
            or record.get("prompt_sha256") != prompt_sha or record.get("source_sha256") != source_sha
            or record.get("output_sha256") != digest or record.get("submissionConfirmed") is not True
            or record.get("verification") != "live-dom+snapshot"):
        return None
    return body, record


def _conversation_id(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"chatgpt.com", "chat.openai.com"}:
        return None
    parts = parsed.path.rstrip("/").split("/")
    if len(parts) < 3 or parts[-2] != "c":
        return None
    value = parts[-1] if parts else ""
    return value if re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value) else None


def _load_or_store_request_metadata(output: Path, *, request_id: str, prompt_sha: str,
                                    source_sha: str, prompt_assets: list[dict],
                                    existing_effect: bool) -> list[dict] | None:
    path = _request_metadata_path(output)
    try:
        record = json.loads(_read_bytes(path).decode("utf-8"))
    except FileNotFoundError:
        if existing_effect:
            return None
        record = {
            "request_id": request_id, "caller_label": CALLER,
            "conversation_url_requested": WORKFLOW_NEW_CHAT_URL,
            "prompt_sha256": prompt_sha, "source_sha256": source_sha,
            "prompt_assets": prompt_assets,
        }
        _atomic_bytes(path, (json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (record.get("request_id") != request_id or record.get("caller_label") != CALLER
            or record.get("conversation_url_requested") != WORKFLOW_NEW_CHAT_URL
            or record.get("prompt_sha256") != prompt_sha or record.get("source_sha256") != source_sha
            or not isinstance(record.get("prompt_assets"), list)):
        return None
    return record["prompt_assets"]


def _store_receipt(output: Path, receipt_path: Path, body: str, *, request_id: str,
                   prompt_sha: str, source_sha: str, bridge_result: dict,
                   prompt_assets: list[dict] | None = None) -> dict:
    record = {
        "status": "succeeded", "request_id": request_id,
        "caller_label": CALLER, "conversation_url_requested": WORKFLOW_NEW_CHAT_URL,
        "conversation_url": bridge_result.get("conversationUrl"),
        "submissionConfirmed": bridge_result.get("submissionConfirmed"),
        "verification": bridge_result.get("verification"),
        "bridge_output_sha256": bridge_result.get("outputSha256"),
        "source_sha256": source_sha, "prompt_sha256": prompt_sha,
        "output_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "prompt_assets": prompt_assets or [],
        "recovery_receipt_status": bridge_result.get("recoveryReceiptStatus"),
        "recovered_from_receipt": bool(bridge_result.get("recoveredFromReceipt")),
    }
    _atomic_bytes(output, body.encode("utf-8"))
    _atomic_bytes(receipt_path, (json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    return record


def _write_document_candidate(*, store: WorkflowStore, job_id: str, root: Path,
                              input_sha: str, markdown: str, xml: str) -> dict:
    directory = root / "document"
    _safe(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    markdown_path = directory / "main-doc.md"
    xml_path = directory / "main-doc.wx_doc.xml"
    receipt_path = directory / "candidate.receipt.json"
    markdown_sha = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    xml_sha = hashlib.sha256(xml.encode("utf-8")).hexdigest()
    try:
        receipt = json.loads(_read_bytes(receipt_path).decode("utf-8"))
        existing_markdown = hashlib.sha256(_read_bytes(markdown_path)).hexdigest()
        existing_xml = hashlib.sha256(_read_bytes(xml_path)).hexdigest()
    except (FileNotFoundError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        receipt = None
        existing_markdown = existing_xml = None
    reusable = (
        isinstance(receipt, dict) and receipt.get("schema") == "long-read-document-candidate/v1"
        and receipt.get("input_sha256") == input_sha and receipt.get("markdown_sha256") == markdown_sha
        and receipt.get("xml_sha256") == xml_sha and existing_markdown == markdown_sha
        and existing_xml == xml_sha and receipt.get("validation") == "pass"
    )
    publish_effect = store.find_effect(job_id, "main-document", "document-publish")
    if not reusable and publish_effect is not None:
        raise ValueError("candidate inputs changed after document publish was prepared; read-only reconciliation required")
    if not reusable:
        _atomic_bytes(markdown_path, markdown.encode("utf-8"))
        _atomic_bytes(xml_path, xml.encode("utf-8"))
        receipt = {
            "schema": "long-read-document-candidate/v1", "input_sha256": input_sha,
            "markdown_sha256": markdown_sha, "xml_sha256": xml_sha,
            "validation": "pass", "created_at": time.time(),
        }
        _atomic_bytes(receipt_path, (json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    return {"status": "candidate_ready", "reused": reusable,
            "markdown_path": str(markdown_path), "xml_path": str(xml_path),
            "input_sha256": input_sha, "markdown_sha256": markdown_sha, "xml_sha256": xml_sha}


def _prepare_text_tasks(payload: dict, snapshot: Path, root: Path) -> dict:
    result = {}
    questions = [(item["skill"], snapshot / item["file"]) for item in payload.get("questions", [])]
    stage = root / "staging"
    stage.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in ["article-decode"] + [skill for skill, _ in questions]:
        task_stage = stage / name
        task_stage.mkdir(mode=0o700, parents=True, exist_ok=True)
        specs = [] if name == "article-decode" else [(name, next(path for skill, path in questions if skill == name))]
        tasks = isolated.prepare_tasks(specs, task_stage)
        task = next(item for item in tasks if item.name == name)
        result[name] = task
    return result


def _mark_artifact(store: WorkflowStore, lease: Lease, artifact_key: str, status: str,
                   reason: str | None = None, *, failure_stage: str = "bridge") -> None:
    code = re.sub(r"[^A-Za-z0-9_.:/+-]", "-", reason or "")[:120]
    metadata = {"failure_stage": failure_stage, "reason_code": code} if status == "failed" and code else {}
    store.set_artifact(lease, artifact_key, status, metadata=metadata)


def _run_text_branch(*, store: WorkflowStore, lease: Lease, lock: threading.RLock,
                     artifact_key: str, output: Path, source_sha: str, prompt: str,
                     validate, prompt_assets: list[dict] | None = None, prompt_sha: str | None = None,
                     bridge_fn=run_bridge, observe_fn=observe_bridge) -> dict:
    with lock:
        effect = store.find_effect(lease.job_id, artifact_key, "bridge-submit")
    prompt_sha = prompt_sha or (effect["request_sha256"] if effect else None)
    if effect is None and prompt is None:
        raise ValueError(f"new Bridge request requires a Prompt: {artifact_key}")
    if prompt_sha is None and prompt is not None:
        prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    if prompt_sha is None:
        raise ValueError(f"persisted Bridge request is missing its Prompt hash: {artifact_key}")
    if prompt is not None and hashlib.sha256(prompt.encode("utf-8")).hexdigest() != prompt_sha and effect:
        raise ValueError(f"persisted Prompt hash changed for {artifact_key}")
    initial_effect_status = effect["status"] if effect else None
    body = None
    bridge_result = None
    recovered = False
    execution_started = None
    if effect and effect["status"] == "confirmed":
        local = _read_local_receipt(*_receipt_paths(output), effect["request_id"], prompt_sha, source_sha)
        if local:
            body, receipt = local
            validate(body)
            with lock:
                _mark_artifact(store, lease, artifact_key, "confirmed")
            return {"artifact": artifact_key, "status": "confirmed", "reused": True,
                    "request_id": effect["request_id"], "output_sha256": receipt["output_sha256"]}
        _renew_for_external_wait(store, lease, lock)
        bridge_result = observe_fn(
            original_run_id=effect["request_id"], conversation_url=WORKFLOW_NEW_CHAT_URL,
            caller_label=CALLER, max_wait_seconds=720,
        )
        recovered = True
    elif effect and effect["status"] in {"attempting", "uncertain"}:
        _renew_for_external_wait(store, lease, lock)
        bridge_result = observe_fn(
            original_run_id=effect["request_id"], conversation_url=WORKFLOW_NEW_CHAT_URL,
            caller_label=CALLER, max_wait_seconds=720,
        )
        recovered = True
    elif effect and effect["status"] == "failed":
        return {"artifact": artifact_key, "status": "failed", "reason": "previous-known-failure",
                "request_id": effect["request_id"]}
    else:
        if effect is None:
            with lock:
                effect = store.prepare_effect(lease, artifact_key, "bridge-submit", prompt_sha)
        elif effect["request_sha256"] != prompt_sha:
            raise ValueError(f"persisted Prompt hash changed for {artifact_key}")
        prompt_assets = _load_or_store_request_metadata(
            output, request_id=effect["request_id"], prompt_sha=prompt_sha,
            source_sha=source_sha, prompt_assets=prompt_assets or [],
            existing_effect=initial_effect_status not in {None, "prepared"},
        )
        if prompt_assets is None:
            return {"artifact": artifact_key, "status": "needs_review", "reason": "request-metadata-unavailable",
                    "request_id": effect["request_id"]}
        _renew_for_external_wait(store, lease, lock)
        with lock:
            attempt = store.begin_effect(lease, effect["op_key"])
        if not attempt["execute"]:
            raise RuntimeError("prepared Bridge effect unexpectedly was not executable")
        execution_started = time.monotonic()
        try:
            bridge_result = bridge_fn(
                prompt, max_wait_seconds=360, caller_label=CALLER,
                request_id=effect["request_id"], conversation_url=WORKFLOW_NEW_CHAT_URL,
            )
        except Exception as exc:
            reason = f"{type(exc).__name__}".lower()
            with lock:
                store.finish_effect(lease, effect["op_key"], "uncertain",
                                    metadata={"reason_code": reason[:120], "failure_stage": "bridge",
                                              "duration_ms": round((time.monotonic() - execution_started) * 1000)})
                _mark_artifact(store, lease, artifact_key, "uncertain")
            return {"artifact": artifact_key, "status": "needs_review", "reason": reason,
                    "request_id": effect["request_id"]}

    if not isinstance(bridge_result, dict) or bridge_result.get("runId") != effect["request_id"]:
        if not recovered:
            with lock:
                store.finish_effect(lease, effect["op_key"], "uncertain", metadata={"reason_code": "bridge-run-id-mismatch", "failure_stage": "bridge"})
                _mark_artifact(store, lease, artifact_key, "uncertain")
        return {"artifact": artifact_key, "status": "needs_review", "reason": "bridge-run-id-mismatch",
                "request_id": effect["request_id"]}
    if bridge_result.get("status") != "succeeded":
        reason = re.sub(r"[^A-Za-z0-9_.:/+-]", "-", str(bridge_result.get("reason") or bridge_result.get("status") or "bridge-unknown"))[:120]
        if recovered:
            return {"artifact": artifact_key, "status": "needs_review", "reason": reason,
                    "request_id": effect["request_id"]}
        outcome = "failed" if reason in SAFE_PRE_SUBMIT_FAILURES or bridge_result.get("status") == "failed" else "uncertain"
        failure_metadata = {"reason_code": reason, "failure_stage": "bridge"}
        if execution_started is not None:
            failure_metadata["duration_ms"] = round((time.monotonic() - execution_started) * 1000)
        with lock:
            store.finish_effect(lease, effect["op_key"], outcome, metadata=failure_metadata)
            _mark_artifact(store, lease, artifact_key, "failed" if outcome == "failed" else "uncertain", reason)
        return {"artifact": artifact_key, "status": "failed" if outcome == "failed" else "needs_review",
                "reason": reason, "request_id": effect["request_id"]}
    try:
        body = validate(verified_text(bridge_result))
    except Exception as exc:
        reason = re.sub(r"[^A-Za-z0-9_.:/+-]", "-", f"{type(exc).__name__}")[:120]
        failure_metadata = {"verification": "live-dom+snapshot", "content_sha256": bridge_result.get("outputSha256"),
                            "failure_stage": "output-validation", "reason_code": "output-validation"}
        if execution_started is not None:
            failure_metadata["duration_ms"] = round((time.monotonic() - execution_started) * 1000)
        if recovered and initial_effect_status != "confirmed":
            with lock:
                store.reconcile_effect(lease, effect["op_key"], "confirmed", remote_id=_conversation_id(bridge_result.get("conversationUrl")),
                                       metadata=failure_metadata)
                _mark_artifact(store, lease, artifact_key, "failed", "output-validation", failure_stage="output-validation")
        elif not recovered:
            with lock:
                store.finish_effect(lease, effect["op_key"], "confirmed", remote_id=_conversation_id(bridge_result.get("conversationUrl")),
                                    metadata=failure_metadata)
                _mark_artifact(store, lease, artifact_key, "failed", "output-validation", failure_stage="output-validation")
        else:
            with lock:
                _mark_artifact(store, lease, artifact_key, "failed", "output-validation", failure_stage="output-validation")
        return {"artifact": artifact_key, "status": "failed", "reason": reason,
                "request_id": effect["request_id"], "failure_stage": "output-validation",
                "business_validation_status": "failed"}
    if recovered:
        recovered_assets = _load_or_store_request_metadata(
            output, request_id=effect["request_id"], prompt_sha=prompt_sha,
            source_sha=source_sha, prompt_assets=prompt_assets or [], existing_effect=True,
        )
        if recovered_assets is None:
            return {"artifact": artifact_key, "status": "needs_review", "reason": "request-metadata-unavailable",
                    "request_id": effect["request_id"]}
        prompt_assets = recovered_assets
    record = _store_receipt(output, _receipt_paths(output)[1], body, request_id=effect["request_id"],
                            prompt_sha=prompt_sha, source_sha=source_sha, bridge_result=bridge_result,
                            prompt_assets=prompt_assets)
    remote_id = _conversation_id(bridge_result.get("conversationUrl"))
    success_metadata = {"verification": "live-dom+snapshot", "content_sha256": record["output_sha256"]}
    if execution_started is not None:
        success_metadata["duration_ms"] = round((time.monotonic() - execution_started) * 1000)
    with lock:
        if recovered and initial_effect_status != "confirmed":
            store.reconcile_effect(lease, effect["op_key"], "confirmed", remote_id=remote_id,
                                   metadata=success_metadata)
        elif initial_effect_status == "confirmed":
            pass
        else:
            store.finish_effect(lease, effect["op_key"], "confirmed", remote_id=remote_id,
                                metadata=success_metadata)
        _mark_artifact(store, lease, artifact_key, "confirmed")
    return {"artifact": artifact_key, "status": "confirmed", "request_id": effect["request_id"],
            "output_sha256": record["output_sha256"], "recovered": recovered,
            "business_validation_status": "succeeded"}


def _record_read_x_text_outcome(outcome_recorder, result: dict, duration_ms: int) -> None:
    request_id = result.get("request_id")
    if outcome_recorder is None or not request_id:
        return
    branch_status = result.get("status")
    outcome_status = "succeeded" if branch_status == "confirmed" else "failed" if branch_status == "failed" else "needs_review"
    validation_status = result.get("business_validation_status")
    if validation_status not in {"succeeded", "failed", "unknown"}:
        if branch_status == "confirmed":
            validation_status = "succeeded"
        elif result.get("failure_stage") == "output-validation":
            validation_status = "failed"
        else:
            validation_status = "unknown"
    try:
        outcome_recorder({
            "source": "read-x", "caller": CALLER,
            "attemptId": request_id, "runId": request_id, "clientRequestId": request_id,
            "status": outcome_status, "deliveryStatus": outcome_status,
            "businessValidationStatus": validation_status,
            "readbackStatus": result.get("readback_status", "unknown"),
            "durationMs": max(0, duration_ms),
        })
    except Exception:
        # Outcome persistence is diagnostic-only and cannot change the durable worker result.
        diagnostic = {
            "event": "bridge-business-outcome-diagnostic-failed",
            "reason": "read-x-outcome-recorder-raised",
            "errorCode": "diagnostic-write-failed",
            "source": "read-x",
            "caller": CALLER,
        }
        try:
            uuid.UUID(request_id)
        except (ValueError, TypeError, AttributeError):
            pass
        else:
            diagnostic.update({"attemptId": request_id, "runId": request_id,
                               "clientRequestId": request_id})
        try:
            print(json.dumps(diagnostic, separators=(",", ":")), file=sys.stderr)
        except Exception:
            pass


def _publish_main_document(*, store: WorkflowStore, lease: Lease, payload: dict,
                           root: Path, candidate: dict, state_dir: Path,
                           adapter: DocumentCompilerAdapter,
                           manual_document_id: str | None = None) -> dict:
    job_id = lease.job_id
    trigger = payload.get("trigger") or {}
    # Document Compiler consumes the Bridge-managed Feishu profile directory.
    # larkCliProfile is only for direct lark-cli calls in Read-X adapters.
    profile = trigger.get("feishuProfile")
    identity = trigger.get("identity", "bot")
    try:
        _renew_for_external_wait(store, lease)
        plan, env = adapter.prepare_plan(
            job_id=job_id, document_dir=root / "document",
            candidate_xml=Path(candidate["xml_path"]), profile=profile, identity=identity,
        )
    except (DocumentCompilerError, KeyError, TypeError, ValueError) as exc:
        return {"status": "needs_review", "reason": f"{type(exc).__name__}: {exc}"}

    effect = store.find_effect(job_id, "main-document", "document-publish")
    if manual_document_id is not None:
        if not DOCUMENT_ID_RE.fullmatch(manual_document_id):
            return {"status": "needs_review", "reason": "manual_document_id_invalid"}
        if effect is None:
            return {"status": "needs_review", "reason": "manual_document_id_has_no_uncertain_publish"}
        if effect["status"] == "confirmed":
            if effect.get("remote_id") == manual_document_id:
                return {"status": "confirmed", "document_id": manual_document_id, "reused": True}
            return {"status": "needs_review", "reason": "manual_document_id_conflicts_with_confirmed_publish"}
        if effect["status"] not in {"attempting", "uncertain"}:
            return {"status": "needs_review", "reason": "manual_document_id_requires_uncertain_publish"}
    try:
        if effect is None:
            effect = store.prepare_effect(
                lease, "main-document", "document-publish", plan["request_sha256"],
            )
        elif effect["request_sha256"] != plan["request_sha256"]:
            return {"status": "needs_review", "reason": "persisted_document_publish_plan_changed"}
    except Exception as exc:
        return {"status": "needs_review", "reason": f"{type(exc).__name__}: {exc}"}

    if effect["status"] == "confirmed":
        return {"status": "confirmed", "document_id": effect.get("remote_id"), "reused": True}
    if effect["status"] == "failed":
        return {"status": "failed", "reason": "previous_known_document_publish_failure"}

    def reconcile_readback() -> dict:
        _renew_for_external_wait(store, lease)
        if manual_document_id is None:
            recovery = adapter.recover(plan=plan, state_dir=state_dir / "document-compiler-state", env=env)
        else:
            recovery = adapter.recover(plan=plan, state_dir=state_dir / "document-compiler-state",
                                       env=env, document_id=manual_document_id)
        if recovery.get("status") == "confirmed":
            store.reconcile_effect(
                lease, effect["op_key"], "confirmed",
                remote_id=recovery.get("remote_id"), metadata=recovery.get("metadata"),
            )
            store.set_artifact(
                lease, "main-document", "confirmed",
                metadata={"verification": "document-compiler-readonly-recovery-matched",
                          "content_sha256": plan["receipt"]["xml_sha256"]},
            )
            return {"status": "confirmed", "document_id": recovery.get("remote_id"),
                    "recovered": True}
        return {"status": "uncertain", "reason": recovery.get("reason_code", "document_publish_unconfirmed")}

    if effect["status"] == "attempting":
        store.finish_effect(
            lease, effect["op_key"], "uncertain",
            metadata={"failure_stage": "document-publish", "reason_code": "worker-interrupted"},
        )
        effect = store.get_effect(effect["op_key"])
        return reconcile_readback()
    if effect["status"] == "uncertain":
        return reconcile_readback()
    if effect["status"] != "prepared":
        return {"status": "needs_review", "reason": "unsupported_document_publish_effect_state"}

    effect = store.begin_effect(lease, effect["op_key"])
    try:
        _renew_for_external_wait(store, lease)
        outcome = adapter.publish(
            plan=plan, state_dir=state_dir / "document-compiler-state", env=env,
        )
    except DocumentCompilerUnknown:
        store.finish_effect(
            lease, effect["op_key"], "uncertain",
            metadata={"failure_stage": "document-publish", "reason_code": "publish-response-unknown"},
        )
        effect = store.get_effect(effect["op_key"])
        return reconcile_readback()
    except DocumentCompilerError:
        store.finish_effect(
            lease, effect["op_key"], "failed",
            metadata={"failure_stage": "document-publish", "reason_code": "compiler-process-unavailable"},
        )
        return {"status": "failed", "reason": "document_compiler_process_unavailable"}

    if outcome.get("status") == "confirmed":
        store.finish_effect(
            lease, effect["op_key"], "confirmed", remote_id=outcome.get("remote_id"),
            metadata=outcome.get("metadata"),
        )
        store.set_artifact(
            lease, "main-document", "confirmed",
            metadata={"verification": "document-compiler-readback-matched",
                      "content_sha256": plan["receipt"]["xml_sha256"]},
        )
        return {"status": "confirmed", "document_id": outcome.get("remote_id")}
    if outcome.get("status") == "failed":
        store.finish_effect(
            lease, effect["op_key"], "failed",
            metadata={"failure_stage": "document-publish",
                      "reason_code": outcome.get("reason_code", "document-compiler-blocked")},
        )
        return {"status": "failed", "reason": outcome.get("reason_code", "document-compiler-blocked")}
    if outcome.get("status") == "uncertain":
        store.finish_effect(
            lease, effect["op_key"], "uncertain",
            metadata={"failure_stage": "document-publish",
                      "reason_code": outcome.get("reason_code", "document-compiler-publish-unknown")},
        )
        effect = store.get_effect(effect["op_key"])
        return reconcile_readback()
    store.finish_effect(
        lease, effect["op_key"], "uncertain",
        metadata={"failure_stage": "document-publish", "reason_code": "invalid-publish-outcome"},
    )
    effect = store.get_effect(effect["op_key"])
    return reconcile_readback()


def _notify(*, store: WorkflowStore, lease: Lease, payload: dict, root: Path,
            event: str, notification_fn=None, **kwargs) -> dict:
    _renew_for_external_wait(store, lease)
    if event == "document_review":
        artifact_key = "document-publish-review-notification"
        artifact = next((item for item in store.artifacts(lease.job_id)
                         if item["artifact_key"] == artifact_key), None)
        if artifact is None:
            raise ValueError("document review notification artifact is missing")
        metadata = dict(artifact.get("metadata", {}))
        reason_code = metadata.get("notification_reason_code", metadata.get("reason_code"))
        if not isinstance(reason_code, str) or not reason_code:
            reason_code = str(kwargs.get("reason_code") or "document-publish-result-unconfirmed")
            metadata.update({"failure_stage": "document-publish", "reason_code": reason_code,
                             "notification_reason_code": reason_code})
            store.set_artifact(lease, artifact_key, artifact["status"], metadata=metadata)
        # Keep the original card body stable so a later read-only recovery can
        # match the same send effect after a lost response.
        kwargs["reason_code"] = reason_code
    if notification_fn is not None:
        return notification_fn(event=event, store=store, lease=lease,
                               payload=payload, root=root, **kwargs)
    if event == "document":
        return workflow_notifications.notify_document_progress(
            store=store, lease=lease, payload=payload, root=root, **kwargs,
        )
    if event == "document_review":
        return workflow_notifications.notify_document_publish_review(
            store=store, lease=lease, payload=payload, root=root, **kwargs,
        )
    if event == "content_review":
        return workflow_notifications.notify_content_review(
            store=store, lease=lease, payload=payload, root=root, **kwargs,
        )
    if event == "image_recovery":
        return workflow_notifications.notify_image_recovery(
            store=store, lease=lease, payload=payload, root=root, **kwargs,
        )
    if event == "final":
        return workflow_notifications.notify_final(
            store=store, lease=lease, payload=payload, root=root, **kwargs,
        )
    raise ValueError(f"unsupported notification event: {event}")


def _deliver_image(*, store: WorkflowStore, lease: Lease, payload: dict, root: Path,
                   image_key: str, image_name: str, source_path: Path,
                   document_id: str, image_index: int, image_delivery_fn=None,
                   clock=time.time) -> dict:
    _renew_for_external_wait(store, lease)
    if image_delivery_fn is not None:
        return image_delivery_fn(
            store=store, lease=lease, payload=payload, root=root,
            image_key=image_key, image_name=image_name, source_path=source_path,
            document_id=document_id, image_index=image_index, clock=clock,
        )
    from long_read_image_delivery import deliver_image
    return deliver_image(
        store=store, lease=lease, payload=payload, root=root,
        image_key=image_key, image_name=image_name, source_path=source_path,
        document_id=document_id, image_index=image_index, clock=clock,
    )


def _advance_claimed_job(store: WorkflowStore, lease: Lease, state_dir: Path, *, bridge_fn, observe_fn,
                         prompt_fetcher, document_compiler=None, image_delivery_fn=None,
                         notification_fn=None, clock=time.time,
                         manual_document_id: str | None = None, outcome_recorder=None) -> dict:
    job_id = lease.job_id
    root = _artifact_root(state_dir, job_id)
    lock = threading.RLock()
    job = store.get_job(job_id)
    payload, snapshot, source, source_sha = _load_job_inputs(job)
    evidence = _read_bytes(snapshot / "evidence.json").decode("utf-8")
    store.set_status(lease, "running")
    task_map = _prepare_text_tasks(payload, snapshot, root)
    branches = []
    for name, task in task_map.items():
        output = root / "analysis" / f"{name}.md"
        effect = store.find_effect(job_id, f"analysis:{name}", "bridge-submit")
        prompt = isolated.build_task_prompt(task, source, evidence) if effect is None or effect["status"] == "prepared" else None
        branches.append({"name": name, "artifact_key": f"analysis:{name}", "output": output,
                         "prompt": prompt, "validate": lambda text, task=task: isolated.validate_task_text(task, text),
                         "prompt_sha": effect["request_sha256"] if effect and prompt is None else None,
                         "prompt_assets": []})
    munger_output = root / "analysis" / "munger-analysis.md"
    munger_effect = store.find_effect(job_id, "analysis:munger-analysis", "bridge-submit")
    munger_prompt_error = None
    if munger_effect and munger_effect["status"] != "prepared":
        # Recovery observes the exact prior request without fetching or resending live Prompt assets.
        munger_prompt = None
        assets = []
    else:
        try:
            asset = (prompt_fetcher or munger.fetch_prompt_asset)("read-x.munger-analysis")
            munger_prompt = munger.build_prompt(source, [asset])
            assets = [{key: asset.get(key) for key in ("prompt_id", "prompt_source", "prompt_revision", "prompt_sha256", "prompt_fetched_at")}]
        except Exception as exc:
            # A governance read failure is local to this required branch. Other
            # independent analysis branches still run and remain recoverable.
            munger_prompt = None
            assets = []
            munger_prompt_error = re.sub(r"[^A-Za-z0-9_.:/+-]", "-", type(exc).__name__)[:120]
            with lock:
                _mark_artifact(store, lease, "analysis:munger-analysis", "failed",
                               "prompt-governance-unavailable", failure_stage="prompt-governance")
    if munger_prompt_error:
        # A prepared request has not crossed the external-call boundary. Keep
        # it reusable: a later run can fetch the governed prompt and submit
        # this exact preallocated request identity safely.
        branches.append({"name": "munger-analysis", "artifact_key": "analysis:munger-analysis",
                         "terminal": True, "status": "failed", "reason": "prompt-governance-unavailable",
                         "error_type": munger_prompt_error})
    elif munger_effect:
        prompt_sha = munger_effect["request_sha256"]
        local = _read_local_receipt(*_receipt_paths(munger_output), munger_effect["request_id"], prompt_sha, source_sha)
        if local:
            body, receipt = local
            bridge_text = body.rstrip("\n")
            munger._validate_text({"status": "succeeded", "format": "markdown", "verification": "live-dom+snapshot",
                                  "submissionConfirmed": True, "actualConversationUrl": receipt.get("conversation_url"),
                                  "conversationUrl": receipt.get("conversation_url"), "text": bridge_text,
                                  "outputSha256": hashlib.sha256(bridge_text.encode("utf-8")).hexdigest()})
            _mark_artifact(store, lease, "analysis:munger-analysis", "confirmed")
            branches.append({"name": "munger-analysis", "artifact_key": "analysis:munger-analysis",
                             "reuse": True, "output": munger_output, "status": "confirmed",
                             "request_id": munger_effect["request_id"]})
        else:
            branches.append({"name": "munger-analysis", "artifact_key": "analysis:munger-analysis",
                             "output": munger_output,
                             "prompt": munger_prompt if munger_effect["status"] == "prepared" else None,
                             "prompt_sha": prompt_sha,
                             "validate": lambda text: text + "\n", "prompt_assets": assets})
    else:
        branches.append({"name": "munger-analysis", "artifact_key": "analysis:munger-analysis",
                         "output": munger_output, "prompt": munger_prompt,
                         "prompt_sha": None, "validate": lambda text: text + "\n",
                         "prompt_assets": assets})

    started = time.monotonic()
    outcomes = []

    def invoke(branch):
        if branch.get("terminal"):
            return branch
        started = time.monotonic()
        if branch.get("reuse"):
            result = branch
        else:
            result = _run_text_branch(
                store=store, lease=lease, lock=lock, artifact_key=branch["artifact_key"],
                output=branch["output"], source_sha=source_sha, prompt=branch["prompt"],
                validate=branch["validate"], prompt_assets=branch.get("prompt_assets"),
                prompt_sha=branch.get("prompt_sha"),
                bridge_fn=bridge_fn, observe_fn=observe_fn,
            )
        _record_read_x_text_outcome(outcome_recorder, result,
                                    round((time.monotonic() - started) * 1000))
        return {"name": branch["name"], **result}

    with ThreadPoolExecutor(max_workers=min(MAX_BRANCHES, max(1, len(branches)))) as pool:
        future_map = {pool.submit(invoke, branch): branch["name"] for branch in branches}
        for future in as_completed(future_map):
            try:
                outcomes.append(future.result())
            except Exception as exc:
                outcomes.append({"name": future_map[future], "status": "needs_review",
                                 "reason": f"{type(exc).__name__}: {exc}"})
    outcomes.sort(key=lambda item: item["name"])
    unresolved = any(item["status"] == "needs_review" for item in outcomes)
    unresolved |= bool(store.connection.execute(
        "SELECT 1 FROM effects WHERE job_id=? AND artifact_key LIKE 'analysis:%' "
        "AND status IN ('attempting','uncertain') LIMIT 1", (job_id,)
    ).fetchone())

    # This small, separately checkpointed Bridge request only fills the two
    # document fragments that are not available as structured source data.
    # It consumes verified article-decode and successful text-ljg artifacts;
    # it never rewrites or replaces their originals.
    current_artifacts = {item["artifact_key"]: item for item in store.artifacts(job_id)}
    article_status = current_artifacts.get("analysis:article-decode", {}).get("status")
    ljg_statuses = [current_artifacts.get(f"analysis:{name}", {}).get("status")
                    for name in task_map if name != "article-decode"]
    text_inputs_ready = article_status == "confirmed" and all(status in {"confirmed", "failed"} for status in ljg_statuses)
    analysis_texts = {}
    supplement_outcome = None
    if text_inputs_ready:
        for name, task in task_map.items():
            key = f"analysis:{name}"
            artifact_status = current_artifacts.get(key, {}).get("status")
            if artifact_status == "failed" and name != "article-decode":
                continue
            effect = store.find_effect(job_id, key, "bridge-submit")
            if effect is None or effect["status"] != "confirmed":
                text_inputs_ready = False
                break
            output = root / "analysis" / f"{name}.md"
            local = _read_local_receipt(*_receipt_paths(output), effect["request_id"],
                                        effect["request_sha256"], source_sha)
            if local is None:
                text_inputs_ready = False
                break
            body, _ = local
            isolated.validate_task_text(task, body.rstrip("\n"))
            analysis_texts[name] = body
    if text_inputs_ready:
        supplement_prompt, supplement_input_sha = document_supplement.build_prompt(
            source=source, evidence=json.loads(evidence), analyses=analysis_texts,
        )
        supplement_effect = store.find_effect(job_id, "analysis:document-supplement", "bridge-submit")
        supplement_output = root / "analysis" / "document-supplement.md"
        if supplement_effect and supplement_effect["status"] != "prepared":
            supplement_prompt = None
        supplement_started = time.monotonic()
        supplement_outcome = _run_text_branch(
            store=store, lease=lease, lock=lock,
            artifact_key="analysis:document-supplement", output=supplement_output,
            source_sha=supplement_input_sha, prompt=supplement_prompt,
            prompt_sha=supplement_effect["request_sha256"] if supplement_effect and supplement_prompt is None else None,
            validate=document_supplement.validate_supplement,
            bridge_fn=bridge_fn, observe_fn=observe_fn,
        )
        _record_read_x_text_outcome(outcome_recorder, supplement_outcome,
                                    round((time.monotonic() - supplement_started) * 1000))
        outcomes.append({"name": "document-supplement", **supplement_outcome})
    else:
        article_status = current_artifacts.get("analysis:article-decode", {}).get("status")
        if article_status == "failed":
            _mark_artifact(store, lease, "analysis:document-supplement", "failed",
                           "required-article-decode-failed", failure_stage="assembly")
            outcomes.append({"name": "document-supplement", "artifact": "analysis:document-supplement",
                             "status": "failed", "reason": "required-article-decode-failed"})
        else:
            outcomes.append({"name": "document-supplement", "artifact": "analysis:document-supplement",
                             "status": "needs_review", "reason": "required-text-inputs-not-confirmed"})
            unresolved = True

    artifacts = store.artifacts(job_id)
    unresolved |= any(item["status"] == "needs_review" for item in outcomes)
    retryable_governance_failure = any(
        item["artifact_key"].startswith("analysis:")
        and item["status"] == "failed"
        and item.get("metadata", {}).get("failure_stage") == "prompt-governance"
        for item in artifacts
    )

    candidate_result = None
    statuses = {item["artifact_key"]: item for item in artifacts}
    content_states_resolved = all(
        statuses.get(f"analysis:{name}", {}).get("status") in {"confirmed", "failed"}
        for name in task_map
    )
    munger_state = statuses.get("analysis:munger-analysis", {}).get("status")
    munger_resolved = munger_state in {"confirmed", "failed"} and not retryable_governance_failure
    supplement_state = statuses.get("analysis:document-supplement", {}).get("status")
    unresolved_effects = store.connection.execute(
        "SELECT 1 FROM effects WHERE job_id=? AND artifact_key LIKE 'analysis:%' "
        "AND status IN ('attempting','uncertain') LIMIT 1", (job_id,)
    ).fetchone()
    if (article_status == "confirmed" and supplement_state == "confirmed" and content_states_resolved
            and munger_resolved and not unresolved_effects):
        article_decode = analysis_texts.get("article-decode")
        supplement_effect = store.find_effect(job_id, "analysis:document-supplement", "bridge-submit")
        if article_decode is None or supplement_effect is None or supplement_effect["status"] != "confirmed":
            raise ValueError("verified article or document-supplement receipt is missing")
        supplement_input_sha = document_supplement.input_sha256(
            source=source, evidence=json.loads(evidence),
            analyses={name: body for name, body in analysis_texts.items()},
        )
        supplement_local = _read_local_receipt(
            *_receipt_paths(root / "analysis" / "document-supplement.md"),
            supplement_effect["request_id"], supplement_effect["request_sha256"], supplement_input_sha,
        )
        if supplement_local is None:
            raise ValueError("verified document-supplement local receipt is missing")
        supplement_text, supplement_receipt = supplement_local
        document_supplement.validate_supplement(supplement_text)
        scoring_raw = _read_bytes(snapshot / "scoring-result.json")
        scoring = json.loads(scoring_raw.decode("utf-8"))
        evidence_value = json.loads(evidence)
        successful_ljgs = [(name, body) for name, body in analysis_texts.items() if name != "article-decode"]

        munger_text = None
        munger_receipt = None
        munger_effect = store.find_effect(job_id, "analysis:munger-analysis", "bridge-submit")
        if munger_state == "confirmed":
            if munger_effect is None or munger_effect["status"] != "confirmed":
                raise ValueError("Munger branch confirmation has no matching Bridge effect")
            munger_local = _read_local_receipt(
                *_receipt_paths(root / "analysis" / "munger-analysis.md"),
                munger_effect["request_id"], munger_effect["request_sha256"], source_sha,
            )
            if munger_local is None:
                raise ValueError("confirmed Munger branch has no matching local receipt")
            munger_text, munger_receipt = munger_local

        assembled = document_assembler.assemble_markdown(
            evidence=evidence_value, scoring=scoring, article_decode=article_decode,
            text_analyses=successful_ljgs, supplement_markdown=supplement_text,
            source=source, munger_markdown=munger_text,
        )
        original_source_url = evidence_value["metadata"]["source_url"]
        conversation_url = munger_receipt.get("conversation_url", "") if munger_receipt else ""
        xml = document_assembler.render_candidate(
            assembled, title=evidence_value["metadata"]["title"],
            source_url=original_source_url, conversation_url=conversation_url,
        )
        candidate_inputs = {
            "snapshot_sha256": payload["snapshot_sha256"],
            "article_decode_sha256": hashlib.sha256(article_decode.encode("utf-8")).hexdigest(),
            "document_supplement_sha256": supplement_receipt["output_sha256"],
            "successful_ljg_sha256": {name: hashlib.sha256(body.encode("utf-8")).hexdigest()
                                       for name, body in successful_ljgs},
            "munger_sha256": hashlib.sha256(munger_text.encode("utf-8")).hexdigest() if munger_text is not None else None,
            "scoring_sha256": hashlib.sha256(scoring_raw).hexdigest(),
        }
        candidate_input_sha = hashlib.sha256(
            json.dumps(candidate_inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        candidate_result = _write_document_candidate(
            store=store, job_id=job_id, root=root, input_sha=candidate_input_sha,
            markdown=assembled, xml=xml,
        )

    content_plan_ready = all(
        item["status"] in {"confirmed", "failed"}
        for item in artifacts if item["kind"] == "analysis" and item["required"]
    )
    if (not unresolved and not retryable_governance_failure and content_plan_ready
            and not payload.get("artifact_plan_finalized")):
        required = {item["artifact_key"] for item in artifacts if item["required"]}
        munger_artifact = next((item for item in artifacts if item["artifact_key"] == "analysis:munger-analysis"), None)
        if munger_artifact and munger_artifact["status"] == "confirmed":
            required.update({"munger-image", "munger-image-insert", "munger-image-send"})
        store.finalize_artifact_plan(lease, required)

    document_delivery = None
    if candidate_result is not None:
        document_delivery = _publish_main_document(
            store=store, lease=lease, payload=payload, root=root, candidate=candidate_result,
            state_dir=state_dir, adapter=document_compiler or DocumentCompilerAdapter(),
            manual_document_id=manual_document_id,
        )
        if document_delivery["status"] in {"needs_review", "uncertain"}:
            unresolved = True
    required_analysis_unresolved = any(
        item["required"] and item["kind"] == "analysis" and item["status"] in {"pending", "uncertain"}
        for item in artifacts
    )
    needs_review = unresolved or retryable_governance_failure or required_analysis_unresolved
    store.set_status(lease, "needs_review" if needs_review else "waiting")
    if needs_review:
        document_review_notification = None
        if document_delivery and document_delivery.get("status") in {"needs_review", "uncertain"}:
            document_review_notification = _notify(
                store=store, lease=lease, payload=payload, root=root,
                event="document_review", notification_fn=notification_fn,
                reason_code=document_delivery.get("reason", "document-publish-result-unconfirmed"),
                clock=clock,
            )
        content_review_notification = None
        content_needs_review = required_analysis_unresolved or retryable_governance_failure
        content_review_artifact = next(
            (item for item in store.artifacts(job_id)
             if item["artifact_key"] == "content-review-notification"),
            None,
        )
        if content_needs_review:
            if content_review_artifact is None:
                # Older durable jobs predate this optional outbox artifact.
                # Add it only while holding the current lease, before any send.
                store.ensure_optional_artifact(
                    lease, "content-review-notification", "message",
                )
            content_review_notification = _notify(
                store=store, lease=lease, payload=payload, root=root,
                event="content_review", notification_fn=notification_fn,
                clock=clock,
            )
        return {
            "status": "needs_review", "job_id": job_id, "delivery_complete": False,
            "content_branches": outcomes, "elapsed_seconds": round(time.monotonic() - started, 3),
            "next_stage": "resolve_content_recovery", "document_candidate": candidate_result,
            "document_delivery": document_delivery,
            "document_review_notification": document_review_notification,
            "content_review_notification": content_review_notification,
            "required_artifacts": [item["artifact_key"] for item in store.artifacts(job_id) if item["required"]],
        }

    if document_delivery and document_delivery.get("status") == "confirmed":
        review_key = "document-publish-review-notification"
        review_effects = store.effects_for(job_id, review_key)
        unresolved_review = next(
            (effect for effect in review_effects if effect["status"] in {"attempting", "uncertain"}),
            None,
        )
        if unresolved_review:
            review_artifact = next(
                (item for item in store.artifacts(job_id) if item["artifact_key"] == review_key),
                None,
            )
            metadata = (review_artifact or {}).get("metadata", {})
            reason_code = metadata.get("notification_reason_code", metadata.get("reason_code"))
            if not isinstance(reason_code, str) or not reason_code:
                store.set_status(lease, "needs_review")
                return _delivery_result(
                    store, job_id, "needs_review", stage="document-publish-review-notification",
                    reason="original-review-notification-payload-unavailable",
                )
            notice = _notify(
                store=store, lease=lease, payload=payload, root=root,
                event="document_review", notification_fn=notification_fn,
                reason_code=reason_code, clock=clock,
            )
            if notice.get("status") != "confirmed":
                store.set_status(lease, "needs_review")
                return _delivery_result(
                    store, job_id, "needs_review", stage="document-publish-review-notification",
                    reason=notice.get("reason_code", "notification-send-result-unconfirmed"),
                    notification=notice.get("status"),
                )

    # Content can be useful even when a required branch failed. That must not
    # strand the job before delivery-state reporting; downstream impossible
    # artifacts are marked as known failures below.
    delivery_result = _advance_delivery(
        store=store, lease=lease, payload=payload, root=root, snapshot=snapshot,
        candidate=candidate_result, document_delivery=document_delivery,
        image_delivery_fn=image_delivery_fn, notification_fn=notification_fn,
        clock=clock,
        content_error=next((item.get("reason") for item in outcomes if item.get("status") == "failed"), None),
    )
    delivery_result.update({
        "content_branches": outcomes,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "document_candidate": candidate_result,
        "document_delivery": document_delivery,
        "required_artifacts": [item["artifact_key"] for item in store.artifacts(job_id) if item["required"]],
    })
    return delivery_result


def _delivery_result(store: WorkflowStore, job_id: str, status: str, **extra) -> dict:
    return {"status": status, "job_id": job_id, "delivery_complete": status == "succeeded", **extra}


def _mark_failed_if_open(store: WorkflowStore, lease: Lease, artifact_key: str,
                         reason: str, stage: str) -> None:
    artifact = next((item for item in store.artifacts(lease.job_id)
                     if item["artifact_key"] == artifact_key), None)
    if artifact and artifact["required"] and artifact["status"] == "pending":
        store.set_artifact(lease, artifact_key, "failed", metadata={
            "failure_stage": stage, "reason_code": re.sub(r"[^A-Za-z0-9_.:/+-]", "-", reason)[:120],
        })


def _defer_recovery_notification(*, store: WorkflowStore, lease, image_key: str,
                                 reason_code: str, clock) -> dict:
    notification_key = f"{image_key}-recovery-notification"
    retry_at = float(clock()) + 60
    store.set_artifact(lease, notification_key, "pending", metadata={
        "failure_stage": "notification", "reason_code": reason_code,
        "next_retry_at": retry_at,
    })
    store.set_status(lease, "waiting")
    return _delivery_result(
        store, lease.job_id, "waiting", stage="image-recovery-notification",
        image=image_key, next_retry_at=retry_at, reason=reason_code,
    )


def _advance_delivery(*, store: WorkflowStore, lease: Lease, payload: dict, root: Path,
                      snapshot: Path, candidate: dict | None, document_delivery: dict | None,
                      image_delivery_fn=None, notification_fn=None, clock=time.time,
                      content_error: str | None = None) -> dict:
    """Advance only durable, independently verifiable delivery stages."""
    job_id = lease.job_id
    artifacts = {item["artifact_key"]: item for item in store.artifacts(job_id)}
    document_id = (document_delivery or {}).get("document_id")
    delivery_failure_reason = content_error

    if candidate is None:
        reason = content_error or "required-content-branch-failed"
        delivery_failure_reason = reason
        _mark_failed_if_open(store, lease, "main-document", reason, "content-assembly")
        _mark_failed_if_open(store, lease, "document-notification", "main-document-unavailable", "notification")
        for key in ("core-image", "core-image-insert", "core-image-send",
                    "munger-image", "munger-image-insert", "munger-image-send"):
            _mark_failed_if_open(store, lease, key, "main-document-unavailable", "upstream-delivery")
        delivery_failed = True
    else:
        delivery_failed = False
        publish_state = (document_delivery or {}).get("status")
        if publish_state in {"needs_review", "uncertain"}:
            store.set_status(lease, "needs_review")
            notice = _notify(
                store=store, lease=lease, payload=payload, root=root,
                event="document_review", notification_fn=notification_fn,
                reason_code=(document_delivery or {}).get("reason", "document-publish-result-unconfirmed"),
                clock=clock,
            )
            return _delivery_result(store, job_id, "needs_review", stage="document-publish",
                                    reason=(document_delivery or {}).get("reason"),
                                    notification=notice.get("status"))
        if publish_state == "failed":
            delivery_failure_reason = "main-document-publish:" + str(
                (document_delivery or {}).get("reason", "known-failure"))
            _mark_failed_if_open(store, lease, "document-notification", "main-document-publish-failed", "notification")
            for key in ("core-image", "core-image-insert", "core-image-send",
                        "munger-image", "munger-image-insert", "munger-image-send"):
                _mark_failed_if_open(store, lease, key, "main-document-publish-failed", "upstream-delivery")
            delivery_failed = True
        elif publish_state != "confirmed" or not document_id:
            store.set_status(lease, "needs_review")
            notice = _notify(
                store=store, lease=lease, payload=payload, root=root,
                event="document_review", notification_fn=notification_fn,
                reason_code="main-document-result-unconfirmed", clock=clock,
            )
            return _delivery_result(store, job_id, "needs_review", stage="document-publish",
                                    reason="main-document-result-unconfirmed",
                                    notification=notice.get("status"))

        if not delivery_failed:
            progress = _notify(store=store, lease=lease, payload=payload, root=root,
                               event="document", notification_fn=notification_fn,
                               document_id=document_id, clock=clock)
            if progress.get("status") != "confirmed":
                status = progress.get("status")
                if status == "failed":
                    delivery_failure_reason = "document-notification:" + str(
                        progress.get("reason_code", "known-failure"))
                    _mark_failed_if_open(store, lease, "document-notification",
                                         progress.get("reason_code", "document-notification-failed"),
                                         "notification")
                    delivery_failed = True
                elif status == "deferred":
                    store.set_status(lease, "waiting")
                    return _delivery_result(store, job_id, "waiting", stage="document-notification",
                                            reason=progress.get("reason_code"))
                else:
                    store.set_status(lease, "needs_review")
                    return _delivery_result(store, job_id, "needs_review", stage="document-notification",
                                            reason=progress.get("reason_code"))

    # If the main document is missing or a required text stage failed, no
    # image can satisfy the combined insert-and-private-send contract.
    if delivery_failed:
        final_status = "failed"
        failure_reason = delivery_failure_reason or "main-document-unavailable"
    else:
        image_specs = [("core-image", "core-image", "核心内容图", 1)]
        if artifacts.get("munger-image", {}).get("required"):
            image_specs.append(("munger-image", "munger-image", "芒格洞察图", 2))
        failure_reason = None
        scheduled_retry = None
        for image_key, image_name, display_name, image_index in image_specs:
            image_states = {item["artifact_key"]: item for item in store.artifacts(job_id)}
            if all(image_states[key]["status"] == "confirmed" for key in (
                    image_key, f"{image_key}-insert", f"{image_key}-send")):
                continue
            recovery_key = f"{image_key}-recovery-notification"
            recovery = image_states.get(recovery_key)
            recovery_meta = recovery.get("metadata", {}) if recovery else {}
            recovery_retry_at = recovery_meta.get("next_retry_at")
            recovery_effects = store.effects_for(job_id, recovery_key)
            has_prepared_recovery_notice = any(item["status"] == "prepared" for item in recovery_effects)
            if (recovery and recovery["status"] == "pending" and has_prepared_recovery_notice
                    and isinstance(recovery_retry_at, (int, float))
                    and float(recovery_retry_at) > float(clock())):
                pending_notice_retry = {
                    "stage": "image-recovery-notification", "image": image_key,
                    "next_retry_at": float(recovery_retry_at),
                    "retries_used": image_states[image_key]["retry_count"],
                }
                if (scheduled_retry is None
                        or pending_notice_retry["next_retry_at"] < scheduled_retry["next_retry_at"]):
                    scheduled_retry = pending_notice_retry
                continue
            if recovery and recovery["status"] == "pending" and has_prepared_recovery_notice:
                image_state = image_states[image_key]
                image_meta = image_state.get("metadata", {})
                notice = _notify(
                    store=store, lease=lease, payload=payload, root=root,
                    event="image_recovery", notification_fn=notification_fn,
                    image_key=image_key, image_name=display_name, document_id=document_id,
                    failure_stage=image_meta.get("failure_stage", "unknown"),
                    reason_code=image_meta.get("reason_code", "unknown"),
                    retries_used=image_state["retry_count"],
                    retry_at=image_meta.get("next_retry_at"), clock=clock,
                )
                if notice.get("status") == "deferred":
                    return _defer_recovery_notification(
                        store=store, lease=lease, image_key=image_key,
                        reason_code=notice.get("reason_code", "message-rate-limit"), clock=clock,
                    )
                if notice.get("status") != "confirmed":
                    store.set_status(lease, "needs_review")
                    return _delivery_result(
                        store, job_id, "needs_review", stage="image-recovery-notification",
                        image=image_key, reason=notice.get("reason_code"),
                    )
            outcome = _deliver_image(
                store=store, lease=lease, payload=payload, root=root,
                image_key=image_key, image_name=image_name,
                source_path=snapshot / "source.md", document_id=document_id,
                image_index=image_index, image_delivery_fn=image_delivery_fn, clock=clock,
            )
            state = outcome.get("status")
            if state in {"retry_scheduled", "failed"}:
                latest = next(item for item in store.artifacts(job_id) if item["artifact_key"] == image_key)
                metadata = latest.get("metadata", {})
                retryable = metadata.get("retryable") is True and bool(metadata.get("next_retry_at"))
                if retryable:
                    notice = _notify(
                        store=store, lease=lease, payload=payload, root=root,
                        event="image_recovery", notification_fn=notification_fn,
                        image_key=image_key, image_name=display_name, document_id=document_id,
                        failure_stage=metadata.get("failure_stage", outcome.get("failure_stage", "unknown")),
                        reason_code=metadata.get("reason_code", outcome.get("reason_code", "unknown")),
                        retries_used=latest["retry_count"], retry_at=metadata["next_retry_at"],
                        clock=clock,
                    )
                    if notice.get("status") == "deferred":
                        return _defer_recovery_notification(
                            store=store, lease=lease, image_key=image_key,
                            reason_code=notice.get("reason_code", "message-rate-limit"), clock=clock,
                        )
                    if notice.get("status") != "confirmed":
                        store.set_status(lease, "needs_review")
                        return _delivery_result(store, job_id, "needs_review", stage="image-recovery-notification",
                                                image=image_key, reason=notice.get("reason_code"))
                    next_retry = {
                        "stage": metadata.get("failure_stage"), "image": image_key,
                        "next_retry_at": metadata["next_retry_at"],
                        "retries_used": latest["retry_count"],
                    }
                    if (scheduled_retry is None
                            or next_retry["next_retry_at"] < scheduled_retry["next_retry_at"]):
                        scheduled_retry = next_retry
                    continue
                failure_reason = f"{image_key}:{metadata.get('failure_stage', outcome.get('failure_stage', 'unknown'))}:{metadata.get('reason_code', outcome.get('reason_code', 'unknown'))}"
                for dependent in (f"{image_key}-insert", f"{image_key}-send"):
                    _mark_failed_if_open(store, lease, dependent, "image-delivery-failed", "upstream-delivery")
                continue
            if state in {"uncertain", "needs_review"}:
                details = outcome.get("reason_code", "image-result-unconfirmed")
                latest = next(item for item in store.artifacts(job_id) if item["artifact_key"] == image_key)
                if latest["status"] == "pending":
                    store.set_artifact(lease, image_key, "uncertain", metadata={
                        "failure_stage": outcome.get("failure_stage", "image-delivery"),
                        "reason_code": details,
                    })
                notice = _notify(
                    store=store, lease=lease, payload=payload, root=root,
                    event="image_recovery", notification_fn=notification_fn,
                    image_key=image_key, image_name=display_name, document_id=document_id,
                    failure_stage=outcome.get("failure_stage", "unknown"), reason_code=details,
                    retries_used=latest["retry_count"], retry_at=None, clock=clock,
                )
                store.set_status(lease, "needs_review")
                return _delivery_result(store, job_id, "needs_review", stage="image-delivery",
                                        image=image_key, reason=details,
                                        notification=notice.get("status"))
            if state == "deferred":
                store.set_status(lease, "waiting")
                return _delivery_result(store, job_id, "waiting", stage=outcome.get("failure_stage"),
                                        image=image_key, next_retry_at=outcome.get("retry_at"),
                                        reason=outcome.get("reason_code"))
            if state != "confirmed":
                store.set_status(lease, "needs_review")
                return _delivery_result(store, job_id, "needs_review", stage="image-delivery",
                                        image=image_key, reason=outcome.get("reason_code", "unsupported-image-state"))

        if scheduled_retry:
            store.set_status(lease, "waiting")
            return _delivery_result(store, job_id, "waiting", **scheduled_retry)

        states = {item["artifact_key"]: item for item in store.artifacts(job_id)}
        required = [item for item in states.values()
                    if item["required"] and item["artifact_key"] != "final-notification"]
        unresolved = any(item["status"] in {"pending", "uncertain"} for item in required)
        terminal_failure = any(item["status"] == "failed" for item in required)
        if unresolved:
            store.set_status(lease, "waiting")
            return _delivery_result(store, job_id, "waiting", stage="required-artifacts")
        final_status = "failed" if terminal_failure else "succeeded"
        if terminal_failure:
            failed_keys = [item["artifact_key"] for item in required if item["status"] == "failed"]
            failure_reason = failure_reason or ",".join(failed_keys[:5])

    final = _notify(store=store, lease=lease, payload=payload, root=root,
                    event="final", notification_fn=notification_fn,
                    status=final_status, document_id=document_id,
                    failure_reason=failure_reason, clock=clock)
    if final.get("status") != "confirmed":
        if final.get("status") == "deferred":
            store.set_status(lease, "waiting")
            return _delivery_result(store, job_id, "waiting", stage="final-notification",
                                    reason=final.get("reason_code"))
        store.set_status(lease, "needs_review")
        return _delivery_result(store, job_id, "needs_review", stage="final-notification",
                                reason=final.get("reason_code"))
    store.set_status(lease, final_status)
    return _delivery_result(store, job_id, final_status, document_id=document_id,
                            final_notification=final)


def process_job(state_dir: Path, job_id: str, *, owner: str = "read-x-worker", lease_seconds: float = 900,
                bridge_fn=run_bridge, observe_fn=observe_bridge, prompt_fetcher=None,
                document_compiler=None, image_delivery_fn=None, notification_fn=None,
                clock=time.time, manual_document_id: str | None = None, outcome_recorder=None) -> dict:
    state_dir = Path(state_dir).absolute()
    if manual_document_id is not None and not DOCUMENT_ID_RE.fullmatch(manual_document_id):
        return {"status": "blocked", "job_id": job_id, "delivery_complete": False,
                "reason_code": "invalid-document-id"}
    lock = threading.RLock()
    with WorkflowStore(state_dir) as store:
        job = store.get_job(job_id)
        if not job:
            return {"status": "blocked", "job_id": job_id, "delivery_complete": False,
                    "reason_code": "job-not-found"}
        payload = job.get("payload")
        admission_issue = workflow_payload_issue(
            payload, app_id=job.get("app_id"), source_message_id=job.get("source_message_id"),
        )
        if admission_issue:
            return {"status": "blocked", "job_id": job_id, "delivery_complete": False,
                    "reason_code": admission_issue}
        lease = store.acquire_lease(job_id, owner, lease_seconds)
        try:
            result = _advance_claimed_job(store, lease, state_dir, bridge_fn=bridge_fn,
                                          observe_fn=observe_fn, prompt_fetcher=prompt_fetcher,
                                          document_compiler=document_compiler,
                                          image_delivery_fn=image_delivery_fn,
                                          notification_fn=notification_fn, clock=clock,
                                          manual_document_id=manual_document_id,
                                          outcome_recorder=outcome_recorder)
        except Exception as exc:
            with lock:
                try:
                    store.set_status(lease, "needs_review")
                except Exception:
                    pass
            result = {"status": "needs_review", "job_id": job_id, "delivery_complete": False,
                      "reason": f"{type(exc).__name__}: {exc}"}
        finally:
            with lock:
                try:
                    store.release_lease(lease)
                except Exception:
                    pass
        return result


def run_once(state_dir: Path, *, job_id: str | None = None, owner: str = "read-x-worker",
             manual_document_id: str | None = None, outcome_recorder=None) -> dict:
    state_dir = Path(state_dir).absolute()
    if job_id:
        return process_job(state_dir, job_id, owner=owner, manual_document_id=manual_document_id,
                           outcome_recorder=outcome_recorder)
    if manual_document_id is not None:
        return {"status": "blocked", "delivery_complete": False,
                "reason_code": "manual-document-id-requires-job-id"}
    db = state_dir / "workflow.sqlite3"
    if not db.is_file() or db.is_symlink():
        return {"status": "idle", "jobs_processed": 0, "delivery_complete": False}
    with WorkflowStore(state_dir) as store:
        now = time.time()
        jobs = store.list_jobs()
        selected = next((job["job_id"] for job in jobs if _job_is_due(store, job, now)), None)
    if selected is None:
        return {"status": "idle", "jobs_processed": 0, "delivery_complete": False}
    return process_job(state_dir, selected, owner=owner, outcome_recorder=outcome_recorder)


def _job_is_due(store: WorkflowStore, job: dict, now: float) -> bool:
    admitted = store.get_job(job["job_id"])
    payload = admitted.get("payload") if admitted else None
    if workflow_payload_issue(
            payload, app_id=admitted.get("app_id") if admitted else None,
            source_message_id=admitted.get("source_message_id") if admitted else None):
        return False
    if job["status"] in {"succeeded", "failed"}:
        return False
    row = store.connection.execute(
        "SELECT lease_expires_at FROM jobs WHERE job_id=?", (job["job_id"],)
    ).fetchone()
    if row and row["lease_expires_at"] and row["lease_expires_at"] > now:
        return False
    artifacts = store.artifacts(job["job_id"])
    if job["status"] == "needs_review":
        # Only uncertain remote actions are eligible for automatic, read-only
        # reconciliation, plus explicitly scheduled outbox delivery attempts.
        unresolved_effect = store.connection.execute(
            "SELECT 1 FROM effects WHERE job_id=? AND status IN ('attempting','uncertain') "
            "AND action NOT LIKE 'message-send%' LIMIT 1",
            (job["job_id"],),
        ).fetchone() is not None
        return unresolved_effect or _notification_retry_due(store, artifacts, now)
    if _notification_retry_due(store, artifacts, now):
        return True
    if any(item["kind"] == "analysis" and item["required"]
           and item["status"] in {"pending", "uncertain"} for item in artifacts):
        return True
    for item in artifacts:
        if (item["required"] or not item["artifact_key"].endswith("-recovery-notification")
                or item["status"] != "pending"):
            continue
        effects = store.effects_for(job["job_id"], item["artifact_key"])
        if not any(effect["status"] == "prepared" for effect in effects):
            continue
        retry_at = item.get("metadata", {}).get("next_retry_at")
        if not isinstance(retry_at, (int, float)) or float(retry_at) <= now:
            return True
    by_key = {item["artifact_key"]: item for item in artifacts}
    deferred_images = {}
    for key in ("core-image", "munger-image"):
        item = by_key.get(key)
        if not item or not item["required"]:
            continue
        metadata = item.get("metadata", {})
        retry_at = metadata.get("next_retry_at")
        retryable_failure = item["status"] == "failed" and metadata.get("retryable") is True
        send_item = by_key.get(f"{key}-send", {})
        send_retry_at = send_item.get("metadata", {}).get("next_retry_at")
        deferred_send = send_item.get("status") == "pending" and isinstance(send_retry_at, (int, float))
        due_at = retry_at if retryable_failure else send_retry_at
        if (retryable_failure or deferred_send) and isinstance(due_at, (int, float)) and float(due_at) > now:
            deferred_images[key] = float(due_at)
    for item in artifacts:
        if not item["required"]:
            continue
        if deferred_images and item["artifact_key"] == "final-notification":
            continue
        if any(item["artifact_key"] in {key, f"{key}-insert", f"{key}-send"}
               for key in deferred_images):
            continue
        if item["artifact_key"] in {"core-image", "munger-image"} and item["status"] == "failed":
            metadata = item.get("metadata", {})
            retry_at = metadata.get("next_retry_at")
            if metadata.get("retryable") is True and isinstance(retry_at, (int, float)):
                if float(retry_at) <= now:
                    return True
                continue
        if item["status"] in {"pending", "uncertain"}:
            retry_at = item.get("metadata", {}).get("next_retry_at")
            if isinstance(retry_at, (int, float)) and float(retry_at) > now:
                continue
            return True
    return False


def _notification_retry_due(store: WorkflowStore, artifacts: list[dict], now: float) -> bool:
    """Schedule message sends and paced read-only checks for unresolved outbox effects."""
    retry_limit = workflow_notifications.NOTIFICATION_RETRY_LIMIT + 1
    idempotency_window = workflow_notifications.FEISHU_MESSAGE_IDEMPOTENCY_WINDOW_SECONDS
    for artifact in artifacts:
        if artifact["kind"] != "message":
            continue
        metadata = artifact.get("metadata", {})
        for effect in store.effects_for(artifact["job_id"], artifact["artifact_key"]):
            is_message_send = (effect["action"] == "message-send"
                               or effect["action"].startswith("message-send:"))
            if not is_message_send:
                continue
            if effect["status"] == "attempting":
                return True
            if effect["status"] == "prepared":
                if artifact["status"] != "pending":
                    continue
                retry_at = metadata.get("next_retry_at")
                if not isinstance(retry_at, (int, float)) or float(retry_at) <= now:
                    return True
                continue
            if effect["status"] == "failed" and effect.get("metadata", {}).get("retryable") is True:
                if artifact["status"] == "uncertain":
                    reconcile_at = metadata.get(
                        "next_reconcile_at", effect.get("metadata", {}).get("next_reconcile_at"),
                    )
                    if not isinstance(reconcile_at, (int, float)) or float(reconcile_at) <= now:
                        return True
                    continue
                retry_at = metadata.get("next_retry_at", effect.get("metadata", {}).get("next_retry_at"))
                if artifact["status"] in {"pending", "failed"}:
                    if not isinstance(retry_at, (int, float)):
                        retry_at = float(effect["updated_at"]) + workflow_notifications.NOTIFICATION_RETRY_DELAY_SECONDS
                    if (float(retry_at) <= now and effect["attempts"] < retry_limit
                            and now - float(effect["created_at"]) < idempotency_window):
                        return True
                if artifact["status"] in {"failed", "uncertain"} and (
                        effect["attempts"] >= retry_limit
                        or now - float(effect["created_at"]) >= idempotency_window):
                    reconcile_at = metadata.get("next_reconcile_at", effect.get("metadata", {}).get("next_reconcile_at"))
                    if not isinstance(reconcile_at, (int, float)) or float(reconcile_at) <= now:
                        return True
                continue
            if (effect["status"] == "uncertain" and artifact["status"] == "uncertain"
                    or effect["status"] == "failed" and artifact["status"] in {"failed", "uncertain"}):
                reconcile_at = metadata.get("next_reconcile_at", effect.get("metadata", {}).get("next_reconcile_at"))
                if not isinstance(reconcile_at, (int, float)) or float(reconcile_at) <= now:
                    return True
    return False
