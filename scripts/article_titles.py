"""Deterministic title validation shared by Read-X ingestion and Base writes."""
from __future__ import annotations

import html
import re
from urllib.parse import urlparse


TITLE_PENDING = "标题待补采"
_SPACE = re.compile(r"\s+")
_URL_PREFIX = re.compile(r"^(?:https?://|www\.)", re.IGNORECASE)
_DOMAIN_PATH = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}(?:/|$)", re.IGNORECASE)
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_GENERIC_TITLES = {
    "untitled", "no title", "404", "404 not found", "page not found",
    "access denied", "just a moment...", "微信公众平台", "无标题", "未命名文章",
}


def normalize_title(value: object) -> str:
    text = html.unescape(str(value or "")).replace("\xa0", " ")
    text = re.sub(r"^\s*#+\s*", "", text)
    return _SPACE.sub(" ", text).strip()


def is_url_title(value: object) -> bool:
    text = normalize_title(value)
    if not text:
        return False
    if _URL_PREFIX.match(text):
        parsed = urlparse(text if "://" in text else "https://" + text)
        return bool(parsed.hostname)
    return bool(_DOMAIN_PATH.match(text))


def is_source_url_title(value: object, source_url: object) -> bool:
    """Recognize the intake placeholder that repeats this article's own URL."""
    title = normalize_title(value)
    source = normalize_title(source_url)
    if not title or not source:
        return False
    if not _URL_PREFIX.match(title):
        if not _DOMAIN_PATH.match(title):
            return False
        title = "https://" + title
    try:
        left, right = urlparse(title), urlparse(source)
    except ValueError:
        return False
    return bool(left.hostname and right.hostname and
                left.hostname.lower() == right.hostname.lower() and
                left.path.rstrip("/") == right.path.rstrip("/"))


def is_article_title(value: object) -> bool:
    text = normalize_title(value)
    if not text or text == TITLE_PENDING or is_url_title(text):
        return False
    if text.casefold() in _GENERIC_TITLES:
        return False
    return True


def has_han(value: object) -> bool:
    return bool(_HAN.search(normalize_title(value)))


def can_update_base_title(current: object, incoming: object, source_url: str = "") -> bool:
    """Allow title writes into empty or known-placeholder cells only."""
    old = normalize_title(current)
    new = normalize_title(incoming)
    if not new:
        return False
    old_is_placeholder = (not old or old == TITLE_PENDING or old == "未命名文章" or
                          (is_url_title(old) and (not source_url or is_source_url_title(old, source_url))))
    if new == TITLE_PENDING:
        return old_is_placeholder
    if not is_article_title(new):
        return False
    return old_is_placeholder or old == new


def choose_article_title(candidates: list[object], source_url: str = "") -> str | None:
    """Choose a real page title; prefer Chinese candidates and reject URL placeholders.

    Candidate order is retained within each language group, allowing callers to
    pass canonical metadata before less authoritative fallbacks.
    """
    accepted: list[str] = []
    for candidate in candidates:
        title = normalize_title(candidate)
        if not is_article_title(title):
            continue
        if source_url and title.rstrip("/") == normalize_title(source_url).rstrip("/"):
            continue
        if title not in accepted:
            accepted.append(title)
    if not accepted:
        return None
    return next((title for title in accepted if has_han(title)), accepted[0])
