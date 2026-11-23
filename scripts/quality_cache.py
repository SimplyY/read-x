#!/usr/bin/env python3
"""Sqlite-backed cache for quality generation outputs (stdlib only, fail-open).

Cache failures are reported through an optional warning callback and never
propagate: a broken cache must never fail a scoring run.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import content_scoring as scoring

DEFAULT_DB_PATH = Path(__file__).parents[1] / "state" / "quality-cache.sqlite"
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS quality_cache ("
    "cache_key TEXT PRIMARY KEY, output TEXT NOT NULL, created_at TEXT NOT NULL)"
)


def _warn_default(message: str) -> None:
    print(f"quality_cache warning: {message}", file=sys.stderr, flush=True)


def _connect(db_path: Path | None) -> tuple[sqlite3.Connection, Path]:
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return sqlite3.connect(str(path), timeout=5), path


def cache_key(source_text: str, runtime_md: str, generator_digest: str | None = None) -> str:
    runtime_digest = hashlib.sha256(runtime_md.encode("utf-8")).hexdigest()
    payload = f"{scoring.content_fingerprint(source_text)}\n{runtime_digest}\n{generator_digest or ''}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get(key: str, *, db_path: Path | None = None, warning=None) -> str | None:
    warn = warning or _warn_default
    try:
        conn, _ = _connect(db_path)
        try:
            conn.execute(_SCHEMA)
            row = conn.execute("SELECT output FROM quality_cache WHERE cache_key = ?", (key,)).fetchone()
        finally:
            conn.close()
        if row is None or not isinstance(row[0], str):
            return None
        return row[0]
    except Exception as exc:
        warn(f"get failed for key {str(key)[:12]}: {exc}")
        return None


def put(key: str, output: str, *, db_path: Path | None = None, warning=None) -> None:
    warn = warning or _warn_default
    try:
        if not isinstance(key, str) or not isinstance(output, str):
            raise ValueError("cache key and output must be strings")
        conn, path = _connect(db_path)
        try:
            conn.execute(_SCHEMA)
            conn.execute(
                "INSERT INTO quality_cache (cache_key, output, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(cache_key) DO UPDATE SET output = excluded.output, created_at = excluded.created_at",
                (key, output, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception as exc:
        warn(f"put failed for key {str(key)[:12]}: {exc}")
