"""Read-X's two context deliverables; no ChatGPT execution is reachable here."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))
from chatgpt_bridge import run_context_pack
from long_read_workflow_store import CONTEXT_ARTIFACTS, CONTEXT_WORKFLOW_CONTRACT_VERSION

CALLER = "read-x:long-read-text"
RETRY_SECONDS = 60
RECONCILE_SECONDS = 300


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _code(value) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/+-]{1,100}", value) else "context-pack-unavailable"


def context_result(job: dict, artifacts: list[dict]) -> dict:
    by_key = {item["artifact_key"]: item for item in artifacts}
    completed = sum(by_key.get(key, {}).get("status") == "confirmed" for key in CONTEXT_ARTIFACTS)
    unresolved = [{"artifact": key, "status": by_key.get(key, {}).get("status", "pending"),
                   "reason_code": by_key.get(key, {}).get("metadata", {}).get("reason_code", "context-pack-pending")}
                  for key in CONTEXT_ARTIFACTS if by_key.get(key, {}).get("status") != "confirmed"
                  or by_key.get(key, {}).get("metadata", {}).get("remote_name_status") == "mismatch"]
    return {"job_status": job["status"], "delivery_complete": completed == 2,
            "context_packages": {"required": 2, "completed": completed,
                                 "summary": f"上下文包：已完成 {completed}/2。", "unresolved": unresolved},
            "automatic_artifacts": {"status": "not_requested", "requested": []}}


def context_job_due(store, job: dict, now: float) -> bool:
    if job["status"] in {"succeeded", "failed"}:
        return False
    for artifact in store.artifacts(job["job_id"]):
        metadata = artifact.get("metadata", {})
        if artifact["status"] != "confirmed" and metadata.get("retry_allowed") is not False:
            due = metadata.get("next_reconcile_at", metadata.get("next_retry_at", 0))
            if not isinstance(due, (int, float)) or due <= now:
                return True
    return False


def _frozen_inputs(payload: dict, state_dir: Path) -> dict[str, bytes]:
    from long_read_workflow_cli import _read_regular, _reject_symlink_path, _verify_snapshot, _canonical
    snapshot = state_dir / "artifacts" / payload["snapshot_sha256"]
    _reject_symlink_path(snapshot)
    files = {}
    for name, reference in payload["inputs"].items():
        path = snapshot / name
        if str(path) != reference["path"]:
            raise ValueError("context input escaped the frozen snapshot")
        _reject_symlink_path(path)
        value = _read_regular(path)
        if _hash(value) != reference["sha256"]:
            raise ValueError("frozen context input hash changed")
        files[name] = value
    manifest = _canonical({name: {"sha256": _hash(value), "bytes": len(value)} for name, value in sorted(files.items())})
    if _hash(manifest) != payload["snapshot_sha256"]:
        raise ValueError("frozen context manifest hash changed")
    _verify_snapshot(snapshot, files, manifest)
    return files


def _verified_receipt(result: dict, pack: dict, task_id: str, prompt: bytes) -> None:
    from long_read_workflow_cli import _read_regular, _reject_symlink_path
    expected_ref = {"packId": pack["pack_id"], "taskId": task_id, "artifactId": pack["artifact_key"],
                    "callerLabel": CALLER, "promptSha256": pack["prompt_sha256"],
                    "submissionSha256": _hash(prompt)}
    if (result.get("schemaVersion") != 2 or result.get("runId") != pack["pack_id"]
            or result.get("contextPackRef") != expected_ref or result.get("callerLabel") != CALLER
            or result.get("taskId") != task_id or result.get("artifactId") != pack["artifact_key"]
            or result.get("promptSha256") != pack["prompt_sha256"]
            or result.get("submissionSha256") != _hash(prompt)
            or result.get("inputSha256") != pack["input_sha256"]
            or result.get("promptAsset") != pack["prompt_asset"]):
        raise ValueError("context pack receipt identity or hash mismatch")
    path = Path(result.get("path", ""))
    if (not path.is_absolute() or path.parent.name != pack["pack_id"]
            or path.name != result.get("fileName")):
        raise ValueError("context pack path or filename mismatch")
    _reject_symlink_path(path)
    if _read_regular(path) != prompt:
        raise ValueError("context TXT readback differs from frozen business input")


def advance_context_job(store, lease, state_dir: Path, *, pack_fn=run_context_pack, clock=time.time) -> dict:
    from long_read_workflow_worker import _artifact_root, _atomic_bytes
    from long_read_workflow_notifications import context_pack_delivery

    job = store.get_job(lease.job_id)
    payload = job["payload"]
    if payload.get("workflow_contract_version") != CONTEXT_WORKFLOW_CONTRACT_VERSION:
        raise ValueError("not a context-only task")
    files = _frozen_inputs(payload, state_dir)
    target = context_pack_delivery(payload["trigger"], store)
    if target.get("invalid"):
        raise ValueError("frozen context delivery target is invalid")
    root = Path(payload["context_pack_dir"]) if payload.get("context_pack_dir") else None
    receipt_dir = _artifact_root(state_dir, lease.job_id) / "context-packs"
    store.set_status(lease, "running")
    receipts = []
    for pack in payload["context_packs"]:
        key = pack["artifact_key"]
        artifact = next(item for item in store.artifacts(lease.job_id) if item["artifact_key"] == key)
        if artifact["status"] == "confirmed":
            continue
        prompt = files[pack["prompt_file"]]
        if _hash(prompt) != pack["prompt_sha256"]:
            raise ValueError("frozen business Prompt hash changed")
        result = pack_fn("prepare", pack_id=pack["pack_id"], root=root, prompt=prompt.decode("utf-8"),
                         metadata={"caller_label": CALLER, "title": payload["title"], "pack_type": pack["pack_type"],
                                   "title_source": payload["title_source"], "task_id": lease.job_id, "artifact_id": key,
                                   "prompt_sha256": pack["prompt_sha256"], "input_sha256": pack["input_sha256"],
                                   "prompt_asset": pack["prompt_asset"], "delivery_context": target})
        try:
            _verified_receipt(result, pack, lease.job_id, prompt)
        except (ValueError, OSError):
            code = _code(result.get("errorCode") or "context-pack-receipt-invalid")
            store.set_artifact(lease, key, "pending", metadata={"failure_stage": "context-pack-prepare", "reason_code": code,
                               "retry_allowed": False})
            continue
        effect = store.find_effect(lease.job_id, key, "context-pack-deliver")
        if effect is None:
            effect = store.prepare_effect(lease, key, "context-pack-deliver", pack["prompt_sha256"])
        recovering = effect["status"] in {"attempting", "uncertain"}
        if recovering:
            # A crashed/unknown sender can only observe the original frozen package.
            result = pack_fn("verify", pack_id=pack["pack_id"], root=root)
        else:
            store.begin_effect(lease, effect["op_key"])
            try:
                result = pack_fn("deliver", pack_id=pack["pack_id"], root=root)
            except Exception:
                result = {"runId": pack["pack_id"], "deliveryStatus": "needs_review", "errorCode": "context-pack-process-unavailable"}
        try:
            _verified_receipt(result, pack, lease.job_id, prompt)
            valid = True
        except (ValueError, OSError):
            valid = False
        status = result.get("deliveryStatus") if valid else "needs_review"
        code = _code(result.get("errorCode") or ("context-pack-pending" if valid else "context-pack-receipt-invalid"))
        metadata = {"failure_stage": "context-pack-delivery", "reason_code": code}
        if status == "delivered" and isinstance(result.get("messageId"), str) and result["messageId"].startswith("om_"):
            metadata = {"verification": "context-pack-readback", "content_sha256": result["submissionSha256"]}
            if result.get("remoteNameStatus") in {"verified", "unobservable", "mismatch"}:
                metadata["remote_name_status"] = result["remoteNameStatus"]
            if result.get("errorCode") == "context-pack-name-mismatch" or result.get("remoteNameStatus") == "mismatch":
                metadata.update(reason_code="context-pack-name-mismatch", remote_name_status="mismatch")
            resolve = store.reconcile_effect if recovering else store.finish_effect
            resolve(lease, effect["op_key"], "confirmed", remote_id=result["messageId"], metadata=metadata)
            store.set_artifact(lease, key, "confirmed", metadata=metadata)
        elif status in {"local_only", "pending", "uploaded"} or status == "failed" and code in {
                "context-pack-send-rejected", "context-pack-upload-rejected"}:
            resolve = store.reconcile_effect if recovering else store.finish_effect
            resolve(lease, effect["op_key"], "failed", metadata=metadata)
            store.set_artifact(lease, key, "pending", metadata={**metadata, "retry_allowed": True,
                               "next_retry_at": clock() + RETRY_SECONDS})
        else:
            if not recovering:
                store.finish_effect(lease, effect["op_key"], "uncertain", metadata=metadata)
            store.set_artifact(lease, key, "uncertain", metadata={**metadata,
                               "next_reconcile_at": clock() + RECONCILE_SECONDS})
        if valid:
            _atomic_bytes(receipt_dir / f"{pack['pack_id']}.json",
                          (json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
            receipts.append(result)
    artifacts = store.artifacts(lease.job_id)
    completed = all(item["status"] == "confirmed" for item in artifacts if item["required"])
    needs_review = any(item["status"] == "uncertain" or item.get("metadata", {}).get("retry_allowed") is False
                       or item.get("metadata", {}).get("remote_name_status") == "mismatch"
                       for item in artifacts)
    store.set_status(lease, "needs_review" if needs_review else "succeeded" if completed else "waiting")
    job = store.get_job(lease.job_id)
    return {"status": job["status"], "job_id": lease.job_id, **context_result(job, artifacts), "packages": receipts}
