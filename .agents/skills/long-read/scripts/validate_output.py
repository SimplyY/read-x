#!/usr/bin/env python3
"""Validate long-read evidence output: structure + quote substring checks.

Usage:
  python3 validate_output.py <evidence.json> <source.md>
  python3 validate_output.py --document <document.xml>
  python3 validate_output.py --self-check

Exit code 0 = pass, 1 = fail. Prints findings to stdout.
"""
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REQUIRED_META = {"title", "author", "source_url", "published_at", "genre", "word_count"}
REQUIRED_CLAIM_FIELDS = {"id", "claim", "evidence", "evidence_type", "confidence"}
EVIDENCE_FIELDS = {"metadata", "claims", "facts", "data_points", "quotes", "uncertainties", "article_structure"}
VALID_EVIDENCE_TYPES = {"quote", "data", "example", "reasoning"}
VALID_CONFIDENCE = {"high", "medium", "low"}
MAX_QUOTES = 8
MAX_PARAGRAPH_CHARS = 100
RESEARCH_HEADING = "值得研究的相关问题"
RESEARCH_QUESTION_HEADING = "问题"
RESEARCH_CONTEXT_HEADING = "上下文"
SCORE_HEADING = "评分"
LEGACY_HEADING = "对飞鱼的意义"
RESEARCH_MIN_ITEMS = 2
RESEARCH_MAX_ITEMS = 3
MAX_RESEARCH_CHARS = 300
LIGHT_YELLOW_CALLOUT_BACKGROUNDS = {"light-yellow", "rgb(254,255,240)"}


def normalize(s):
    """Normalize whitespace for substring matching: collapse runs, strip ends."""
    if not isinstance(s, str):
        return ""
    return " ".join(s.split())


def check_structure(ev):
    """Check evidence JSON has required top-level keys and field types."""
    findings = []
    if not isinstance(ev, dict):
        return ["evidence is not a JSON object"]
    for key in EVIDENCE_FIELDS:
        if key not in ev:
            findings.append(f"missing top-level key: {key}")
    for key in set(ev) - EVIDENCE_FIELDS:
        findings.append(f"unexpected top-level key: {key}")
    meta = ev.get("metadata", {})
    if not isinstance(meta, dict):
        findings.append("metadata is not an object")
    else:
        for k in REQUIRED_META:
            if k not in meta:
                findings.append(f"metadata missing field: {k}")
        for k in set(meta) - REQUIRED_META:
            findings.append(f"metadata has unexpected field: {k}")
        for key in ("title", "genre"):
            if key in meta and (not isinstance(meta[key], str) or not meta[key].strip()):
                findings.append(f"metadata {key} must be a non-empty string")
        for key in ("author", "source_url", "published_at"):
            if key in meta and meta[key] is not None and (not isinstance(meta[key], str) or not meta[key].strip()):
                findings.append(f"metadata {key} must be null or a non-empty string")
        if "word_count" in meta and (isinstance(meta["word_count"], bool) or not isinstance(meta["word_count"], int) or meta["word_count"] < 0):
            findings.append("metadata word_count must be a non-negative integer")
    claims = ev.get("claims", [])
    if not isinstance(claims, list):
        findings.append("claims is not a list")
        claims = []
    for i, c in enumerate(claims):
        if not isinstance(c, dict):
            findings.append(f"claims[{i}] is not an object")
            continue
        for f in REQUIRED_CLAIM_FIELDS:
            if f not in c:
                findings.append(f"claims[{i}] missing field: {f}")
        for f in set(c) - REQUIRED_CLAIM_FIELDS:
            findings.append(f"claims[{i}] has unexpected field: {f}")
        for f in REQUIRED_CLAIM_FIELDS:
            if f in c and (not isinstance(c[f], str) or not c[f].strip()):
                findings.append(f"claims[{i}] {f} must be a non-empty string")
        et = c.get("evidence_type")
        if isinstance(et, str) and et and et not in VALID_EVIDENCE_TYPES:
            findings.append(f"claims[{i}] invalid evidence_type: {et}")
        conf = c.get("confidence")
        if isinstance(conf, str) and conf and conf not in VALID_CONFIDENCE:
            findings.append(f"claims[{i}] invalid confidence: {conf}")
    quotes = ev.get("quotes", [])
    if isinstance(quotes, list) and len(quotes) > MAX_QUOTES:
        findings.append(
            f"quotes has {len(quotes)} items; maximum is {MAX_QUOTES}")
    for key in ("facts", "data_points", "quotes", "uncertainties", "article_structure"):
        if key in ev and not isinstance(ev[key], list):
            findings.append(f"{key} is not a list")
    for key in ("facts", "data_points", "quotes", "uncertainties", "article_structure"):
        for i, value in enumerate(ev.get(key, []) if isinstance(ev.get(key), list) else []):
            if not isinstance(value, str) or not value.strip():
                findings.append(f"{key}[{i}] is not a non-empty string")
    return findings


def check_quotes_substring(ev, source_text):
    """Check every quote is a contiguous substring of the source."""
    findings = []
    norm_source = normalize(source_text)
    if not norm_source:
        return ["source text is empty"]
    # top-level quotes
    for i, q in enumerate(ev.get("quotes", [])):
        nq = normalize(q)
        if not nq:
            continue
        if nq not in norm_source:
            findings.append(f"quotes[{i}] not a substring of source")
    # claim evidence where type=quote
    for i, c in enumerate(ev.get("claims", [])):
        if not isinstance(c, dict):
            continue
        if c.get("evidence_type") == "quote":
            nq = normalize(c.get("evidence", ""))
            if not nq:
                continue
            if nq not in norm_source:
                findings.append(
                    f"claims[{i}] (quote) evidence not a substring of source")
    return findings


def validate(evidence_path, source_path):
    ev = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
    src = Path(source_path).read_text(encoding="utf-8")
    findings = check_structure(ev) + check_quotes_substring(ev, src)
    return findings


def visible_text(node):
    """Return normalized visible text inside an XML node."""
    return normalize("".join(node.itertext()))


def check_document_xml(xml_text):
    """Check deterministic long-read XML layout constraints."""
    findings = []
    try:
        root = ET.fromstring(f"<root>{xml_text}</root>")
    except ET.ParseError as exc:
        return [f"document XML is invalid: {exc}"]

    paragraphs = list(root.iter("p"))
    for i, paragraph in enumerate(paragraphs):
        length = len(visible_text(paragraph))
        if length > MAX_PARAGRAPH_CHARS:
            findings.append(
                f"paragraph[{i}] has {length} chars; maximum is "
                f"{MAX_PARAGRAPH_CHARS}")

    gold_callouts = [
        node for node in root.iter("callout")
        if node.attrib.get("background-color") in LIGHT_YELLOW_CALLOUT_BACKGROUNDS
    ]
    if len(gold_callouts) != 1:
        findings.append(
            f"document must contain exactly one light-yellow callout; "
            f"found {len(gold_callouts)}")

    quote_blocks = list(root.iter("blockquote"))
    if len(quote_blocks) > MAX_QUOTES:
        findings.append(
            f"document has {len(quote_blocks)} quote blocks; maximum is "
            f"{MAX_QUOTES}")

    for heading in [*root.iter("h1"), *root.iter("h2"), *root.iter("h3")]:
        title = visible_text(heading).replace(" ", "")
        if "骨架" in title or "X光" in title:
            findings.append(f"forbidden heading: {visible_text(heading)}")

    children = list(root)
    if not children or children[0].tag != "title":
        findings.append("document root must begin with <title>")
    document_title = visible_text(children[0]) if children and children[0].tag == "title" else ""
    headings = [
        (i, node.tag, visible_text(node).replace(" ", ""))
        for i, node in enumerate(children)
        if node.tag in {"h1", "h2", "h3"}
    ]
    if not headings:
        findings.append("document must contain a main heading")
    else:
        first_index, first_tag, first_title = headings[0]
        if document_title and first_title == document_title.replace(" ", ""):
            findings.append("document title must not be repeated as the first body heading")
        if first_tag != "h1" or first_title != SCORE_HEADING:
            findings.append("first main heading must be <h1>评分</h1>")
    score_headings = [(i, tag) for i, tag, title in headings if title == SCORE_HEADING]
    if len(score_headings) != 1:
        findings.append(f"document must contain exactly one {SCORE_HEADING} heading; found {len(score_headings)}")
    elif score_headings[0][1] != "h1":
        findings.append(f"{SCORE_HEADING} must be an h1 heading")
    legacy = [i for i, _, title in headings if title == LEGACY_HEADING]
    if legacy:
        findings.append(f"forbidden heading: {LEGACY_HEADING}")

    research = [i for i, _, title in headings if title == RESEARCH_HEADING]
    if len(research) != 1:
        findings.append(
            f"document must contain exactly one {RESEARCH_HEADING} heading; "
            f"found {len(research)}")
        return findings

    research_index = research[0]
    previous_titles = [title for i, _, title in headings if i < research_index]
    next_headings = [i for i, tag, _ in headings if i > research_index and tag == "h1"]
    next_index = next_headings[0] if next_headings else len(children)
    if "基石/边缘/暗流" not in previous_titles:
        findings.append("research questions must follow 基石 / 边缘 / 暗流")
    if next_index == len(children) or visible_text(children[next_index]).replace(" ", "") != "与作者对话":
        findings.append("research questions must precede 与作者对话")

    section_nodes = [node for node in children[research_index + 1:next_index] if node.tag != "hr"]
    if [node.tag for node in section_nodes] != ["h2", "ol", "h2", "ul"]:
        findings.append("research questions must contain h2 问题 + ol + h2 上下文 + ul")
        return findings

    if visible_text(section_nodes[0]).replace(" ", "") != RESEARCH_QUESTION_HEADING:
        findings.append("research questions must begin with h2 问题")
    if visible_text(section_nodes[2]).replace(" ", "") != RESEARCH_CONTEXT_HEADING:
        findings.append("research questions must contain h2 上下文")
    items = section_nodes[1].findall("li")
    if not RESEARCH_MIN_ITEMS <= len(items) <= RESEARCH_MAX_ITEMS:
        findings.append(
            f"research questions has {len(items)} items; expected "
            f"{RESEARCH_MIN_ITEMS}-{RESEARCH_MAX_ITEMS}")
    for i, item in enumerate(items):
        text = visible_text(item)
        if text.startswith("问题：") or "上下文：" in text:
            findings.append(f"research question[{i}] must not inline 问题/上下文 labels")
        if "？" not in text and "?" not in text:
            findings.append(f"research item[{i}] is not phrased as a question")

    context_items = section_nodes[3].findall("li")
    if not context_items:
        findings.append("research context must contain at least one shared context item")
    for i, item in enumerate(context_items):
        text = visible_text(item)
        if text.startswith("问题：") or text.startswith("上下文："):
            findings.append(f"research context[{i}] must not inline labels")

    section_length = len("".join(visible_text(node) for node in section_nodes if node.tag not in {"h2"}))
    if section_length > MAX_RESEARCH_CHARS:
        findings.append(
            f"research questions has {section_length} chars; maximum is "
            f"{MAX_RESEARCH_CHARS}")
    return findings


def self_check():
    """Run built-in assertions covering pass/fail/malformed cases."""
    cases = []

    # pass case: quote present in source
    good = {
        "metadata": {"title": "t", "author": "a", "source_url": "u",
                     "published_at": None, "genre": "essay", "word_count": 100},
        "claims": [{"id": "C1", "claim": "x", "evidence": "real quote",
                    "evidence_type": "quote", "confidence": "high"}],
        "facts": [], "data_points": [], "quotes": ["real quote"],
        "uncertainties": [], "article_structure": [],
    }
    src_ok = "here is a real quote inside the body."
    f = check_structure(good) + check_quotes_substring(good, src_ok)
    cases.append(("pass: valid evidence + matching quotes", f == [], f))

    # fail case: quote not a substring
    bad_q = json.loads(json.dumps(good))
    bad_q["quotes"] = ["fabricated quote not in source"]
    f = check_structure(bad_q) + check_quotes_substring(bad_q, src_ok)
    cases.append(("fail: quote not in source", len(f) == 1, f))

    # fail case: missing top-level key
    bad_k = json.loads(json.dumps(good))
    del bad_k["uncertainties"]
    f = check_structure(bad_k)
    cases.append(("fail: missing uncertainties", len(f) == 1, f))

    # fail case: bad evidence_type
    bad_t = json.loads(json.dumps(good))
    bad_t["claims"][0]["evidence_type"] = "hearsay"
    f = check_structure(bad_t)
    cases.append(("fail: bad evidence_type", len(f) == 1, f))

    bad_types = json.loads(json.dumps(good))
    bad_types["metadata"]["title"] = {"user_profile": "x"}
    bad_types["metadata"]["word_count"] = "100"
    bad_types["claims"][0]["evidence"] = ""
    bad_types["quotes"] = [""]
    f = check_structure(bad_types) + check_quotes_substring(bad_types, src_ok)
    cases.append(("fail: malformed field types and empty quotes", len(f) == 4, f))

    # author null is allowed
    null_author = json.loads(json.dumps(good))
    null_author["metadata"]["author"] = None
    f = check_structure(null_author)
    cases.append(("pass: author null allowed", f == [], f))

    # fail case: more than eight quotes
    too_many_quotes = json.loads(json.dumps(good))
    too_many_quotes["quotes"] = ["real quote"] * 9
    f = check_structure(too_many_quotes)
    cases.append(("fail: more than eight quotes", len(f) == 1, f))

    # document layout pass
    good_doc = (
        '<title>t</title><h1>评分</h1>'
        '<table><tbody><tr><td>x</td></tr></tbody></table>'
        '<callout background-color="light-yellow"><p>sharp</p></callout>'
        '<h1>基石 / 边缘 / 暗流</h1>'
        '<h1>值得研究的相关问题</h1><h2>问题</h2>'
        '<ol><li>因果链在哪里断裂？</li><li>这个机制何时失效？</li></ol>'
        '<h2>上下文</h2><ul><li>文章只证明相关性。</li><li>边界条件没有展开。</li></ul>'
        '<h1>与作者对话</h1><p>short paragraph</p>'
        '<h1>Evidence</h1><blockquote>quote</blockquote>')
    f = check_document_xml(good_doc)
    cases.append(("pass: valid document layout", f == [], f))

    legacy_color_doc = good_doc.replace(
        'background-color="light-yellow"',
        'background-color="rgb(254,255,240)"',
    )
    f = check_document_xml(legacy_color_doc)
    cases.append(("pass: equivalent light-yellow RGB callout", f == [], f))

    three_item_doc = good_doc.replace(
        '</ol><h2>上下文</h2>',
        '<li>什么证据能推翻这个机制？</li></ol><h2>上下文</h2>')
    f = check_document_xml(three_item_doc)
    cases.append(("pass: three research questions", f == [], f))

    exact_300_doc = (
        '<title>t</title><h1>评分</h1><callout background-color="light-yellow"><p>one</p></callout>'
        '<h1>基石 / 边缘 / 暗流</h1><h1>值得研究的相关问题</h1><h2>问题</h2><ol>'
        '<li>为什么？</li><li>何时失效？</li></ol><h2>上下文</h2><ul><li>' + ('长' * 291) + '</li></ul>'
        '<h1>与作者对话</h1>')
    f = check_document_xml(exact_300_doc)
    cases.append(("pass: research questions exactly 300 chars", f == [], f))

    # document layout failures
    bad_doc = (
        '<title>t</title><h1>错误首章</h1>'
        '<callout background-color="light-yellow"><p>one</p></callout>'
        '<callout background-color="light-yellow"><p>two</p></callout>'
        '<h1>文章骨架</h1><h2>X 光阅读</h2><p>' + ('长' * 101) + '</p>'
        '<h1>基石 / 边缘 / 暗流</h1>'
        '<h1>值得研究的相关问题</h1><ol><li>只有一个？</li></ol>'
        '<h1>与作者对话</h1><h1>对飞鱼的意义</h1>')
    f = check_document_xml(bad_doc)
    cases.append(("fail: invalid document layout", len(f) >= 1, f))

    duplicate_title_doc = good_doc.replace('<h1>评分</h1>', '<h1>t</h1><h1>评分</h1>')
    f = check_document_xml(duplicate_title_doc)
    cases.append(("fail: duplicate document title", any("repeated" in item for item in f), f))

    wrong_score_level_doc = good_doc.replace('<h1>评分</h1>', '<h2>评分</h2>')
    f = check_document_xml(wrong_score_level_doc)
    cases.append(("fail: score is not h1", any("评分" in item for item in f), f))

    old_research_doc = good_doc.replace(
        '<h2>问题</h2><ol><li>因果链在哪里断裂？</li><li>这个机制何时失效？</li></ol>'
        '<h2>上下文</h2><ul><li>文章只证明相关性。</li><li>边界条件没有展开。</li></ul>',
        '<ol><li>问题：因果链在哪里断裂？ 上下文：文章只证明相关性。</li>'
        '<li>问题：这个机制何时失效？ 上下文：边界条件没有展开。</li></ol>')
    f = check_document_xml(old_research_doc)
    cases.append(("fail: inline research context", any("h2 问题" in item for item in f), f))

    too_long_research = (
        '<title>t</title><h1>评分</h1>'
        '<callout background-color="light-yellow"><p>one</p></callout>'
        '<h1>基石 / 边缘 / 暗流</h1><h1>值得研究的相关问题</h1><h2>问题</h2><ol>'
        '<li>这个问题足够长吗？</li><li>另一个问题是什么？</li></ol><h2>上下文</h2><ul><li>' + ('长' * 280) + '</li><li>补充。</li></ul>'
        '<h1>与作者对话</h1>')
    f = check_document_xml(too_long_research)
    cases.append(("fail: research questions over 300 chars", len(f) == 1, f))

    wrong_order = (
        '<title>t</title><h1>评分</h1>'
        '<callout background-color="light-yellow"><p>one</p></callout>'
        '<h1>值得研究的相关问题</h1><h2>问题</h2><ol>'
        '<li>为什么？</li><li>何时失效？</li></ol><h2>上下文</h2><ul><li>背景。</li><li>边界。</li></ul>'
        '<h1>基石 / 边缘 / 暗流</h1><h1>与作者对话</h1>')
    f = check_document_xml(wrong_order)
    cases.append(("fail: research questions wrong order", len(f) == 2, f))

    missing_research = (
        '<title>t</title><h1>评分</h1>'
        '<callout background-color="light-yellow"><p>one</p></callout>'
        '<h1>基石 / 边缘 / 暗流</h1><h1>与作者对话</h1>')
    f = check_document_xml(missing_research)
    cases.append(("fail: missing research questions", len(f) == 1, f))

    duplicate_research = good_doc.replace(
        '<h1>与作者对话</h1>',
        '<h1>值得研究的相关问题</h1><h1>与作者对话</h1>')
    f = check_document_xml(duplicate_research)
    cases.append(("fail: duplicate research questions", len(f) == 1, f))

    four_item_doc = good_doc.replace(
        '</ol><h2>上下文</h2>',
        '<li>还能验证什么？</li><li>还有什么边界？</li></ol><h2>上下文</h2>')
    f = check_document_xml(four_item_doc)
    cases.append(("fail: four research questions", len(f) == 1, f))

    too_many_doc_quotes = (
        '<title>t</title><h1>评分</h1>'
        '<callout background-color="light-yellow"><p>one</p></callout>'
        '<h1>基石 / 边缘 / 暗流</h1><h1>值得研究的相关问题</h1><h2>问题</h2><ol>'
        '<li>为什么？</li><li>何时失效？</li></ol><h2>上下文</h2><ul><li>背景。</li><li>边界。</li></ul>'
        '<h1>与作者对话</h1>'
        + ('<blockquote>quote</blockquote>' * 9))
    f = check_document_xml(too_many_doc_quotes)
    cases.append(("fail: more than eight document quotes", len(f) == 1, f))

    ok = True
    for name, expect, f in cases:
        status = "ok" if expect else "FAIL"
        if not expect:
            ok = False
        print(f"  [{status}] {name}")
        if f:
            print(f"        findings: {f}")
    return ok


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(2)
    if args[0] == "--self-check":
        ok = self_check()
        print("self-check:", "ok" if ok else "FAIL")
        sys.exit(0 if ok else 1)
    if args[0] == "--document":
        if len(args) != 2:
            print("usage: validate_output.py --document <document.xml>")
            sys.exit(2)
        findings = check_document_xml(
            Path(args[1]).read_text(encoding="utf-8"))
        if findings:
            print("FAIL:")
            for finding in findings:
                print(f"  - {finding}")
            sys.exit(1)
        print("ok: document XML satisfies long-read layout constraints")
        sys.exit(0)
    if len(args) != 2:
        print("usage: validate_output.py <evidence.json> <source.md>")
        sys.exit(2)
    findings = validate(args[0], args[1])
    if findings:
        print("FAIL:")
        for f in findings:
            print(f"  - {f}")
        sys.exit(1)
    print("ok: evidence structure valid, all quotes are source substrings")
    sys.exit(0)


if __name__ == "__main__":
    main()
