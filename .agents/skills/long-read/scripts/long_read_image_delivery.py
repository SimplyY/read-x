#!/usr/bin/env python3
"""Durable per-image generation, document insertion, and private-message delivery."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

import run_chatgpt_core_image as core_image
from feishu_long_read_delivery import (
    DeliveryError, FeishuLongReadDelivery, RetryableDeliveryError, UnknownRemoteResult,
    match_image_messages, verify_image_message, message_rate_slot,
)
from long_read_workflow_store import WorkflowStore


RETRY_DELAY_SECONDS = 240
MAX_IMAGE_RETRIES = 2


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", value)[:120]


def _write_private(path: Path, payload: bytes) -> None:
    path = path.absolute()
    allowed_aliases = {Path("/tmp"), Path("/var")}
    if any(part.is_symlink() and part not in allowed_aliases for part in (path, *path.parents)):
        raise ValueError("image workflow files must not traverse symlinks")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
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
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("persisted image request is missing or unsafe")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("persisted image request is invalid")
    return value


def _action(base: str, round_number: int) -> str:
    return base if round_number == 0 else f"{base}:retry-{round_number}"


def _action_round(action: str) -> int:
    match = re.search(r":retry-([1-2])\Z", action)
    return int(match.group(1)) if match else 0


def _select_effect(store: WorkflowStore, job_id: str, artifact_key: str,
                   base_action: str, round_number: int) -> tuple[dict | None, str]:
    current_action = _action(base_action, round_number)
    previous = store.latest_effect_for_action(job_id, artifact_key, base_action)
    if previous is None:
        return None, current_action
    previous_round = _action_round(previous["action"])
    if previous["status"] == "failed" and previous_round < round_number:
        return None, current_action
    return previous, previous["action"]


def _artifact(store: WorkflowStore, job_id: str, artifact_key: str) -> dict:
    return next(item for item in store.artifacts(job_id) if item["artifact_key"] == artifact_key)


def _mark_failure(store: WorkflowStore, lease, image_key: str, *, stage: str,
                  reason: str, retryable: bool, clock) -> dict:
    artifact = _artifact(store, lease.job_id, image_key)
    retry_count = artifact["retry_count"]
    can_retry = retryable and retry_count < MAX_IMAGE_RETRIES
    metadata = {
        "failure_stage": _safe_name(stage),
        "reason_code": _safe_name(reason),
        "retryable": can_retry,
    }
    if can_retry:
        metadata["next_retry_at"] = float(clock()) + RETRY_DELAY_SECONDS
    store.set_artifact(lease, image_key, "failed", metadata=metadata)
    return {"status": "retry_scheduled" if can_retry else "failed",
            "failure_stage": stage, "reason_code": reason,
            "next_retry_at": metadata.get("next_retry_at"),
            "retries_used": retry_count}


def _mark_uncertain(store: WorkflowStore, lease, image_key: str, *, stage: str,
                    reason: str) -> dict:
    store.set_artifact(lease, image_key, "uncertain", metadata={
        "failure_stage": _safe_name(stage), "reason_code": _safe_name(reason),
        "retryable": False,
    })
    return {"status": "uncertain", "failure_stage": stage, "reason_code": reason}


def _prompt_request(*, source_path: Path, conversation_url: str | None,
                    request_id: str, prompt_asset: dict) -> tuple[dict, str]:
    source = core_image._read_regular(source_path, "source")
    asset_content = str(prompt_asset.get("content") or "")
    prompt = core_image.build_prompt(source, asset_content)
    prompt_sha = str(prompt_asset.get("prompt_sha256") or "")
    if (prompt_asset.get("prompt_id") != core_image.PROMPT_GOVERNANCE_ID or not asset_content.strip()
            or not re.fullmatch(r"[0-9a-f]{64}", prompt_sha)
            or hashlib.sha256(asset_content.encode("utf-8")).hexdigest() != prompt_sha):
        raise ValueError("current governed image Prompt is incomplete or has a mismatched hash")
    requested_url = conversation_url or core_image.WORKFLOW_NEW_CHAT_URL
    body = {
        "schema": "read-x-image-request/v1", "run_id": request_id,
        "caller_label": core_image.CALLER_LABEL,
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "conversation_url_requested": requested_url,
        "prompt_id": core_image.PROMPT_GOVERNANCE_ID,
        "prompt_source": prompt_asset.get("prompt_source"),
        "prompt_revision": prompt_asset.get("prompt_revision"),
        "prompt_sha256": prompt_sha,
        "prompt_fetched_at": prompt_asset.get("prompt_fetched_at"),
        "input_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "asset": prompt_asset,
    }
    digest = _sha(_canonical({key: body[key] for key in (
        "source_sha256", "conversation_url_requested", "prompt_id", "prompt_sha256", "input_sha256",
    )}))
    return body, digest


def _summary_seed(request: dict) -> dict:
    return {
        "runId": request["run_id"], "run_id": request["run_id"],
        "caller_label": request["caller_label"],
        "source_sha256": request["source_sha256"],
        "conversation_url_requested": request["conversation_url_requested"],
        "prompt_id": request["prompt_id"], "prompt_source": request.get("prompt_source"),
        "prompt_revision": request.get("prompt_revision"),
        "prompt_sha256": request["prompt_sha256"],
        "prompt_fetched_at": request.get("prompt_fetched_at"),
        "input_sha256": request["input_sha256"],
        "status": "prepared", "attempts": [],
    }


def _round_paths(root: Path, image_name: str, round_number: int) -> tuple[Path, Path, Path]:
    directory = root / "images"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    label = _safe_name(image_name)
    output = directory / f"{label}-round-{round_number}.png"
    summary = directory / f"{label}-round-{round_number}.summary.json"
    request = directory / f"{label}-round-{round_number}.request.json"
    return output, summary, request


def _materialize_recovered_image(*, image_runner, source_path: Path, output: Path,
                                 summary: Path, request: dict, effect, observe_fn) -> dict:
    if not summary.exists():
        _write_private(summary, _canonical(_summary_seed(request)) + b"\n")
    try:
        args = {"request_id": effect["request_id"]}
        if observe_fn is not None:
            args["observe_fn"] = observe_fn
        result = image_runner.recover(source_path, output, summary, **args)
    except Exception:
        return {"status": "uncertain", "reason_code": "bridge-image-recovery-failed"}
    if result.get("status") != "succeeded":
        return {"status": "uncertain", "reason_code": "bridge-image-recovery-unconfirmed"}
    return {"status": "confirmed", "result": result}


def _prepare_generation(*, store: WorkflowStore, lease, image_key: str, image_name: str,
                        source_path: Path, root: Path, round_number: int,
                        conversation_url: str | None, prompt_fetcher, image_runner) -> dict:
    effect, action = _select_effect(store, lease.job_id, image_key, "image-generate", round_number)
    effect_round = _action_round(effect["action"]) if effect else round_number
    output, summary, request_path = _round_paths(root, image_name, effect_round)

    if effect is None:
        try:
            if request_path.exists() or request_path.is_symlink():
                request = _read_json(request_path)
                asset = request.get("asset")
                request_id = request.get("run_id")
                if not isinstance(asset, dict) or not isinstance(request_id, str):
                    raise ValueError("saved image request is incomplete")
                verified, digest = _prompt_request(
                    source_path=source_path, conversation_url=conversation_url,
                    request_id=request_id, prompt_asset=asset,
                )
                if verified != request:
                    raise ValueError("saved image request no longer matches its source")
            else:
                asset = prompt_fetcher(core_image.PROMPT_GOVERNANCE_ID)
                request_id = str(uuid.uuid4())
                request, digest = _prompt_request(
                    source_path=source_path, conversation_url=conversation_url,
                    request_id=request_id, prompt_asset=asset,
                )
                _write_private(request_path, _canonical(request) + b"\n")
                _write_private(summary, _canonical(_summary_seed(request)) + b"\n")
            effect = store.prepare_effect(
                lease, image_key, action, digest, request_id=request_id,
            )
        except Exception as exc:
            return {"status": "failed", "reason_code": "image-prompt-or-request-invalid",
                    "detail": type(exc).__name__}
    else:
        try:
            request = _read_json(request_path)
            asset = request.get("asset")
            if (request.get("run_id") != effect["request_id"] or not isinstance(asset, dict)
                    or request.get("schema") != "read-x-image-request/v1"):
                raise ValueError("saved image request identity mismatch")
            _, digest = _prompt_request(
                source_path=source_path, conversation_url=conversation_url,
                request_id=effect["request_id"], prompt_asset=asset,
            )
            if digest != effect["request_sha256"]:
                raise ValueError("saved image request hash mismatch")
        except Exception:
            return {"status": "needs_review", "reason_code": "image-request-receipt-invalid"}

    if effect["status"] == "confirmed":
        try:
            body = image_runner._reuse_image(output, _read_json(summary),
                                             request["source_sha256"], effect["request_id"],
                                             request["conversation_url_requested"])
        except Exception:
            recovered = _materialize_recovered_image(
                image_runner=image_runner, source_path=source_path, output=output,
                summary=summary, request=request, effect=effect,
                observe_fn=getattr(image_runner, "observe_bridge", None),
            )
            if recovered["status"] != "confirmed":
                return {"status": "uncertain", "reason_code": "confirmed-image-local-output-missing"}
            body = recovered["result"]
        canonical = root / "images" / f"{image_name}.png"
        if output != canonical and output.is_file():
            _write_private(canonical, output.read_bytes())
        return {"status": "confirmed", "result": body, "effect": effect,
                "output": output, "summary": summary, "reused": True}

    if effect["status"] in {"attempting", "uncertain"}:
        if effect["status"] == "attempting":
            store.finish_effect(
                lease, effect["op_key"], "uncertain",
                metadata={"failure_stage": "image-generate", "reason_code": "worker-interrupted"},
            )
            effect = store.get_effect(effect["op_key"])
        recovered = _materialize_recovered_image(
            image_runner=image_runner, source_path=source_path, output=output,
            summary=summary, request=request, effect=effect,
            observe_fn=getattr(image_runner, "observe_bridge", None),
        )
        if recovered["status"] != "confirmed":
            return {"status": "uncertain", "reason_code": recovered.get("reason_code", "bridge-image-recovery-unconfirmed")}
        store.reconcile_effect(
            lease, effect["op_key"], "confirmed", remote_id=effect["request_id"],
            metadata={"verification": "bridge-observe-image-recovered",
                      "content_sha256": recovered["result"]["image_sha256"]},
        )
        store.set_artifact(lease, image_key, "confirmed", metadata={
            "verification": "bridge-observe-image-recovered",
            "content_sha256": recovered["result"]["image_sha256"],
        })
        canonical = root / "images" / f"{image_name}.png"
        if output != canonical and output.is_file():
            _write_private(canonical, output.read_bytes())
        return {"status": "confirmed", "result": recovered["result"], "effect": effect,
                "output": output, "summary": summary, "recovered": True}

    if effect["status"] == "failed":
        return {"status": "failed", "reason_code": "previous-known-image-generation-failure"}
    if effect["status"] != "prepared":
        return {"status": "needs_review", "reason_code": "unsupported-image-effect-state"}

    attempt = store.begin_effect(lease, effect["op_key"])
    try:
        result = image_runner.run(
            source_path, output, summary_path=summary,
            conversation_url=request["conversation_url_requested"],
            prompt_fetcher=lambda _prompt_id: asset,
            request_id=attempt["request_id"],
        )
    except Exception:
        store.finish_effect(
            lease, effect["op_key"], "uncertain",
            metadata={"failure_stage": "image-generate", "reason_code": "image-runner-exception"},
        )
        effect = store.get_effect(effect["op_key"])
        recovered = _materialize_recovered_image(
            image_runner=image_runner, source_path=source_path, output=output,
            summary=summary, request=request, effect=effect,
            observe_fn=getattr(image_runner, "observe_bridge", None),
        )
        if recovered["status"] != "confirmed":
            return {"status": "uncertain", "reason_code": "bridge-image-recovery-unconfirmed"}
        store.reconcile_effect(
            lease, effect["op_key"], "confirmed", remote_id=effect["request_id"],
            metadata={"verification": "bridge-observe-image-recovered",
                      "content_sha256": recovered["result"]["image_sha256"]},
        )
        store.set_artifact(lease, image_key, "confirmed", metadata={
            "verification": "bridge-observe-image-recovered",
            "content_sha256": recovered["result"]["image_sha256"],
        })
        canonical = root / "images" / f"{image_name}.png"
        if output != canonical and output.is_file():
            _write_private(canonical, output.read_bytes())
        return {"status": "confirmed", "result": recovered["result"], "effect": effect,
                "output": output, "summary": summary, "recovered": True}

    if result.get("status") != "succeeded":
        bridge = result.get("bridge") or {}
        safe_pre_submit = (result.get("submissionConfirmed") is False
                           and result.get("reason") in {
                               "bridge-busy", "local-rate-limit-cooldown",
                               "recovery-capability-unavailable", "invalid-bridge-transport",
                               "invalid-mode", "invalid-request-id",
                           })
        if safe_pre_submit:
            retryable = result.get("reason") in {"bridge-busy", "local-rate-limit-cooldown"}
            store.finish_effect(
                lease, effect["op_key"], "failed",
                metadata={"failure_stage": "image-generate", "reason_code": result.get("reason", "pre-submit-rejected"),
                          "retryable": retryable},
            )
            return {"status": "failed", "reason_code": result.get("reason", "pre-submit-rejected"),
                    "retryable": retryable, "effect": effect}
        store.finish_effect(
            lease, effect["op_key"], "uncertain",
            metadata={"failure_stage": "image-generate", "reason_code": "bridge-image-result-unknown"},
        )
        return {"status": "uncertain", "reason_code": "bridge-image-result-unknown"}

    if (result.get("runId") != effect["request_id"] or not output.is_file()
            or result.get("submissionConfirmed") is not True
            or result.get("verification") != "live-dom+snapshot"):
        store.finish_effect(
            lease, effect["op_key"], "uncertain",
            metadata={"failure_stage": "image-generate", "reason_code": "bridge-image-receipt-mismatch"},
        )
        return {"status": "uncertain", "reason_code": "bridge-image-receipt-mismatch"}
    try:
        checked = image_runner._reuse_image(
            output, _read_json(summary), request["source_sha256"], effect["request_id"],
            request["conversation_url_requested"],
        )
    except Exception:
        store.finish_effect(
            lease, effect["op_key"], "uncertain",
            metadata={"failure_stage": "image-generate", "reason_code": "bridge-image-local-readback-failed"},
        )
        return {"status": "uncertain", "reason_code": "bridge-image-local-readback-failed"}
    store.finish_effect(
        lease, effect["op_key"], "confirmed", remote_id=effect["request_id"],
        metadata={"verification": "bridge-live-dom-snapshot",
                  "content_sha256": checked["image_sha256"]},
    )
    store.set_artifact(lease, image_key, "confirmed", metadata={
        "verification": "bridge-live-dom-snapshot", "content_sha256": checked["image_sha256"],
    })
    canonical = root / "images" / f"{image_name}.png"
    if output != canonical:
        _write_private(canonical, output.read_bytes())
    return {"status": "confirmed", "result": checked, "effect": effect,
            "output": canonical, "summary": summary}


def _top_level_blocks(client, document_id: str) -> list[dict]:
    blocks = client.list_document_blocks(document_id)
    selected = [item for item in blocks if item.get("parent_id") in {None, document_id}]
    if len(selected) != len(blocks) or not selected:
        raise DeliveryError("document root block listing is incomplete or contains nested blocks")
    return selected


def _stage_round(store: WorkflowStore, job_id: str, image_key: str) -> int:
    return _artifact(store, job_id, image_key)["retry_count"]


def _existing_effect(store: WorkflowStore, job_id: str, artifact_key: str,
                     base_action: str) -> dict | None:
    return store.latest_effect_for_action(job_id, artifact_key, base_action)


def _document_insert(*, store: WorkflowStore, lease, image_key: str, image_name: str,
                     document_id: str, png_path: Path, image_index: int, root: Path,
                     client, round_number: int, clock) -> dict:
    insert_key = f"{image_key}-insert"
    block_effect, block_action = _select_effect(
        store, lease.job_id, insert_key, "image-block-create", round_number,
    )
    image_sha = _sha(png_path.read_bytes())
    insert_dir = root / "images" / "insert"
    insert_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(insert_dir, 0o700)
    effect_round = _action_round(block_effect["action"]) if block_effect else round_number
    plan_path = insert_dir / f"{_safe_name(image_name)}-round-{effect_round}.json"
    if block_effect is None:
        try:
            before = _top_level_blocks(client, document_id)
            before_ids = [item["block_id"] for item in before]
            if image_index < 0 or image_index > len(before_ids):
                raise DeliveryError("document does not have the expected title/anchor position")
            plan = {"schema": "read-x-image-insert/v1", "document_id": document_id,
                    "parent_block_id": document_id, "image_index": image_index,
                    "before_child_ids": before_ids, "image_sha256": image_sha}
            body = _canonical(plan)
            _write_private(plan_path, body + b"\n")
            block_effect = store.prepare_effect(lease, insert_key, block_action, _sha(body))
        except Exception as exc:
            return {"status": "failed", "failure_stage": "image-block-create",
                    "reason_code": "image-block-create-preflight-failed", "retryable": False,
                    "detail": type(exc).__name__}
    else:
        try:
            plan = _read_json(plan_path)
            if (plan.get("document_id") != document_id or plan.get("image_sha256") != image_sha
                    or plan.get("schema") != "read-x-image-insert/v1"):
                raise ValueError("image block create plan identity mismatch")
        except Exception:
            return {"status": "needs_review", "reason_code": "image-block-create-plan-invalid"}

    if block_effect["status"] == "confirmed":
        block_id = block_effect.get("remote_id")
        try:
            block = client.get_document_block(document_id, block_id)
            if block.get("block_type") != 27 or block.get("parent_id") != document_id:
                raise UnknownRemoteResult("confirmed image block readback changed")
        except Exception:
            return {"status": "uncertain", "failure_stage": "image-block-create",
                    "reason_code": "image-block-confirmation-lost"}
    elif block_effect["status"] in {"attempting", "uncertain"}:
        if block_effect["status"] == "attempting":
            store.finish_effect(lease, block_effect["op_key"], "uncertain",
                                metadata={"failure_stage": "image-block-create", "reason_code": "worker-interrupted"})
            block_effect = store.get_effect(block_effect["op_key"])
        try:
            if block_effect.get("remote_id"):
                block_id = block_effect["remote_id"]
                block = client.get_document_block(document_id, block_id)
                if block.get("block_type") != 27 or block.get("parent_id") != document_id:
                    raise UnknownRemoteResult("known image block id no longer matches its parent")
            else:
                current = _top_level_blocks(client, document_id)
                after_ids = [item["block_id"] for item in current]
                blocks_by_id = {item["block_id"]: item for item in current}
                block_id = client.verify_new_image_child(
                    before_child_ids=plan["before_child_ids"], after_child_ids=after_ids,
                    index=plan["image_index"], blocks_by_id=blocks_by_id,
                    parent_block_id=plan["parent_block_id"],
                )
            store.reconcile_effect(
                lease, block_effect["op_key"], "confirmed", remote_id=block_id,
                metadata={"verification": "exact-ordered-document-block-diff",
                          "content_sha256": image_sha},
            )
            block_effect = store.get_effect(block_effect["op_key"])
        except Exception:
            return {"status": "uncertain", "failure_stage": "image-block-create",
                    "reason_code": "image-block-create-result-unconfirmed"}
    elif block_effect["status"] == "failed":
        return {"status": "failed", "failure_stage": "image-block-create",
                "reason_code": "previous-image-block-create-failure",
                "retryable": block_effect.get("metadata", {}).get("retryable") is True}
    elif block_effect["status"] == "prepared":
        effect = store.begin_effect(lease, block_effect["op_key"])
        block_id = None
        try:
            block_id = client.create_image_block(
                document_id=document_id, parent_block_id=document_id,
                index=image_index, client_token=effect["request_id"],
            )
            try:
                block = client.get_document_block(document_id, block_id)
            except Exception as exc:
                raise UnknownRemoteResult("created image block readback is unavailable") from exc
            if block.get("block_type") != 27 or block.get("parent_id") != document_id:
                raise UnknownRemoteResult("created image block readback does not match")
        except UnknownRemoteResult:
            store.finish_effect(lease, effect["op_key"], "uncertain", remote_id=block_id,
                                metadata={"failure_stage": "image-block-create", "reason_code": "create-result-unknown"})
            return _document_insert(
                store=store, lease=lease, image_key=image_key, image_name=image_name,
                document_id=document_id, png_path=png_path, image_index=image_index,
                root=root, client=client, round_number=round_number, clock=clock,
            )
        except DeliveryError as exc:
            retryable = isinstance(exc, RetryableDeliveryError)
            store.finish_effect(lease, effect["op_key"], "failed",
                                metadata={"failure_stage": "image-block-create", "reason_code": "api-rejected",
                                          "retryable": retryable})
            return {"status": "failed", "failure_stage": "image-block-create",
                    "reason_code": "image-block-create-rejected", "retryable": retryable}
        else:
            store.finish_effect(
                lease, effect["op_key"], "confirmed", remote_id=block_id,
                metadata={"verification": "image-block-id-parent-readback",
                          "content_sha256": image_sha},
            )
            block_effect = store.get_effect(effect["op_key"])
    else:
        return {"status": "needs_review", "reason_code": "unsupported-image-block-create-state"}

    block_id = block_effect.get("remote_id")
    upload_effect, upload_action = _select_effect(
        store, lease.job_id, insert_key, "image-upload", round_number,
    )
    upload_sha = _sha(_canonical({"document_id": document_id, "block_id": block_id,
                                  "image_sha256": image_sha, "parent_type": "docx_image"}))
    if upload_effect is None:
        upload_effect = store.prepare_effect(lease, insert_key, upload_action, upload_sha)
    elif upload_effect["request_sha256"] != upload_sha:
        return {"status": "needs_review", "reason_code": "image-upload-request-changed"}
    if upload_effect["status"] == "confirmed":
        file_token = upload_effect.get("remote_id")
    elif upload_effect["status"] in {"attempting", "uncertain"}:
        if upload_effect["status"] == "attempting":
            store.finish_effect(lease, upload_effect["op_key"], "uncertain",
                                remote_id=upload_effect.get("remote_id"),
                                metadata={"failure_stage": "image-upload", "reason_code": "worker-interrupted"})
            upload_effect = store.get_effect(upload_effect["op_key"])
        try:
            block = client.get_document_block(document_id, block_id)
            image = block.get("image") or {}
            file_token = image.get("token") if isinstance(image, dict) else None
            if not isinstance(file_token, str) or not file_token:
                raise UnknownRemoteResult("uploaded image token is not visible on the exact block")
            store.reconcile_effect(
                lease, upload_effect["op_key"], "confirmed", remote_id=file_token,
                metadata={"verification": "uploaded-token-read-from-target-block",
                          "content_sha256": image_sha},
            )
        except Exception:
            return {"status": "uncertain", "failure_stage": "image-upload",
                    "reason_code": "image-upload-result-unconfirmed"}
    elif upload_effect["status"] == "failed":
        return {"status": "failed", "failure_stage": "image-upload",
                "reason_code": "previous-image-upload-failure",
                "retryable": upload_effect.get("metadata", {}).get("retryable") is True}
    elif upload_effect["status"] == "prepared":
        attempt = store.begin_effect(lease, upload_effect["op_key"])
        try:
            file_token = client.upload_document_image(
                document_id=document_id, image_block_id=block_id, png_path=png_path,
            )
        except UnknownRemoteResult:
            store.finish_effect(lease, attempt["op_key"], "uncertain",
                                metadata={"failure_stage": "image-upload", "reason_code": "upload-result-unknown"})
            return _document_insert(
                store=store, lease=lease, image_key=image_key, image_name=image_name,
                document_id=document_id, png_path=png_path, image_index=image_index,
                root=root, client=client, round_number=round_number, clock=clock,
            )
        except DeliveryError as exc:
            retryable = isinstance(exc, RetryableDeliveryError)
            store.finish_effect(lease, attempt["op_key"], "failed",
                                metadata={"failure_stage": "image-upload", "reason_code": "api-rejected",
                                          "retryable": retryable})
            return {"status": "failed", "failure_stage": "image-upload",
                    "reason_code": "image-upload-rejected", "retryable": retryable}
        else:
            store.finish_effect(
                lease, attempt["op_key"], "confirmed", remote_id=file_token,
                metadata={"verification": "upload-receipt",
                          "content_sha256": image_sha},
            )
            upload_effect = store.get_effect(attempt["op_key"])
    else:
        return {"status": "needs_review", "reason_code": "unsupported-image-upload-state"}

    patch_effect, patch_action = _select_effect(
        store, lease.job_id, insert_key, "image-block-patch", round_number,
    )
    patch_sha = _sha(_canonical({"document_id": document_id, "block_id": block_id,
                                 "file_token": file_token, "image_sha256": image_sha}))
    if patch_effect is None:
        patch_effect = store.prepare_effect(lease, insert_key, patch_action, patch_sha)
    elif patch_effect["request_sha256"] != patch_sha:
        return {"status": "needs_review", "reason_code": "image-patch-request-changed"}
    if patch_effect["status"] == "confirmed":
        try:
            client.verify_document_image(
                document_id=document_id, block_id=block_id,
                parent_block_id=document_id, file_token=file_token,
            )
        except Exception:
            return {"status": "uncertain", "failure_stage": "image-block-patch",
                    "reason_code": "image-patch-confirmation-lost"}
    elif patch_effect["status"] in {"attempting", "uncertain"}:
        if patch_effect["status"] == "attempting":
            store.finish_effect(lease, patch_effect["op_key"], "uncertain",
                                metadata={"failure_stage": "image-block-patch", "reason_code": "worker-interrupted"})
            patch_effect = store.get_effect(patch_effect["op_key"])
        try:
            client.verify_document_image(
                document_id=document_id, block_id=block_id,
                parent_block_id=document_id, file_token=file_token,
            )
            store.reconcile_effect(
                lease, patch_effect["op_key"], "confirmed", remote_id=block_id,
                metadata={"verification": "exact-image-token-parent-readback",
                          "content_sha256": image_sha},
            )
        except Exception:
            return {"status": "uncertain", "failure_stage": "image-block-patch",
                    "reason_code": "image-patch-result-unconfirmed"}
    elif patch_effect["status"] == "failed":
        return {"status": "failed", "failure_stage": "image-block-patch",
                "reason_code": "previous-image-patch-failure",
                "retryable": patch_effect.get("metadata", {}).get("retryable") is True}
    elif patch_effect["status"] == "prepared":
        attempt = store.begin_effect(lease, patch_effect["op_key"])
        try:
            client.patch_image_block(
                document_id=document_id, block_id=block_id,
                file_token=file_token, client_token=attempt["request_id"],
            )
            try:
                client.verify_document_image(
                    document_id=document_id, block_id=block_id,
                    parent_block_id=document_id, file_token=file_token,
                )
            except Exception as exc:
                raise UnknownRemoteResult("patched image block readback is unavailable") from exc
        except UnknownRemoteResult:
            store.finish_effect(lease, attempt["op_key"], "uncertain",
                                metadata={"failure_stage": "image-block-patch", "reason_code": "patch-result-unknown"})
            return _document_insert(
                store=store, lease=lease, image_key=image_key, image_name=image_name,
                document_id=document_id, png_path=png_path, image_index=image_index,
                root=root, client=client, round_number=round_number, clock=clock,
            )
        except DeliveryError as exc:
            retryable = isinstance(exc, RetryableDeliveryError)
            store.finish_effect(lease, attempt["op_key"], "failed",
                                metadata={"failure_stage": "image-block-patch", "reason_code": "api-rejected",
                                          "retryable": retryable})
            return {"status": "failed", "failure_stage": "image-block-patch",
                    "reason_code": "image-patch-rejected", "retryable": retryable}
        else:
            store.finish_effect(
                lease, attempt["op_key"], "confirmed", remote_id=block_id,
                metadata={"verification": "exact-image-token-parent-readback",
                          "content_sha256": image_sha},
            )
            patch_effect = store.get_effect(attempt["op_key"])
    else:
        return {"status": "needs_review", "reason_code": "unsupported-image-patch-state"}

    if not store.set_artifact:
        raise AssertionError("workflow store artifact API is unavailable")
    store.set_artifact(lease, insert_key, "confirmed", metadata={
        "verification": "image-block-create-upload-patch-readback",
        "content_sha256": image_sha,
    })
    return {"status": "confirmed", "block_id": block_id, "image_sha256": image_sha}


def _verify_message(client, message_id: str, image_key: str, *, sender_id: str,
                    chat_id: str | None) -> bool:
    try:
        message = client.get_message(message_id)
    except DeliveryError:
        return False
    return verify_image_message(
        message, message_id=message_id, image_key=image_key,
        sender_id=sender_id, sender_id_type="app_id", chat_id=chat_id,
    )


def _send_image(*, store: WorkflowStore, lease, image_key: str, image_name: str,
                png_path: Path, trigger: dict, sender_app_id: str, client,
                round_number: int, clock) -> dict:
    message_artifact = f"{image_key}-send"
    if trigger.get("chatType") == "p2p":
        target = {"chat_id": trigger.get("chatId")}
        history_chat_id = trigger.get("chatId")
    elif trigger.get("chatType") == "group":
        if not trigger.get("privateChatId"):
            return {"status": "needs_review", "failure_stage": "image-send",
                    "reason_code": "private-chat-readback-target-missing"}
        target = {"user_id": trigger.get("senderId")}
        history_chat_id = trigger.get("privateChatId")
    else:
        return {"status": "failed", "failure_stage": "image-send",
                "reason_code": "unsupported-private-message-target", "retryable": False}
    if not next(iter(target.values()), None):
        return {"status": "failed", "failure_stage": "image-send",
                "reason_code": "missing-private-message-target", "retryable": False}

    image_sha = _sha(png_path.read_bytes())
    upload_effect, upload_action = _select_effect(
        store, lease.job_id, message_artifact, "message-image-upload", round_number,
    )
    upload_sha = _sha(_canonical({"image_sha256": image_sha, "image_type": "message"}))
    if upload_effect is None:
        upload_effect = store.prepare_effect(lease, message_artifact, upload_action, upload_sha)
    elif upload_effect["request_sha256"] != upload_sha:
        return {"status": "needs_review", "reason_code": "message-image-upload-request-changed"}
    if upload_effect["status"] == "confirmed":
        image_key_value = upload_effect.get("remote_id")
    elif upload_effect["status"] in {"attempting", "uncertain"}:
        if upload_effect["status"] == "attempting":
            store.finish_effect(lease, upload_effect["op_key"], "uncertain",
                                metadata={"failure_stage": "message-image-upload", "reason_code": "worker-interrupted"})
        return {"status": "uncertain", "failure_stage": "message-image-upload",
                "reason_code": "message-image-upload-result-unconfirmed"}
    elif upload_effect["status"] == "failed":
        return {"status": "failed", "failure_stage": "message-image-upload",
                "reason_code": "previous-message-image-upload-failure",
                "retryable": upload_effect.get("metadata", {}).get("retryable") is True}
    elif upload_effect["status"] == "prepared":
        attempt = store.begin_effect(lease, upload_effect["op_key"])
        try:
            image_key_value = client.upload_message_image(png_path=png_path)
        except UnknownRemoteResult:
            store.finish_effect(lease, attempt["op_key"], "uncertain",
                                metadata={"failure_stage": "message-image-upload", "reason_code": "upload-result-unknown"})
            return {"status": "uncertain", "failure_stage": "message-image-upload",
                    "reason_code": "message-image-upload-result-unconfirmed"}
        except DeliveryError as exc:
            retryable = isinstance(exc, RetryableDeliveryError)
            store.finish_effect(lease, attempt["op_key"], "failed",
                                metadata={"failure_stage": "message-image-upload", "reason_code": "api-rejected",
                                          "retryable": retryable})
            return {"status": "failed", "failure_stage": "message-image-upload",
                    "reason_code": "message-image-upload-rejected", "retryable": retryable}
        store.finish_effect(
            lease, attempt["op_key"], "confirmed", remote_id=image_key_value,
            metadata={"verification": "image-key-upload-receipt", "content_sha256": image_sha},
        )
        upload_effect = store.get_effect(attempt["op_key"])
    else:
        return {"status": "needs_review", "reason_code": "unsupported-message-image-upload-state"}

    send_effect, send_action = _select_effect(
        store, lease.job_id, message_artifact, "message-send", round_number,
    )
    send_sha = _sha(_canonical({"image_sha256": image_sha, "image_key": image_key_value,
                                "target": target, "sender_app_id": sender_app_id}))
    if send_effect is None:
        send_effect = store.prepare_effect(lease, message_artifact, send_action, send_sha)
    elif send_effect["request_sha256"] != send_sha:
        return {"status": "needs_review", "reason_code": "image-message-request-changed"}
    expected_chat_id = history_chat_id
    if send_effect["status"] == "confirmed":
        message_id = send_effect.get("remote_id")
        if not message_id or not _verify_message(
                client, message_id, image_key_value,
                sender_id=sender_app_id, chat_id=expected_chat_id):
            return {"status": "uncertain", "failure_stage": "image-send",
                    "reason_code": "image-message-confirmation-lost"}
    elif send_effect["status"] in {"attempting", "uncertain"}:
        if send_effect["status"] == "attempting":
            store.finish_effect(lease, send_effect["op_key"], "uncertain",
                                metadata={"failure_stage": "image-send", "reason_code": "worker-interrupted"})
            send_effect = store.get_effect(send_effect["op_key"])
        message_id = send_effect.get("remote_id")
        if message_id and _verify_message(
                client, message_id, image_key_value,
                sender_id=sender_app_id, chat_id=expected_chat_id):
            store.reconcile_effect(
                lease, send_effect["op_key"], "confirmed", remote_id=message_id,
                metadata={"verification": "message-id-content-sender-readback",
                          "content_sha256": image_sha},
            )
        elif expected_chat_id:
            start = max(0, int(float(send_effect["updated_at"]) - 60))
            end = max(start, int(clock() + 60))
            try:
                messages = client.list_chat_messages(
                    chat_id=expected_chat_id, start_time=start, end_time=end,
                )
                matches = match_image_messages(
                    messages, chat_id=expected_chat_id, image_key=image_key_value,
                    sender_id=sender_app_id, sender_id_type="app_id",
                )
                if len(matches) == 1:
                    message_id = matches[0]["message_id"]
                    store.reconcile_effect(
                        lease, send_effect["op_key"], "confirmed", remote_id=message_id,
                        metadata={"verification": "history-exact-message-match",
                                  "content_sha256": image_sha},
                    )
                else:
                    return {"status": "uncertain", "failure_stage": "image-send",
                            "reason_code": "image-message-history-ambiguous"}
            except DeliveryError:
                return {"status": "uncertain", "failure_stage": "image-send",
                        "reason_code": "image-message-history-unavailable"}
        else:
            return {"status": "uncertain", "failure_stage": "image-send",
                    "reason_code": "bot-cannot-read-target-p2p-history"}
    elif send_effect["status"] == "failed":
        return {"status": "failed", "failure_stage": "image-send",
                "reason_code": "previous-image-send-failure",
                "retryable": send_effect.get("metadata", {}).get("retryable") is True}
    elif send_effect["status"] == "prepared":
        try:
            if not message_rate_slot(
                    client, chat_id=history_chat_id, sender_id=sender_app_id,
                    sender_id_type="app_id", now=float(clock())):
                return {"status": "deferred", "failure_stage": "image-send",
                        "reason_code": "message-rate-limit", "retry_at": float(clock()) + 60}
        except DeliveryError:
            return {"status": "needs_review", "failure_stage": "image-send",
                    "reason_code": "message-rate-preflight-unavailable"}
        attempt = store.begin_effect(lease, send_effect["op_key"])
        try:
            message_id = client.send_image_message(
                image_key=image_key_value, idempotency_key=attempt["request_id"], **target,
            )
        except UnknownRemoteResult:
            store.finish_effect(lease, attempt["op_key"], "uncertain",
                                metadata={"failure_stage": "image-send", "reason_code": "send-result-unknown"})
            send_effect = store.get_effect(attempt["op_key"])
            if expected_chat_id:
                return _send_image(
                    store=store, lease=lease, image_key=image_key, image_name=image_name,
                    png_path=png_path, trigger=trigger, sender_app_id=sender_app_id,
                    client=client, round_number=round_number, clock=clock,
                )
            return {"status": "uncertain", "failure_stage": "image-send",
                    "reason_code": "bot-cannot-read-target-p2p-history"}
        except DeliveryError as exc:
            retryable = isinstance(exc, RetryableDeliveryError)
            store.finish_effect(lease, attempt["op_key"], "failed",
                                metadata={"failure_stage": "image-send", "reason_code": "api-rejected",
                                          "retryable": retryable})
            return {"status": "failed", "failure_stage": "image-send",
                    "reason_code": "image-send-rejected", "retryable": retryable}
        if not _verify_message(
                client, message_id, image_key_value,
                sender_id=sender_app_id, chat_id=expected_chat_id):
            store.finish_effect(
                lease, attempt["op_key"], "uncertain", remote_id=message_id,
                metadata={"failure_stage": "image-send", "reason_code": "message-readback-unmatched"},
            )
            return {"status": "uncertain", "failure_stage": "image-send",
                    "reason_code": "image-message-readback-unmatched"}
        store.finish_effect(
            lease, attempt["op_key"], "confirmed", remote_id=message_id,
            metadata={"verification": "message-id-content-sender-readback",
                      "content_sha256": image_sha},
        )
        send_effect = store.get_effect(attempt["op_key"])
    else:
        return {"status": "needs_review", "reason_code": "unsupported-image-send-state"}

    store.set_artifact(lease, message_artifact, "confirmed", metadata={
        "verification": "image-message-readback-confirmed", "content_sha256": image_sha,
    })
    return {"status": "confirmed", "message_id": send_effect.get("remote_id"),
            "image_key": image_key_value}


def deliver_image(*, store: WorkflowStore, lease, payload: dict, root: Path,
                  image_key: str, image_name: str, source_path: Path,
                  document_id: str, image_index: int, image_runner=None,
                  client=None, prompt_fetcher=None, clock=time.time) -> dict:
    image_runner = image_runner or core_image
    trigger = payload.get("trigger") or {}
    profile = trigger.get("feishuProfile")
    sender_app_id = str(trigger.get("appId") or "")
    if not profile or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", str(profile)):
        return {"status": "needs_review", "reason_code": "feishu-profile-missing"}
    if not sender_app_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", sender_app_id):
        return {"status": "needs_review", "reason_code": "bot-app-identity-missing"}
    if trigger.get("chatType") == "group" and not trigger.get("privateChatId"):
        return {"status": "needs_review", "reason_code": "private-chat-readback-target-missing"}
    client = client or FeishuLongReadDelivery(identity="bot", profile=profile)
    image_artifact = _artifact(store, lease.job_id, image_key)
    round_number = image_artifact["retry_count"]
    if image_artifact["status"] == "failed":
        retry_at = image_artifact.get("metadata", {}).get("next_retry_at")
        if image_artifact.get("metadata", {}).get("retryable") is not True or not retry_at:
            return {"status": "failed", "failure_stage": image_artifact.get("metadata", {}).get("failure_stage"),
                    "reason_code": image_artifact.get("metadata", {}).get("reason_code")}
        if float(clock()) < float(retry_at):
            return {"status": "retry_scheduled", "next_retry_at": retry_at,
                    "retries_used": round_number}
        try:
            round_number = store.consume_image_retry(lease, image_key)
        except Exception:
            return {"status": "failed", "reason_code": "image-retry-budget-exhausted",
                    "retries_used": image_artifact["retry_count"]}
    elif image_artifact["status"] == "uncertain":
        # Only the in-flight stage may resolve this state. Do not begin a new
        # generation or delivery action until that stage has a read-back proof.
        unresolved = [item for item in store.effects_for(lease.job_id, image_key)
                      if item["status"] in {"attempting", "uncertain"}]
        generation_effects = [item for item in unresolved
                              if item["action"] == "image-generate"
                              or item["action"].startswith("image-generate:retry-")]
        other_unresolved = [item for item in unresolved if item not in generation_effects]
        if other_unresolved:
            return {"status": "uncertain", "reason_code": "image-stage-recovery-pending"}
        # _prepare_generation observes the original Bridge request and can
        # reconcile an ambiguous generation without submitting a new request.

    if image_name == "core-image":
        munger_receipt_path = root / "analysis" / "munger-analysis.md.receipt.json"
        try:
            munger_receipt = _read_json(munger_receipt_path)
            conversation_url = munger_receipt.get("conversation_url")
        except Exception:
            conversation_url = None
    else:
        conversation_url = None
    generation = _prepare_generation(
        store=store, lease=lease, image_key=image_key, image_name=image_name,
        source_path=source_path, root=root, round_number=round_number,
        conversation_url=conversation_url, prompt_fetcher=prompt_fetcher or core_image.fetch_prompt_asset,
        image_runner=image_runner,
    )
    if generation["status"] != "confirmed":
        if generation.get("status") == "failed":
            return _mark_failure(
                store, lease, image_key, stage="image-generate",
                reason=generation.get("reason_code", "image-generation-failed"),
                retryable=generation.get("retryable") is True, clock=clock,
            )
        if generation.get("status") == "uncertain":
            return _mark_uncertain(store, lease, image_key, stage="image-generate",
                                   reason=generation.get("reason_code", "image-generation-unknown"))
        return generation
    image_artifact = _artifact(store, lease.job_id, image_key)
    if image_artifact["status"] != "confirmed":
        store.set_artifact(lease, image_key, "confirmed", metadata={
            "verification": "bridge-image-generation-confirmed",
            "content_sha256": generation["result"]["image_sha256"],
        })
    png_path = root / "images" / f"{image_name}.png"

    inserted = _document_insert(
        store=store, lease=lease, image_key=image_key, image_name=image_name,
        document_id=document_id, png_path=png_path, image_index=image_index,
        root=root, client=client, round_number=round_number, clock=clock,
    )
    insert_failure = None
    if inserted["status"] == "failed":
        insert_failure = inserted
    elif inserted["status"] in {"uncertain", "needs_review"}:
        _mark_uncertain(store, lease, image_key, stage=inserted.get("failure_stage", "image-insert"),
                        reason=inserted.get("reason_code", "image-insert-unconfirmed"))
        return {"status": "uncertain", "failure_stage": inserted.get("failure_stage", "image-insert"),
                "reason_code": inserted.get("reason_code", "image-insert-unconfirmed")}
    elif inserted["status"] == "retry_scheduled":
        insert_failure = inserted

    sent = _send_image(
        store=store, lease=lease, image_key=image_key, image_name=image_name,
        png_path=png_path, trigger=trigger, sender_app_id=sender_app_id,
        client=client, round_number=round_number, clock=clock,
    )
    if sent["status"] == "deferred":
        store.set_artifact(lease, f"{image_key}-send", "pending", metadata={
            "failure_stage": sent.get("failure_stage", "image-send"),
            "reason_code": sent.get("reason_code", "message-rate-limit"),
            "next_retry_at": float(sent.get("retry_at", float(clock()) + 60)),
        })
        return sent
    if sent["status"] == "needs_review":
        return sent
    if sent["status"] == "failed":
        failure = _mark_failure(
            store, lease, image_key, stage=sent.get("failure_stage", "image-send"),
            reason=sent.get("reason_code", "image-send-failed"),
            retryable=sent.get("retryable") is True, clock=clock,
        )
        if insert_failure:
            failure["insert"] = insert_failure
        return failure
    if sent["status"] == "uncertain":
        uncertain = _mark_uncertain(
            store, lease, image_key, stage=sent.get("failure_stage", "image-send"),
            reason=sent.get("reason_code", "image-send-unconfirmed"),
        )
        if insert_failure:
            uncertain["insert"] = insert_failure
        return uncertain
    if insert_failure:
        if insert_failure["status"] == "failed":
            return _mark_failure(
                store, lease, image_key, stage=insert_failure.get("failure_stage", "image-insert"),
                reason=insert_failure.get("reason_code", "image-insert-failed"),
                retryable=insert_failure.get("retryable") is True, clock=clock,
            )
        return {"status": insert_failure["status"], "insert": insert_failure,
                "message_id": sent.get("message_id")}
    return {"status": "confirmed", "image": image_name,
            "document_block_id": inserted.get("block_id"),
            "message_id": sent.get("message_id")}
