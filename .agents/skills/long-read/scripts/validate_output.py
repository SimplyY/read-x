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
VALID_EVIDENCE_TYPES = {"quote", "data", "example", "reasoning"}
VALID_CONFIDENCE = {"high", "medium", "low"}
MAX_QUOTES = 8
MAX_PARAGRAPH_CHARS = 100
MAX_PERSONALIZATION_CHARS = 50


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
    for key in ["metadata", "claims", "facts", "data_points", "quotes",
                "uncertainties", "article_structure"]:
        if key not in ev:
            findings.append(f"missing top-level key: {key}")
    meta = ev.get("metadata", {})
    if not isinstance(meta, dict):
        findings.append("metadata is not an object")
    else:
        for k in REQUIRED_META:
            if k not in meta:
                findings.append(f"metadata missing field: {k}")
    for i, c in enumerate(ev.get("claims", [])):
        if not isinstance(c, dict):
            findings.append(f"claims[{i}] is not an object")
            continue
        for f in REQUIRED_CLAIM_FIELDS:
            if f not in c:
                findings.append(f"claims[{i}] missing field: {f}")
        et = c.get("evidence_type")
        if et and et not in VALID_EVIDENCE_TYPES:
            findings.append(f"claims[{i}] invalid evidence_type: {et}")
        conf = c.get("confidence")
        if conf and conf not in VALID_CONFIDENCE:
            findings.append(f"claims[{i}] invalid confidence: {conf}")
    quotes = ev.get("quotes", [])
    if isinstance(quotes, list) and len(quotes) > MAX_QUOTES:
        findings.append(
            f"quotes has {len(quotes)} items; maximum is {MAX_QUOTES}")
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
        if node.attrib.get("background-color") == "light-yellow"
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
    for i, node in enumerate(children):
        if node.tag not in {"h1", "h2", "h3"}:
            continue
        if visible_text(node).replace(" ", "") != "对飞鱼的意义":
            continue
        text_parts = []
        for following in children[i + 1:]:
            if following.tag in {"h1", "h2", "h3"}:
                break
            text_parts.append(visible_text(following))
        length = len("".join(text_parts))
        if length > MAX_PERSONALIZATION_CHARS:
            findings.append(
                f"personalization has {length} chars; maximum is "
                f"{MAX_PERSONALIZATION_CHARS}")
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
        '<title>t</title><table><tbody><tr><td>x</td></tr></tbody></table>'
        '<callout background-color="light-yellow"><p>sharp</p></callout>'
        '<h1>真正的核心</h1><p>short paragraph</p>'
        '<h1>对飞鱼的意义</h1><p>有增量才写。</p>'
        '<h1>Evidence</h1><blockquote>quote</blockquote>')
    f = check_document_xml(good_doc)
    cases.append(("pass: valid document layout", f == [], f))

    # document layout failures
    bad_doc = (
        '<callout background-color="light-yellow"><p>one</p></callout>'
        '<callout background-color="light-yellow"><p>two</p></callout>'
        '<h1>文章骨架</h1><h2>X 光阅读</h2><p>' + ('长' * 101) + '</p>'
        '<h1>对飞鱼的意义</h1><p>' + ('多' * 51) + '</p>')
    f = check_document_xml(bad_doc)
    cases.append(("fail: invalid document layout", len(f) == 5, f))

    too_many_doc_quotes = (
        '<callout background-color="light-yellow"><p>one</p></callout>'
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
