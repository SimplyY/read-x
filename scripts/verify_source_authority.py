#!/usr/bin/env python3
"""Deterministic authority resolver for identity/search evidence (v3.18)."""
from __future__ import annotations

import argparse
import html
import ipaddress
import json
import re
import socket
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse


SCORE_VERSION = "3.18"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
RETRYABLE_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}
_URL = re.compile(r"https?://[^\s<>()\[\]{}\"']+")
_PUBLISHER_ALIASES = {
    "麻省理工科技评论": ("MIT Technology Review", "Technology Review"),
    "麻省理工学院技术评论": ("MIT Technology Review", "Technology Review"),
}
_ENTITY_ALIASES = {"比尔·盖茨": ("Bill Gates",), "比尔盖茨": ("Bill Gates",)}
AUTHORITY_STATUSES = {"verified", "corroborated", "inferred", "source_missing", "fetch_failed", "mismatch", "rejected"}
SOURCE_LEVELS = {"official", "wikipedia", "baidu", "reputable_secondary", "search_snippet"}
TOPIC_MATCHES = {"strong", "weak", "none", "unknown"}
DIMENSION_SCORES = (0.0, 2.0, 4.0, 6.0, 6.5, 7.0, 7.5, 8.0, 8.5, 9.0, 9.5, 10.0)
EVIDENCE_KINDS = {
    "publisher": "identity",
    "author": "identity",
    "interview": "expertise",
    "primary_source": "provenance",
    "self_assertion": "identity",
}


def _clean_url(value: str) -> str:
    return value.rstrip(".,;:!?，。；：！？、）》」』】")


def _controlled_header(source: str) -> list[str]:
    """Read only the metadata block, never arbitrary article prose."""
    lines = source.splitlines()
    separator = next((index for index, line in enumerate(lines) if line.strip() == "---"), len(lines))
    return lines[:separator]


def original_url_from_source(source: str) -> str | None:
    """Find an explicitly labelled non-WeChat original URL in source metadata."""
    lines = _controlled_header(source)
    for index, line in enumerate(lines):
        match = re.match(r"^\s*>?\s*(?:原始出处候选|原文链接)\s*[:：]?\s*(.*)$", line)
        if not match:
            continue
        candidates = [match.group(1)] + lines[index + 1:index + 4]
        for candidate in candidates:
            for raw_url in _URL.findall(candidate):
                url = _clean_url(raw_url)
                host = (urlparse(url).hostname or "").lower()
                if host and host != "mp.weixin.qq.com" and not host.endswith(".mp.weixin.qq.com"):
                    return url
    return None


def source_label_aliases(source: str, checks: list[tuple[str, str]]) -> dict[str, list[str]]:
    """Add only explicit parenthetical aliases and a small publisher translation map."""
    header = "\n".join(_controlled_header(source))
    aliases: dict[str, list[str]] = {}
    for kind, label in checks:
        values = []
        for match in re.finditer(rf"{re.escape(label)}\s*[（(]([^（）()\n]{{2,80}})[）)]", header):
            values.append(match.group(1).strip())
        if kind == "publisher":
            values.extend(_PUBLISHER_ALIASES.get(re.sub(r"\s+", "", label), ()))
        if values:
            aliases[kind] = list(dict.fromkeys(values))
    return aliases


def _normalise(value: str) -> str:
    value = unicodedata.normalize("NFKC", html.unescape(value or "")).casefold()
    return re.sub(r"[\s\W_]+", "", value, flags=re.UNICODE)


class _PageTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag in {"script", "style", "noscript"}:
            self.skip_depth += 1
        elif tag == "meta" and values.get("content"):
            self.parts.append(values["content"])

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)


def _page_text(raw: bytes, charset: str) -> str:
    text = raw.decode(charset, errors="replace")
    parser = _PageTextParser()
    parser.feed(text)
    return " ".join(parser.parts)


def _public_host(host: str) -> bool:
    host = (host or "").strip("[]").lower().rstrip(".")
    if not host or host in {"localhost", "localhost.localdomain"}:
        return False
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)]
        except socket.gaierror:
            return True
    return all(address.is_global for address in addresses)


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not _public_host(parsed.hostname):
        raise ValueError("unsafe_url")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_page(url: str, timeout: float) -> tuple[str, int]:
    _validate_url(url)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    )
    opener = urllib.request.build_opener(_SafeRedirectHandler, urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        _validate_url(response.geturl())
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("response_too_large")
        charset = response.headers.get_content_charset() or "utf-8"
        return _page_text(raw, charset), int(getattr(response, "status", 200) or 200)


def _failure(status: str, reason_code: str, attempts: int, started: float, rationale: str) -> dict:
    return {
        "schema_version": SCORE_VERSION,
        "authority_score": None,
        "evidence": [],
        "entity": None,
        "topic_match": "unknown",
        "search_observation": {"query_count": 0, "result_count": 0, "tool_status": "not_run"},
        "authority_confidence": "partial" if status in {"fetch_failed", "source_missing"} else "low",
        "confidence": "unavailable",
        "authority_status": status,
        "reason_code": reason_code,
        "attempts": attempts,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "rationale": rationale,
    }


def _direct_evidence(kind: str, label: str, url: str, verified: bool) -> dict:
    """Emit the v3.18 evidence shape for the direct-URL compatibility path."""
    return {
        "url": url,
        "title": label,
        "source_level": "official",
        "evidence_kind": EVIDENCE_KINDS.get(kind, "provenance"),
        "excerpt": "",
        "verified": verified,
    }


def _identity_valid(identity: dict | None) -> bool:
    if identity is None:
        return False
    if not isinstance(identity, dict) or identity.get("schema_version") != "1":
        return False
    if any(not isinstance(identity.get(key), str) for key in ("title", "author", "publisher", "event_hint")):
        return False
    topic = identity.get("topic")
    if not isinstance(topic, dict) or not isinstance(topic.get("primary"), str) or not isinstance(topic.get("secondary", ""), str):
        return False
    entities = identity.get("entities")
    if not isinstance(entities, list) or any(
        not isinstance(item, dict) or item.get("type") not in {"person", "organization"} or not isinstance(item.get("name"), str)
        or not isinstance(item.get("aliases", []), list) or any(not isinstance(alias, str) for alias in item.get("aliases", []))
        for item in entities
    ):
        return False
    return isinstance(identity.get("source_candidates", []), list) and all(
        isinstance(url, str) and urlparse(url).scheme in {"http", "https"} and bool(urlparse(url).netloc)
        for url in identity["source_candidates"]
    )


def _observation_valid(observation: dict | None) -> bool:
    if observation is None:
        return False
    if not isinstance(observation, dict) or observation.get("schema_version") != "1":
        return False
    if observation.get("provider") != "agent-web" or observation.get("tool_status") not in {"ok", "unavailable", "timeout", "error"}:
        return False
    queries = observation.get("queries", [])
    if not isinstance(queries, list) or len(queries) > 3 or any(
        not isinstance(item, dict) or not isinstance(item.get("hash"), str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", item.get("hash", ""))
        or item.get("kind") not in {"title", "entity_topic", "entity_event"} for item in queries
    ):
        return False
    results = observation.get("results", [])
    if not isinstance(results, list) or len(results) > 4:
        return False
    for item in results:
        if not isinstance(item, dict) or item.get("source_level") not in SOURCE_LEVELS or item.get("evidence_kind") not in {"identity", "expertise", "event", "provenance"}:
            return False
        if not isinstance(item.get("url"), str) or not _URL.fullmatch(item["url"]):
            return False
        if not isinstance(item.get("title", ""), str) or not isinstance(item.get("excerpt", ""), str) or len(item.get("excerpt", "")) > 200:
            return False
    assessment = observation.get("assessment", {})
    return isinstance(assessment, dict) and assessment.get("entity_match") in {"confirmed", "ambiguous", "none", "unknown"} and assessment.get("topic_match") in TOPIC_MATCHES


def _search_result_evidence(observation: dict) -> list[dict]:
    evidence = []
    for item in observation.get("results", []):
        try:
            _validate_url(item["url"])
        except (ValueError, KeyError):
            continue
        evidence.append({key: item.get(key, "") for key in ("url", "title", "source_level", "evidence_kind", "excerpt")})
    return evidence


def resolve_identity(identity: dict | None, observation: dict | None) -> dict:
    """Map bounded search evidence to a score; no model/network call happens here."""
    started = time.monotonic()
    attempts = len((observation or {}).get("queries", [])) if isinstance(observation, dict) else 0
    if not _identity_valid(identity):
        return _failure("rejected", "invalid_identity_packet", attempts, started, "身份包不符合 v1 契约")
    if observation is not None and not _observation_valid(observation):
        return _failure("rejected", "invalid_search_observation", attempts, started, "搜索观察不符合受控契约")
    if observation is None or observation.get("tool_status") in {"unavailable", "timeout", "error"}:
        return {**_failure("fetch_failed", "search_unavailable", attempts, started, "搜索桥不可用，未产生可核验证据"), "search_observation": {"query_count": attempts, "result_count": 0, "tool_status": (observation or {}).get("tool_status", "unavailable")}}
    assessment = observation["assessment"]
    evidence = _search_result_evidence(observation)
    entity_match, topic_match = assessment["entity_match"], assessment["topic_match"]
    levels = {item["source_level"] for item in evidence}
    entity_terms = [term for item in identity["entities"] for term in [item["name"], *item.get("aliases", []), *_ENTITY_ALIASES.get(item["name"], ())] if term]
    evidence_text = _normalise(" ".join(f"{item['title']} {item['excerpt']}" for item in evidence))
    if entity_match == "confirmed" and evidence and entity_terms and not any(_normalise(term) in evidence_text for term in entity_terms):
        entity_match = "ambiguous"
    strong = bool(identity["entities"]) and entity_match == "confirmed" and topic_match == "strong"
    if not identity["entities"]:
        return {**_failure("mismatch", "entity_missing", attempts, started, "身份包没有可消歧实体"), "topic_match": topic_match, "search_observation": {"query_count": len(observation.get("queries", [])), "result_count": len(evidence), "tool_status": observation.get("tool_status")}}
    if entity_match in {"ambiguous", "none"} or topic_match == "none":
        status, score, reason = "mismatch", None, "entity_or_topic_mismatch"
    elif strong and ("official" in levels or "wikipedia" in levels):
        status, score, reason = "verified", 8.0, "entity_expertise_topic_verified"
        if "official" in levels and len([item for item in evidence if item["source_level"] != "search_snippet"]) >= 2:
            score, reason = 9.0, "entity_expertise_topic_corroborated"
    elif "baidu" in levels and ("official" in levels or "reputable_secondary" in levels):
        status, score, reason = "corroborated", 7.0, "baidu_corroborated"
    elif strong and len([item for item in evidence if item["source_level"] == "reputable_secondary"]) >= 2:
        status, score, reason = "corroborated", 7.0, "reputable_secondary_corroborated"
    else:
        suggested = assessment.get("suggested_score")
        if isinstance(suggested, (int, float)) and not isinstance(suggested, bool) and suggested > 0:
            bounded = min(max(float(suggested), 0.0), 8.0)
            score = min(DIMENSION_SCORES, key=lambda value: abs(value - bounded))
            status, reason = "inferred", "model_knowledge_inferred"
        else:
            status, score, reason = "mismatch", None, "insufficient_authority_evidence"
    evidence = [dict(item, verified=status in {"verified", "corroborated"} and item["source_level"] != "search_snippet") for item in evidence]
    return {
        "schema_version": SCORE_VERSION, "authority_score": score, "authority_status": status,
        "authority_confidence": "high" if status == "verified" and score == 9.0 else "medium" if status in {"verified", "corroborated"} else "low" if status == "inferred" else "partial",
        "confidence": "high" if status == "verified" and score == 9.0 else "medium" if status in {"verified", "corroborated"} else "low" if status == "inferred" else "unavailable",
        "entity": {"type": identity["entities"][0]["type"], "canonical": identity["entities"][0]["name"], "ambiguity": entity_match != "confirmed"} if identity["entities"] else None,
        "topic_match": topic_match, "evidence": evidence, "reason_code": reason,
        "attempts": attempts, "elapsed_ms": round((time.monotonic() - started) * 1000),
        "rationale": "；".join(filter(None, [assessment.get("basis", ""), reason])),
        "search_observation": {"query_count": len(observation.get("queries", [])), "result_count": len(evidence), "tool_status": observation.get("tool_status")},
    }


def verify(
    url: str,
    checks: list[tuple[str, str]],
    timeout: float = 20,
    label_aliases: dict[str, list[str]] | None = None,
) -> dict:
    """Fetch only the supplied URL; return evidence without retaining page content."""
    started = time.monotonic()
    budget = min(max(float(timeout), 0.01), 20.0)
    deadline = started + budget
    label_aliases = label_aliases or {}
    last_error: Exception | None = None
    attempts = 0
    for attempts in range(1, 3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _failure("fetch_failed", "fetch_timeout", attempts - 1, started, "核验总耗时超过预算")
        try:
            text, status = _fetch_page(url, remaining)
            haystack = _normalise(text)
            evidence = [
                _direct_evidence(
                    kind,
                    label,
                    url,
                    status in range(200, 300) and bool(_normalise(label)) and any(
                        _normalise(candidate) in haystack for candidate in [label, *label_aliases.get(kind, [])]
                    ),
                )
                for kind, label in checks
            ]
            verified = [item for item in evidence if item["verified"]]
            if not verified:
                return {
                    "schema_version": SCORE_VERSION,
                    "authority_score": None,
                    "entity": None,
                    "topic_match": "unknown",
                    "search_observation": {"query_count": 0, "result_count": 0, "tool_status": "not_run"},
                    "evidence": evidence,
                    "authority_confidence": "partial",
                    "confidence": "unavailable",
                    "authority_status": "mismatch",
                    "reason_code": "authority_mismatch",
                    "attempts": attempts,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "rationale": "原始出处可访问，但未匹配出版方或一手材料关键词",
                }
            primary = any(
                kind in {"interview", "primary_source"}
                for (kind, _), item in zip(checks, evidence)
                if item["verified"]
            )
            authority = 9.0 if len(verified) >= 2 and primary else 8.0
            return {
                "schema_version": SCORE_VERSION,
                "authority_score": authority,
                "entity": None,
                "topic_match": "unknown",
                "search_observation": {"query_count": 0, "result_count": len(evidence), "tool_status": "not_run"},
                "evidence": evidence,
                "authority_confidence": "high" if authority >= 9 else "medium",
                "confidence": "high" if authority >= 9 else "medium",
                "authority_status": "verified",
                "reason_code": "authority_verified",
                "attempts": attempts,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "rationale": "原始出处可访问，且出版方/一手材料关键词核验完成",
            }
        except ValueError as exc:
            reason = str(exc)
            if reason == "unsafe_url":
                return _failure("rejected", "unsafe_url", attempts, started, "原始出处地址不属于公开 HTTP(S) 地址")
            if reason == "response_too_large":
                return _failure("rejected", "response_too_large", attempts, started, "原始出处响应超过核验上限")
            return _failure("fetch_failed", "fetch_network_error", attempts, started, reason)
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in RETRYABLE_HTTP_CODES or attempts == 2:
                return _failure("fetch_failed", "fetch_http_error", attempts, started, f"HTTP {exc.code}")
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            last_error = exc
            if attempts == 2:
                reason = "fetch_timeout" if isinstance(exc, (TimeoutError, socket.timeout)) else "fetch_network_error"
                return _failure("fetch_failed", reason, attempts, started, type(exc).__name__)
        if attempts == 1:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _failure("fetch_failed", "fetch_timeout", attempts, started, "核验总耗时超过预算")
            time.sleep(min(0.5, remaining))
    return _failure("fetch_failed", "fetch_network_error", attempts, started, type(last_error).__name__ if last_error else "unknown")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?")
    parser.add_argument("--source", type=Path, help="source.md containing controlled original URL metadata")
    parser.add_argument("--publisher", help="expected publisher text")
    parser.add_argument("--interview", help="expected interview subject or first-party text")
    parser.add_argument("--primary-source", help="expected first-party source text")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--identity", type=Path)
    parser.add_argument("--search-observation", type=Path)
    args = parser.parse_args()
    url = args.url
    source_text = ""
    if not url and args.source:
        try:
            source_text = args.source.read_text(encoding="utf-8")
            url = original_url_from_source(source_text)
        except OSError:
            url = None
    checks = [("publisher", args.publisher), ("interview", args.interview), ("primary_source", args.primary_source)]
    checks = [(kind, label) for kind, label in checks if label]
    identity = json.loads(args.identity.read_text(encoding="utf-8")) if args.identity else None
    observation = json.loads(args.search_observation.read_text(encoding="utf-8")) if args.search_observation else None
    if args.identity or args.search_observation:
        result = resolve_identity(identity, observation)
    elif not url:
        result = _failure("source_missing", "source_missing", 0, time.monotonic(), "未找到受控的非微信原始出处链接")
    else:
        result = verify(url, checks, label_aliases=source_label_aliases(source_text, checks))
    print(json.dumps({"event": "authority_verification", "status": result["authority_status"], "reason_code": result["reason_code"], "attempts": result["attempts"], "elapsed_ms": result["elapsed_ms"]}, ensure_ascii=False, separators=(",", ":")), file=sys.stderr, flush=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
