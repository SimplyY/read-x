#!/usr/bin/env python3
"""Archive frozen Read-X context packages into a reading Base.

The default invocation only inspects frozen local state.  Remote writes require
--execute and are journalled before each non-idempotent operation.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from reading_intake import normalize_url
from article_titles import TITLE_PENDING, can_update_base_title, choose_article_title

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".agents/skills/long-read/scripts"))
from long_read_context_packs import _frozen_inputs
from long_read_workflow_store import CONTEXT_WORKFLOW_CONTRACT_VERSION, state_lock

BASE = "HZLvb3lnUaH3z3sn3plcSRg0nPc"
TABLE = "tbl6IkGyidB5H75v"
FIELDS = {"article-decode.prompt.txt": "深层解码", "munger-analysis.prompt.txt": "芒格洞察"}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_job(state_dir: Path, job_id: str) -> dict:
    """Read and validate the complete frozen snapshot while retention is locked."""
    with state_lock(state_dir):
        db = sqlite3.connect(f"file:{state_dir / 'workflow.sqlite3'}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            row = db.execute("SELECT app_id,source_message_id,payload_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise ValueError("job not found")
            payload = json.loads(row["payload_json"])
            if payload.get("workflow_contract_version") != CONTEXT_WORKFLOW_CONTRACT_VERSION:
                raise ValueError("not a context delivery job")
            if not row["app_id"] or not row["source_message_id"]:
                raise ValueError("source identity missing or scrubbed")
            files = _frozen_inputs(payload, state_dir)
        finally:
            db.close()
    required = {"source.md", "evidence.json", *FIELDS}
    if not required.issubset(files) or any(not files[name] for name in required):
        raise ValueError("frozen input incomplete")
    evidence = json.loads(files["evidence.json"])
    source_url = evidence.get("metadata", {}).get("source_url")
    parsed_url = urlsplit(source_url) if isinstance(source_url, str) else None
    if (not parsed_url or parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname
            or parsed_url.username or parsed_url.password):
        source_url = None
    return {"job_id": job_id, "app_id": row["app_id"], "message_id": row["source_message_id"],
            "snapshot": payload["snapshot_sha256"], "title": choose_article_title([payload.get("title")], source_url or "") or TITLE_PENDING,
            "author": evidence.get("metadata", {}).get("author"), "source_url": source_url, "files": files,
            "article_key": payload.get('article_key','')}


class LarkBase:
    def __init__(self, base: str, table: str):
        self.base, self.table = base, table

    def verify_identity(self, app_id: str) -> None:
        try:
            result = subprocess.run(["lark-cli", "auth", "status", "--json", "--verify"],
                                    capture_output=True, text=True, timeout=30, check=False)
            response = json.loads(result.stdout)
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("cannot verify Base service identity") from exc
        bot = response.get("identities", {}).get("bot", {})
        if result.returncode != 0 or response.get("appId") != app_id or bot.get("verified") is not True:
            raise ValueError("Base service identity does not match the frozen job")

    def call(self, command: str, *args: str, cwd: str | None = None) -> dict:
        cmd = ["lark-cli", "base", command, "--base-token", self.base, "--table-id", self.table,
               *args, "--as", "bot", "--format", "json"]
        try:
            result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=90, check=False)
            response = json.loads(result.stdout)
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("remote result unknown") from exc
        if result.returncode != 0 or response.get("ok") is not True:
            raise RuntimeError("remote operation failed or unknown")
        return response["data"]

    def find(self, key: str) -> list[dict]:
        # Scan every page and compare exact values. Search keywords are partial.
        records = []
        offset = 0
        while True:
            page = self.call("+record-list", "--limit", "200", "--offset", str(offset))
            names, rows, ids = page["fields"], page["data"], page["record_id_list"]
            if len(rows) != len(ids) or "文章键" not in names:
                raise ValueError("invalid Base record page")
            for record_id, values in zip(ids, rows):
                if len(values) != len(names):
                    raise ValueError("invalid Base row")
                fields = dict(zip(names, values))
                if fields["文章键"] == key:
                    records.append({"id": record_id, "fields": fields})
            if not page.get("has_more"):
                return records
            if not rows:
                raise ValueError("Base pagination stalled")
            offset += len(rows)

    def find_source(self, url: str) -> list[dict]:
        expected = normalize_url(url)
        matches, offset = [], 0
        while True:
            page = self.call('+record-list', '--limit', '200', '--offset', str(offset))
            names, rows, ids = page['fields'], page['data'], page['record_id_list']
            if len(rows) != len(ids):
                raise ValueError('invalid Base record page')
            for record_id, values in zip(ids, rows):
                if len(values) != len(names):
                    raise ValueError('invalid Base row')
                fields = dict(zip(names, values))
                source = fields.get('原文链接')
                if not source and fields.get('校验清单'):
                    try:
                        source = json.loads(fields['校验清单']).get('source_url')
                    except (TypeError, ValueError):
                        continue
                try:
                    same = source and normalize_url(source) == expected
                except ValueError:
                    same = False
                if same:
                    matches.append({'id': record_id, 'fields': fields})
            if not page.get('has_more'):
                return matches
            if not rows:
                raise ValueError('Base pagination stalled')
            offset += len(rows)

    def create(self, key: str, title: str) -> None:
        self.call("+record-upsert", "--json", json.dumps({"标题": title, "文章键": key}, ensure_ascii=False))

    def set_manifest(self, record_id: str, manifest: str, fields: dict | None = None) -> None:
        self.call("+record-upsert", "--record-id", record_id,
                  "--json", json.dumps({"校验清单": manifest, **(fields or {})}, ensure_ascii=False))

    def upload(self, record_id: str, field: str, content: bytes) -> None:
        with tempfile.TemporaryDirectory(prefix="readx-archive-") as directory:
            file = Path(directory) / "context.txt"
            file.write_bytes(content)
            os.chmod(file, 0o600)
            self.call("+record-upload-attachment", "--record-id", record_id,
                      "--field-id", field, "--file", file.name, cwd=directory)

    def download(self, record_id: str, token: str) -> bytes:
        with tempfile.TemporaryDirectory(prefix="readx-archive-") as directory:
            self.call("+record-download-attachment", "--record-id", record_id,
                      "--file-token", token, "--output", "context.txt", cwd=directory)
            return (Path(directory) / "context.txt").read_bytes()


def load_journal(path: Path) -> dict:
    if not path.exists():
        return {"schema": 1, "jobs": {}}
    if path.is_symlink() or not path.is_file():
        raise ValueError("unsafe archive journal")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != 1 or not isinstance(value.get("jobs"), dict):
        raise ValueError("invalid archive journal")
    return value


def save_journal(path: Path, journal: dict) -> None:
    payload = json.dumps(journal, ensure_ascii=False, sort_keys=True).encode()
    fd, name = tempfile.mkstemp(prefix=".archive-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def archive(job: dict, base: LarkBase, journal: dict, persist) -> dict:
    key = f"context:{job['app_id']}:{job['message_id']}"
    if job.get('article_key'):
        from reading_article_identity import article_key
        if not job.get('source_url') or article_key(job['source_url'],multi=True) != job['article_key']:
            raise ValueError('frozen article identity mismatch')
        key += f":{job['article_key']}"
    expected = {name: {"sha256": digest(job["files"][name]), "bytes": len(job["files"][name])} for name in FIELDS}
    entry = journal["jobs"].setdefault(job["job_id"], {"key": key, "snapshot": job["snapshot"], "expected": expected})
    if entry.get("key") != key or entry.get("snapshot") != job["snapshot"] or entry.get("expected") != expected:
        raise ValueError("archive journal conflicts with frozen input")
    entry["status"] = "verifying"
    persist()
    record_key = entry.get('record_key', key)
    matches = base.find(record_key)
    if not entry.get('record_key') and job.get('source_url'):
        matches = list({record['id']: record for record in matches + base.find_source(job['source_url'])}.values())
    if len(matches) > 1:
        raise ValueError("multiple Base records match source identity")
    if not matches:
        if entry.get("create_attempted"):
            raise RuntimeError("record creation outcome unresolved")
        entry["create_attempted"] = True
        persist()
        try:
            base.create(key, job["title"])
        except RuntimeError:
            pass
        matches = base.find(key)
        if len(matches) != 1:
            raise RuntimeError("record creation outcome unresolved")
    record = matches[0]
    record_key = record['fields'].get('文章键')
    if not isinstance(record_key, str) or not record_key:
        raise ValueError('Base record key missing')
    entry['record_key'] = record_key
    if entry.get("record_id") not in (None, record["id"]):
        raise ValueError("Base record identity changed")
    entry["record_id"] = record["id"]
    persist()
    for filename, field in FIELDS.items():
        matches = base.find(record_key)
        if len(matches) != 1 or matches[0]["id"] != entry["record_id"]:
            raise ValueError("Base record identity changed")
        attachments = matches[0]["fields"].get(field) or []
        if not isinstance(attachments, list) or len(attachments) > 1:
            raise ValueError("unexpected Base attachments")
        if not attachments:
            if entry.get("upload_attempted", {}).get(field):
                raise RuntimeError("attachment upload outcome unresolved")
            entry.setdefault("upload_attempted", {})[field] = True
            persist()
            try:
                base.upload(entry["record_id"], field, job["files"][filename])
            except RuntimeError:
                pass
            matches = base.find(record_key)
            if len(matches) != 1 or matches[0]["id"] != entry["record_id"]:
                raise RuntimeError("attachment upload outcome unresolved")
            attachments = matches[0]["fields"].get(field) or []
        if len(attachments) != 1 or not attachments[0].get("file_token"):
            raise RuntimeError("attachment upload outcome unresolved")
        token = attachments[0]["file_token"]
        remote = base.download(entry["record_id"], token)
        if len(remote) != expected[filename]["bytes"] or digest(remote) != expected[filename]["sha256"]:
            raise ValueError("Base attachment readback mismatch")
        entry.setdefault("verified", {})[field] = {"file_token": token, **expected[filename]}
        persist()
    manifest = json.dumps({"schema": "readx.context-archive/v1", "source_key": record_key,
                           "source_url": job.get("source_url"), "snapshot_sha256": job["snapshot"],
                           "packs": {"decode": entry["verified"]["深层解码"],
                                     "munger": entry["verified"]["芒格洞察"]}},
                          ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    matches = base.find(record_key)
    if len(matches) != 1 or matches[0]["id"] != entry["record_id"]:
        raise ValueError("Base record identity changed")
    for field, verified in entry["verified"].items():
        current = matches[0]["fields"].get(field) or []
        if len(current) != 1 or current[0].get("file_token") != verified["file_token"]:
            raise ValueError("Base attachment changed during verification")
    existing = matches[0]["fields"].get("校验清单")
    if existing and existing != manifest:
        raise ValueError("Base manifest conflicts with verified attachments")
    if not existing:
        if entry.get("manifest_attempted"):
            raise RuntimeError("manifest update outcome unresolved")
        entry["manifest_attempted"] = True
        persist()
    old = matches[0]["fields"]
    fields = {}
    if old.get("抓取状态") != "已完成":
        fields["抓取状态"] = "已完成"
    if old.get("双包状态") != "已就绪":
        fields["双包状态"] = "已就绪"
    if can_update_base_title(old.get("标题"), job.get("title"), job.get("source_url", "")):
        fields["标题"] = job["title"]
    if job.get("author") and not old.get("作者"):
        fields["作者"] = str(job["author"])[:200]
    if job.get("source_url") and not old.get("原文链接"):
        fields["原文链接"] = job["source_url"]
    if job.get("source_url") and not old.get("来源"):
        fields["来源"] = urlsplit(job["source_url"]).hostname or ""
    if fields or not existing:
        entry["metadata_attempted"] = True
        persist()
        try:
            base.set_manifest(entry["record_id"], manifest, fields)
        except RuntimeError:
            pass
        matches = base.find(record_key)
        if (len(matches) != 1 or matches[0]["id"] != entry["record_id"]
                or matches[0]["fields"].get("校验清单") != manifest
                or any(matches[0]["fields"].get(name) != value for name, value in fields.items())):
            raise RuntimeError("manifest update outcome unresolved")
    entry["metadata_complete"] = True
    entry["status"] = "verified"
    entry["verified_at"] = time.time()
    persist()
    return entry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=ROOT / "state/long-read")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    state = args.state_dir.absolute()
    if not args.execute:
        job = read_job(state, args.job_id)
        print(json.dumps({"status": "dry_run", "job_id": args.job_id,
                          "snapshot": job["snapshot"], "packs": {FIELDS[n]: len(job["files"][n]) for n in FIELDS}}))
        return 0
    lock_path = state / "reading-archive.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        job = read_job(state, args.job_id)
        journal_path = state / "reading-archive-journal.json"
        journal = load_journal(journal_path)
        base = LarkBase(BASE, TABLE)
        base.verify_identity(job["app_id"])
        entry = archive(job, base, journal, lambda: save_journal(journal_path, journal))
        print(json.dumps({"status": entry["status"], "job_id": args.job_id, "record_id": entry["record_id"]}))
        return 0
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"status": "needs_review", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
