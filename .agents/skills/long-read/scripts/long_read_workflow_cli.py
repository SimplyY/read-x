#!/usr/bin/env python3
"""Durable long-read admission, worker advancement, and read-only inspection."""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import re
import uuid
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from chatgpt_bridge import record_business_outcome
from long_read_document_assembler import validate_scoring_fields
from render_long_read_delivery_card import _load_score_evidence
from long_read_workflow_store import (
    StoreConflict, WORKFLOW_CONTRACT_VERSION, CONTEXT_WORKFLOW_CONTRACT_VERSION,
    CONTEXT_ARTIFACTS, WorkflowStore, is_valid_source_created_at,
    state_lock, workflow_payload_issue,
)
from run_isolated_analyses import validate_evidence

DEFAULT_STATE_DIR = REPO_ROOT / "state" / "long-read"
CANARY_PREFIX = "【CANARY｜Read-X long_read 端到端验收】"
CANARY_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")
# Already-admitted jobs may resume frozen plans, but new submissions never add
# optional text-skill branches; long-read now runs article-decode only.
NEW_SUBMISSION_LJG = frozenset()


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _reject_constant(_):
    raise ValueError("non-finite JSON is not supported")


def _object(payload: bytes, label: str) -> dict:
    value = json.loads(payload.decode("utf-8"), parse_constant=_reject_constant)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _read_regular(path: Path) -> bytes:
    path = Path(path)
    _reject_symlink_path(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("input must be a regular file")
        value = handle.read()
    if not value.strip():
        raise ValueError("input files must not be empty")
    return value


def _reject_symlink_path(path: Path):
    # macOS exposes its real temporary roots through these platform aliases.
    for part in (path.absolute(), *path.absolute().parents):
        if part.is_symlink() and part not in {Path("/tmp"), Path("/var")}:
            raise ValueError("input and state paths must not traverse symlinks")


def _private_directory(path: Path):
    if path.is_symlink():
        raise ValueError("private directories must not be symlinks")
    if not path.exists():
        if not path.parent.exists():
            _private_directory(path.parent)
        try:
            path.mkdir(mode=0o700)
            _fsync_directory(path.parent)
        except FileExistsError:
            pass
    if path.is_symlink() or not path.is_dir():
        raise ValueError("private state path is not a directory")
    os.chmod(path, 0o700)


def _fsync_directory(path: Path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_private(path: Path, value: bytes):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())


def _verify_snapshot(directory: Path, files: dict[str, bytes], manifest: bytes):
    if directory.is_symlink() or not directory.is_dir() or directory.stat().st_mode & 0o777 != 0o700:
        raise ValueError("snapshot directory is missing or unsafe")
    expected = {**files, "manifest.json": manifest}
    if {path.name for path in directory.iterdir()} != set(expected):
        raise ValueError("snapshot file set has changed")
    for name, value in expected.items():
        path = directory / name
        if path.stat().st_mode & 0o777 != 0o600 or _read_regular(path) != value:
            raise ValueError("immutable snapshot content or permissions changed")


def _snapshot_has_job_reference(state_dir: Path, digest: str) -> bool:
    """Return true on uncertainty so cleanup can never remove admitted input."""
    db = state_dir / "workflow.sqlite3"
    try:
        _reject_symlink_path(db)
        connection = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
        try:
            for row in connection.execute("SELECT payload_json FROM jobs"):
                payload = json.loads(row[0], parse_constant=_reject_constant)
                if not isinstance(payload, dict):
                    return True
                if payload.get("snapshot_sha256") == digest:
                    return True
            return False
        finally:
            connection.close()
    except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError):
        return True


def _validate_test_task_marker(context: dict, test_task: bool) -> bool:
    readx_canary = context.get("readxCanary", False)
    if not isinstance(readx_canary, bool) or test_task is not readx_canary:
        raise ValueError("test-task flag must match the trusted bridge Read-X canary marker")
    return readx_canary


def _run_lark_cli_json(arguments: list[str], *, profile: str | None = None) -> object:
    """Run a bounded, read-only lark-cli call without surfacing its output on failure."""
    command = ["lark-cli"]
    if profile is not None:
        if not isinstance(profile, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", profile):
            raise ValueError("Feishu profile is invalid")
        command.extend(["--profile", profile])
    command.extend(arguments)
    env = os.environ.copy()
    env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    try:
        result = subprocess.run(
            command, capture_output=True, text=True,
            timeout=20, check=False, env=env,
        )
        value = json.loads(result.stdout) if result.stdout.strip() else None
    except (OSError, subprocess.SubprocessError, UnicodeError, json.JSONDecodeError):
        raise ValueError("Feishu identity or source readback is unavailable") from None
    if result.returncode != 0 or (
            isinstance(value, dict) and value.get("ok") is False):
        raise ValueError("Feishu identity or source readback is unavailable")
    return value


def resolve_worker_profile_key(*, app_id: str, bridge_profile_key: str) -> str:
    """Resolve a local lark-cli profile by the Bridge-bound app and verify it explicitly."""
    if not isinstance(app_id, str) or not app_id or not isinstance(bridge_profile_key, str) or not bridge_profile_key.strip():
        raise ValueError("Bridge app/profile identity is missing or inconsistent")
    profiles = _run_lark_cli_json(["profile", "list"])
    matches = [item for item in profiles if isinstance(item, dict)
               and item.get("appId") == app_id
               and isinstance(item.get("name"), str)
               and item["name"].strip()] if isinstance(profiles, list) else []
    if len(matches) != 1:
        raise ValueError("Bridge-bound Feishu app does not resolve to one local lark-cli profile")
    worker_profile = matches[0]["name"]
    identity = _run_lark_cli_json(["whoami"], profile=worker_profile)
    if (not isinstance(identity, dict) or identity.get("identity") != "user"
            or identity.get("appId") != app_id or identity.get("profile") != worker_profile):
        raise ValueError("Bridge-bound Feishu identity does not match the configured app")
    return worker_profile


def _canonical_http_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().rstrip(".,;:!?，。；：！？）》】")
    try:
        parsed = urlsplit(candidate)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        netloc = parsed.netloc.lower()
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))
    except ValueError:
        return None


def verify_canary_source(*, profile: str, app_id: str, context: dict,
                         source_message_id: str, evidence: dict) -> dict:
    """Read back the exact Feishu source event before excluding it from production metrics."""
    if (not isinstance(context, dict)
            or any(not isinstance(context.get(key), str) or not context[key].strip()
                   for key in ("chatId", "senderId"))
            or context.get("readxCanary") is not True or context.get("chatType") != "group"
            or context.get("senderType") != "user"
            or context.get("sourceMessageId") != source_message_id):
        raise ValueError("Read-X canary source context is incomplete")

    identity = _run_lark_cli_json(["whoami"], profile=profile)
    on_behalf_of = identity.get("onBehalfOf") if isinstance(identity, dict) else None
    current_user = on_behalf_of.get("openId") if isinstance(on_behalf_of, dict) else None
    if (not isinstance(identity, dict) or identity.get("identity") != "user"
            or identity.get("appId") != app_id or current_user != context.get("senderId")):
        raise ValueError("Feishu source reader does not match the Bridge app and sender")

    chat_response = _run_lark_cli_json([
        "im", "chats", "get", "--as", "user", "--chat-id", context["chatId"],
    ], profile=profile)
    chat_data = chat_response.get("data") if isinstance(chat_response, dict) else None
    if (not isinstance(chat_response, dict) or chat_response.get("ok") is not True
            or not isinstance(chat_data, dict) or chat_data.get("chat_mode") != "group"):
        raise ValueError("Read-X canary source chat is not a verified group")

    response = _run_lark_cli_json([
        "im", "+messages-mget", "--as", "user",
        "--message-ids", source_message_id, "--no-reactions",
    ], profile=profile)
    data = response.get("data") if isinstance(response, dict) else None
    messages = data.get("messages") if isinstance(data, dict) else None
    if (not isinstance(response, dict) or response.get("ok") is not True
            or not isinstance(messages, list) or len(messages) != 1):
        raise ValueError("Read-X canary source message could not be uniquely verified")
    message = messages[0]
    sender = message.get("sender") if isinstance(message, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if (not isinstance(sender, dict) or not isinstance(content, str)
            or not is_valid_source_created_at(message.get("create_time"))
            or message.get("message_id") != source_message_id
            or message.get("chat_id") != context.get("chatId")
            or message.get("msg_type") != "text"
            or message.get("deleted") is not False
            or sender.get("sender_type") != "user"
            or sender.get("id") != context.get("senderId")
            or not content.startswith(CANARY_PREFIX)):
        raise ValueError("Read-X canary source does not match the original user event")

    source_urls = {
        normalized
        for match in CANARY_URL_PATTERN.findall(content)
        if (normalized := _canonical_http_url(match)) is not None
    }
    evidence_metadata = evidence.get("metadata") if isinstance(evidence, dict) else None
    source_url = _canonical_http_url(
        evidence_metadata.get("source_url") if isinstance(evidence_metadata, dict) else None
    )
    if len(source_urls) != 1 or source_url is None or source_url not in source_urls:
        raise ValueError("Read-X canary article URL does not match the Feishu source event")

    url_bytes = source_url.encode("utf-8")
    content_bytes = content.encode("utf-8")
    return {
        "schema": "readx-canary-source-proof/v1",
        "method": "lark-cli.im.messages-mget",
        "source_created_at": message["create_time"],
        "app_id": app_id,
        "profile": profile,
        "message_id": source_message_id,
        "chat_id": context["chatId"],
        "sender_id": context["senderId"],
        "source_url_sha256": _hash(url_bytes),
        "source_content_sha256": _hash(content_bytes),
    }


def _validate_target(context: dict, gate: dict, app_id: str, source_message_id: str):
    if context.get("appId") != app_id:
        raise ValueError("trigger app identity does not match app-id")
    if context.get("sourceMessageId") != source_message_id:
        raise ValueError("source message id does not match trusted trigger context")
    if gate.get("chat_id") and gate.get("user_id"):
        raise ValueError("score gate has multiple delivery targets")
    if context.get("senderType") == "bot":
        target, key = context.get("chatId"), "chat_id"
    elif context.get("chatType") == "p2p":
        target, key = context.get("chatId"), "chat_id"
    elif context.get("chatType") == "group":
        target, key = context.get("senderId"), "user_id"
    else:
        raise ValueError("trigger chat type and sender type are not supported")
    if not target or gate.get(key) != target:
        raise ValueError("score gate does not match the trigger delivery target")


def submit_inputs(*, source: Path, evidence: Path, scoring_result: Path, score_gate: Path,
                  trigger_context: Path, app_id: str, feishu_profile: str,
                  source_message_id: str,
                  questions: list[tuple[str, Path]] | None = None,
                  test_task: bool = False,
                  profile_resolver=resolve_worker_profile_key,
                  canary_verifier=verify_canary_source,
                  state_dir: Path = DEFAULT_STATE_DIR) -> dict:
    """Copy immutable inputs first; only then commit admission to SQLite."""
    questions = questions or []
    if not isinstance(test_task, bool):
        raise ValueError("test_task must be a boolean")
    if not isinstance(source_message_id, str) or not source_message_id.startswith("om_"):
        raise ValueError("submission requires a trusted Feishu source message id")
    if questions:
        names = sorted({name for name, _ in questions})
        raise ValueError(
            f"text skill questions are removed for new Read-X long-read submissions: {', '.join(names)}"
        )
    files = {
        "source.md": _read_regular(source), "evidence.json": _read_regular(evidence),
        "scoring-result.json": _read_regular(scoring_result), "score-gate.json": _read_regular(score_gate),
        "trigger-context.json": _read_regular(trigger_context),
    }
    scoring = _object(files["scoring-result.json"], "scoring result")
    if scoring.get("score_status") != "scored" or scoring.get("route") != "long_read":
        raise ValueError("submission requires score_status=scored and route=long_read")
    validate_scoring_fields(scoring)
    context = _object(files["trigger-context.json"], "trigger context")
    if not isinstance(feishu_profile, str) or not feishu_profile.strip():
        raise ValueError("submission requires an explicit Feishu profile")
    if context.get("feishuProfile") != feishu_profile:
        raise ValueError("Feishu profile does not match trusted trigger context")
    readx_canary = _validate_test_task_marker(context, test_task)
    validate_evidence(files["evidence.json"].decode("utf-8"), files["source.md"].decode("utf-8"))
    state_dir = Path(state_dir).absolute()
    # Do not resolve away a symlink in the caller's state directory.
    _reject_symlink_path(state_dir)
    _private_directory(state_dir)
    artifacts_dir = state_dir / "artifacts"
    _private_directory(artifacts_dir)
    # Identity resolution and optional CANARY source readback call lark-cli.
    # Keep those bounded remote reads outside the shared filesystem/DB lock;
    # only snapshot admission and the SQLite commit need serialization.
    cli_profile = profile_resolver(app_id=app_id, bridge_profile_key=feishu_profile)
    canary_source_proof = None
    if readx_canary:
        canary_source_proof = canary_verifier(
            profile=cli_profile, app_id=app_id, context=context,
            source_message_id=source_message_id,
            evidence=_object(files["evidence.json"], "evidence"),
        )
    snapshot_lock = state_lock(state_dir)
    snapshot_lock.__enter__()
    temporary = None
    snapshot = None
    snapshot_created = False
    try:
        temporary = Path(tempfile.mkdtemp(prefix=".incoming-", dir=artifacts_dir))
        for filename, value in files.items():
            _write_private(temporary / filename, value)
        # Reuse the actual delivery-card gate against the copied exact bytes;
        # validation never races a caller changing its original scoring file.
        gate = _load_score_evidence(temporary / "score-gate.json", temporary / "scoring-result.json")
        _validate_target(context, gate, app_id, source_message_id)
        from long_read_workflow_notifications import resolve_delivery_target
        trigger = {**context, "appId": app_id, "larkCliProfile": cli_profile}
        with WorkflowStore(state_dir) as target_store:
            delivery_target = resolve_delivery_target(trigger, target_store)
        if delivery_target is None:
            raise ValueError("submission requires a verified private message delivery target")
        if context.get("chatType") == "group":
            # Pin the exact verified P2P chat in the durable task so later
            # report-target changes cannot redirect an already accepted task.
            context = {**context, "privateChatId": delivery_target[1]}
        manifest = _canonical({name: {"sha256": _hash(value), "bytes": len(value)} for name, value in sorted(files.items())})
        digest = _hash(manifest)
        snapshot = artifacts_dir / digest
        _write_private(temporary / "manifest.json", manifest)
        _fsync_directory(temporary)
        if snapshot.exists() or snapshot.is_symlink():
            _verify_snapshot(snapshot, files, manifest)
        else:
            try:
                os.rename(temporary, snapshot)
                snapshot_created = True
            except OSError as exc:
                if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise
                _verify_snapshot(snapshot, files, manifest)
            _fsync_directory(artifacts_dir)
        _verify_snapshot(snapshot, files, manifest)
        payload = {
            "schema": "long-read-submission/v1", "snapshot_sha256": digest,
            "workflow_contract_version": WORKFLOW_CONTRACT_VERSION,
            "inputs": {name: {"path": str(snapshot / name), "sha256": _hash(value)} for name, value in sorted(files.items())},
            "route": "long_read", "score_status": "scored",
            "bridge_new_chat_url": "https://chatgpt.com/",
            "trigger": {
                **{key: context[key] for key in ("profile", "feishuProfile", "appId", "chatType", "chatId", "privateChatId", "senderId", "senderType", "sourceMessageId") if key in context},
                "larkCliProfile": cli_profile,
                "identity": "bot",
                "readxCanary": readx_canary,
            },
            "test_task": test_task,
            **({"canary_source_proof": canary_source_proof} if canary_source_proof else {}),
            "artifact_plan_finalized": False,
        }
        payload_issue = workflow_payload_issue(
            payload, app_id=app_id, source_message_id=source_message_id,
        )
        if payload_issue:
            raise ValueError(f"submission payload failed workflow validation: {payload_issue}")
        with WorkflowStore(state_dir) as store:
            job = store.submit(payload, app_id=app_id, source_message_id=source_message_id,
                               artifacts=[
                                   {"key": "analysis:article-decode", "kind": "analysis"},
                                   {"key": "analysis:munger-analysis", "kind": "analysis"},
                                   {"key": "analysis:document-supplement", "kind": "analysis"},
                                   {"key": "main-document", "kind": "document"},
                                   {"key": "document-notification", "kind": "message"},
                                   {"key": "content-review-notification", "kind": "message", "required": False},
                                   {"key": "document-publish-review-notification", "kind": "message", "required": False},
                                   {"key": "final-notification", "kind": "message"},
                                   {"key": "core-image", "kind": "image"},
                                   {"key": "core-image-insert", "kind": "image_insert"},
                                   {"key": "core-image-send", "kind": "message"},
                                   {"key": "core-image-recovery-notification", "kind": "message", "required": False},
                                   {"key": "munger-image", "kind": "image", "required": False},
                                   {"key": "munger-image-insert", "kind": "image_insert", "required": False},
                                   {"key": "munger-image-send", "kind": "message", "required": False},
                                   {"key": "munger-image-recovery-notification", "kind": "message", "required": False},
                               ])
        return {"status": "accepted", "job_id": job["job_id"], "created": job["created"],
                "job_status": job["status"], "input_sha256": job["input_sha256"],
                "snapshot_sha256": digest, "snapshot_dir": str(snapshot),
                "test_task": test_task, "delivery_complete": False}
    finally:
        try:
            if temporary is not None and temporary.exists():
                shutil.rmtree(temporary)
        finally:
            try:
                if (snapshot_created and snapshot is not None and snapshot.exists()
                        and not snapshot.is_symlink() and not _snapshot_has_job_reference(state_dir, digest)):
                    shutil.rmtree(snapshot)
                    _fsync_directory(artifacts_dir)
            finally:
                snapshot_lock.__exit__(None, None, None)


def submit_context_inputs(**options) -> dict:
    """Persist a non-content admission receipt before preparation can fail."""
    if options.get("score_only") or options.get("special_fast_path"):
        return _submit_context_inputs(**options)
    app_id, source_id = options.get("app_id"), options.get("source_message_id")
    if (not isinstance(app_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", app_id)
            or not isinstance(source_id, str) or not re.fullmatch(r"om_[A-Za-z0-9_-]{1,150}", source_id)):
        raise ValueError("context admission requires a valid trusted task identity")
    state_dir = Path(options.get("state_dir", DEFAULT_STATE_DIR)).absolute()
    _reject_symlink_path(state_dir)
    task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"read-x:context:{app_id}:{source_id}"))
    fields = {"task_id": task_id, "app_id": app_id, "source_message_id": source_id}
    with WorkflowStore(state_dir) as store:
        store.record_context_admission(**fields, status="preparing")
    try:
        result = _submit_context_inputs(**options)
    except Exception as exc:
        safe_reasons = {"context packages require a complete source": "context-source-incomplete",
                        "context packages require a nonempty source": "context-source-empty",
                        "input files must not be empty": "context-input-empty",
                        "source message already submitted with different context input": "context-input-conflict"}
        reason = safe_reasons.get(str(exc), f"context-admission-{type(exc).__name__.lower()}")
        with WorkflowStore(state_dir) as store:
            store.record_context_admission(**fields, status="blocked", reason_code=reason)
        error = StoreConflict(reason) if isinstance(exc, StoreConflict) else ValueError(reason)
        error.task_id = task_id
        raise error from None
    with WorkflowStore(state_dir) as store:
        store.record_context_admission(**fields, status="accepted")
    return result


def _submit_context_inputs(*, source: Path, evidence: Path, trigger_context: Path,
                          title: str, title_source: str, source_status: str,
                          app_id: str, feishu_profile: str, source_message_id: str,
                          route: str = "context", score_only: bool = False,
                          special_fast_path: bool = False, context_pack_dir: Path | None = None,
                          prompt_fetcher=None, profile_resolver=resolve_worker_profile_key,
                          state_dir: Path = DEFAULT_STATE_DIR) -> dict:
    """Freeze business bytes and a context-only plan, without any model submission."""
    from long_read_context_packs import context_result
    import run_isolated_analyses as isolated
    import run_chatgpt_munger as munger
    from long_read_prompt_contract import CONCISION_PROMPT_PATH, load_concision_prompt

    if score_only or special_fast_path:
        return {"status": "not_requested", "context_packages": {"required": 0, "completed": 0},
                "reason_code": "score-only" if score_only else "special-fast-path"}
    if source_status != "complete":
        raise ValueError("context packages require a complete source")
    if (not isinstance(title, str) or not title.strip() or len(title) > 200
            or any(ord(char) < 32 for char in title) or title_source not in {"original", "agent"}
            or route not in {"context", "card", "long_read"}):
        raise ValueError("context package title, title source or route is invalid")
    files = {"source.md": _read_regular(source), "evidence.json": _read_regular(evidence),
             "trigger-context.json": _read_regular(trigger_context)}
    source_text = files["source.md"].decode("utf-8")
    if not source_text.strip():
        raise ValueError("context packages require a nonempty source")
    evidence_text = validate_evidence(files["evidence.json"].decode("utf-8"), source_text)
    context = _object(files["trigger-context.json"], "trigger context")
    if (context.get("appId") != app_id or context.get("sourceMessageId") != source_message_id
            or not source_message_id.startswith("om_") or context.get("feishuProfile") != feishu_profile
            or context.get("senderType") != "user"):
        raise ValueError("context packages require trusted matching trigger identity")
    state_dir = Path(state_dir).absolute()
    _reject_symlink_path(state_dir)
    admission_sha = _hash(_canonical({"files": {key: _hash(value) for key, value in files.items()},
                                     "title": title, "title_source": title_source, "route": route,
                                     "context_pack_dir": str(context_pack_dir.absolute()) if context_pack_dir else None}))

    def existing_job(store):
        row = store.connection.execute("SELECT job_id FROM jobs WHERE app_id=? AND source_message_id=?",
                                       (app_id, source_message_id)).fetchone()
        if not row:
            return None
        job = store.get_job(row["job_id"])
        if (not job.get("payload") or job["payload"].get("workflow_contract_version") != CONTEXT_WORKFLOW_CONTRACT_VERSION
                or job["payload"].get("admission_sha256") != admission_sha):
            raise StoreConflict("source message already submitted with different context input")
        return {"status": "accepted", "job_id": job["job_id"], "created": False,
                **context_result(job, store.artifacts(job["job_id"]))}

    with WorkflowStore(state_dir) as store:
        prior = existing_job(store)
        if prior:
            return prior
    profile = profile_resolver(app_id=app_id, bridge_profile_key=feishu_profile)
    from long_read_workflow_notifications import resolve_delivery_target
    trigger = {**context, "larkCliProfile": profile, "identity": "bot"}
    with WorkflowStore(state_dir) as store:
        target = resolve_delivery_target(trigger, store)
    if target is None:
        raise ValueError("context packages require a verified private delivery target")
    if trigger.get("chatType") == "group":
        trigger["privateChatId"] = target[1]
    article_raw = _read_regular(isolated.ARTICLE_SKILL)
    concision_raw = _read_regular(CONCISION_PROMPT_PATH)
    concision = load_concision_prompt()
    asset = (prompt_fetcher or munger.fetch_prompt_asset)("read-x.munger-analysis")
    if (not isinstance(asset, dict) or asset.get("prompt_id") != "read-x.munger-analysis"
            or _hash(str(asset.get("content", "")).encode("utf-8")) != asset.get("prompt_sha256")):
        raise ValueError("Munger Prompt asset hash is invalid")
    if any(key not in asset for key in ("prompt_source", "prompt_revision", "prompt_fetched_at")):
        raise ValueError("Munger Prompt asset provenance is incomplete")
    article_task = isolated.prepare_tasks([], state_dir / ".context-prompt-staging")[0]
    if article_task.skill_sha256 != _hash(article_raw):
        raise ValueError("article Skill changed while freezing the task")
    files["article-decode.prompt.txt"] = isolated.build_business_prompt(
        article_task, source_text, evidence_text, concision_prompt=concision).encode("utf-8")
    files["munger-analysis.prompt.txt"] = munger.build_business_prompt(source_text, [asset], concision).encode("utf-8")
    files["prompt-assets.json"] = _canonical({
        "article-decode": {"prompt_id": "article-decode", "prompt_source": str(isolated.ARTICLE_SKILL),
                           "prompt_revision": _hash(article_raw), "prompt_sha256": _hash(article_raw),
                           "content": article_raw.decode("utf-8")},
        "concision": {"prompt_source": str(CONCISION_PROMPT_PATH), "prompt_sha256": _hash(concision_raw),
                      "content": concision_raw.decode("utf-8")}, "munger-analysis": asset})
    task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"read-x:context:{app_id}:{source_message_id}"))
    input_sha = _hash(_canonical({"source": _hash(files["source.md"]), "evidence": _hash(files["evidence.json"])}))
    packs = [{"artifact_key": key, "pack_id": str(uuid.uuid5(uuid.UUID(task_id), key)),
              "prompt_file": filename, "prompt_sha256": _hash(files[filename]),
              "input_sha256": input_sha if index == 0 else _hash(files["source.md"]),
              "pack_type": "文章深层解码" if index == 0 else "芒格洞察",
              "prompt_asset": {"promptId": "article-decode" if index == 0 else asset["prompt_id"],
                               "revision": _hash(article_raw) if index == 0 else asset["prompt_revision"],
                               "sha256": _hash(article_raw) if index == 0 else asset["prompt_sha256"]}}
             for index, (key, filename) in enumerate(zip(CONTEXT_ARTIFACTS,
                 ("article-decode.prompt.txt", "munger-analysis.prompt.txt")))]
    with state_lock(state_dir):
        with WorkflowStore(state_dir) as store:
            prior = existing_job(store)
            if prior:
                return prior
            artifacts_dir = state_dir / "artifacts"
            _private_directory(artifacts_dir)
            manifest = _canonical({name: {"sha256": _hash(value), "bytes": len(value)} for name, value in sorted(files.items())})
            digest = _hash(manifest)
            snapshot = artifacts_dir / digest
            temporary = Path(tempfile.mkdtemp(prefix=".incoming-", dir=artifacts_dir))
            try:
                for name, value in files.items():
                    _write_private(temporary / name, value)
                _write_private(temporary / "manifest.json", manifest)
                _fsync_directory(temporary)
                if not snapshot.exists():
                    os.rename(temporary, snapshot)
                    _fsync_directory(artifacts_dir)
                _verify_snapshot(snapshot, files, manifest)
                payload = {"schema": "read-x-context-submission/v1", "workflow_contract_version": CONTEXT_WORKFLOW_CONTRACT_VERSION,
                           "admission_sha256": admission_sha, "snapshot_sha256": digest,
                           "inputs": {name: {"path": str(snapshot / name), "sha256": _hash(value)} for name, value in files.items()},
                           "title": title, "title_source": title_source, "source_status": source_status, "route": route,
                           "trigger": trigger, "requested_automatic": [], "required_artifacts": list(CONTEXT_ARTIFACTS),
                           "artifact_plan_finalized": True, "context_packs": packs,
                           "context_pack_dir": str(context_pack_dir.absolute()) if context_pack_dir else None}
                issue = workflow_payload_issue(payload, app_id=app_id, source_message_id=source_message_id)
                if issue:
                    raise ValueError(f"context submission failed workflow validation: {issue}")
                job = store.submit(payload, app_id=app_id, source_message_id=source_message_id, job_id=task_id,
                                   artifacts=[{"key": key, "kind": "context_pack"} for key in CONTEXT_ARTIFACTS])
                return {"status": "accepted", "job_id": job["job_id"], "created": job["created"],
                        **context_result(job, store.artifacts(job["job_id"]))}
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)


def inspect_jobs(state_dir: Path = DEFAULT_STATE_DIR, *, job_id: str | None = None,
                 chat_id: str | None = None, report_key: str | None = None,
                 resume: bool = False) -> dict:
    """Read-only candidate listing. Never acquire leases or change effect state."""
    state_dir = Path(state_dir).absolute()
    if report_key and (job_id or chat_id):
        raise ValueError("report status cannot be combined with job or chat status")
    db = state_dir / "workflow.sqlite3"
    _reject_symlink_path(db)
    if not db.exists():
        return {"status": "not_initialized", "jobs": [], "execution_performed": False}
    connection = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        if report_key:
            report = connection.execute(
                "SELECT report_key,period_type,window_start,window_end,status,created_at,"
                "snapshot_sha256,markdown_sha256 FROM reports WHERE report_key=?", (report_key,),
            ).fetchone()
            if not report:
                return {"status": "report_not_found", "report_key": report_key,
                        "execution_performed": False}
            outbox = []
            for row in connection.execute(
                    "SELECT artifact_key,status,attempts,remote_id,metadata_json "
                    "FROM report_outbox WHERE report_key=? ORDER BY artifact_key", (report_key,)):
                metadata = json.loads(row["metadata_json"])
                outbox.append({
                    "artifact_key": row["artifact_key"], "status": row["status"],
                    "attempts": row["attempts"], "remote_result_known": bool(row["remote_id"]),
                    "metadata": metadata,
                })
            report_value = dict(report)
            report_value["outbox"] = outbox
            report_value["delivery_pending"] = report_value["status"] == "pending_delivery"
            return {"status": "report_status", "report": report_value,
                    "execution_performed": False}
        where, params = (" WHERE job_id=?", (job_id,)) if job_id else ("", ())
        rows = connection.execute("SELECT job_id,status,version,created_at,updated_at,lease_expires_at,payload_json FROM jobs" + where + " ORDER BY created_at,job_id", params).fetchall()
        if not rows and job_id and connection.execute("SELECT 1 FROM sqlite_master WHERE name='admission_attempts'").fetchone():
            attempt = connection.execute("SELECT * FROM admission_attempts WHERE task_id=?", (job_id,)).fetchone()
            if attempt:
                admission = dict(attempt)
                admission["requested_plan"] = json.loads(admission.pop("requested_plan_json"))
                return {"status": "admission_status", "admission": admission, "jobs": [], "execution_performed": False}
        jobs = []
        chat_nonterminal_count = 0
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            if chat_id is not None:
                if not isinstance(payload, dict) or payload.get("trigger", {}).get("chatId") != chat_id:
                    continue
                if row["status"] in {"succeeded", "failed"}:
                    continue
                chat_nonterminal_count += 1
                continue
            if resume and row["status"] in {"succeeded", "failed"}:
                continue
            job = dict(row)
            job.pop("payload_json", None)
            job["artifacts"] = [dict(item) for item in connection.execute("SELECT artifact_key,kind,required,status,retry_count FROM artifacts WHERE job_id=? ORDER BY artifact_key", (row["job_id"],))]
            effects = [dict(item) for item in connection.execute("SELECT op_key,artifact_key,action,status,attempts FROM effects WHERE job_id=? AND status IN ('attempting','uncertain') ORDER BY created_at,op_key", (row["job_id"],))]
            job["unresolved_effects"] = effects
            if isinstance(payload, dict) and payload.get("workflow_contract_version") == CONTEXT_WORKFLOW_CONTRACT_VERSION:
                from long_read_context_packs import context_result
                artifacts = []
                for item in connection.execute("SELECT * FROM artifacts WHERE job_id=? ORDER BY artifact_key", (row["job_id"],)):
                    artifacts.append(WorkflowStore._decode(item))
                job.update(context_result(job, artifacts))
            if resume:
                active = row["lease_expires_at"] is not None and row["lease_expires_at"] > time.time()
                job["recovery_action"] = "wait_for_lease" if active else "reconcile_only" if effects else "continue_from_checkpoint"
            jobs.append(job)
        if chat_id is not None:
            return {"status": "chat_status", "chat_id": chat_id,
                    "has_nonterminal": chat_nonterminal_count > 0,
                    "nonterminal_count": chat_nonterminal_count, "execution_performed": False}
        return {"status": "recovery_candidates" if resume else "status", "jobs": jobs, "execution_performed": False}
    finally:
        connection.close()


def worker_service_tick(state_dir: Path, *, owner: str, report_target: dict, include_patrol: bool = True) -> dict:
    """Run scheduled report delivery and advance one job in the same worker."""
    from long_read_workflow_scheduler import run_due_reports
    from long_read_workflow_worker import run_once

    # New context tasks get an independent turn before legacy reports or recovery.
    context_job = run_once(Path(state_dir), owner=owner, context_only=True)
    if context_job.get("status") != "idle":
        result = {"status": "tick", "reports": {"status": "deferred", "reason_code": "context-task-priority"},
                  "worker": context_job}
        patrol = _voice_x_patrol_tick() if include_patrol else None
        if patrol is not None:
            result["voice_x_patrol"] = patrol
        return result
    database = Path(state_dir) / "workflow.sqlite3"
    unadmitted = []
    if database.is_file():
        with WorkflowStore(Path(state_dir)) as store:
            unadmitted = store.unadmitted_nonterminal_jobs()
    try:
        reports = run_due_reports(Path(state_dir), delivery_target=report_target)
    except Exception:
        reports = {"status": "needs_review", "reason_code": "legacy-report-unavailable"}
    job = run_once(Path(state_dir), owner=owner, outcome_recorder=record_business_outcome)
    result = {"status": "tick", "reports": reports, "worker": job}
    if unadmitted:
        result["legacy_jobs_skipped"] = len(unadmitted)
    patrol = _voice_x_patrol_tick() if include_patrol else None
    if patrol is not None:
        result["voice_x_patrol"] = patrol
    return result


def _voice_x_patrol_tick() -> dict | None:
    """Kick one short Voice-X supervisor tick only behind an explicit opt-in."""
    if os.environ.get("VOICE_X_PATROL") != "1":
        return None
    voice_x_root = REPO_ROOT.parent / "voice-x"
    script = voice_x_root / ".agents" / "skills" / "voice-x" / "scripts" / "voice-x.mjs"
    try:
        completed = subprocess.run(
            ["node", str(script), "supervise"], cwd=voice_x_root,
            capture_output=True, text=True, timeout=10, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "reason": "supervisor-timeout"}
    except OSError:
        return {"status": "blocked", "reason": "supervisor-launch-failed"}
    except Exception:
        return {"status": "failed", "reason": "supervisor-error"}

    payload = None
    for line in reversed(completed.stdout.splitlines()):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and isinstance(candidate.get("status"), str):
            payload = candidate
            break
    if payload is None:
        return {"status": "failed", "reason": "supervisor-result-unavailable"}
    result = {"status": payload["status"]}
    for key in ("reason", "runId", "recordId", "generation"):
        if key in payload and isinstance(payload[key], (str, int)):
            result[key] = payload[key]
    if completed.returncode != 0 and result["status"] not in {"blocked", "idle"}:
        return {"status": "failed", "reason": "supervisor-nonzero-exit"}
    return result


def _start_voice_x_patrol_scheduler(interval_seconds: int):
    """Run patrol independently of synchronous Read-X/report work in serve mode."""
    if os.environ.get("VOICE_X_PATROL") != "1":
        return None
    stop_event = threading.Event()

    def patrol_loop() -> None:
        while not stop_event.is_set():
            result = _voice_x_patrol_tick()
            if result is not None:
                print(json.dumps({"voice_x_patrol": result}, ensure_ascii=False), flush=True)
            if stop_event.wait(interval_seconds):
                break

    thread = threading.Thread(target=patrol_loop, name="voice-x-patrol", daemon=True)
    thread.start()
    return stop_event, thread


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit", help="校验并持久接管输入，然后立即推进一次 Worker")
    for name in ("source", "evidence", "scoring-result", "score-gate", "trigger-context"):
        submit.add_argument("--" + name, type=Path, required=True)
    submit.add_argument("--app-id", required=True)
    submit.add_argument("--feishu-profile", required=True, help="Bridge-provided Feishu profile value; preserved as source context")
    submit.add_argument("--source-message-id", required=True, help="trusted source Feishu message id for event deduplication")
    submit.add_argument("--test-task", action="store_true",
                        help="仅对显式 CANARY/验收任务启用；从正式成功率统计中排除")
    context_submit = commands.add_parser("submit-context-packs", help="冻结完整正文与双业务 Prompt；不提交 ChatGPT")
    for name in ("source", "evidence", "trigger-context"):
        context_submit.add_argument("--" + name, type=Path, required=True)
    for name in ("app-id", "feishu-profile", "source-message-id", "title"):
        context_submit.add_argument("--" + name, required=True)
    context_submit.add_argument("--title-source", choices=("original", "agent"), default="agent")
    context_submit.add_argument("--source-status", choices=("complete", "partial", "unknown"), required=True)
    context_submit.add_argument("--route", choices=("context", "card", "long_read"), default="context")
    context_submit.add_argument("--score-only", action="store_true")
    context_submit.add_argument("--special-fast-path", action="store_true")
    context_submit.add_argument("--context-pack-dir", type=Path)
    status = commands.add_parser("status")
    status.add_argument("--chat-id", help="只读查询该会话的持久工作流终态")
    status.add_argument("--report-key", help="只读查看可靠性报告和附件/卡片投递收据")
    resume = commands.add_parser("resume", help="恢复指定任务的已持久阶段；--document-id 仅用于主文档丢响应后的只读核验")
    worker = commands.add_parser("run-worker", help="推进长文任务；--serve 启动含报告调度的常驻 Worker")
    report = commands.add_parser("report", help="生成或复用固定周期的可靠性报告快照")
    report.add_argument("--period", required=True, choices=("weekly", "monthly"))
    report.add_argument("--now", help="可控时钟，ISO-8601；默认当前时间")
    report.add_argument("--deliver", action="store_true", help="发送待交付报告；需要显式提供可信目标")
    report.add_argument("--profile", help="可选断言；必须与已保存的投递目标一致")
    report.add_argument("--user-id", help="可选断言；必须与已保存的投递目标一致")
    report.add_argument("--chat-id", help="可选断言；必须与已保存的投递目标一致")
    report.add_argument("--sender-id", help="可选断言；必须与已保存的投递目标一致")
    due_reports = commands.add_parser("run-due-reports", help="补跑持久追踪开始后的周报和月报周期")
    due_reports.add_argument("--now", help="可控时钟，ISO-8601；默认当前时间")
    due_reports.add_argument("--deliver", action="store_true", help="发送到显式配置的本人私聊")
    due_reports.add_argument("--profile")
    due_reports.add_argument("--user-id")
    due_reports.add_argument("--chat-id")
    due_reports.add_argument("--sender-id")
    configure_target = commands.add_parser("configure-report-target", help="持久设置周/月报告的本人私聊收件目标")
    configure_target.add_argument("--profile", required=True)
    configure_target.add_argument("--user-id", required=True)
    configure_target.add_argument("--chat-id", required=True)
    configure_target.add_argument("--sender-id", required=True)
    configure_target.add_argument("--replace", action="store_true", help="替换已有目标")
    record_event = commands.add_parser("record-event", help="追加经字段校验的人工处置或修复证据")
    record_event.add_argument("--job-id", required=True)
    record_event.add_argument("--event-type", required=True, choices=(
        "manual_intervention", "manual_reconciled", "manual_repair",
        "repair_applied", "root_cause_fixed", "repair_comparison",
    ))
    record_event.add_argument("--details-json", required=True,
                              help="符合该事件类型字段契约的 JSON 对象；禁止包含文章正文或凭据")
    for command in (submit, context_submit, status, resume, worker, report, due_reports, configure_target, record_event):
        command.add_argument("--state-dir", type=Path, default=argparse.SUPPRESS)
        if command is status:
            command.add_argument("--job-id")
    resume.add_argument("--job-id", required=True)
    resume.add_argument("--document-id", help="人工提供首次创建时返回丢失的飞书文档 ID；只读比对本地期望结构")
    worker.add_argument("--job-id")
    worker.add_argument("--owner", default="read-x-worker")
    worker.add_argument("--serve", action="store_true", help="常驻轮询任务并检查周/月报告")
    worker.add_argument("--poll-seconds", type=float, default=15)
    worker.add_argument("--report-profile")
    worker.add_argument("--report-user-id")
    worker.add_argument("--report-chat-id")
    worker.add_argument("--report-sender-id")
    args = parser.parse_args(argv)
    try:
        if args.command == "submit":
            result = submit_inputs(source=args.source, evidence=args.evidence, scoring_result=args.scoring_result,
                                   score_gate=args.score_gate, trigger_context=args.trigger_context, app_id=args.app_id,
                                   feishu_profile=args.feishu_profile,
                                   source_message_id=args.source_message_id,
                                   test_task=args.test_task,
                                   profile_resolver=resolve_worker_profile_key,
                                   canary_verifier=verify_canary_source,
                                   state_dir=args.state_dir)
            from long_read_workflow_worker import run_once
            result["worker"] = run_once(
                args.state_dir, job_id=result["job_id"], owner="read-x-admission-worker",
                outcome_recorder=record_business_outcome,
            )
        elif args.command == "submit-context-packs":
            result = submit_context_inputs(source=args.source, evidence=args.evidence,
                trigger_context=args.trigger_context, title=args.title, title_source=args.title_source,
                source_status=args.source_status, app_id=args.app_id, feishu_profile=args.feishu_profile,
                source_message_id=args.source_message_id, route=args.route, score_only=args.score_only,
                special_fast_path=args.special_fast_path, context_pack_dir=args.context_pack_dir,
                state_dir=args.state_dir)
            if result["status"] == "accepted":
                from long_read_workflow_worker import run_once
                result["worker"] = run_once(args.state_dir, job_id=result["job_id"], owner="read-x-context-admission")
        elif args.command == "status":
            result = inspect_jobs(args.state_dir, job_id=args.job_id, chat_id=args.chat_id,
                                  report_key=args.report_key)
        elif args.command == "configure-report-target":
            from long_read_workflow_report_delivery import configure_report_target
            result = configure_report_target(
                state_dir=args.state_dir, profile=args.profile, user_id=args.user_id,
                chat_id=args.chat_id, sender_id=args.sender_id, replace=args.replace,
            )
        elif args.command == "record-event":
            details = _object(args.details_json.encode("utf-8"), "reliability event details")
            with WorkflowStore(args.state_dir) as store:
                result = store.record_reliability_event(args.job_id, args.event_type, details)
        elif args.command == "report":
            from long_read_workflow_reports import create_report_snapshot
            report_now = datetime.fromisoformat(args.now) if args.now else None
            result = create_report_snapshot(args.state_dir, args.period, now=report_now)
            if args.deliver and result["status"] == "pending_delivery":
                from long_read_workflow_report_delivery import deliver_report
                result["delivery"] = deliver_report(
                    state_dir=args.state_dir, report_key=result["report_key"], identity="bot",
                    profile=args.profile, user_id=args.user_id, chat_id=args.chat_id,
                    sender_id=args.sender_id,
                )
        elif args.command == "run-due-reports":
            from long_read_workflow_scheduler import run_due_reports
            scheduler_now = datetime.fromisoformat(args.now) if args.now else None
            target = None
            if args.deliver:
                from long_read_workflow_report_delivery import load_report_target
                target = load_report_target(args.state_dir)
                for key in ("profile", "user_id", "chat_id", "sender_id"):
                    value = getattr(args, key)
                    if value is not None and value != target[key]:
                        raise ValueError(f"--{key.replace('_', '-')} does not match persisted report target")
            result = run_due_reports(args.state_dir, now=scheduler_now, delivery_target=target)
        elif args.command == "run-worker" and args.serve:
            if args.job_id:
                raise ValueError("--serve cannot be combined with --job-id")
            if not 1 <= args.poll_seconds <= 3600:
                raise ValueError("--poll-seconds must be between 1 and 3600")
            from long_read_workflow_report_delivery import load_report_target
            target = load_report_target(args.state_dir)
            for key in ("profile", "user_id", "chat_id", "sender_id"):
                value = getattr(args, "report_" + key)
                if value is not None and value != target[key]:
                    raise ValueError(f"--report-{key.replace('_', '-')} does not match persisted report target")
            patrol_scheduler = _start_voice_x_patrol_scheduler(args.poll_seconds)
            try:
                while True:
                    result = worker_service_tick(
                        args.state_dir, owner=args.owner, report_target=target,
                        include_patrol=patrol_scheduler is None,
                    )
                    print(json.dumps(result, ensure_ascii=False), flush=True)
                    time.sleep(args.poll_seconds)
            except KeyboardInterrupt:
                return 0
            finally:
                if patrol_scheduler is not None:
                    stop_event, patrol_thread = patrol_scheduler
                    stop_event.set()
                    patrol_thread.join(timeout=2)
        else:
            from long_read_workflow_worker import run_once
            result = run_once(args.state_dir, job_id=args.job_id if args.command == "resume" else args.job_id,
                              owner=args.owner if args.command == "run-worker" else "read-x-resume",
                              manual_document_id=args.document_id if args.command == "resume" else None,
                              outcome_recorder=record_business_outcome)
    except (ValueError, OSError, StoreConflict, sqlite3.Error) as exc:
        failure = {"status": "blocked", "reason_code": type(exc).__name__, "message": str(exc)}
        if args.command == "submit-context-packs":
            failure.update(task_id=getattr(exc, "task_id", None),
                           context_packages={"required": 2, "completed": 0, "summary": "上下文包：已完成 0/2。"},
                           automatic_artifacts={"status": "not_requested", "requested": []})
        print(json.dumps(failure, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
