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
import sys
import tempfile
import time
from datetime import datetime

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from render_long_read_delivery_card import _load_score_evidence
from long_read_workflow_store import StoreConflict, WorkflowStore
from run_isolated_analyses import ALLOWED_LJG, validate_evidence

DEFAULT_STATE_DIR = REPO_ROOT / "state" / "long-read"


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


def _validate_target(context: dict, gate: dict, app_id: str):
    if context.get("appId") != app_id:
        raise ValueError("trigger app identity does not match app-id")
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
                  source_message_id: str | None = None,
                  questions: list[tuple[str, Path]] | None = None,
                  state_dir: Path = DEFAULT_STATE_DIR) -> dict:
    """Copy immutable inputs first; only then commit admission to SQLite."""
    questions = questions or []
    names = [name for name, _ in questions]
    if len(names) > 3 or len(names) != len(set(names)) or set(names) - ALLOWED_LJG:
        raise ValueError("questions require zero to three unique supported text skills")
    files = {
        "source.md": _read_regular(source), "evidence.json": _read_regular(evidence),
        "scoring-result.json": _read_regular(scoring_result), "score-gate.json": _read_regular(score_gate),
        "trigger-context.json": _read_regular(trigger_context),
    }
    question_files = []
    for index, (name, path) in enumerate(questions, 1):
        filename = f"question-{index:02d}-{name}.md"
        files[filename] = _read_regular(path)
        question_files.append({"skill": name, "file": filename})
    scoring = _object(files["scoring-result.json"], "scoring result")
    if scoring.get("score_status") != "scored" or scoring.get("route") != "long_read":
        raise ValueError("submission requires score_status=scored and route=long_read")
    context = _object(files["trigger-context.json"], "trigger context")
    if not isinstance(feishu_profile, str) or not feishu_profile.strip():
        raise ValueError("submission requires an explicit Feishu profile")
    if context.get("feishuProfile") != feishu_profile:
        raise ValueError("Feishu profile does not match trusted trigger context")
    validate_evidence(files["evidence.json"].decode("utf-8"), files["source.md"].decode("utf-8"))
    state_dir = Path(state_dir).absolute()
    # Do not resolve away a symlink in the caller's state directory.
    _reject_symlink_path(state_dir)
    _private_directory(state_dir)
    artifacts_dir = state_dir / "artifacts"
    _private_directory(artifacts_dir)
    temporary = Path(tempfile.mkdtemp(prefix=".incoming-", dir=artifacts_dir))
    try:
        for filename, value in files.items():
            _write_private(temporary / filename, value)
        # Reuse the actual delivery-card gate against the copied exact bytes;
        # validation never races a caller changing its original scoring file.
        gate = _load_score_evidence(temporary / "score-gate.json", temporary / "scoring-result.json")
        _validate_target(context, gate, app_id)
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
            except OSError as exc:
                if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise
                _verify_snapshot(snapshot, files, manifest)
            _fsync_directory(artifacts_dir)
        _verify_snapshot(snapshot, files, manifest)
        payload = {
            "schema": "long-read-submission/v1", "snapshot_sha256": digest,
            "inputs": {name: {"path": str(snapshot / name), "sha256": _hash(value)} for name, value in sorted(files.items())},
            "questions": question_files, "route": "long_read", "score_status": "scored",
            "bridge_new_chat_url": "https://chatgpt.com/",
            "trigger": {key: context[key] for key in ("profile", "feishuProfile", "appId", "identity", "chatType", "chatId", "senderId", "senderType") if key in context},
            "artifact_plan_finalized": False,
        }
        with WorkflowStore(state_dir) as store:
            job = store.submit(payload, app_id=app_id, source_message_id=source_message_id,
                               artifacts=[
                                   {"key": "analysis:article-decode", "kind": "analysis"},
                                   {"key": "analysis:munger-analysis", "kind": "analysis"},
                                   {"key": "analysis:document-supplement", "kind": "analysis"},
                                   *[{"key": f"analysis:{name}", "kind": "analysis"} for name in names],
                                   {"key": "main-document", "kind": "document"},
                                   {"key": "document-notification", "kind": "message"},
                                   {"key": "core-image", "kind": "image"},
                                   {"key": "core-image-insert", "kind": "image_insert"},
                                   {"key": "core-image-send", "kind": "message"},
                                   {"key": "munger-image", "kind": "image", "required": False},
                                   {"key": "munger-image-insert", "kind": "image_insert", "required": False},
                                   {"key": "munger-image-send", "kind": "message", "required": False},
                               ])
        return {"status": "accepted", "job_id": job["job_id"], "created": job["created"],
                "job_status": job["status"], "input_sha256": job["input_sha256"],
                "snapshot_sha256": digest, "snapshot_dir": str(snapshot), "delivery_complete": False}
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def inspect_jobs(state_dir: Path = DEFAULT_STATE_DIR, *, job_id: str | None = None, resume: bool = False) -> dict:
    """Read-only candidate listing. Never acquire leases or change effect state."""
    state_dir = Path(state_dir).absolute()
    db = state_dir / "workflow.sqlite3"
    _reject_symlink_path(db)
    if not db.exists():
        return {"status": "not_initialized", "jobs": [], "execution_performed": False}
    connection = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        where, params = (" WHERE job_id=?", (job_id,)) if job_id else ("", ())
        rows = connection.execute("SELECT job_id,status,version,created_at,updated_at,lease_expires_at FROM jobs" + where + " ORDER BY created_at,job_id", params).fetchall()
        jobs = []
        for row in rows:
            if resume and row["status"] in {"succeeded", "failed"}:
                continue
            job = dict(row)
            job["artifacts"] = [dict(item) for item in connection.execute("SELECT artifact_key,kind,required,status,retry_count FROM artifacts WHERE job_id=? ORDER BY artifact_key", (row["job_id"],))]
            effects = [dict(item) for item in connection.execute("SELECT op_key,artifact_key,action,status,attempts FROM effects WHERE job_id=? AND status IN ('attempting','uncertain') ORDER BY created_at,op_key", (row["job_id"],))]
            job["unresolved_effects"] = effects
            if resume:
                active = row["lease_expires_at"] is not None and row["lease_expires_at"] > time.time()
                job["recovery_action"] = "wait_for_lease" if active else "reconcile_only" if effects else "continue_from_checkpoint"
            jobs.append(job)
        return {"status": "recovery_candidates" if resume else "status", "jobs": jobs, "execution_performed": False}
    finally:
        connection.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit", help="校验并持久接管输入；不启动交付")
    for name in ("source", "evidence", "scoring-result", "score-gate", "trigger-context"):
        submit.add_argument("--" + name, type=Path, required=True)
    submit.add_argument("--app-id", required=True)
    submit.add_argument("--feishu-profile", required=True, help="trusted lark-cli profile captured at ingress")
    submit.add_argument("--source-message-id")
    submit.add_argument("--question", action="append", nargs=2, metavar=("SKILL", "FILE"), default=[])
    status = commands.add_parser("status")
    resume = commands.add_parser("resume", help="恢复指定任务的已持久阶段")
    worker = commands.add_parser("run-worker", help="推进一个待处理长文任务的内容阶段")
    report = commands.add_parser("report", help="生成或复用固定周期的可靠性报告快照")
    report.add_argument("--period", required=True, choices=("weekly", "monthly"))
    report.add_argument("--now", help="可控时钟，ISO-8601；默认当前时间")
    report.add_argument("--deliver", action="store_true", help="发送待交付报告；需要显式提供可信目标")
    report.add_argument("--profile", help="显式 Feishu 应用 profile")
    report.add_argument("--user-id", help="本人 Feishu open_id")
    report.add_argument("--chat-id", help="用于丢响应恢复的本人私聊 chat_id")
    report.add_argument("--sender-id", help="发送方应用 ID，用于远端读回核验")
    due_reports = commands.add_parser("run-due-reports", help="补跑持久追踪开始后的周报和月报周期")
    due_reports.add_argument("--now", help="可控时钟，ISO-8601；默认当前时间")
    due_reports.add_argument("--deliver", action="store_true", help="发送到显式配置的本人私聊")
    due_reports.add_argument("--profile")
    due_reports.add_argument("--user-id")
    due_reports.add_argument("--chat-id")
    due_reports.add_argument("--sender-id")
    for command in (submit, status, resume, worker, report, due_reports):
        command.add_argument("--state-dir", type=Path, default=argparse.SUPPRESS)
        if command is status:
            command.add_argument("--job-id")
    resume.add_argument("--job-id", required=True)
    worker.add_argument("--job-id")
    worker.add_argument("--owner", default="read-x-worker")
    args = parser.parse_args(argv)
    try:
        if args.command == "submit":
            result = submit_inputs(source=args.source, evidence=args.evidence, scoring_result=args.scoring_result,
                                   score_gate=args.score_gate, trigger_context=args.trigger_context, app_id=args.app_id,
                                   feishu_profile=args.feishu_profile,
                                   source_message_id=args.source_message_id, questions=[(name, Path(path)) for name, path in args.question],
                                   state_dir=args.state_dir)
        elif args.command == "status":
            result = inspect_jobs(args.state_dir, job_id=args.job_id)
        elif args.command == "report":
            from long_read_workflow_reports import create_report_snapshot
            report_now = datetime.fromisoformat(args.now) if args.now else None
            result = create_report_snapshot(args.state_dir, args.period, now=report_now)
            if args.deliver and result["status"] == "pending_delivery":
                missing = [name for name in ("profile", "user_id", "chat_id", "sender_id") if not getattr(args, name)]
                if missing:
                    raise ValueError("--deliver requires --" + ", --".join(name.replace("_", "-") for name in missing))
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
                missing = [name for name in ("profile", "user_id", "chat_id", "sender_id") if not getattr(args, name)]
                if missing:
                    raise ValueError("--deliver requires --" + ", --".join(name.replace("_", "-") for name in missing))
                target = {"identity": "bot", "profile": args.profile, "user_id": args.user_id,
                          "chat_id": args.chat_id, "sender_id": args.sender_id}
            result = run_due_reports(args.state_dir, now=scheduler_now, delivery_target=target)
        else:
            from long_read_workflow_worker import run_once
            result = run_once(args.state_dir, job_id=args.job_id if args.command == "resume" else args.job_id,
                              owner=args.owner if args.command == "run-worker" else "read-x-resume")
    except (ValueError, OSError, StoreConflict, sqlite3.Error) as exc:
        print(json.dumps({"status": "blocked", "reason_code": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
