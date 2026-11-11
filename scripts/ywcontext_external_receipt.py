"""Validation shared by relevance generation and deterministic scoring."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

RULE_TABLE_SOURCE_ID = "yw-context-rule-table"
RULE_TABLE_SCHEMA = "yw-context-live-classification-rule-table/v1"


class _LiveRuleTableUnavailable(RuntimeError):
    pass


def _canonical_sha256(value) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _repo_root() -> Path:
    return Path(os.environ.get("YW_CONTEXT_ROOT", "/Users/yuwei/code/yw-context")).resolve()


def _assert_ywcontext_repo(root: Path, commit: str) -> None:
    try:
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=root,
            capture_output=True, text=True, timeout=10, check=False,
        )
        object_check = subprocess.run(
            ["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=root,
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("YWContext repository identity could not be verified") from None
    official = (remote.returncode == 0 and re.fullmatch(
        r"(?:https://github\.com/SimplyY/yw-context(?:\.git)?|git@github\.com:SimplyY/yw-context(?:\.git)?)",
        remote.stdout.strip(), re.IGNORECASE,
    ))
    if not official or object_check.returncode != 0 or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise RuntimeError("YWContext repository identity or routed commit is invalid")


def _run_json(command: list[str], root: Path, *, unavailable_on_error: bool = False) -> dict:
    try:
        result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=45, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        if unavailable_on_error:
            raise _LiveRuleTableUnavailable from exc
        raise RuntimeError("pinned YWContext rule snapshot could not be read") from None
    if result.returncode != 0:
        if unavailable_on_error:
            raise _LiveRuleTableUnavailable
        raise RuntimeError("pinned YWContext rule snapshot could not be read")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        if unavailable_on_error:
            raise _LiveRuleTableUnavailable
        raise RuntimeError("pinned YWContext rule snapshot is invalid") from None
    if not isinstance(value, dict):
        raise RuntimeError("YWContext rule readback is not an object")
    return value


def _validate_rule_table_payload(payload: dict, route: dict) -> dict:
    rows = payload.get("rows")
    record_count = payload.get("record_count")
    rows_sha256 = payload.get("rows_sha256")
    if (payload.get("schema") != RULE_TABLE_SCHEMA
            or payload.get("source_id") != RULE_TABLE_SOURCE_ID
            or payload.get("snapshot_commit") != route.get("snapshot_commit")
            or not isinstance(payload.get("source_revision"), str)
            or not re.fullmatch(r"base-revision:[1-9][0-9]*", payload["source_revision"])
            or type(record_count) is not int or record_count < 1
            or not isinstance(rows, list) or len(rows) != record_count
            or not isinstance(rows_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", rows_sha256)
            or _canonical_sha256(rows) != rows_sha256):
        raise RuntimeError("authoritative YWContext rule table readback is invalid")
    return payload


def _read_live_rule_table(route: dict, root: Path) -> dict:
    _assert_ywcontext_repo(root, route.get("snapshot_commit", ""))
    script = root / "scripts" / "read-classification-rules.mjs"
    if not script.is_file():
        raise _LiveRuleTableUnavailable
    payload = _run_json([
        "node", str(script), "--commit", route["snapshot_commit"], "--repo-root", str(root),
    ], root, unavailable_on_error=True)
    return _validate_rule_table_payload(payload, route)


def _read_pinned_rule_table(route: dict, root: Path) -> dict:
    commit = route["snapshot_commit"]
    _assert_ywcontext_repo(root, commit)
    registry = _run_json(["git", "show", f"{commit}:sources/registry.json"], root)
    baseline = _run_json(["git", "show", f"{commit}:sources/classification-baseline.json"], root)
    policy = registry.get("classification_policy")
    expected = policy.get("source_snapshot") if isinstance(policy, dict) else None
    snapshot = baseline.get("rule_table_snapshot")
    if (registry.get("schema") != "yw-context-source-registry/v1"
            or baseline.get("schema") != "yw-context-classification-baseline/v1"
            or not isinstance(policy, dict) or not isinstance(expected, dict)
            or not isinstance(snapshot, dict) or not isinstance(snapshot.get("rows"), list)
            or snapshot.get("complete") is not True
            or baseline.get("policy_sha256") != _canonical_sha256(policy)
            or snapshot.get("source_id") != RULE_TABLE_SOURCE_ID
            or snapshot.get("source_id") != expected.get("source_id")
            or snapshot.get("source_revision") != expected.get("source_revision")
            or snapshot.get("total_records") != expected.get("record_count")
            or len(snapshot["rows"]) != expected.get("record_count")
            or _canonical_sha256(snapshot["rows"]) != expected.get("rows_sha256")):
        raise RuntimeError("pinned YWContext rule table snapshot is incomplete or mismatched")
    payload = {
        "schema": RULE_TABLE_SCHEMA,
        "source_id": RULE_TABLE_SOURCE_ID,
        "source_revision": snapshot["source_revision"],
        "record_count": snapshot["total_records"],
        "rows_sha256": expected["rows_sha256"],
        "rows": snapshot["rows"],
        "snapshot_commit": commit,
        "pinned_policy_status": "pinned_snapshot_only_live_unavailable",
    }
    return _validate_rule_table_payload(payload, route)


def read_authoritative_rule_table(route: dict) -> tuple[dict, str]:
    """Read the live rule table, falling back only to the complete snapshot in the routed commit."""
    root = _repo_root()
    try:
        live = _read_live_rule_table(route, root)
    except _LiveRuleTableUnavailable:
        return _read_pinned_rule_table(route, root), "pinned_snapshot_only_live_unavailable"
    provenance = ("live_readback_matches_pinned" if live.get("pinned_policy_status") == "matches_pinned_policy"
                 else "live_readback_differs_from_pinned")
    return live, provenance


def _rule_table_evidence(table: dict, provenance: str) -> tuple[dict, dict]:
    references = sorted({
        reference.split("&record=", 1)[0]
        for row in table["rows"]
        if isinstance(row, dict) and isinstance((reference := row.get("url")), str) and reference.strip()
    })
    if not references:
        raise RuntimeError("authoritative YWContext rule table has no stable source reference")
    content = {key: table[key] for key in ("source_id", "source_revision", "record_count", "rows_sha256", "rows")}
    content_text = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    metadata = {
        "source_id": RULE_TABLE_SOURCE_ID,
        "status": "verified",
        "source_reference": references[0],
        "source_version": table["source_revision"],
        "sha256": hashlib.sha256(content_text.encode("utf-8")).hexdigest(),
        "verification_provenance": provenance,
    }
    return metadata, {**metadata, "content": content_text}


def verify_external_followup_sources(validated: dict, route: dict, *, reader=None) -> dict:
    """Re-read the rule table; require authority proof before other external content can affect scoring."""
    reader = reader or read_authoritative_rule_table
    table, provenance = reader(route)
    table = _validate_rule_table_payload(table, route)
    trusted_rule_metadata, trusted_rule_prompt_item = _rule_table_evidence(table, provenance)
    metadata_items = []
    prompt_items = []
    for metadata, prompt_item in zip(validated["items"], validated["prompt_items"]):
        status = metadata["status"]
        if metadata["source_id"] == RULE_TABLE_SOURCE_ID:
            if status == "verified" and (
                metadata.get("source_reference") != trusted_rule_metadata["source_reference"]
                or metadata.get("source_version") != trusted_rule_metadata["source_version"]
                or metadata.get("sha256") != trusted_rule_metadata["sha256"]
                or prompt_item.get("content") != trusted_rule_prompt_item["content"]
            ):
                raise RuntimeError("verified rule table receipt does not match the authoritative source readback")
            metadata_items.append(trusted_rule_metadata)
            prompt_items.append(trusted_rule_prompt_item)
            continue
        if status == "not_relevant":
            raise RuntimeError("not_relevant claims require a source-completeness verifier; record unavailable until supported")
        if status != "verified":
            metadata_items.append(metadata)
            prompt_items.append(prompt_item)
            continue
        raise RuntimeError(f"no authoritative source verifier is registered for {metadata['source_id']}")
    if len(metadata_items) != len(validated["items"]) or len(prompt_items) != len(validated["prompt_items"]):
        raise RuntimeError("external followup receipt item pairing is invalid")
    return {"items": metadata_items, "prompt_items": prompt_items}


def validate_external_followup_receipt(receipt: dict, route: dict) -> dict:
    """Bind every external followup to the pinned route and validate its evidence."""
    if not isinstance(receipt, dict) or receipt.get("schema") != "yw-context-external-followup-receipt/v1":
        raise RuntimeError("external followup receipt schema is invalid")
    if (receipt.get("snapshot_commit") != route.get("snapshot_commit")
            or receipt.get("manifest_sha256") != route.get("manifest_sha256")):
        raise RuntimeError("external followup receipt does not match the pinned YWContext route")
    checked_at = receipt.get("checked_at")
    if not isinstance(checked_at, str):
        raise RuntimeError("external followup receipt timestamp is missing")
    try:
        timestamp = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("external followup receipt timestamp is invalid") from exc
    if timestamp.tzinfo is None:
        raise RuntimeError("external followup receipt timestamp requires a timezone")

    expected = {item["source_id"] for item in route.get("external_followups", [])}
    items = receipt.get("items")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise RuntimeError("external followup receipt items are invalid")
    actual_ids = [item.get("source_id") for item in items]
    if (any(not isinstance(source_id, str) or not source_id for source_id in actual_ids)
            or len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != expected):
        raise RuntimeError("external followup receipt must assess every routed followup exactly once")

    verified = []
    prompt_items = []
    total_chars = 0
    for item in items:
        source_id = item["source_id"]
        status = item.get("status")
        reason = item.get("reason")
        if status == "verified":
            reference = item.get("source_reference")
            version = item.get("source_version")
            content = item.get("content")
            digest = item.get("sha256")
            try:
                content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest() if isinstance(content, str) else None
            except UnicodeEncodeError:
                content_digest = None
            if (not isinstance(reference, str) or not reference.strip()
                    or not isinstance(version, str) or not version.strip()
                    or not isinstance(content, str) or not content.strip()
                    or len(content) > 100_000
                    or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                    or content_digest != digest):
                raise RuntimeError("verified external followup must include source identity, version, content, and matching SHA-256")
            total_chars += len(content)
            verified.append({"source_id": source_id, "status": status, "source_reference": reference,
                             "source_version": version, "sha256": digest})
            prompt_items.append({"source_id": source_id, "status": status, "source_reference": reference,
                                 "source_version": version, "sha256": digest, "content": content})
        elif status in {"unavailable", "not_relevant"}:
            body = item.get("content")
            if not isinstance(reason, str) or not reason.strip() or (body is not None and body != ""):
                raise RuntimeError("unavailable/not_relevant followup must include a reason and no source body")
            verified.append({"source_id": source_id, "status": status, "reason": reason.strip()})
            prompt_items.append({"source_id": source_id, "status": status, "reason": reason.strip()})
        else:
            raise RuntimeError("external followup status is invalid")
    if total_chars > 500_000:
        raise RuntimeError("external followup receipt content exceeds the scoring context limit")
    return {"items": verified, "prompt_items": prompt_items}
