#!/usr/bin/env python3
"""Apply the approved long-read content and aggregate-statistics retention."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import time
import uuid
from collections import Counter

from long_read_workflow_store import WorkflowStore, is_verified_test_task, state_lock


CONTENT_RETENTION_SECONDS = 30 * 24 * 60 * 60
STATISTICS_RETENTION_SECONDS = 180 * 24 * 60 * 60
_EVENT_FIELDS = {
    "status", "artifact_key", "action", "retry_count", "required_artifacts",
    "reconciled", "metadata", "reason_code", "failure_stage",
}
_METADATA_FIELDS = {"reason_code", "notification_reason_code", "failure_stage", "duration_ms", "retryable", "retry_allowed", "code", "verification",
                    "next_retry_at", "next_reconcile_at"}
_REASON_CODES = frozenset({
    "api-rejected", "bot-app-identity-missing", "bot-cannot-read-target-p2p-history",
    "bridge-image-local-readback-failed", "bridge-image-receipt-mismatch",
    "bridge-image-recovery-failed", "bridge-image-recovery-unconfirmed",
    "bridge-image-result-unknown", "bridge-run-id-mismatch", "compiler-process-unavailable",
    "confirmed-image-local-output-missing", "create-result-unknown", "document-compiler-blocked",
    "document-compiler-dry-run", "document-compiler-publish-unknown",
    "document-compiler-recovery-unconfirmed", "document-compiler-recovery-unreadable",
    "feishu-profile-missing", "image-block-confirmation-lost", "image-block-create-plan-invalid",
    "image-block-create-preflight-failed", "image-block-create-rejected",
    "image-block-create-result-unconfirmed", "image-message-confirmation-lost",
    "image-delivery-identity-unverified", "image-insert-identity-unverified",
    "image-message-history-ambiguous", "image-message-history-unavailable",
    "image-message-readback-unmatched", "image-message-request-changed",
    "image-patch-confirmation-lost", "image-patch-rejected", "image-patch-request-changed",
    "image-patch-result-unconfirmed", "image-prompt-or-request-invalid",
    "image-request-receipt-invalid", "image-retry-budget-exhausted", "image-runner-exception",
    "image-send-identity-unverified", "image-send-rejected", "image-stage-recovery-pending", "image-upload-rejected",
    "image-upload-request-changed", "image-upload-result-unconfirmed", "image-delivery-failed",
    "invalid-document-id", "invalid-publish-outcome", "lease-expired", "main-document-publish-failed",
    "main-document-result-unconfirmed", "main-document-unavailable", "message-image-upload-rejected",
    "message-image-upload-request-changed", "message-image-upload-result-unconfirmed",
    "message-rate-limit", "message-rate-preflight-unavailable",
    "manual-document-id-requires-job-id", "message-readback-unconfirmed", "message-readback-unmatched", "missing-private-message-target",
    "outcome-artifact-missing", "outcome-effect-mismatch", "outcome-effect-missing",
    "notification-history-incomplete", "notification-identity-missing", "notification-identity-unverified",
    "notification-payload-changed", "notification-readback-ambiguous",
    "notification-readback-unconfirmed", "notification-retry-backoff", "notification-retry-exhausted",
    "notification-send-rejected", "notification-send-result-unconfirmed", "output-validation",
    "patch-result-unknown", "previous-image-block-create-failure", "previous-image-patch-failure",
    "previous-image-send-failure", "previous-image-upload-failure",
    "previous-known-image-generation-failure", "previous-message-image-upload-failure",
    "previous-notification-failure", "private-chat-readback-target-missing", "publish-response-unknown",
    "document-notification-failed", "required-content-branch-failed",
    "remote-rejected", "remote-result-unknown", "report-history-ambiguous", "report-history-incomplete",
    "report-retry-exhausted",
    "report-history-no-match", "send-response-unknown", "send-result-unknown",
    "document_publish_unconfirmed", "image-generation-failed", "image-generation-unknown",
    "image-insert-failed", "image-insert-unconfirmed", "image-result-unconfirmed",
    "image-send-failed", "image-send-unconfirmed", "job-not-found", "known-failure",
    "upload-result-unknown", "upload-result-unconfirmed", "unsupported-image-block-create-state", "unsupported-image-effect-state",
    "unsupported-image-patch-state", "unsupported-image-send-state", "unsupported-image-upload-state",
    "unsupported-message-image-upload-state", "unsupported-notification-state",
    "unadmitted-legacy-job", "unsupported-image-state", "unsupported-private-message-target",
    "worker-interrupted", "unknown",
})
_FAILURE_STAGES = frozenset({
    "assembly", "bridge", "content-assembly", "document-publish", "image-block-create", "image-block-patch",
    "image-delivery", "image-insert",
    "image-generate", "image-send", "image-upload", "message-image-upload", "notification",
    "notification-send", "output-validation", "prompt-governance", "unknown", "upstream-delivery",
})
_VERIFICATIONS = frozenset({
    "bridge-image-generation-confirmed", "bridge-live-dom-snapshot", "bridge-observe-image-recovered",
    "document-compiler-readback-matched", "document-compiler-readonly-recovery-matched",
    "exact-card-readback", "exact-image-token-parent-readback", "exact-ordered-document-block-diff",
    "history-exact-message-match", "image-block-create-upload-patch-readback",
    "image-block-id-parent-readback", "image-key-upload-receipt", "image-message-readback-confirmed",
    "live-dom+snapshot", "message-id-card-readback", "message-id-content-sender-readback",
    "message-id-readback", "upload-receipt", "uploaded-token-read-from-target-block", "unknown",
})


class ExpiredReportWindow(ValueError):
    pass


def _safe_remove_tree(parent: Path, name: str) -> bool:
    if parent.is_symlink():
        raise ValueError("retention parent must not be a symlink")
    target = parent / name
    if target.is_symlink():
        raise ValueError("retention target must not be a symlink")
    if not target.exists():
        return False
    if not target.is_dir():
        raise ValueError("retention target must be a directory")
    shutil.rmtree(target)
    return True


def _safe_remove_file(parent: Path, name: str) -> bool:
    if parent.is_symlink():
        raise ValueError("retention parent must not be a symlink")
    target = parent / name
    if target.is_symlink():
        raise ValueError("retention target must not be a symlink")
    if not target.exists():
        return False
    if not target.is_file():
        raise ValueError("retention target must be a regular file")
    target.unlink()
    return True


def _retained_value(key: str, value):
    if key == "reason_code":
        return value if isinstance(value, str) and value in _REASON_CODES else "unknown"
    if key == "notification_reason_code":
        return value if isinstance(value, str) and value in _REASON_CODES else "unknown"
    if key == "failure_stage":
        return value if isinstance(value, str) and value in _FAILURE_STAGES else "unknown"
    if key == "verification":
        return value if isinstance(value, str) and value in _VERIFICATIONS else "unknown"
    if key == "code":
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value):
            return value
        return "unknown"
    if key == "duration_ms":
        return value if (isinstance(value, (int, float)) and not isinstance(value, bool)
                         and math.isfinite(value)) else None
    if key == "retryable":
        return value if isinstance(value, bool) else None
    return None


def _safe_metadata(value: str) -> str:
    metadata = json.loads(value)
    if not isinstance(metadata, dict):
        return "{}"
    safe = {}
    for key, item in metadata.items():
        if key not in _METADATA_FIELDS:
            continue
        retained = _retained_value(key, item)
        if retained is not None:
            safe[key] = retained
    return json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_event_details(value: str) -> str:
    details = json.loads(value)
    if not isinstance(details, dict):
        return "{}"
    safe = {}
    for key, item in details.items():
        if key not in _EVENT_FIELDS or key == "metadata":
            continue
        if key in {"reason_code", "failure_stage"}:
            safe[key] = _retained_value(key, item)
        else:
            safe[key] = item
    metadata = details.get("metadata")
    if isinstance(metadata, dict):
        safe["metadata"] = json.loads(_safe_metadata(json.dumps(metadata)))
    return json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _snapshot_digest(payload: dict) -> str | None:
    value = payload.get("snapshot_sha256")
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("stored snapshot digest is invalid; retention stopped")
    return value


def _active_snapshot_refs(store: WorkflowStore) -> Counter:
    counts = Counter()
    rows = store.connection.execute("SELECT payload_json FROM jobs").fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            raise ValueError("stored workflow payload is invalid; retention stopped")
        if not isinstance(payload, dict):
            raise ValueError("stored workflow payload is invalid; retention stopped")
        if payload.get("content_scrubbed_at") is None:
            digest = _snapshot_digest(payload)
            if digest:
                counts[digest] += 1
    return counts


def _set_meta(store: WorkflowStore, key: str, value: str) -> None:
    store.connection.execute(
        "INSERT INTO workflow_meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value),
    )


def _content_targets(state_dir: Path, job: dict) -> bool:
    payload = json.loads(job["payload_json"])
    _snapshot_digest(payload)
    return _safe_remove_tree(state_dir / "jobs", job["job_id"])


def _remove_orphan_snapshots(state_dir: Path, active_refs: Counter) -> int:
    parent = state_dir / "artifacts"
    if not parent.exists():
        return 0
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError("snapshot parent must be a real directory")
    removed = 0
    for child in parent.iterdir():
        stale_temporary = child.name.startswith(".incoming-")
        unreferenced_snapshot = re.fullmatch(r"[a-f0-9]{64}", child.name) and not active_refs[child.name]
        if stale_temporary or unreferenced_snapshot:
            removed += int(_safe_remove_tree(parent, child.name))
    return removed


def apply_retention(state_dir: Path, *, now: float | None = None) -> dict:
    """Purge terminal content at 30d and its sanitized event statistics at 180d.

    Active jobs and any job with an uncertain effect are never modified. The
    shared state lock prevents snapshot admission from racing snapshot deletion.
    """
    state_dir = Path(state_dir).absolute()
    now = time.time() if now is None else float(now)
    if not (now > 0):
        raise ValueError("retention clock must be positive")
    content_cutoff = now - CONTENT_RETENTION_SECONDS
    stats_cutoff = now - STATISTICS_RETENTION_SECONDS
    result = {
        "content_jobs_scrubbed": 0, "job_directories_removed": 0,
        "snapshots_removed": 0, "statistics_jobs_removed": 0,
        "reports_removed": 0, "report_attachments_removed": 0,
        "unresolved_jobs_preserved": 0,
    }

    # Report-only runs may be the first use of this state directory.
    with WorkflowStore(state_dir):
        pass
    with state_lock(state_dir), WorkflowStore(state_dir) as store:
        terminal = store.connection.execute(
            "SELECT job_id,status,updated_at,payload_json "
            "FROM jobs WHERE status IN ('succeeded','failed') AND updated_at<=? "
            "AND NOT EXISTS (SELECT 1 FROM effects e WHERE e.job_id=jobs.job_id "
            "AND e.status IN ('prepared','attempting','uncertain')) ORDER BY updated_at,job_id",
            (content_cutoff,),
        ).fetchall()
        protected = store.connection.execute(
            "SELECT COUNT(*) AS count FROM jobs j WHERE j.status NOT IN ('succeeded','failed') "
            "OR EXISTS (SELECT 1 FROM effects e WHERE e.job_id=j.job_id "
            "AND e.status IN ('prepared','attempting','uncertain'))"
        ).fetchone()["count"]
        result["unresolved_jobs_preserved"] = int(protected)

        scrub_ids = []
        for row in terminal:
            payload = json.loads(row["payload_json"])
            if payload.get("content_scrubbed_at") is not None:
                continue
            removed_job = _content_targets(state_dir, dict(row))
            result["job_directories_removed"] += int(removed_job)
            scrub_ids.append((row["job_id"], float(row["updated_at"]), payload))

        with store._transaction():
            _set_meta(store, "retention_mode", "scrub")
            for job_id, terminal_at, payload in scrub_ids:
                # A scrubbed job keeps only route/test markers and an opaque
                # identity so aggregate reports can still count it safely.
                minimal_payload = {
                    "schema": "retained-long-read-statistics/v1",
                    "route": "long_read",
                    "synthetic": payload.get("synthetic") is True,
                    "test_task": is_verified_test_task(payload),
                    "verified_test_task": is_verified_test_task(payload),
                    "content_scrubbed_at": now,
                    "snapshot_sha256": payload.get("snapshot_sha256"),
                }
                source_hash = hashlib.sha256(f"retained:{job_id}".encode()).hexdigest()
                store.connection.execute(
                    "UPDATE jobs SET app_id='retained',source_message_id=NULL,input_sha256=?,payload_json=? "
                    "WHERE job_id=? AND status IN ('succeeded','failed') AND updated_at=?",
                    (source_hash, json.dumps(minimal_payload, ensure_ascii=False, sort_keys=True), job_id, terminal_at),
                )
                effects = store.connection.execute(
                    "SELECT op_key,job_id,artifact_key,action FROM effects WHERE job_id=?", (job_id,)
                ).fetchall()
                for effect in effects:
                    request_hash = hashlib.sha256(
                        f"retained:{job_id}:{effect['artifact_key']}:{effect['action']}".encode()
                    ).hexdigest()
                    store.connection.execute(
                        "UPDATE effects SET request_sha256=?,request_id=?,remote_id=NULL,metadata_json=? WHERE op_key=?",
                        (request_hash, str(uuid.uuid4()), _safe_metadata(
                            store.connection.execute("SELECT metadata_json FROM effects WHERE op_key=?", (effect["op_key"],)).fetchone()["metadata_json"]
                        ), effect["op_key"]),
                    )
                artifacts = store.connection.execute(
                    "SELECT artifact_key,metadata_json FROM artifacts WHERE job_id=?", (job_id,)
                ).fetchall()
                for artifact in artifacts:
                    store.connection.execute(
                        "UPDATE artifacts SET metadata_json=? WHERE job_id=? AND artifact_key=?",
                        (_safe_metadata(artifact["metadata_json"]), job_id, artifact["artifact_key"]),
                    )
                events = store.connection.execute(
                    "SELECT seq,details_json FROM events WHERE job_id=?", (job_id,)
                ).fetchall()
                for event in events:
                    store.connection.execute(
                        "UPDATE events SET details_json=? WHERE seq=?",
                        (_safe_event_details(event["details_json"]), event["seq"]),
                    )
            store.connection.execute("DELETE FROM workflow_meta WHERE key='retention_mode'")
            result["content_jobs_scrubbed"] = len(scrub_ids)

        # A long-offline worker may reach 180 days before any 30-day sweep.
        # Recheck shared snapshots after all eligible jobs have been scrubbed,
        # before their rows disappear and lose the only path to those files.
        active_snapshot_refs = _active_snapshot_refs(store)
        scrubbed_rows = store.connection.execute(
            "SELECT job_id,payload_json FROM jobs WHERE status IN ('succeeded','failed') AND updated_at<=?",
            (content_cutoff,),
        ).fetchall()
        for row in scrubbed_rows:
            payload = json.loads(row["payload_json"])
            if payload.get("content_scrubbed_at") is None:
                continue
            digest = _snapshot_digest(payload)
            if digest and not active_snapshot_refs[digest]:
                result["snapshots_removed"] += int(_safe_remove_tree(state_dir / "artifacts", digest))

        with store._transaction():
            _set_meta(store, "retention_mode", "purge")
            old_jobs = store.connection.execute(
                "SELECT job_id,status,payload_json FROM jobs "
                "WHERE status IN ('succeeded','failed') AND updated_at<=? "
                "AND NOT EXISTS (SELECT 1 FROM effects e WHERE e.job_id=jobs.job_id "
                "AND e.status IN ('prepared','attempting','uncertain'))",
                (stats_cutoff,),
            ).fetchall()
            job_ids = [row["job_id"] for row in old_jobs]
            archived_row = store.connection.execute(
                "SELECT value FROM workflow_meta WHERE key='lifetime_archived_counts'"
            ).fetchone()
            archived = json.loads(archived_row["value"]) if archived_row else {"N": 0, "S": 0, "F": 0}
            for row in old_jobs:
                job_id = row["job_id"]
                payload = json.loads(row["payload_json"])
                if payload.get("synthetic") is not True and not is_verified_test_task(payload):
                    archived["N"] += 1
                    archived["S" if row["status"] == "succeeded" else "F"] += 1
                store.connection.execute("DELETE FROM events WHERE job_id=?", (job_id,))
                store.connection.execute("DELETE FROM effects WHERE job_id=?", (job_id,))
                store.connection.execute("DELETE FROM artifacts WHERE job_id=?", (job_id,))
                store.connection.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
            _set_meta(store, "lifetime_archived_counts", json.dumps(archived, sort_keys=True))
            result["statistics_jobs_removed"] = len(job_ids)

            expired_reports = store.connection.execute(
                "SELECT report_key FROM reports WHERE created_at<=? AND status IN ('delivered','suppressed_weekly')",
                (stats_cutoff,),
            ).fetchall()
            report_keys = [row["report_key"] for row in expired_reports]
            reports_dir = state_dir / "reports"
            for report_key in report_keys:
                safe_name = report_key.replace(":", "-") + ".md"
                result["report_attachments_removed"] += int(_safe_remove_file(reports_dir, safe_name))
            for report_key in report_keys:
                store.connection.execute("DELETE FROM report_outbox WHERE report_key=?", (report_key,))
                store.connection.execute("DELETE FROM reports WHERE report_key=?", (report_key,))
            result["reports_removed"] = len(report_keys)
            current_floor = store.connection.execute(
                "SELECT value FROM workflow_meta WHERE key='report_retention_floor'"
            ).fetchone()
            floor = max(stats_cutoff, float(current_floor["value"]) if current_floor else stats_cutoff)
            _set_meta(store, "report_retention_floor", str(floor))
            store.connection.execute("DELETE FROM workflow_meta WHERE key='retention_mode'")

        result["snapshots_removed"] += _remove_orphan_snapshots(
            state_dir, _active_snapshot_refs(store),
        )

    return result


def assert_report_window_retained(store: WorkflowStore, start_epoch: float) -> None:
    row = store.connection.execute(
        "SELECT value FROM workflow_meta WHERE key='report_retention_floor'"
    ).fetchone()
    if row and start_epoch < float(row["value"]):
        raise ExpiredReportWindow("report window starts before the 180-day statistics retention floor")
