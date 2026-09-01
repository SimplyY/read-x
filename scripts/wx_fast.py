#!/usr/bin/env python3
"""使用标准库抓取微信文章并转换为 Markdown，不启动浏览器。"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener


UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36"
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.parts: list[str] = []
        self.capture = False
        self.depth = 0
        self.in_pre = False
        self._anchors: list[dict[str, list[str] | str]] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "meta":
            key = values.get("property") or values.get("name")
            if key and values.get("content"):
                self.meta.setdefault(key, values["content"])
        if not self.capture:
            if values.get("id") == "js_content":
                self.capture, self.depth = True, 1
            return
        if tag == "a":
            self._anchors.append({"href": html.unescape(values.get("href", "")), "text": []})
        if tag not in VOID_TAGS:
            self.depth += 1
        if tag in {"h1", "h2", "h3", "h4"}:
            self.parts.append(f"\n\n{'#' * int(tag[1])} ")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag == "blockquote":
            self.parts.append("\n> ")
        elif tag == "pre":
            self.in_pre = True
            self.parts.append("\n```\n")
        elif tag == "br":
            self.parts.append("\n")
        elif tag in {"p", "section", "div", "ul", "ol"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if not self.capture:
            return
        if tag == "a" and self._anchors:
            anchor = self._anchors.pop()
            href = str(anchor["href"])
            text = "".join(str(part) for part in anchor["text"]).strip()
            if href and text:
                self.links.append((href, text))
        if tag == "pre":
            self.in_pre = False
            self.parts.append("\n```\n")
        elif tag in {"p", "section", "div", "li", "blockquote", "h1", "h2", "h3", "h4", "ul", "ol"}:
            self.parts.append("\n")
        if tag not in VOID_TAGS:
            self.depth -= 1
            if self.depth == 0:
                self.capture = False

    def handle_data(self, data: str) -> None:
        if not self.capture:
            return
        if self._anchors:
            self._anchors[-1]["text"].append(data)  # type: ignore[union-attr]
        self.parts.append(data if self.in_pre else re.sub(r"\s+", " ", data))

    def markdown(self) -> str:
        text = "".join(self.parts).replace("\xa0", " ")
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n[ \t]+", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def _js_string(source: str, name: str) -> str:
    match = re.search(rf"(?:var\s+)?{re.escape(name)}\s*=\s*(['\"])(.*?)\1\s*;?", source, re.DOTALL)
    if not match:
        return ""
    try:
        return json.loads(f'"{match.group(2)}"')
    except json.JSONDecodeError:
        return html.unescape(match.group(2))


def _external_http_url(value: str) -> str | None:
    value = html.unescape(value).strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    if host == "mp.weixin.qq.com" or host.endswith(".mp.weixin.qq.com"):
        return None
    return value


def _original_source_candidate(source_url: str, links: list[tuple[str, str]]) -> str | None:
    candidate = _external_http_url(source_url)
    if candidate:
        return candidate
    labels = {"原文", "原文链接", "阅读原文", "原始出处", "source", "original"}
    for href, text in links:
        normalized = re.sub(r"[\s：:·。.!！?？]+", "", text).casefold()
        if normalized in labels:
            candidate = _external_http_url(href)
            if candidate:
                return candidate
    return None


def parse_article(source: str, url: str) -> str:
    parser = ArticleParser()
    parser.feed(source)
    title = parser.meta.get("og:title") or _js_string(source, "msg_title")
    author = parser.meta.get("og:article:author") or parser.meta.get("author") or _js_string(source, "nickname")
    published = parser.meta.get("article:published_time")
    if not published:
        timestamp = _js_string(source, "ct")
        if timestamp.isdigit():
            published = datetime.fromtimestamp(int(timestamp), timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    body = parser.markdown()
    if not title:
        raise ValueError("no_title")
    if len(body) < 200:
        raise ValueError("no_content")
    metadata = [f"# {title}", ""]
    if author:
        metadata.append(f"> 公众号: {author}")
    if published:
        metadata.append(f"> 发布时间: {published}")
    candidate = _original_source_candidate(_js_string(source, "source_url"), parser.links)
    if candidate:
        metadata.append(f"> 原始出处候选: {candidate}")
    metadata.extend([f"> 原文链接: {url}", "", "---", ""])
    return "\n".join(metadata) + body + "\n"


def fetch(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "mp.weixin.qq.com":
        raise ValueError("只允许公开的 https://mp.weixin.qq.com/ URL")
    request = Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    with build_opener(ProxyHandler({})).open(request, timeout=15) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        source = response.read().decode(charset, errors="replace")
    return parse_article(source, url)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch WeChat article over HTTP only (no browser, no images)")
    parser.add_argument("url")
    parser.add_argument("-o", "--output", default="-")
    args = parser.parse_args()
    started = time.monotonic()
    try:
        markdown = fetch(args.url)
    except Exception as exc:
        print(f"❌ HTTP 抓取失败: {exc}", file=sys.stderr)
        return 1
    if args.output == "-":
        sys.stdout.write(markdown)
    else:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(markdown, encoding="utf-8")
        print(f"✅ 已保存: {output}", file=sys.stderr)
    print(f"⏱️  总耗时 {time.monotonic() - started:.1f}s (方式: HTTP)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
