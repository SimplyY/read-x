#!/usr/bin/env python3
"""Private, local durable state for long-read. This module performs no external I/O.

Payloads are protected by filesystem permissions, not encryption. Callers must
commit begin_effect() before invoking an external service. A lost response must
be recorded as uncertain and reconciled, never submitted as a new operation.
Use one store connection per worker/thread; leases are mutable version handles.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid


class StoreConflict(RuntimeError):
    pass


class LeaseConflict(StoreConflict):
    pass


class UncertainEffect(StoreConflict):
    pass


class RetryExhausted(StoreConflict):
    pass


VERIFIED_EFFECTS_BY_KIND = {
    "analysis": {"bridge-submit"},
    "document": {"document-publish"},
    "image": {"image-generate"},
    "image_insert": {"image-block-create", "image-upload", "image-block-patch"},
    "message": {"message-send"},
}


def _base_action(action: str) -> str:
    return re.sub(r":retry-[1-2]\Z", "", action)


def _action_round(action: str) -> int:
    match = re.search(r":retry-([1-2])\Z", action)
    return int(match.group(1)) if match else 0


def _latest_action_states(rows) -> dict[str, str]:
    states = {}
    def order(item):
        action = item["action"]
        return (_base_action(action), _action_round(action), item["created_at"], item["op_key"])

    for row in sorted(rows, key=order):
        states[_base_action(row["action"])] = row["status"]
    return states


def _effect_contract_confirmed(rows, required: set[str]) -> bool:
    if any(row["status"] in {"attempting", "uncertain"} for row in rows):
        return False
    states = _latest_action_states(rows)
    return required.issubset(states) and all(states[action] == "confirmed" for action in required)


@dataclass
class Lease:
    job_id: str
    owner: str
    token: str
    fencing: int
    version: int
    expires_at: float


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _name(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:/+-]{1,200}", value):
        raise ValueError("identifier must contain 1-200 safe characters")
    return value


def _metadata(value: dict | None) -> str:
    """Only machine codes and measurements belong in receipts, never raw text."""
    allowed = {"reason_code", "failure_stage", "duration_ms", "retryable", "code", "verification",
               "content_sha256", "next_retry_at", "round", "image_sha256"}
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("metadata must contain only approved receipt fields")
    for item in value.values():
        if isinstance(item, str):
            _name(item)
        elif isinstance(item, bool) or item is None:
            continue
        elif not isinstance(item, (int, float)) or not math.isfinite(item):
            raise ValueError("metadata values must be machine codes or finite measurements")
    return _json(value)


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
 job_id TEXT PRIMARY KEY, app_id TEXT NOT NULL, source_message_id TEXT,
 input_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 0, lease_owner TEXT, lease_token TEXT,
 fencing INTEGER NOT NULL DEFAULT 0, lease_expires_at REAL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL,
 UNIQUE(app_id, source_message_id)
);
CREATE TABLE IF NOT EXISTS artifacts (
 job_id TEXT NOT NULL REFERENCES jobs(job_id), artifact_key TEXT NOT NULL,
 kind TEXT NOT NULL, required INTEGER NOT NULL CHECK(required IN (0,1)),
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','confirmed','failed','uncertain')),
 retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count BETWEEN 0 AND 2),
 metadata_json TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(job_id, artifact_key)
);
CREATE TABLE IF NOT EXISTS effects (
 op_key TEXT PRIMARY KEY, job_id TEXT NOT NULL, artifact_key TEXT NOT NULL,
 action TEXT NOT NULL, request_sha256 TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
 status TEXT NOT NULL CHECK(status IN ('prepared','attempting','confirmed','failed','uncertain')),
 attempts INTEGER NOT NULL DEFAULT 0, retry_count_at_attempt INTEGER NOT NULL DEFAULT 0,
 fencing INTEGER, remote_id TEXT, metadata_json TEXT NOT NULL DEFAULT '{}',
 created_at REAL NOT NULL, updated_at REAL NOT NULL,
 FOREIGN KEY(job_id,artifact_key) REFERENCES artifacts(job_id,artifact_key),
 UNIQUE(job_id,artifact_key,action)
);
CREATE TABLE IF NOT EXISTS events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES jobs(job_id),
 at REAL NOT NULL, event_type TEXT NOT NULL, details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_meta (
 key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
 report_key TEXT PRIMARY KEY, period_type TEXT NOT NULL,
 window_start REAL NOT NULL, window_end REAL NOT NULL,
 snapshot_json TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL,
 markdown TEXT NOT NULL, markdown_sha256 TEXT NOT NULL,
 status TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS report_outbox (
 outbox_id TEXT PRIMARY KEY, report_key TEXT NOT NULL REFERENCES reports(report_key),
 artifact_key TEXT NOT NULL, request_sha256 TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
 status TEXT NOT NULL CHECK(status IN ('prepared','attempting','confirmed','failed','uncertain')),
 attempts INTEGER NOT NULL DEFAULT 0, remote_id TEXT, metadata_json TEXT NOT NULL DEFAULT '{}',
 created_at REAL NOT NULL, updated_at REAL NOT NULL,
 UNIQUE(report_key,artifact_key)
);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
 BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
 BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""


class WorkflowStore:
    def __init__(self, state_dir: Path, *, clock=time.time):
        self.state_dir = Path(state_dir)
        self.clock = clock
        if self.state_dir.is_symlink():
            raise ValueError("state directory must not be a symlink")
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        self.db_path = self.state_dir / "workflow.sqlite3"
        for path in (self.db_path, Path(str(self.db_path) + "-wal"), Path(str(self.db_path) + "-shm")):
            if path.is_symlink():
                raise ValueError("database files must not be symlinks")
        fd = os.open(self.db_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        self.connection = sqlite3.connect(self.db_path, timeout=5, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(SCHEMA)
        self.connection.execute(
            "INSERT OR IGNORE INTO workflow_meta(key,value) VALUES('tracking_started_at',?)",
            (str(self.clock()),),
        )
        effect_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(effects)")}
        if "request_id" not in effect_columns:
            self.connection.execute("ALTER TABLE effects ADD COLUMN request_id TEXT")
            rows = self.connection.execute("SELECT op_key FROM effects WHERE request_id IS NULL").fetchall()
            for row in rows:
                self.connection.execute("UPDATE effects SET request_id=? WHERE op_key=?", (str(uuid.uuid4()), row["op_key"]))
            self.connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS effects_request_id_unique ON effects(request_id)")
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.db_path) + suffix)
            if path.exists():
                os.chmod(path, 0o600)

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def _transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def _event(self, job_id, event_type, details):
        self.connection.execute("INSERT INTO events(job_id,at,event_type,details_json) VALUES(?,?,?,?)",
                                (job_id, self.clock(), event_type, _json(details)))

    def _guard(self, lease: Lease):
        row = self.connection.execute("SELECT * FROM jobs WHERE job_id=?", (lease.job_id,)).fetchone()
        if (not row or row["lease_owner"] != lease.owner or row["lease_token"] != lease.token
                or row["fencing"] != lease.fencing or row["version"] != lease.version
                or row["lease_expires_at"] <= self.clock()):
            raise LeaseConflict("lease expired, fenced, or version changed")
        return row

    @contextmanager
    def _change(self, lease: Lease):
        with self._transaction():
            self._guard(lease)
            yield
            self.connection.execute("UPDATE jobs SET version=version+1,updated_at=? WHERE job_id=?",
                                    (self.clock(), lease.job_id))
        lease.version += 1

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        value = dict(row)
        for key in ("payload_json", "metadata_json", "details_json"):
            if key in value:
                value[key.removesuffix("_json")] = json.loads(value.pop(key))
        return value

    def submit(self, payload: dict, *, app_id: str, source_message_id: str | None = None,
               artifacts: list[dict] | None = None) -> dict:
        _name(app_id)
        if source_message_id is not None:
            _name(source_message_id)
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        specs = []
        for artifact in artifacts or []:
            key, kind = _name(artifact["key"]), _name(artifact["kind"])
            required = artifact.get("required", True)
            if not isinstance(required, bool):
                raise ValueError("required must be boolean")
            specs.append({"key": key, "kind": kind, "required": required})
        specs.sort(key=lambda item: item["key"])
        if len({item["key"] for item in specs}) != len(specs):
            raise ValueError("duplicate artifact keys")
        encoded = _json(payload)
        digest = hashlib.sha256(_json({"payload": payload, "artifacts": specs}).encode()).hexdigest()
        with self._transaction():
            old = self.connection.execute("SELECT * FROM jobs WHERE app_id=? AND source_message_id=?",
                                          (app_id, source_message_id)).fetchone() if source_message_id else None
            if old:
                if old["input_sha256"] != digest:
                    raise StoreConflict("source message already submitted with different input")
                result = self._decode(old)
                result["created"] = False
                return result
            job_id, now = str(uuid.uuid4()), self.clock()
            self.connection.execute("INSERT INTO jobs(job_id,app_id,source_message_id,input_sha256,payload_json,status,created_at,updated_at) VALUES(?,?,?,?,?,'pending',?,?)",
                                    (job_id, app_id, source_message_id, digest, encoded, now, now))
            for item in specs:
                self.connection.execute("INSERT INTO artifacts(job_id,artifact_key,kind,required) VALUES(?,?,?,?)",
                                        (job_id, item["key"], item["kind"], int(item["required"])))
            self._event(job_id, "submitted", {"input_sha256": digest})
        result = self.get_job(job_id)
        result["created"] = True
        return result

    def get_job(self, job_id: str) -> dict | None:
        return self._decode(self.connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    def list_jobs(self) -> list[dict]:
        return [dict(row) for row in self.connection.execute("SELECT job_id,status,version,created_at,updated_at FROM jobs ORDER BY created_at,job_id")]

    def acquire_lease(self, job_id: str, owner: str, ttl_seconds: float = 60) -> Lease:
        _name(owner)
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("lease TTL must be positive and finite")
        with self._transaction():
            row = self.connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            now = self.clock()
            if not row:
                raise KeyError(job_id)
            if row["lease_token"] and row["lease_expires_at"] > now:
                raise LeaseConflict("job is already leased")
            if row["status"] in {"succeeded", "failed"}:
                raise LeaseConflict("terminal job cannot be leased")
            inflight = self.connection.execute("SELECT op_key FROM effects WHERE job_id=? AND status='attempting'", (job_id,)).fetchall()
            for effect in inflight:
                self.connection.execute("UPDATE effects SET status='uncertain',updated_at=? WHERE op_key=?", (now, effect["op_key"]))
                self._event(job_id, "effect_uncertain", {"op_key": effect["op_key"], "reason_code": "lease-expired"})
            if inflight:
                self.connection.execute("UPDATE artifacts SET status='uncertain' WHERE job_id=? AND artifact_key IN (SELECT artifact_key FROM effects WHERE job_id=? AND status='uncertain')", (job_id, job_id))
            token = str(uuid.uuid4())
            lease = Lease(job_id, owner, token, row["fencing"] + 1, row["version"] + 1, now + ttl_seconds)
            status = "needs_review" if inflight else row["status"]
            self.connection.execute("UPDATE jobs SET lease_owner=?,lease_token=?,fencing=?,version=?,lease_expires_at=?,status=?,updated_at=? WHERE job_id=?",
                                    (owner, token, lease.fencing, lease.version, lease.expires_at, status, now, job_id))
            self._event(job_id, "lease_acquired", {"owner": owner, "fencing": lease.fencing})
        return lease

    def renew_lease(self, lease: Lease, ttl_seconds: float = 60):
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("lease TTL must be positive and finite")
        expiry = self.clock() + ttl_seconds
        with self._change(lease):
            self.connection.execute("UPDATE jobs SET lease_expires_at=? WHERE job_id=?", (expiry, lease.job_id))
        lease.expires_at = expiry

    def release_lease(self, lease: Lease):
        with self._change(lease):
            self.connection.execute("UPDATE jobs SET lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL WHERE job_id=?", (lease.job_id,))

    def set_status(self, lease: Lease, status: str):
        if status not in {"pending", "running", "waiting", "needs_review", "succeeded", "failed"}:
            raise ValueError("invalid job status")
        with self._change(lease):
            current = self.connection.execute("SELECT status FROM jobs WHERE job_id=?", (lease.job_id,)).fetchone()["status"]
            if current in {"succeeded", "failed"} and current != status:
                raise StoreConflict("terminal job status is immutable")
            unsafe = self.connection.execute("SELECT 1 FROM effects WHERE job_id=? AND status IN ('attempting','uncertain') LIMIT 1", (lease.job_id,)).fetchone()
            if status in {"succeeded", "failed"} and unsafe:
                raise StoreConflict("uncertain or in-flight effects prevent a terminal outcome")
            if status == "succeeded":
                payload_row = self.connection.execute("SELECT payload_json FROM jobs WHERE job_id=?", (lease.job_id,)).fetchone()
                payload = json.loads(payload_row["payload_json"])
                if payload.get("artifact_plan_finalized") is not True:
                    raise StoreConflict("artifact plan must be finalized before success")
                required = self.connection.execute(
                    "SELECT artifact_key,kind,status FROM artifacts WHERE job_id=? AND required=1",
                    (lease.job_id,),
                ).fetchall()
                if not required or any(row["status"] != "confirmed" for row in required):
                    raise StoreConflict("required artifacts or effects are not confirmed")
                for artifact in required:
                    expected = VERIFIED_EFFECTS_BY_KIND.get(artifact["kind"])
                    if not expected:
                        raise StoreConflict(f"required artifact has no verification contract: {artifact['artifact_key']}")
                    effects = self.connection.execute(
                        "SELECT op_key,action,status,created_at FROM effects WHERE job_id=? AND artifact_key=?",
                        (lease.job_id, artifact["artifact_key"]),
                    ).fetchall()
                    if not _effect_contract_confirmed(effects, expected):
                        raise StoreConflict(f"required artifact lacks confirmed remote effects: {artifact['artifact_key']}")
            self.connection.execute("UPDATE jobs SET status=? WHERE job_id=?", (status, lease.job_id))
            self._event(lease.job_id, "job_status", {"status": status})

    def artifacts(self, job_id: str) -> list[dict]:
        return [self._decode(row) for row in self.connection.execute("SELECT * FROM artifacts WHERE job_id=? ORDER BY artifact_key", (job_id,))]

    def set_artifact(self, lease: Lease, artifact_key: str, status: str, metadata: dict | None = None):
        if status not in {"pending", "confirmed", "failed", "uncertain"}:
            raise ValueError("invalid artifact status")
        encoded = _metadata(metadata)
        with self._change(lease):
            artifact = self.connection.execute(
                "SELECT kind FROM artifacts WHERE job_id=? AND artifact_key=?", (lease.job_id, artifact_key)
            ).fetchone()
            if not artifact:
                raise KeyError(artifact_key)
            if status == "confirmed":
                expected = VERIFIED_EFFECTS_BY_KIND.get(artifact["kind"])
                if not expected:
                    raise StoreConflict(f"artifact kind has no verification contract: {artifact['kind']}")
                effects = self.connection.execute(
                    "SELECT op_key,action,status,created_at FROM effects WHERE job_id=? AND artifact_key=?",
                    (lease.job_id, artifact_key),
                ).fetchall()
                if not _effect_contract_confirmed(effects, expected):
                    raise StoreConflict("artifact cannot be confirmed without its expected verified effects")
            self.connection.execute("UPDATE artifacts SET status=?,metadata_json=? WHERE job_id=? AND artifact_key=?", (status, encoded, lease.job_id, artifact_key))
            self._event(lease.job_id, "artifact_status", {
                "artifact_key": artifact_key, "status": status,
                "metadata": json.loads(encoded),
            })

    def finalize_artifact_plan(self, lease: Lease, required_artifacts: set[str]) -> None:
        """Freeze the conditional image plan only after analysis outcomes are known."""
        required_artifacts = {_name(key) for key in required_artifacts}
        with self._transaction():
            self._guard(lease)
            job = self.connection.execute("SELECT payload_json FROM jobs WHERE job_id=?", (lease.job_id,)).fetchone()
            payload = json.loads(job["payload_json"])
            rows = self.connection.execute("SELECT artifact_key,status,required FROM artifacts WHERE job_id=?", (lease.job_id,)).fetchall()
            known = {row["artifact_key"] for row in rows}
            if required_artifacts - known:
                raise StoreConflict("required artifact plan names unknown artifacts")
            if any(row["artifact_key"].startswith("analysis:") and row["status"] not in {"confirmed", "failed"} for row in rows):
                raise StoreConflict("analysis outcomes must be resolved before finalizing the artifact plan")
            if self.connection.execute("SELECT 1 FROM effects WHERE job_id=? AND status IN ('attempting','uncertain') LIMIT 1", (lease.job_id,)).fetchone():
                raise UncertainEffect("unknown external outcomes prevent finalizing the artifact plan")
            if payload.get("artifact_plan_finalized") is True:
                current = {row["artifact_key"] for row in rows if row["required"]}
                if current != required_artifacts:
                    raise StoreConflict("finalized artifact plan is immutable")
                return
            self.connection.execute("UPDATE artifacts SET required=0 WHERE job_id=?", (lease.job_id,))
            for artifact_key in required_artifacts:
                self.connection.execute("UPDATE artifacts SET required=1 WHERE job_id=? AND artifact_key=?", (lease.job_id, artifact_key))
            payload["artifact_plan_finalized"] = True
            payload["required_artifacts"] = sorted(required_artifacts)
            self.connection.execute("UPDATE jobs SET payload_json=? WHERE job_id=?", (_json(payload), lease.job_id))
            self._event(lease.job_id, "artifact_plan_finalized", {"required_artifacts": sorted(required_artifacts)})
            self.connection.execute("UPDATE jobs SET version=version+1,updated_at=? WHERE job_id=?",
                                    (self.clock(), lease.job_id))
        lease.version += 1

    def consume_image_retry(self, lease: Lease, artifact_key: str) -> int:
        with self._change(lease):
            artifact = self.connection.execute("SELECT * FROM artifacts WHERE job_id=? AND artifact_key=?", (lease.job_id, artifact_key)).fetchone()
            if not artifact or artifact["kind"] != "image":
                raise ValueError("retry budget belongs to an image artifact")
            if artifact["status"] != "failed":
                raise StoreConflict("only a known image failure may be retried")
            if artifact["retry_count"] >= 2:
                raise RetryExhausted("image retry budget exhausted")
            count = artifact["retry_count"] + 1
            self.connection.execute("UPDATE artifacts SET retry_count=?,status='pending',metadata_json='{}' WHERE job_id=? AND artifact_key=?", (count, lease.job_id, artifact_key))
            self._event(lease.job_id, "image_retry", {"artifact_key": artifact_key, "retry_count": count})
        return count

    def prepare_effect(self, lease: Lease, artifact_key: str, action: str, request_sha256: str,
                       request_id: str | None = None) -> dict:
        _name(action)
        if not isinstance(request_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", request_sha256):
            raise ValueError("effect request requires SHA-256")
        if request_id is not None:
            try:
                request_id = str(uuid.UUID(request_id))
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError("request_id must be a UUID") from exc
        op_key = hashlib.sha256(_json([lease.job_id, artifact_key, action]).encode()).hexdigest()
        with self._change(lease):
            row = self.connection.execute("SELECT * FROM effects WHERE op_key=?", (op_key,)).fetchone()
            if row:
                if row["request_sha256"] != request_sha256:
                    raise StoreConflict("effect already exists with different input")
                if request_id is not None and row["request_id"] != request_id:
                    raise StoreConflict("effect already exists with a different request_id")
            else:
                now = self.clock()
                request_id = request_id or str(uuid.uuid4())
                self.connection.execute("INSERT INTO effects(op_key,job_id,artifact_key,action,request_sha256,request_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,'prepared',?,?)", (op_key, lease.job_id, artifact_key, action, request_sha256, request_id, now, now))
                self._event(lease.job_id, "effect_prepared", {"op_key": op_key, "artifact_key": artifact_key, "action": action})
        return self.get_effect(op_key)

    def get_effect(self, op_key: str) -> dict | None:
        return self._decode(self.connection.execute("SELECT * FROM effects WHERE op_key=?", (op_key,)).fetchone())

    def find_effect(self, job_id: str, artifact_key: str, action: str) -> dict | None:
        return self._decode(self.connection.execute(
            "SELECT * FROM effects WHERE job_id=? AND artifact_key=? AND action=?",
            (job_id, artifact_key, action),
        ).fetchone())

    def effects_for(self, job_id: str, artifact_key: str) -> list[dict]:
        return [self._decode(row) for row in self.connection.execute(
            "SELECT * FROM effects WHERE job_id=? AND artifact_key=? ORDER BY created_at,op_key",
            (job_id, artifact_key),
        )]

    def latest_effect_for_action(self, job_id: str, artifact_key: str, action: str) -> dict | None:
        rows = [item for item in self.effects_for(job_id, artifact_key)
                if _base_action(item["action"]) == action]
        if not rows:
            return None
        # Fixed test clocks and fast retries can share a timestamp. Order by
        # the explicit retry round so recovery always selects the newest intent.
        return max(rows, key=lambda item: (
            _action_round(item["action"]),
            item["created_at"], item["op_key"],
        ))

    def begin_effect(self, lease: Lease, op_key: str) -> dict:
        with self._change(lease):
            row = self.connection.execute("SELECT * FROM effects WHERE job_id=? AND op_key=?", (lease.job_id, op_key)).fetchone()
            if not row:
                raise KeyError(op_key)
            if row["status"] in {"attempting", "uncertain"}:
                raise UncertainEffect("effect must be reconciled before another attempt")
            execute = row["status"] != "confirmed"
            if execute:
                artifact = self.connection.execute("SELECT * FROM artifacts WHERE job_id=? AND artifact_key=?", (lease.job_id, row["artifact_key"])).fetchone()
                if artifact["status"] == "uncertain":
                    raise UncertainEffect("artifact has an uncertain result")
                if row["status"] == "failed" and artifact["kind"] == "image" and artifact["retry_count"] <= row["retry_count_at_attempt"]:
                    raise StoreConflict("reserve an image retry before another attempt")
                self.connection.execute("UPDATE effects SET status='attempting',attempts=attempts+1,retry_count_at_attempt=?,fencing=?,updated_at=? WHERE op_key=?", (artifact["retry_count"], lease.fencing, self.clock(), op_key))
                self._event(lease.job_id, "effect_attempting", {"op_key": op_key, "fencing": lease.fencing})
        result = self.get_effect(op_key)
        result["execute"] = execute
        return result

    def finish_effect(self, lease: Lease, op_key: str, status: str, *, remote_id: str | None = None, metadata: dict | None = None):
        self._resolve_effect(lease, op_key, status, remote_id, metadata, reconcile=False)

    def reconcile_effect(self, lease: Lease, op_key: str, status: str, *, remote_id: str | None = None, metadata: dict | None = None):
        """Caller must first verify the remote outcome; this method never probes it."""
        if status not in {"confirmed", "failed"}:
            raise ValueError("reconciliation requires a known outcome")
        self._resolve_effect(lease, op_key, status, remote_id, metadata, reconcile=True)

    def _resolve_effect(self, lease, op_key, status, remote_id, metadata, *, reconcile):
        if status not in {"confirmed", "failed", "uncertain"}:
            raise ValueError("invalid effect outcome")
        if remote_id is not None:
            _name(remote_id)
        encoded = _metadata(metadata)
        with self._change(lease):
            row = self.connection.execute("SELECT * FROM effects WHERE job_id=? AND op_key=?", (lease.job_id, op_key)).fetchone()
            expected = {"uncertain"} if reconcile else {"attempting"}
            if not row or row["status"] not in expected:
                raise StoreConflict("effect is not in the expected state")
            self.connection.execute("UPDATE effects SET status=?,remote_id=?,metadata_json=?,updated_at=? WHERE op_key=?", (status, remote_id, encoded, self.clock(), op_key))
            if status in {"failed", "uncertain"}:
                self.connection.execute("UPDATE artifacts SET status=? WHERE job_id=? AND artifact_key=?", (status, lease.job_id, row["artifact_key"]))
            elif reconcile:
                unresolved = self.connection.execute("SELECT 1 FROM effects WHERE job_id=? AND artifact_key=? AND status IN ('attempting','uncertain') LIMIT 1", (lease.job_id, row["artifact_key"])).fetchone()
                if not unresolved:
                    # A verified stage is reusable; the whole artifact still
                    # needs its remaining insert/send stages before confirmation.
                    self.connection.execute("UPDATE artifacts SET status='pending' WHERE job_id=? AND artifact_key=? AND status='uncertain'", (lease.job_id, row["artifact_key"]))
            self._event(lease.job_id, "effect_" + status, {"op_key": op_key, "reconciled": reconcile, "metadata": json.loads(encoded)})

    def events(self, job_id: str) -> list[dict]:
        return [self._decode(row) for row in self.connection.execute("SELECT * FROM events WHERE job_id=? ORDER BY seq", (job_id,))]

    def prepare_report_effect(self, report_key: str, artifact_key: str, request_sha256: str) -> dict:
        _name(report_key)
        _name(artifact_key)
        if not isinstance(request_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", request_sha256):
            raise ValueError("report effect request requires SHA-256")
        row = self.connection.execute(
            "SELECT * FROM report_outbox WHERE report_key=? AND artifact_key=?", (report_key, artifact_key)
        ).fetchone()
        if row:
            if row["request_sha256"] != request_sha256:
                raise StoreConflict("report effect already exists with different input")
            return self._decode(row)
        if not self.connection.execute("SELECT 1 FROM reports WHERE report_key=?", (report_key,)).fetchone():
            raise KeyError(report_key)
        now = self.clock()
        record = {
            "outbox_id": str(uuid.uuid4()), "report_key": report_key, "artifact_key": artifact_key,
            "request_sha256": request_sha256, "request_id": str(uuid.uuid4()),
            "status": "prepared", "attempts": 0, "remote_id": None,
            "metadata_json": "{}", "created_at": now, "updated_at": now,
        }
        with self._transaction():
            existing = self.connection.execute(
                "SELECT * FROM report_outbox WHERE report_key=? AND artifact_key=?", (report_key, artifact_key)
            ).fetchone()
            if existing:
                if existing["request_sha256"] != request_sha256:
                    raise StoreConflict("report effect already exists with different input")
                return self._decode(existing)
            self.connection.execute(
                "INSERT INTO report_outbox(outbox_id,report_key,artifact_key,request_sha256,request_id,status,attempts,remote_id,metadata_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'prepared',0,NULL,'{}',?,?)",
                (record["outbox_id"], report_key, artifact_key, request_sha256, record["request_id"], now, now),
            )
        return record

    def report_effects(self, report_key: str) -> list[dict]:
        return [self._decode(row) for row in self.connection.execute(
            "SELECT * FROM report_outbox WHERE report_key=? ORDER BY created_at,artifact_key", (report_key,)
        )]

    def begin_report_effect(self, report_key: str, artifact_key: str) -> dict:
        with self._transaction():
            row = self.connection.execute(
                "SELECT * FROM report_outbox WHERE report_key=? AND artifact_key=?", (report_key, artifact_key)
            ).fetchone()
            if not row:
                raise KeyError((report_key, artifact_key))
            if row["status"] in {"attempting", "uncertain"}:
                raise UncertainEffect("report effect must be reconciled before another attempt")
            if row["status"] == "confirmed":
                result = self._decode(row)
                result["execute"] = False
                return result
            now = self.clock()
            self.connection.execute(
                "UPDATE report_outbox SET status='attempting',attempts=attempts+1,updated_at=? WHERE outbox_id=?",
                (now, row["outbox_id"]),
            )
        result = self.connection.execute("SELECT * FROM report_outbox WHERE report_key=? AND artifact_key=?",
                                         (report_key, artifact_key)).fetchone()
        payload = self._decode(result)
        payload["execute"] = True
        return payload

    def finish_report_effect(self, report_key: str, artifact_key: str, status: str, *,
                             remote_id: str | None = None, metadata: dict | None = None,
                             reconcile: bool = False):
        if status not in ({"confirmed", "failed"} if reconcile else {"confirmed", "failed", "uncertain"}):
            raise ValueError("invalid report effect outcome")
        if remote_id is not None:
            _name(remote_id)
        encoded = _metadata(metadata)
        expected = {"uncertain"} if reconcile else {"attempting"}
        with self._transaction():
            row = self.connection.execute(
                "SELECT * FROM report_outbox WHERE report_key=? AND artifact_key=?", (report_key, artifact_key)
            ).fetchone()
            if not row or row["status"] not in expected:
                raise StoreConflict("report effect is not in the expected state")
            self.connection.execute(
                "UPDATE report_outbox SET status=?,remote_id=?,metadata_json=?,updated_at=? WHERE outbox_id=?",
                (status, remote_id, encoded, self.clock(), row["outbox_id"]),
            )

    def mark_report_delivered(self, report_key: str):
        required = {"markdown-upload", "markdown-send", "card-send"}
        with self._transaction():
            rows = self.connection.execute(
                "SELECT artifact_key,status FROM report_outbox WHERE report_key=?", (report_key,)
            ).fetchall()
            states = {row["artifact_key"]: row["status"] for row in rows}
            if not required <= set(states) or any(states[key] != "confirmed" for key in required):
                raise StoreConflict("report card and Markdown attachment must both be confirmed")
            changed = self.connection.execute(
                "UPDATE reports SET status='delivered' WHERE report_key=? AND status='pending_delivery'", (report_key,)
            )
            if changed.rowcount != 1:
                raise StoreConflict("report is not pending delivery")
