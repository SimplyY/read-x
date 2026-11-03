#!/usr/bin/env python3
"""Read-X adapter for the shared ChatGPT manual-handoff record and candidate API."""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import subprocess
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[4]
CODE_ROOT = REPO_ROOT.parent
CORE_CLI = CODE_ROOT / "browser-runtime-governance" / "skills" / "chatgpt-web-bridge" / "scripts" / "manual-handoff-cli.mjs"
CALLER_LABEL = "read-x:long-read-text"
MANUAL_ACTION = "manual-handoff"
HANDOFF_SCHEMA = "handoff/v1"
RESULT_SCHEMA = "handoff-result/v1"
FULL_FLOW = [
    "验证每个分析分支并按原 Read-X 规则组装长文",
    "生成正文及必要图片候选，保留原有分支隔离",
    "等待原有最终人工确认",
    "写入、归档并发布到原业务目标",
    "远端读回核验后反馈最终产物",
]


class ManualHandoffError(RuntimeError):
    """A fail-closed error from the shared handoff store or Read-X binding."""


def _safe(path: Path) -> None:
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() and part not in {Path("/tmp"), Path("/var")}:
            raise ManualHandoffError("manual-handoff-unsafe-path")


def handoff_root(state_dir: Path) -> Path:
    root = Path(state_dir).absolute() / "manual-handoffs"
    _safe(root)
    return root


IMAGE_ARTIFACTS = frozenset({"core-image", "munger-image"})


def _stage_for_artifact(artifact_key: str) -> str:
    if re.fullmatch(r"analysis:[A-Za-z0-9_.-]{1,100}", artifact_key):
        return "analysis"
    if artifact_key in IMAGE_ARTIFACTS:
        return "image"
    raise ManualHandoffError("manual-handoff-artifact-invalid")


def _invoke(command: str, *, root: Path, payload: dict | None = None,
            handoff_id: str | None = None, candidate_id: str | None = None,
            caller_label: str | None = None) -> dict:
    if not CORE_CLI.is_file():
        raise ManualHandoffError("manual-handoff-core-unavailable")
    argv = ["node", str(CORE_CLI), command, "--root", str(root)]
    if handoff_id:
        argv += ["--handoff-id", handoff_id]
    if candidate_id:
        argv += ["--candidate-id", candidate_id]
    if caller_label:
        argv += ["--caller-label", caller_label]
    try:
        completed = subprocess.run(
            argv,
            input=(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                   if payload is not None else None),
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManualHandoffError("manual-handoff-core-unavailable") from exc
    output = completed.stdout.decode("utf-8", errors="replace").strip().splitlines()
    try:
        result = json.loads(output[-1]) if output else json.loads(completed.stderr.decode("utf-8"))
    except (json.JSONDecodeError, IndexError) as exc:
        raise ManualHandoffError("manual-handoff-core-invalid-response") from exc
    # The shared CLI uses 75 to mark a durable waiting handoff or received
    # candidate. These are valid outcomes; other nonzero exits fail closed.
    review_result = (
        command in {"prepare", "import"} and completed.returncode == 2
        and result.get("status") in {"needs_review", "expired", "superseded", "missing"}
    )
    if completed.returncode not in {0, 75} and not review_result:
        code = result.get("errorCode") or result.get("reason") or result.get("status")
        raise ManualHandoffError(f"manual-handoff-{code or 'rejected'}")
    return result


def prune_handoffs(*, state_dir: Path) -> dict:
    """Sweep expired private handoff copies without creating an empty store."""
    root = handoff_root(state_dir)
    try:
        root.lstat()
    except FileNotFoundError:
        return {"pruned": 0, "skipped": 0}
    except OSError as exc:
        raise ManualHandoffError("manual-handoff-retention-store-unavailable") from exc
    result = _invoke("prune", root=root)
    if result.get("errorCode"):
        raise ManualHandoffError("manual-handoff-retention-sweep-failed")
    if any(type(result.get(key)) is not int or result[key] < 0 for key in ("pruned", "skipped")):
        raise ManualHandoffError("manual-handoff-retention-result-invalid")
    return result


def binding_for_job(job: dict, artifact_key: str, *, prompt_sha: str | None = None) -> dict:
    if not isinstance(job, dict) or not isinstance(job.get("payload"), dict):
        raise ManualHandoffError("manual-handoff-job-unavailable")
    payload = job["payload"]
    snapshot_sha = payload.get("snapshot_sha256")
    if not isinstance(snapshot_sha, str) or not re.fullmatch(r"[a-f0-9]{64}", snapshot_sha):
        raise ManualHandoffError("manual-handoff-input-version-unavailable")
    if prompt_sha is not None and not re.fullmatch(r"[a-f0-9]{64}", prompt_sha):
        raise ManualHandoffError("manual-handoff-prompt-hash-invalid")
    stage = _stage_for_artifact(artifact_key)
    source_id = job.get("source_message_id")
    if not isinstance(source_id, str) or not source_id:
        source_id = f"codex-task:{job['job_id']}"
    if not re.fullmatch(r"[A-Za-z0-9:_./-]{1,160}", source_id):
        raise ManualHandoffError("manual-handoff-origin-event-invalid")
    return {
        "callerLabel": CALLER_LABEL,
        "taskId": job["job_id"],
        "stage": stage,
        "artifactId": artifact_key,
        "inputVersion": f"snapshot:{snapshot_sha}:prompt:{prompt_sha}" if prompt_sha else f"snapshot:{snapshot_sha}",
        "originEventId": source_id,
    }


def _source_requirements(job: dict, artifact_key: str, source_sha: str,
                         prompt_assets: list[dict] | None) -> dict:
    inputs = job["payload"].get("inputs", {})
    source_hashes = []
    for name in ("source.md", "evidence.json"):
        reference = inputs.get(name, {}) if isinstance(inputs, dict) else {}
        digest = reference.get("sha256") if isinstance(reference, dict) else None
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ManualHandoffError("manual-handoff-source-proof-unavailable")
        source_hashes.append({"name": name, "sha256": digest})
    stage = _stage_for_artifact(artifact_key)
    image = stage == "image"
    return {
        "sourceHashes": source_hashes,
        "branchSourceSha256": source_sha,
        "formalPromptAssets": prompt_assets or [],
        "expectedOutput": {
            "format": "PNG image/png" if image else "Markdown UTF-8",
            "businessValidation": (
                "Read-X core image PNG validator, then the existing image insertion, private-send, and readback gates"
                if image else "Read-X long-read branch validator"
            ),
            "returnInstructions": (
                "Return one original PNG image in this original Codex task; import the PNG as --result."
                if image else "Return the complete result in this original Codex task; attach original files separately."
            ),
        },
        "historyDependency": {
            "required": False,
            "verified": True,
            "items": [],
            "basis": "This isolated Read-X prompt contains its source and evidence inputs; no ChatGPT history or account Memory is required.",
        },
        "fullFlow": FULL_FLOW,
        "continuationChoice": {
            "required": False,
            "selected": "continue-complete-workflow",
            "basis": "The accepted Read-X long-read request already authorizes its existing full business flow.",
            "finalConfirmationRequired": True,
        },
    }


def _prompt_identity(prompt_sha: str, prompt_assets: list[dict] | None) -> dict:
    if not re.fullmatch(r"[a-f0-9]{64}", prompt_sha) or not isinstance(prompt_assets, list) or len(prompt_assets) != 1:
        raise ManualHandoffError("manual-handoff-prompt-identity-unavailable")
    asset = prompt_assets[0]
    if not isinstance(asset, dict):
        raise ManualHandoffError("manual-handoff-prompt-identity-unavailable")
    # Local Skill prompts are versioned by the actual Skill source hash; governed
    # Prompt assets retain their fetched ID and revision. In both cases sha256
    # binds the exact composed bytes sent to ChatGPT.
    if asset.get("assetType") == "skill":
        prompt_id, revision = f"read-x.skill/{asset.get('name', '')}", asset.get("sha256")
    else:
        prompt_id = asset.get("promptId", asset.get("prompt_id"))
        revision = asset.get("revision", asset.get("prompt_revision"))
        asset_sha = asset.get("sha256", asset.get("prompt_sha256"))
        if not isinstance(asset_sha, str) or not re.fullmatch(r"[a-f0-9]{64}", asset_sha):
            raise ManualHandoffError("manual-handoff-prompt-identity-unavailable")
    if not isinstance(prompt_id, str) or not prompt_id.strip() or not isinstance(revision, (str, int)) or not str(revision).strip():
        raise ManualHandoffError("manual-handoff-prompt-identity-unavailable")
    if not isinstance(revision, int) and not re.fullmatch(r"[A-Za-z0-9:_./-]{1,200}", revision):
        raise ManualHandoffError("manual-handoff-prompt-identity-unavailable")
    if isinstance(revision, int) and revision < 0:
        raise ManualHandoffError("manual-handoff-prompt-identity-unavailable")
    return {"promptId": prompt_id, "revision": revision, "sha256": prompt_sha}


def instructions_for_record(state_dir: Path, record: dict) -> dict:
    cli = REPO_ROOT / ".agents" / "skills" / "long-read" / "scripts" / "long_read_workflow_cli.py"
    image = record.get("stage") == "image"
    return {
        "submitTo": "ChatGPT new chat",
        "returnTo": "this original Codex task",
        "resultFormat": "one original PNG image; use it as --result" if image
        else "complete UTF-8 Markdown; attach original files separately with --file",
        "importCommand": (
            f'python3 "{cli}" --state-dir "{Path(state_dir).absolute()}" import-manual-result '
            f'--job-id {record.get("taskId")} --artifact {record.get("artifactId")} --result '
            + ("<candidate.png>" if image else "<result-file> [--file <original-file>]")
        ),
        "resumeCommand": (
            f'python3 "{cli}" --state-dir "{Path(state_dir).absolute()}" resume '
            f'--job-id {record.get("taskId")} '
        ),
        "expiresAt": record.get("expiresAt"),
    }


def prepare_branch(*, state_dir: Path, job: dict, artifact_key: str, prompt_bytes: bytes,
                   source_sha: str, prompt_assets: list[dict] | None = None) -> dict:
    if not prompt_bytes:
        raise ManualHandoffError("manual-handoff-prompt-missing")
    prompt_sha = hashlib.sha256(prompt_bytes).hexdigest()
    binding = binding_for_job(job, artifact_key, prompt_sha=prompt_sha)
    root = handoff_root(state_dir)
    source_requirements = _source_requirements(job, artifact_key, source_sha, prompt_assets)
    payload = {
        **binding,
        "promptBase64": base64.b64encode(prompt_bytes).decode("ascii"),
        "promptIdentity": _prompt_identity(prompt_sha, prompt_assets),
        "sourceRequirements": source_requirements,
    }
    result = _invoke("prepare", root=root, payload=payload)
    if result.get("status") != "prepared" or not isinstance(result.get("record"), dict):
        return result
    record = result["record"]
    _assert_record_binding(record, binding)
    if record.get("promptSha256") != prompt_sha:
        raise ManualHandoffError("manual-handoff-prompt-hash-mismatch")
    return {
        **result,
        "promptSha256": prompt_sha,
        "fullFlow": source_requirements["fullFlow"],
        "continuationChoice": source_requirements["continuationChoice"],
        "historyDependency": source_requirements["historyDependency"],
        "manualInstructions": instructions_for_record(state_dir, record),
    }


def _assert_record_binding(record: dict, binding: dict) -> None:
    if record.get("schema") != HANDOFF_SCHEMA:
        raise ManualHandoffError("manual-handoff-schema-mismatch")
    _assert_binding(record, binding)


def _assert_binding(record: dict, binding: dict) -> None:
    for key, value in binding.items():
        if record.get(key) != value:
            raise ManualHandoffError(f"manual-handoff-{key}-mismatch")


def _assert_candidate_binding(candidate: dict, binding: dict) -> None:
    if candidate.get("schema") != RESULT_SCHEMA:
        raise ManualHandoffError("manual-handoff-candidate-schema-mismatch")
    _assert_binding(candidate, binding)


def read_handoff(*, state_dir: Path, handoff_id: str, binding: dict | None = None) -> dict:
    result = _invoke("read", root=handoff_root(state_dir), handoff_id=handoff_id,
                     caller_label=CALLER_LABEL)
    record = result.get("record")
    if not isinstance(record, dict):
        raise ManualHandoffError("manual-handoff-record-unavailable")
    if binding is not None:
        _assert_record_binding(record, binding)
    prompt_bytes = base64.b64decode(result.get("promptBase64", ""), validate=True)
    if hashlib.sha256(prompt_bytes).hexdigest() != record.get("promptSha256"):
        raise ManualHandoffError("manual-handoff-prompt-hash-mismatch")
    return {"record": record, "promptBytes": prompt_bytes,
            "privatePaths": result.get("privatePaths") or {}}


def import_result(*, state_dir: Path, handoff_id: str, binding: dict, event_id: str,
                  source_claim: str, result_bytes: bytes, files: list[dict] | None = None) -> dict:
    payload = {
        "handoffId": handoff_id,
        **binding,
        "eventId": event_id,
        "intakeChannel": "codex",
        "operatorBasis": {
            "kind": "codex-user-message",
            "referenceSha256": hashlib.sha256(event_id.encode("utf-8")).hexdigest(),
        },
        "sourceClaim": source_claim,
        "resultBase64": base64.b64encode(result_bytes).decode("ascii"),
        "files": [
            {"name": item["name"], "mimeType": item["mimeType"],
             "bytesBase64": base64.b64encode(item["bytes"]).decode("ascii")}
            for item in (files or [])
        ],
    }
    return _invoke("import", root=handoff_root(state_dir), payload=payload)


def read_candidate(*, state_dir: Path, handoff_id: str, candidate_id: str,
                   binding: dict) -> dict:
    result = _invoke("read-result", root=handoff_root(state_dir), handoff_id=handoff_id,
                     candidate_id=candidate_id)
    candidate = result.get("candidate")
    if not isinstance(candidate, dict) or candidate.get("schema") != RESULT_SCHEMA:
        raise ManualHandoffError("manual-handoff-candidate-unavailable")
    _assert_candidate_binding(candidate, binding)
    if candidate.get("evidenceLevel") != "user-asserted" or candidate.get("status") not in {"received", "duplicate"}:
        raise ManualHandoffError("manual-handoff-candidate-not-acceptable")
    result_bytes = base64.b64decode(result.get("resultBase64", ""), validate=True)
    if hashlib.sha256(result_bytes).hexdigest() != candidate.get("rawSha256"):
        raise ManualHandoffError("manual-handoff-result-hash-mismatch")
    files = []
    for item in result.get("files", []):
        raw = base64.b64decode(item.get("bytesBase64", ""), validate=True)
        if hashlib.sha256(raw).hexdigest() != item.get("sha256"):
            raise ManualHandoffError("manual-handoff-result-file-hash-mismatch")
        files.append({"name": item["name"], "mimeType": item["mimeType"],
                      "sha256": item["sha256"], "bytes": raw})
    return {"candidate": candidate, "resultBytes": result_bytes, "files": files}


def inspect_source_file(path: Path) -> dict:
    path = Path(path).absolute()
    _safe(path)
    path.lstat()
    if path.is_symlink() or not path.is_file():
        raise ManualHandoffError("manual-handoff-result-file-must-be-regular-file")
    raw = path.read_bytes()
    mime_type, _ = mimetypes.guess_type(path.name)
    return {"name": path.name, "mimeType": mime_type or "application/octet-stream", "bytes": raw}


def write_candidate_files(*, output: Path, candidate_id: str, files: list[dict], atomic_write) -> list[dict]:
    if not files:
        return []
    directory = output.parent / "manual-results" / output.stem / candidate_id
    _safe(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    saved = []
    for item in files:
        name = item["name"]
        if not isinstance(name, str) or not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ManualHandoffError("manual-handoff-result-file-name-invalid")
        path = directory / name
        _safe(path)
        atomic_write(path, item["bytes"])
        saved.append({"name": name, "mimeType": item["mimeType"],
                      "sha256": item["sha256"], "path": str(path)})
    return saved
