#!/usr/bin/env python3
"""Small, read-back-first Feishu adapter for durable long-read delivery.

The adapter performs no implicit retries. Callers must persist an effect intent
before each write and reconcile a lost response through the read methods here.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable


class DeliveryError(RuntimeError):
    """A Feishu call returned a known failure or invalid read response."""


class RetryableDeliveryError(DeliveryError):
    """A known Feishu transient error that can be safely retried as a new round."""


class UnknownRemoteResult(DeliveryError):
    """A write may have reached Feishu; do not resubmit it automatically."""


_RETRYABLE_FEISHU_CODES = {
    # Official Feishu common error codes: API/message rate limits and listed
    # internal errors whose troubleshooting guidance explicitly permits retry.
    "99991400", "1000004", "1000005", "11247", "2200", "1500", "1503",
    "1551", "1557", "1642", "1663", "1665", "1668", "11311", "1000003",
}


def _raise_api_error(payload: dict) -> None:
    code = payload.get("code", payload.get("error_code"))
    detail = payload.get("msg") or payload.get("error") or payload.get("message") or "Feishu rejected request"
    error_type = RetryableDeliveryError if str(code) in _RETRYABLE_FEISHU_CODES else DeliveryError
    prefix = f"Feishu API code={code}: " if code not in (None, "") else ""
    raise error_type(prefix + str(detail))


def _object(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise DeliveryError(f"{label} response is not an object")
    return value


def _data(payload: dict) -> dict:
    """Accept lark-cli's normal envelope and the raw OpenAPI envelope."""
    if payload.get("ok") is False:
        _raise_api_error(payload)
    if "code" in payload and payload.get("code") != 0:
        _raise_api_error(payload)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise DeliveryError("Feishu response is missing data object")
    return data


def created_image_block_id(payload: dict) -> str:
    children = _data(payload).get("children")
    if not isinstance(children, list) or len(children) != 1:
        raise UnknownRemoteResult("image block create response did not identify exactly one block")
    block = _object(children[0], "created block")
    block_id = block.get("block_id")
    if block.get("block_type") != 27 or not isinstance(block_id, str) or not block_id:
        raise UnknownRemoteResult("image block create response is incomplete")
    return block_id


def uploaded_media_token(payload: dict) -> str:
    token = _data(payload).get("file_token")
    if not isinstance(token, str) or not token:
        raise UnknownRemoteResult("media upload response is missing file_token")
    return token


def uploaded_message_image_key(payload: dict) -> str:
    image_key = _data(payload).get("image_key")
    if not isinstance(image_key, str) or not image_key.startswith("img_"):
        raise UnknownRemoteResult("message image upload response is missing image_key")
    return image_key


def sent_message_id(payload: dict) -> str:
    data = _data(payload)
    message_id = data.get("message_id")
    if not isinstance(message_id, str) or not message_id.startswith("om_"):
        raise UnknownRemoteResult("send response is missing a verifiable message_id")
    return message_id


class FeishuLongReadDelivery:
    def __init__(self, *, identity: str, profile: str, binary: str | None = None,
                 runner: Callable[..., Any] = subprocess.run, timeout: float = 45,
                 expected_app_id: str | None = None):
        if identity not in {"bot", "user"}:
            raise ValueError("Feishu identity must be explicit: bot or user")
        if not isinstance(profile, str) or not profile.strip():
            raise ValueError("Feishu profile must be explicit")
        self.identity = identity
        self.profile = profile.strip()
        self.binary = binary or shutil.which("lark-cli") or "lark-cli"
        self.runner = runner
        self.timeout = timeout
        self.expected_app_id = expected_app_id

    def _run_readonly_cli_json(self, arguments: list[str], *, profile: str | None = None):
        command = [self.binary]
        if profile is not None:
            command.extend(["--profile", profile])
        command.extend(arguments)
        env = dict(os.environ)
        env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
        env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
        try:
            result = self.runner(command, capture_output=True, text=True,
                                 timeout=self.timeout, check=False, env=env)
        except (subprocess.TimeoutExpired, TimeoutError, OSError) as exc:
            raise DeliveryError("Feishu bound app identity could not be verified") from exc
        if getattr(result, "returncode", 1) != 0:
            raise DeliveryError("Feishu bound app identity could not be verified")
        try:
            payload = json.loads(getattr(result, "stdout", "") or "")
        except (ValueError, TypeError) as exc:
            raise DeliveryError("Feishu bound app identity returned malformed output") from exc
        if isinstance(payload, dict) and payload.get("ok") is False:
            raise DeliveryError("Feishu bound app identity was rejected")
        return payload

    def verify_app_identity(self, expected_app_id: str) -> bool:
        """Fail closed unless this exact local profile still resolves to the admitted app."""
        if (self.identity != "bot" or not isinstance(expected_app_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", expected_app_id)):
            raise DeliveryError("Read-X bot identity is invalid")
        profiles = self._run_readonly_cli_json(["profile", "list"])
        matches = [item for item in profiles if isinstance(item, dict)
                   and item.get("appId") == expected_app_id] if isinstance(profiles, list) else []
        if len(matches) != 1 or matches[0].get("name") != self.profile:
            raise DeliveryError("Read-X bot profile no longer resolves uniquely to the admitted app")
        identity = self._run_readonly_cli_json(["whoami"], profile=self.profile)
        if (not isinstance(identity, dict) or identity.get("identity") != "user"
                or identity.get("appId") != expected_app_id
                or identity.get("profile") != self.profile):
            raise DeliveryError("Read-X bot profile identity changed")
        return True

    def auth_status(self) -> dict:
        """Read and verify the current CLI identity using auth status's own flags."""
        command = [self.binary, "auth", "status", "--json", "--verify"]
        env = dict(os.environ)
        env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
        env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
        try:
            result = self.runner(command, capture_output=True, text=True,
                                 timeout=self.timeout, check=False, env=env)
        except (subprocess.TimeoutExpired, TimeoutError, OSError) as exc:
            raise DeliveryError("Feishu current identity verification could not complete") from exc
        stdout = getattr(result, "stdout", "") or ""
        if getattr(result, "returncode", 1) != 0:
            raise DeliveryError("Feishu current identity verification failed")
        try:
            payload = _object(json.loads(stdout), "lark-cli auth status")
        except (ValueError, TypeError, DeliveryError) as exc:
            raise DeliveryError("Feishu current identity verification returned malformed output") from exc
        if payload.get("ok") is False:
            raise DeliveryError("Feishu current identity verification was rejected")
        return payload

    def _run(self, args: list[str], *, write: bool) -> dict:
        if write and self.expected_app_id is not None:
            self.verify_app_identity(self.expected_app_id)
        command = [self.binary, *args, "--as", self.identity, "--profile", self.profile, "--format", "json"]
        try:
            result = self.runner(command, capture_output=True, text=True, timeout=self.timeout, check=False)
        except (subprocess.TimeoutExpired, TimeoutError) as exc:
            if write:
                raise UnknownRemoteResult("Feishu write timed out; remote outcome is unknown") from exc
            raise DeliveryError("Feishu read timed out") from exc
        except OSError as exc:
            if write:
                raise UnknownRemoteResult("Feishu write transport failed; remote outcome is unknown") from exc
            raise DeliveryError("Feishu CLI could not be executed") from exc
        stdout = getattr(result, "stdout", "") or ""
        stderr = getattr(result, "stderr", "") or ""
        if getattr(result, "returncode", 1) != 0:
            # A nonzero process exit is not proof that a write was rejected:
            # the remote side effect may have completed before the CLI failed.
            try:
                payload = _object(json.loads(stdout), "lark-cli")
            except (ValueError, TypeError, DeliveryError):
                payload = None
            detail = stderr.strip() or (stdout.strip() if stdout else "unknown failure")
            if write:
                explicit_rejection = isinstance(payload, dict) and (
                    payload.get("ok") is False
                    or ("code" in payload and payload.get("code") not in (0, None))
                )
                if explicit_rejection:
                    _raise_api_error(payload)
                raise UnknownRemoteResult("Feishu write exited nonzero without a confirmed rejection")
            raise DeliveryError(f"lark-cli failed ({getattr(result, 'returncode', '?')}): {detail}")
        try:
            payload = _object(json.loads(stdout), "lark-cli")
        except (ValueError, TypeError, DeliveryError) as exc:
            if write:
                raise UnknownRemoteResult("Feishu write returned malformed output") from exc
            raise DeliveryError("Feishu read returned malformed output") from exc
        # Validate known API rejections, but preserve uncertainty for writes
        # whose successful side effect has no usable receipt.
        if payload.get("ok") is False or ("code" in payload and payload.get("code") != 0):
            _raise_api_error(payload)
        return payload

    @staticmethod
    def _validate_id(value: str, label: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
            raise ValueError(f"invalid {label}")
        return value

    def _api(self, method: str, path: str, *, params: dict | None = None,
             body: dict | None = None, write: bool = False) -> dict:
        args = ["api", method, path]
        if params is not None:
            args += ["--params", json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))]
        if body is not None:
            args += ["--data", json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))]
        return self._run(args, write=write)

    def list_document_blocks(self, document_id: str) -> list[dict]:
        document_id = self._validate_id(document_id, "document_id")
        path = f"/open-apis/docx/v1/documents/{document_id}/blocks"
        page_token = None
        found: list[dict] = []
        seen_tokens: set[str] = set()
        seen_ids: set[str] = set()
        while True:
            params: dict[str, Any] = {"document_revision_id": -1, "page_size": 500}
            if page_token:
                params["page_token"] = page_token
            data = _data(self._api("GET", path, params=params))
            items = data.get("items")
            if not isinstance(items, list):
                raise DeliveryError("document block list response is incomplete")
            for value in items:
                item = _object(value, "document block")
                block_id = item.get("block_id")
                if not isinstance(block_id, str) or not block_id or block_id in seen_ids:
                    raise DeliveryError("document block list contains a missing or duplicate block_id")
                seen_ids.add(block_id)
                found.append(item)
            has_more = data.get("has_more")
            if has_more is False:
                return found
            if has_more is not True:
                raise DeliveryError("document block list is missing a valid has_more flag")
            next_token = data.get("page_token")
            if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
                raise DeliveryError("document block pagination is incomplete")
            seen_tokens.add(next_token)
            page_token = next_token

    def get_document_block(self, document_id: str, block_id: str) -> dict:
        document_id = self._validate_id(document_id, "document_id")
        block_id = self._validate_id(block_id, "block_id")
        payload = self._api(
            "GET", f"/open-apis/docx/v1/documents/{document_id}/blocks/{block_id}",
            params={"document_revision_id": -1},
        )
        block = _data(payload).get("block")
        if not isinstance(block, dict) or block.get("block_id") != block_id:
            raise DeliveryError("document block read-back does not match requested block")
        return block

    def create_image_block(self, *, document_id: str, parent_block_id: str, index: int,
                           client_token: str) -> str:
        document_id = self._validate_id(document_id, "document_id")
        parent_block_id = self._validate_id(parent_block_id, "parent_block_id")
        if not isinstance(index, int) or index < 0:
            raise ValueError("image insertion index must be a non-negative integer")
        if not isinstance(client_token, str) or not re.fullmatch(
                r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}", client_token):
            raise ValueError("client_token must be a UUID")
        payload = self._api(
            "POST", f"/open-apis/docx/v1/documents/{document_id}/blocks/{parent_block_id}/children",
            params={"document_revision_id": -1, "client_token": client_token},
            body={"index": index, "children": [{"block_type": 27, "image": {}}]}, write=True,
        )
        try:
            return created_image_block_id(payload)
        except DeliveryError as exc:
            raise UnknownRemoteResult("image block create result is not verifiable") from exc

    def upload_document_image(self, *, document_id: str, image_block_id: str, png_path: Path) -> str:
        document_id = self._validate_id(document_id, "document_id")
        image_block_id = self._validate_id(image_block_id, "image_block_id")
        path = Path(png_path)
        if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
            raise ValueError("image upload requires a non-empty regular PNG file")
        if path.suffix.lower() != ".png":
            raise ValueError("image upload requires a .png file")
        with path.open("rb") as handle:
            if handle.read(8) != b"\x89PNG\r\n\x1a\n":
                raise ValueError("image upload requires a valid PNG signature")
        payload = self._run([
            "docs", "+media-upload", "--doc-id", document_id,
            "--parent-node", image_block_id, "--parent-type", "docx_image", "--file", str(path),
        ], write=True)
        try:
            return uploaded_media_token(payload)
        except DeliveryError as exc:
            raise UnknownRemoteResult("image upload result is not verifiable") from exc

    def upload_message_image(self, *, png_path: Path) -> str:
        path = Path(png_path)
        if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
            raise ValueError("message image upload requires a non-empty regular PNG file")
        if path.suffix.lower() != ".png":
            raise ValueError("message image upload requires a .png file")
        with path.open("rb") as handle:
            if handle.read(8) != b"\x89PNG\r\n\x1a\n":
                raise ValueError("message image upload requires a valid PNG signature")
        payload = self._run([
            "im", "images", "create",
            "--data", json.dumps({"image_type": "message"}, separators=(",", ":")),
            "--file", str(path),
        ], write=True)
        try:
            return uploaded_message_image_key(payload)
        except DeliveryError as exc:
            raise UnknownRemoteResult("message image upload result is not verifiable") from exc

    def patch_image_block(self, *, document_id: str, block_id: str, file_token: str,
                          client_token: str) -> None:
        document_id = self._validate_id(document_id, "document_id")
        block_id = self._validate_id(block_id, "block_id")
        file_token = self._validate_id(file_token, "file_token")
        if not isinstance(client_token, str) or not re.fullmatch(
                r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}", client_token):
            raise ValueError("client_token must be a UUID")
        self._api(
            "PATCH", f"/open-apis/docx/v1/documents/{document_id}/blocks/{block_id}",
            params={"document_revision_id": -1, "client_token": client_token},
            body={"replace_image": {"token": file_token}}, write=True,
        )

    def verify_document_image(self, *, document_id: str, block_id: str, parent_block_id: str,
                              file_token: str) -> dict:
        block = self.get_document_block(document_id, block_id)
        image = block.get("image")
        if (block.get("block_type") != 27 or block.get("parent_id") != parent_block_id
                or not isinstance(image, dict) or image.get("token") != file_token):
            raise UnknownRemoteResult("image block read-back does not match expected parent and token")
        return block

    @staticmethod
    def verify_new_image_child(*, before_child_ids: list[str], after_child_ids: list[str],
                               index: int, blocks_by_id: dict[str, dict], parent_block_id: str) -> str:
        """Identify a lost create response only when one exact ordered diff exists."""
        if not isinstance(index, int) or index < 0 or index > len(before_child_ids):
            raise ValueError("invalid insertion index")
        if len(after_child_ids) != len(before_child_ids) + 1:
            raise UnknownRemoteResult("document children changed by more than one block")
        added = [block_id for block_id in after_child_ids if block_id not in set(before_child_ids)]
        if len(added) != 1:
            raise UnknownRemoteResult("document children do not identify one new block")
        block_id = added[0]
        expected = before_child_ids[:index] + [block_id] + before_child_ids[index:]
        block = blocks_by_id.get(block_id)
        if (after_child_ids != expected or not isinstance(block, dict)
                or block.get("block_id") != block_id or block.get("block_type") != 27
                or block.get("parent_id") != parent_block_id):
            raise UnknownRemoteResult("new image block position or ownership is ambiguous")
        return block_id

    def get_message(self, message_id: str) -> dict:
        message_id = self._validate_id(message_id, "message_id")
        data = _data(self._api("GET", f"/open-apis/im/v1/messages/{message_id}"))
        items = data.get("items")
        if not isinstance(items, list) or len(items) != 1:
            raise DeliveryError("message read-back did not return exactly one message")
        message = items[0]
        if not isinstance(message, dict) or message.get("message_id") != message_id:
            raise DeliveryError("message read-back does not match requested message")
        return message

    def send_image_message(self, *, image_key: str, idempotency_key: str,
                           chat_id: str | None = None, user_id: str | None = None) -> str:
        if bool(chat_id) == bool(user_id):
            raise ValueError("provide exactly one Feishu chat_id or user_id")
        if chat_id:
            chat_id = self._validate_id(chat_id, "chat_id")
        if user_id:
            user_id = self._validate_id(user_id, "user_id")
        image_key = self._validate_id(image_key, "image_key")
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", idempotency_key):
            raise ValueError("idempotency_key must be 1-50 URL-safe characters")
        target = ["--chat-id", chat_id] if chat_id else ["--user-id", user_id]
        payload = self._run([
            "im", "+messages-send", *target, "--image", image_key,
            "--idempotency-key", idempotency_key,
        ], write=True)
        try:
            return sent_message_id(payload)
        except DeliveryError as exc:
            raise UnknownRemoteResult("image message send result is not verifiable") from exc

    def upload_markdown_attachment(self, *, path: Path) -> str:
        path = Path(path)
        if not path.is_file() or path.is_symlink() or path.stat().st_size == 0 or path.stat().st_size > 30 * 1024 * 1024:
            raise ValueError("report attachment must be a non-empty regular file no larger than 30 MB")
        try:
            path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("report attachment must be UTF-8 Markdown") from exc
        payload = self._run([
            "im", "files", "create",
            "--data", json.dumps({"file_name": path.name, "file_type": "stream"}, ensure_ascii=False, separators=(",", ":")),
            "--file", f"file={path}",
        ], write=True)
        file_key = _data(payload).get("file_key")
        if not isinstance(file_key, str) or not file_key.startswith("file_"):
            raise UnknownRemoteResult("report attachment upload result is not verifiable")
        return file_key

    def send_interactive_card(self, *, card: dict, idempotency_key: str,
                              chat_id: str | None = None, user_id: str | None = None) -> str:
        if bool(chat_id) == bool(user_id):
            raise ValueError("provide exactly one Feishu chat_id or user_id")
        target = ["--chat-id", self._validate_id(chat_id, "chat_id")] if chat_id else ["--user-id", self._validate_id(user_id, "user_id")]
        if not isinstance(card, dict) or card.get("schema") != "2.0":
            raise ValueError("interactive card must use Card 2.0")
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", idempotency_key):
            raise ValueError("idempotency_key must be 1-50 URL-safe characters")
        payload = self._run([
            "im", "+messages-send", *target, "--msg-type", "interactive",
            "--content", json.dumps(card, ensure_ascii=False, separators=(",", ":")),
            "--idempotency-key", idempotency_key,
        ], write=True)
        try:
            return sent_message_id(payload)
        except DeliveryError as exc:
            raise UnknownRemoteResult("report card send result is not verifiable") from exc

    def send_file_message(self, *, file_key: str, idempotency_key: str,
                          chat_id: str | None = None, user_id: str | None = None) -> str:
        if bool(chat_id) == bool(user_id):
            raise ValueError("provide exactly one Feishu chat_id or user_id")
        file_key = self._validate_id(file_key, "file_key")
        if not file_key.startswith("file_"):
            raise ValueError("file_key has an invalid prefix")
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", idempotency_key):
            raise ValueError("idempotency_key must be 1-50 URL-safe characters")
        target = ["--chat-id", self._validate_id(chat_id, "chat_id")] if chat_id else ["--user-id", self._validate_id(user_id, "user_id")]
        payload = self._run([
            "im", "+messages-send", *target, "--file", file_key,
            "--idempotency-key", idempotency_key,
        ], write=True)
        try:
            return sent_message_id(payload)
        except DeliveryError as exc:
            raise UnknownRemoteResult("report attachment send result is not verifiable") from exc

    def list_chat_messages(self, *, chat_id: str, start_time: int, end_time: int) -> list[dict]:
        chat_id = self._validate_id(chat_id, "chat_id")
        if not isinstance(start_time, int) or not isinstance(end_time, int) or start_time > end_time:
            raise ValueError("message read-back requires an ordered timestamp window")
        params: dict[str, Any] = {
            "container_id_type": "chat", "container_id": chat_id,
            "start_time": str(start_time), "end_time": str(end_time),
            "page_size": 50, "sort_type": "ByCreateTimeAsc",
            "card_msg_content_type": "user_card_content",
        }
        page_token = None
        seen_tokens: set[str] = set()
        messages: list[dict] = []
        while True:
            query = dict(params)
            if page_token:
                query["page_token"] = page_token
            data = _data(self._api("GET", "/open-apis/im/v1/messages", params=query))
            items = data.get("items")
            if not isinstance(items, list):
                raise DeliveryError("message history response is incomplete")
            messages.extend(_object(item, "message") for item in items)
            has_more = data.get("has_more")
            if has_more is False:
                return messages
            if has_more is not True:
                raise DeliveryError("message history response is missing a valid has_more flag")
            next_token = data.get("page_token")
            if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
                raise DeliveryError("message history pagination is incomplete")
            seen_tokens.add(next_token)
            page_token = next_token


def message_rate_slot(client, *, chat_id: str, sender_id: str,
                      sender_id_type: str = "app_id", now: float) -> bool:
    """Require a complete rolling-minute history and keep sends below five."""
    start_time, end_time = max(0, int(now) - 60), int(now)
    messages = client.list_chat_messages(
        chat_id=chat_id, start_time=start_time, end_time=end_time,
    )
    sent_by_identity = 0
    for message in messages:
        if not isinstance(message, dict):
            raise DeliveryError("recent message history is incomplete")
        created = message.get("create_time") or message.get("created_at")
        sender = message.get("sender")
        observed_id = sender.get("id") if isinstance(sender, dict) else None
        observed_type = sender.get("id_type") if isinstance(sender, dict) else None
        try:
            timestamp = int(created)
        except (TypeError, ValueError) as exc:
            raise DeliveryError("recent message history has an unreadable timestamp") from exc
        # Feishu message create_time is milliseconds; list time-window
        # parameters and our policy clock are Unix seconds.
        if timestamp >= 100_000_000_000:
            timestamp //= 1000
        if not isinstance(observed_id, str) or not observed_id or not isinstance(observed_type, str) or not observed_type:
            raise DeliveryError("recent message history has an unreadable sender")
        if timestamp < start_time or timestamp > end_time:
            continue
        if observed_id == sender_id and observed_type == sender_id_type:
            sent_by_identity += 1
    return sent_by_identity < 4


def match_image_messages(messages: list[dict], *, chat_id: str, image_key: str,
                         sender_id: str, sender_id_type: str | None = None) -> list[dict]:
    """Return only exact image-message matches; callers decide unknown/no-match policy."""
    matches = []
    for message in messages:
        if not isinstance(message, dict) or message.get("msg_type") != "image":
            continue
        if message.get("chat_id") != chat_id:
            continue
        sender = message.get("sender") or {}
        observed_sender_id = sender.get("id") if isinstance(sender, dict) else None
        if observed_sender_id is None and isinstance(sender, dict):
            sender_info = sender.get("sender_id") or {}
            observed_sender_id = sender_info.get("open_id") if isinstance(sender_info, dict) else None
        if observed_sender_id != sender_id:
            continue
        if sender_id_type and isinstance(sender, dict) and sender.get("id_type") != sender_id_type:
            continue
        try:
            content = json.loads(message.get("body", {}).get("content", "{}"))
        except (ValueError, TypeError, AttributeError):
            continue
        if isinstance(content, dict) and content.get("image_key") == image_key:
            matches.append(message)
    return matches


def verify_image_message(message: dict, *, message_id: str, image_key: str,
                         sender_id: str, sender_id_type: str,
                         chat_id: str | None = None) -> bool:
    if (not isinstance(message, dict) or message.get("message_id") != message_id
            or message.get("msg_type") != "image"):
        return False
    if chat_id is not None and message.get("chat_id") != chat_id:
        return False
    sender, observed_type = _message_sender(message)
    if sender != sender_id or observed_type != sender_id_type:
        return False
    content = _message_content(message)
    return isinstance(content, dict) and content.get("image_key") == image_key


def _message_sender(message: dict) -> tuple[str | None, str | None]:
    sender = message.get("sender") or {}
    if not isinstance(sender, dict):
        return None, None
    sender_id = sender.get("id")
    if sender_id is None:
        sender_info = sender.get("sender_id") or {}
        sender_id = sender_info.get("open_id") if isinstance(sender_info, dict) else None
    return sender_id, sender.get("id_type")


def _message_content(message: dict) -> dict | None:
    try:
        content = json.loads(message.get("body", {}).get("content", "{}"))
    except (ValueError, TypeError, AttributeError):
        return None
    return content if isinstance(content, dict) else None


def match_report_messages(messages: list[dict], *, chat_id: str, sender_id: str,
                          artifact: str, expected: dict,
                          sender_id_type: str | None = None) -> list[dict]:
    """Match report messages by exact destination, sender, type, and payload."""
    expected_types = {"markdown-send": "file", "card-send": "interactive"}
    if artifact not in expected_types:
        raise ValueError("unsupported report message artifact")
    matches = []
    for message in messages:
        if not isinstance(message, dict) or message.get("msg_type") != expected_types[artifact]:
            continue
        if message.get("chat_id") != chat_id:
            continue
        observed_sender, observed_type = _message_sender(message)
        if observed_sender != sender_id or (sender_id_type and observed_type != sender_id_type):
            continue
        content = _message_content(message)
        if content is None:
            continue
        if artifact == "markdown-send":
            exact = content.get("file_key") == expected.get("file_key")
        else:
            exact = content == expected
        if exact and isinstance(message.get("message_id"), str):
            matches.append(message)
    return matches
