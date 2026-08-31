#!/usr/bin/env python3
"""Content Scoring deterministic calculator.

Usage:
  python3 scripts/content_scoring.py quality.json source.md \
    [--retry-quality-output retry.json] \
    [--importance-output importance.json] \
    [--relevance-output relevance.json --context context.md] \
    [--config-from-base <base-config.json>]
  python3 scripts/content_scoring.py --self-check

When --config-from-base is provided, the external config file overrides
scoring-policy.json for all tunable parameters (route thresholds, quality
bands, priority bands, relevance bonus). Structural constants (dimension
scores, weights, disqualifiers, evidence caps, claims, retry) remain
from policy.json and cannot be changed via Base.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import math
import json
import os
import re
import sys
import unicodedata
from urllib.parse import urlparse
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

POLICY_PATH = Path(__file__).parents[1] / ".agents/skills/content-scoring/references/scoring-policy.json"

# Fields that can be overridden by Base config
_TUNABLE_KEYS = {"route", "quality_bands", "priority_bands", "relevance_bonus"}


def _validate_runtime_policy(policy):
    route = policy.get("route")
    if not isinstance(route, dict):
        raise ValueError("base config route is invalid")
    floor, threshold = route.get("quality_floor"), route.get("long_read_threshold")
    chatgpt_threshold = route.get("chatgpt_munger_threshold", 8.5)
    if any(isinstance(value, bool) or not isinstance(value, (int, float, Decimal)) or not math.isfinite(value)
           for value in (floor, threshold, chatgpt_threshold)) or floor < 0 or threshold < 0 or chatgpt_threshold < 0 or chatgpt_threshold > 10 or floor >= threshold:
        raise ValueError("base config route thresholds are invalid")

    for key in ("quality_bands", "priority_bands"):
        bands = policy.get(key)
        if not isinstance(bands, list) or not bands:
            raise ValueError(f"base config {key} is invalid")
        for band in bands:
            if not isinstance(band, dict):
                raise ValueError(f"base config {key} contains an invalid band")
            minimum = band.get("minimum")
            if (not isinstance(minimum, (int, float, Decimal)) or isinstance(minimum, bool) or not math.isfinite(minimum)
                    or not isinstance(band.get("label"), str) or not band["label"].strip()):
                raise ValueError(f"base config {key} contains an invalid band")
        if bands[-1]["minimum"] > 0:
            raise ValueError(f"base config {key} is invalid")
        if any(bands[index]["minimum"] < bands[index + 1]["minimum"] for index in range(len(bands) - 1)):
            raise ValueError(f"base config {key} is not sorted")
    for band in policy["quality_bands"]:
        depth = band.get("ljg_range")
        if (not isinstance(depth, list) or len(depth) != 2 or any(isinstance(value, bool) or not isinstance(value, int) for value in depth)
                or depth[0] < 0 or depth[0] > depth[1] or not isinstance(band.get("ljg_card"), bool)):
            raise ValueError("base config quality depth is invalid")
    bonus = policy.get("relevance_bonus")
    if not isinstance(bonus, dict):
        raise ValueError("base config relevance bonus is invalid")
    values = [bonus.get(key) for key in ("max", "relevance_max", "interest_max")]
    if any(isinstance(value, bool) or not isinstance(value, (int, float, Decimal)) or not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError("base config relevance bonus is invalid")
    if values[1] + values[2] > values[0]:
        raise ValueError("base config relevance bonus exceeds max")
    importance_weight = policy.get("importance_weight")
    if (isinstance(importance_weight, bool) or not isinstance(importance_weight, (int, float, Decimal))
            or not math.isfinite(importance_weight) or not 0 < importance_weight < 1):
        raise ValueError("importance weight is invalid")


def _load_policy(external_config_path: str | None = None):
    """Load policy.json, optionally overriding tunable fields from external config."""
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"), parse_float=Decimal)
    if external_config_path:
        ext_path = Path(external_config_path)
        if not ext_path.is_file():
            raise ValueError(f"external config not found: {external_config_path}")
        ext = json.loads(ext_path.read_text(encoding="utf-8"), parse_float=Decimal)
        if not isinstance(ext, dict) or not _TUNABLE_KEYS.issubset(ext):
            raise ValueError("base config is incomplete")
        for key in _TUNABLE_KEYS:
            policy[key] = ext[key]
        _validate_runtime_policy(policy)
    return policy


POLICY = _load_policy()
POLICY_SOURCE = "local"
SCORE_VERSION = POLICY["versions"]["score"]
QUALITY_VERSION = POLICY["versions"]["quality"]
RELEVANCE_VERSION = POLICY["versions"]["relevance"]
IMPORTANCE_WEIGHT = Decimal(str(POLICY["importance_weight"]))
DIMENSION_SCORES = {Decimal(str(value)) for value in POLICY["dimension_scores"]}
QUALITY_DIMENSIONS = POLICY["quality_dimensions"]
QUALITY_DISQUALIFIERS = POLICY["quality_disqualifiers"]
CLAIM_TYPES = {"empirical", "causal", "experiential", "normative", "method"}
SUPPORT_LEVELS = {"direct", "partial", "asserted"}
CONTEXT_SECTIONS = {"当前主线", "当前张力", "长期校准", "暂不做什么"}
FULL_CONTEXT_SECTIONS = CONTEXT_SECTIONS | {"领域兴趣"}
READING_CATEGORY_KEYS = ("invest", "ai", "cognition", "business", "culture", "other")
READING_CATEGORY_LABELS = {"invest": "投资/财经", "ai": "AI/技术", "cognition": "认知/成长", "business": "商业/产业", "culture": "人文/生活", "other": "未分类"}
_CATEGORY_SYNONYMS = [
    ("invest", ("投资", "财经", "金融", "股票", "股市", "基金", "复利", "经济", "宏观", "能源", "energy", "财富", "资本", "估值")),
    ("ai", ("ai", "人工智能", "大模型", "llm", "agent", "智能体", "技术", "科技", "编程", "软件", "算法", "saas", "tech")),
    ("cognition", ("认知", "成长", "教育", "学习", "思维", "心智", "方法", "自驱力")),
    ("business", ("商业", "产业", "创业", "公司", "企业", "管理", "组织", "战略", "创始人", "品牌")),
    ("culture", ("人文", "生活", "艺术", "历史", "文学", "哲学", "电影", "播客", "访谈", "健康", "人生")),
]


def _normalize_reading_category(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    if value in READING_CATEGORY_KEYS:
        return value
    for key, terms in _CATEGORY_SYNONYMS:
        if any(term in value for term in terms):
            return key
    return None
CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"


def _reload_policy(external_config_path: str | None = None):
    """Reload policy from external config, updating module-level globals."""
    global POLICY, POLICY_SOURCE
    POLICY = _load_policy(external_config_path)
    POLICY_SOURCE = "base" if external_config_path else "local"
    globals()["IMPORTANCE_WEIGHT"] = Decimal(str(POLICY["importance_weight"]))


def round1(value) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(rf"(?<=[{CJK}]) (?=[{CJK}\w])|(?<=\w) (?=[{CJK}])", "", text)


def content_fingerprint(text: str) -> str:
    payload = f"{normalize_text(text)}\n{QUALITY_VERSION}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def context_fingerprint(content_fp: str, context_text: str) -> str:
    payload = f"{content_fp}\n{normalize_text(context_text)}\n{RELEVANCE_VERSION}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _weighted_score(dimensions: dict, policy_dimensions: dict) -> float:
    total = Decimal("0")
    for key, spec in policy_dimensions.items():
        total += Decimal(str(dimensions[key]["score"])) * Decimal(str(spec["weight"]))
    return round1(total)


def _nonempty(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


QUOTE_EQUIVALENTS = str.maketrans("“”‘’", "\"\"''")


def _resolve_exact_quote(quote: str, source_text: str) -> str | None:
    if quote in source_text:
        return quote
    canonical_quote = quote.translate(QUOTE_EQUIVALENTS)
    canonical_source = source_text.translate(QUOTE_EQUIVALENTS)
    if canonical_source.count(canonical_quote) != 1:
        return None
    start = canonical_source.index(canonical_quote)
    return source_text[start:start + len(quote)]


def _validate_claims(claims, source_text: str) -> list[str]:
    errors = []
    if not isinstance(claims, list):
        return ["claim_ledger must be an array"]
    limits = POLICY["claims"]
    if len(source_text.strip()) < int(limits["short_text_chars"]):
        minimum = int(limits["short_min"])
        maximum = int(limits["short_max"])
        if not minimum <= len(claims) <= maximum:
            errors.append(f"claim_ledger count must be {minimum}..{maximum} for short text")
    else:
        allowed = [int(value) for value in limits["standard_counts"]]
        if len(claims) not in allowed:
            errors.append(f"claim_ledger count must be one of {allowed}")
    seen = set()
    for index, claim in enumerate(claims):
        prefix = f"claim_ledger[{index}]"
        if not isinstance(claim, dict):
            errors.append(f"{prefix} must be an object")
            continue
        claim_id = claim.get("id")
        if not _nonempty(claim_id):
            errors.append(f"{prefix}.id must be non-empty and unique")
        elif claim_id in seen:
            errors.append(f"{prefix}.id must be non-empty and unique")
        else:
            seen.add(claim_id)
        if claim.get("type") not in CLAIM_TYPES:
            errors.append(f"{prefix}.type is invalid")
        if claim.get("importance") not in {"core", "supporting"}:
            errors.append(f"{prefix}.importance is invalid")
        if not _nonempty(claim.get("claim")):
            errors.append(f"{prefix}.claim is required")
        quote = claim.get("source_quote")
        exact_quote = _resolve_exact_quote(quote, source_text) if _nonempty(quote) else None
        if exact_quote is None:
            errors.append(f"{prefix}.source_quote must be an exact source substring")
        else:
            claim["source_quote"] = exact_quote
        if claim.get("support") not in SUPPORT_LEVELS:
            errors.append(f"{prefix}.support is invalid")
        if claim.get("uncertainty") is not None and not isinstance(claim.get("uncertainty"), str):
            errors.append(f"{prefix}.uncertainty must be string or null")
    return errors


def _quality_level_score(key: str, item: dict) -> tuple[float | None, list[str]]:
    errors = []
    level = item.get("level")
    if not isinstance(level, (int, float)) or isinstance(level, bool) or Decimal(str(level)) not in DIMENSION_SCORES:
        return None, [f"dimensions.{key}.level is invalid"]
    if "passed_levels" in item:
        errors.append(f"dimensions.{key}.passed_levels is obsolete")
    if "semantic_floor" in item:
        errors.append(f"dimensions.{key}.semantic_floor is obsolete")
    if "score" in item:
        errors.append(f"dimensions.{key}.score is script-owned")
    disqualifiers = item.get("disqualifiers")
    if not isinstance(disqualifiers, list) or any(not isinstance(value, str) for value in disqualifiers) or len(disqualifiers) != len(set(disqualifiers)):
        return None, errors + [f"dimensions.{key}.disqualifiers must be a unique string array"]
    unknown_disqualifiers = sorted(set(disqualifiers) - set(QUALITY_DISQUALIFIERS[key]))
    if unknown_disqualifiers:
        errors.append(f"dimensions.{key}.disqualifiers contains unknown values: {unknown_disqualifiers}")
    computed = Decimal(str(level))
    for value in set(disqualifiers) & set(QUALITY_DISQUALIFIERS[key]):
        computed = min(computed, Decimal(str(QUALITY_DISQUALIFIERS[key][value])))
    return float(computed), errors


def _validate_dimensions(dimensions, expected: dict, claim_ids: set[str], relevance=False) -> list[str]:
    errors = []
    if not isinstance(dimensions, dict):
        return ["dimensions must be an object"]
    if set(dimensions) != set(expected):
        errors.append("dimensions must contain exactly the policy keys")
        return errors
    for key in expected:
        item = dimensions[key]
        if not isinstance(item, dict):
            errors.append(f"dimensions.{key} must be an object")
            continue
        if relevance:
            dimension_score = item.get("score")
            if not isinstance(dimension_score, (int, float)) or isinstance(dimension_score, bool) or Decimal(str(dimension_score)) not in DIMENSION_SCORES:
                errors.append(f"dimensions.{key}.score is invalid")
        else:
            dimension_score, level_errors = _quality_level_score(key, item)
            errors += level_errors
            if dimension_score is not None and not level_errors:
                item["score"] = dimension_score
        if not _nonempty(item.get("rationale")):
            errors.append(f"dimensions.{key}.rationale is required")
        if relevance:
            sections = item.get("context_sections")
            if not isinstance(sections, list) or not sections or any(section not in CONTEXT_SECTIONS for section in sections):
                errors.append(f"dimensions.{key}.context_sections is invalid")
        else:
            ids = item.get("claim_ids")
            if not isinstance(ids, list) or not ids or any(claim_id not in claim_ids for claim_id in ids):
                errors.append(f"dimensions.{key}.claim_ids is invalid")
            if not _nonempty(item.get("ceiling_reason")):
                errors.append(f"dimensions.{key}.ceiling_reason is required")
    return errors


def _validate_problem_significance(item, claim_ids: set[str]) -> list[str]:
    if not isinstance(item, dict):
        return ["problem_significance must be an object"]
    errors = []
    level = item.get("level")
    if isinstance(level, bool) or not isinstance(level, (int, float)) or Decimal(str(level)) not in DIMENSION_SCORES:
        errors.append("problem_significance.level is invalid")
    if "score" in item:
        errors.append("problem_significance.score is script-owned")
    ids = item.get("claim_ids")
    if not isinstance(ids, list) or not ids or any(claim_id not in claim_ids for claim_id in ids):
        errors.append("problem_significance.claim_ids is invalid")
    if not _nonempty(item.get("rationale")):
        errors.append("problem_significance.rationale is required")
    if not _nonempty(item.get("ceiling_reason")):
        errors.append("problem_significance.ceiling_reason is required")
    return errors


def _validate_importance_output(output: dict) -> tuple[dict | None, list[str]]:
    """Validate read-only provenance evidence; scoring never invents authority."""
    if output is None:
        return None, ["importance_unavailable"]
    if not isinstance(output, dict):
        return None, ["importance output must be an object", "importance_unavailable"]
    errors = []
    if output.get("schema_version") != SCORE_VERSION:
        errors.append(f"importance schema_version must be {SCORE_VERSION}")
    authority = output.get("authority_score")
    if isinstance(authority, bool) or not isinstance(authority, (int, float)) or Decimal(str(authority)) not in DIMENSION_SCORES:
        errors.append("authority_score is invalid")
    unavailable_artifact = output.get("confidence") == "unavailable" and authority == 4.0
    evidence = output.get("evidence")
    if not isinstance(evidence, list) or not evidence or any(not isinstance(item, dict) for item in evidence):
        if not unavailable_artifact:
            errors.append("importance evidence must be a non-empty array")
        evidence = []
    verified = [item for item in evidence if item.get("verified") is True]
    for index, item in enumerate(evidence):
        if item.get("kind") not in {"publisher", "author", "interview", "primary_source", "self_assertion"}:
            errors.append(f"importance evidence[{index}].kind is invalid")
        if not _nonempty(item.get("label")):
            errors.append(f"importance evidence[{index}].label is required")
        parsed_url = urlparse(str(item.get("url", "")))
        if item.get("verified") is True and (parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc):
            errors.append(f"importance evidence[{index}].url is required for verified evidence")
    if isinstance(authority, (int, float)) and not isinstance(authority, bool):
        if authority >= 9 and (len(verified) < 2 or not any(item.get("kind") in {"interview", "primary_source"} for item in verified)):
            errors.append("authority_score>=9 requires two verified sources including interview or primary_source")
        elif authority > 6 and not verified:
            errors.append("authority_score>6 requires verified evidence")
    if output.get("confidence") not in {"high", "medium", "unavailable"}:
        errors.append("importance confidence must be high or medium")
    if not _nonempty(output.get("rationale")):
        errors.append("importance rationale is required")
    if unavailable_artifact and not errors and not verified:
        return None, ["importance_unavailable"]
    if errors:
        return None, errors + ["importance_unavailable"]
    return {
        "authority_score": round1(authority),
        "evidence": evidence,
        "confidence": output["confidence"],
        "rationale": output["rationale"],
    }, []


def _quality_attempt(output: dict, source_text: str) -> dict:
    if not isinstance(output, dict):
        raise ValueError("quality output must be an object")
    output = deepcopy(output)
    if output.get("schema_version") != QUALITY_VERSION:
        raise ValueError(f"quality schema_version must be {QUALITY_VERSION}")
    if output.get("source_status") != "complete":
        raise ValueError("quality attempt requires complete source")
    claims = output.get("claim_ledger")
    errors = _validate_claims(claims, source_text)
    domain = output.get("detected_domain")
    if not isinstance(domain, dict) or not _nonempty(domain.get("primary")) or not isinstance(domain.get("secondary", ""), str):
        errors.append("detected_domain requires primary and string secondary")
    claim_ids = {
        claim.get("id") for claim in claims
        if isinstance(claim, dict) and isinstance(claim.get("id"), str)
    } if isinstance(claims, list) else set()
    errors += _validate_dimensions(output.get("dimensions"), QUALITY_DIMENSIONS, claim_ids)
    errors += _validate_problem_significance(output.get("problem_significance"), claim_ids)
    if "calibration" in output:
        errors.append("calibration is forbidden in closed-book quality output")
    confidence = output.get("domain_confidence")
    if confidence not in {"high", "medium", "low"}:
        errors.append("domain_confidence is invalid")

    if not _nonempty(output.get("conclusion")):
        errors.append("conclusion is required")
    questions = output.get("questions", [])
    if not isinstance(questions, list) or len(questions) > 3 or any(not _nonempty(question) for question in questions):
        errors.append("questions must contain at most three non-empty strings")
    if errors:
        raise ValueError("; ".join(errors))
    raw = _weighted_score(output["dimensions"], QUALITY_DIMENSIONS)
    evidence_score = Decimal(str(output["dimensions"]["evidence_quality"]["score"]))
    cap = next((item["quality_cap"] for item in POLICY["evidence_caps"] if evidence_score <= Decimal(str(item["maximum_dimension_score"]))), None)
    score_value = min(raw, float(cap)) if cap is not None else raw
    score_value = round1(score_value)
    retry_reasons = []
    if confidence == "low":
        retry_reasons.append("low_domain_confidence")
    problem = output["problem_significance"]
    return {
        "score": score_value,
        "raw": raw,
        "output": output,
        "problem_significance": {"score": round1(problem["level"]), **problem},
        "retry_reasons": retry_reasons,
    }


def _quality_band(value: float) -> str:
    for index, band in enumerate(POLICY["quality_bands"]):
        if value >= float(band["minimum"]):
            return str(index)
    raise ValueError("quality policy has no catch-all band")


def _needs_result(status: str, fp: str, issues: list[str], quality_output=None) -> dict:
    output = quality_output if isinstance(quality_output, dict) else {}
    questions = output.get("questions")
    return {
        "score_version": SCORE_VERSION,
        "policy_source": POLICY_SOURCE,
        "quality_version": QUALITY_VERSION,
        "relevance_version": RELEVANCE_VERSION,
        "content_fingerprint": fp,
        "context_fingerprint": None,
        "score_status": status,
        "quality_score": None,
        "quality_confidence": "low",
        "importance_score": None,
        "importance_confidence": "unavailable",
        "importance_dimensions": {},
        "relevance_score": None,
        "relevance_confidence": "unavailable",
        "decision_score": None,
        "quality_label": status,
        "priority_label": "相关性不可用",
        "route": "card",
        "ljg_range": None,
        "ljg_card": False,
        "chatgpt_munger_doc": False,
        "claims": output.get("claim_ledger") if isinstance(output.get("claim_ledger"), list) else [],
        "quality_dimensions": output.get("dimensions") if isinstance(output.get("dimensions"), dict) else {},
        "relevance_dimensions": {},
        "conclusion": output.get("conclusion") if isinstance(output.get("conclusion"), str) else "",
        "questions": questions[:3] if isinstance(questions, list) else [],
        "issues": issues,
    }


def _validate_full_context(context_text: str) -> bool:
    if not all(re.search(rf"^##\s+{re.escape(section)}\s*$", context_text, re.MULTILINE) for section in FULL_CONTEXT_SECTIONS):
        return False
    interest = re.search(r"^##\s+领域兴趣\s*$([\s\S]*?)(?=^##\s+|\Z)", context_text, re.MULTILINE)
    return bool(interest and re.search(r"^###\s+.*(?:长期|持续关注|持久|核心兴趣)", interest.group(1), re.MULTILINE))


def _validate_context(context_text: str) -> bool:
    return _validate_full_context(context_text)


def _relevance_result(output, context_text: str | None):
    """双轴相关性：relevance_score（元主线命中）+ interest_score（领域兴趣），各自封顶后相加为总 bonus。"""
    if output is None or context_text is None or not _validate_context(context_text):
        return None, None, "unavailable", {}, ["relevance_context_unavailable"]
    errors = []
    # 兼容历史生成侧字段名 relevance_version（契约字段为 schema_version，值须一致）
    if not isinstance(output, dict) or output.get("schema_version", output.get("relevance_version")) != RELEVANCE_VERSION:
        errors.append(f"relevance schema_version must be {RELEVANCE_VERSION}")
    else:
        if any(key in output for key in ("quality_score", "decision_score")):
            errors.append("relevance output must not receive quality or decision scores")
        rel_raw = output.get("relevance_score")
        int_raw = output.get("interest_score")
        for name, raw in (("relevance_score", rel_raw), ("interest_score", int_raw)):
            if (not isinstance(raw, (int, float)) or isinstance(raw, bool)
                    or not math.isfinite(raw)):
                errors.append(f"relevance {name} must be a number")
        matched = output.get("matched_mainlines")
        if not isinstance(matched, list) or any(not isinstance(m, str) or not m.strip() for m in matched):
            errors.append("relevance matched_mainlines must be an array of non-empty strings")
        matched_interests = output.get("matched_interests")
        if not isinstance(matched_interests, list) or any(not isinstance(m, str) or not m.strip() for m in matched_interests):
            errors.append("relevance matched_interests must be an array of non-empty strings")
        if isinstance(rel_raw, (int, float)) and not isinstance(rel_raw, bool) and rel_raw > 0 and not matched:
            errors.append("relevance relevance_score>0 requires non-empty matched_mainlines")
        if isinstance(int_raw, (int, float)) and not isinstance(int_raw, bool) and int_raw > 0 and not matched_interests:
            errors.append("relevance interest_score>0 requires non-empty matched_interests")
        if not _nonempty(output.get("rationale")):
            errors.append("relevance rationale is required")
        if output.get("confidence") not in {"high", "medium", "low"}:
            errors.append("relevance confidence is invalid")
        # 结论与 generate_relevance 一致：conclusion 缺失时回退 rationale（rationale 已必填）
        if not _nonempty(output.get("conclusion") or output.get("rationale")):
            errors.append("relevance conclusion is required")
    confidence = output.get("confidence") if isinstance(output, dict) else None
    if errors or confidence == "low":
        return None, None, "unavailable", {}, errors or ["low_relevance_confidence"]
    rel_max = Decimal(str(POLICY["relevance_bonus"]["relevance_max"]))
    int_max = Decimal(str(POLICY["relevance_bonus"]["interest_max"]))
    rel_bonus = Decimal(str(round1(max(Decimal("0"), min(Decimal(str(output["relevance_score"])), rel_max)))))
    int_bonus = Decimal(str(round1(max(Decimal("0"), min(Decimal(str(output["interest_score"])), int_max)))))
    info = {
        "relevance_score": float(rel_bonus),
        "interest_score": float(int_bonus),
        "matched_mainlines": output.get("matched_mainlines", []),
        "matched_interests": output.get("matched_interests", []),
        "rationale": output.get("rationale"),
    }
    return rel_bonus, int_bonus, confidence, info, []


def _quality_label(score_value: float) -> str:
    for band in POLICY["quality_bands"]:
        if score_value >= float(band["minimum"]):
            return band["label"]
    raise ValueError("quality policy has no catch-all band")


def _priority_label(score_value) -> str:
    if score_value is None:
        return "相关性不可用"
    for band in POLICY["priority_bands"]:
        if score_value >= float(band["minimum"]):
            return band["label"]
    raise ValueError("priority policy has no catch-all band")


def _interest_label(score_value) -> str:
    if score_value is None:
        return "兴趣不可用"
    for band in POLICY["interest_bands"]:
        if score_value >= float(band["minimum"]):
            return band["label"]
    raise ValueError("interest policy has no catch-all band")


def _depth(score_value: float):
    for band in POLICY["quality_bands"]:
        if score_value >= float(band["minimum"]):
            return list(band["ljg_range"]), bool(band["ljg_card"])
    return [0, 1], False


def score(
    quality_output,
    source_text: str,
    retry_quality_output=None,
    relevance_output=None,
    context_text=None,
    relevance_unavailable=False,
    importance_output=None,
):
    if not isinstance(source_text, str):
        return _needs_result("needs_review", content_fingerprint(""), ["source text must be a string"])
    fp = content_fingerprint(source_text)
    if not isinstance(quality_output, dict):
        return _needs_result("needs_review", fp, ["quality output must be an object"])
    if quality_output.get("schema_version") != QUALITY_VERSION:
        return _needs_result("needs_review", fp, [f"quality schema_version must be {QUALITY_VERSION}"], quality_output)
    if quality_output.get("source_status") in {"partial", "unknown"}:
        return _needs_result("needs_full_text", fp, ["source_status is not complete"], quality_output)
    if quality_output.get("source_status") != "complete":
        return _needs_result("needs_review", fp, ["source_status is invalid"], quality_output)
    recovered_invalid_attempt = False
    try:
        first = _quality_attempt(quality_output, source_text)
    except ValueError as exc:
        if retry_quality_output is None:
            return _needs_result("needs_review", fp, [str(exc), "isolated_retry_required"], quality_output)
        try:
            first = _quality_attempt(retry_quality_output, source_text)
        except ValueError as retry_exc:
            return _needs_result("needs_review", fp, [str(exc), f"retry_invalid: {retry_exc}"], quality_output)
        if first["retry_reasons"]:
            return _needs_result("needs_review", fp, [str(exc)] + first["retry_reasons"], quality_output)
        recovered_invalid_attempt = True

    quality_score = first["score"]
    effective_quality_output = first["output"]
    problem = first["problem_significance"]
    raw_category = _normalize_reading_category(effective_quality_output.get("reading_category"))
    reading_category = READING_CATEGORY_LABELS.get(raw_category) if raw_category else None
    reading_category_confidence = effective_quality_output.get("reading_category_confidence")
    if reading_category_confidence not in {"high", "medium", "low"}:
        reading_category_confidence = None
    quality_confidence = "medium" if recovered_invalid_attempt else quality_output["domain_confidence"]
    issues = ["invalid_attempt_recovered_by_isolated_retry"] if recovered_invalid_attempt else list(first["retry_reasons"])
    if first["retry_reasons"] and not recovered_invalid_attempt:
        if retry_quality_output is None:
            return _needs_result("needs_review", fp, issues + ["isolated_retry_required"], quality_output)
        try:
            second = _quality_attempt(retry_quality_output, source_text)
        except ValueError as exc:
            return _needs_result("needs_review", fp, issues + [f"retry_invalid: {exc}"], quality_output)
        delta = abs(first["score"] - second["score"])
        maximum_delta = float(POLICY["retry"]["maximum_score_delta"])
        if second["retry_reasons"] or delta > maximum_delta or _quality_band(first["score"]) != _quality_band(second["score"]):
            return _needs_result("needs_review", fp, issues + second["retry_reasons"] + [f"retry_delta={round1(delta)}"], quality_output)
        quality_score = round1((Decimal(str(first["score"])) + Decimal(str(second["score"]))) / Decimal("2"))
        problem = second["problem_significance"]
        quality_confidence = "medium"
        issues.append("isolated_retry_resolved")

    floor = float(POLICY["route"]["quality_floor"])
    threshold = float(POLICY["route"]["long_read_threshold"])
    chatgpt_threshold = float(POLICY["route"].get("chatgpt_munger_threshold", 8.5))
    relevance_needed = quality_score >= floor
    authority, importance_issues = _validate_importance_output(importance_output)
    importance_score = None
    importance_confidence = "unavailable"
    importance_dimensions = {
        "authority_score": None,
        "problem_significance_score": problem["score"],
        "evidence": [],
    }
    issues += importance_issues
    if authority is not None:
        importance_score = round1((Decimal(str(authority["authority_score"])) + Decimal(str(problem["score"]))) / Decimal("2"))
        importance_confidence = authority["confidence"]
        importance_dimensions = {
            "authority_score": authority["authority_score"],
            "problem_significance_score": problem["score"],
            "evidence": authority["evidence"],
            "authority_rationale": authority["rationale"],
            "problem_significance_rationale": problem["rationale"],
        }
    if relevance_needed and relevance_output is None and not relevance_unavailable:
        return {
            "score_version": SCORE_VERSION,
            "policy_source": POLICY_SOURCE,
            "quality_version": QUALITY_VERSION,
            "relevance_version": RELEVANCE_VERSION,
            "content_fingerprint": fp,
            "context_fingerprint": None,
            "score_status": "needs_relevance",
            "quality_score": quality_score,
            "quality_confidence": quality_confidence,
            "importance_score": importance_score,
            "importance_confidence": importance_confidence,
            "importance_dimensions": importance_dimensions,
            "relevance_score": None,
            "relevance_confidence": "unavailable",
            "decision_score": None,
            "quality_label": "待计算",
            "priority_label": "待计算",
            "interest_label": "待计算",
            "route": None,
            "ljg_range": None,
            "ljg_card": False,
            "chatgpt_munger_doc": False,
            "claims": effective_quality_output["claim_ledger"],
            "quality_dimensions": effective_quality_output["dimensions"],
            "relevance_dimensions": {},
            "conclusion": effective_quality_output.get("conclusion", ""),
            "reading_category": reading_category,
            "reading_category_confidence": reading_category_confidence,
            "questions": list(effective_quality_output.get("questions", []))[:3],
            "issues": issues,
        }

    relevance_intentionally_skipped = not relevance_needed
    if relevance_intentionally_skipped:
        rel_bonus, int_bonus, relevance_confidence, relevance_info, relevance_issues = None, None, "unavailable", {}, []
    elif relevance_unavailable:
        rel_bonus, int_bonus, relevance_confidence, relevance_info, relevance_issues = None, None, "unavailable", {}, ["relevance_context_unavailable"]
    else:
        rel_bonus, int_bonus, relevance_confidence, relevance_info, relevance_issues = _relevance_result(relevance_output, context_text)
    issues += relevance_issues
    base_priority = quality_score if importance_score is None else round1(
        Decimal("0.70") * Decimal(str(quality_score)) + IMPORTANCE_WEIGHT * Decimal(str(importance_score))
    )
    if rel_bonus is None:
        decision_score = base_priority
        relevance_score = None
        interest_score = None
        context_fp = None
    else:
        rel_eff = float(rel_bonus) if quality_score >= floor else 0.0
        int_eff = float(int_bonus) if quality_score >= floor else 0.0
        decision_score = round1(Decimal(str(base_priority)) + Decimal(str(rel_eff)) + Decimal(str(int_eff)))
        relevance_score = rel_eff
        interest_score = int_eff
        context_fp = context_fingerprint(fp, context_text)
    route = "long_read" if quality_score >= floor and decision_score >= threshold else "card"
    depth, cast_card = _depth(decision_score)
    return {
        "score_version": SCORE_VERSION,
        "policy_source": POLICY_SOURCE,
        "quality_version": QUALITY_VERSION,
        "relevance_version": RELEVANCE_VERSION,
        "content_fingerprint": fp,
        "context_fingerprint": context_fp,
        "score_status": "scored",
        "quality_score": quality_score,
        "quality_confidence": quality_confidence,
        "importance_score": importance_score,
        "importance_confidence": importance_confidence,
        "relevance_score": relevance_score,
        "interest_score": interest_score,
        "relevance_confidence": relevance_confidence,
        "decision_score": decision_score,
        "base_priority": base_priority,
        "quality_label": _quality_label(quality_score),
        "priority_label": "未计算（不影响本次路由）" if relevance_intentionally_skipped else _priority_label(relevance_score),
        "interest_label": "未计算（不影响本次路由）" if relevance_intentionally_skipped else _interest_label(interest_score),
        "route": route,
        "ljg_range": depth if route == "long_read" else None,
        "ljg_card": cast_card and route == "long_read",
        "chatgpt_munger_doc": route == "long_read" and decision_score >= chatgpt_threshold,
        "claims": effective_quality_output["claim_ledger"],
        "quality_dimensions": effective_quality_output["dimensions"],
        "importance_dimensions": importance_dimensions,
        "relevance_dimensions": relevance_info,
        "conclusion": effective_quality_output.get("conclusion", ""),
        "reading_category": reading_category,
        "reading_category_confidence": reading_category_confidence,
        "questions": list(effective_quality_output.get("questions", []))[:3],
        "issues": issues,
    }


def self_check() -> bool:
    assert round1(6.25) == 6.3
    assert content_fingerprint("a \n b") == content_fingerprint("a b")
    assert sum(Decimal(str(item["weight"])) for item in QUALITY_DIMENSIONS.values()) == Decimal("1.00")
    assert IMPORTANCE_WEIGHT == Decimal("0.30")
    assert sum(Decimal(str(item["weight"])) for item in QUALITY_DIMENSIONS.values()) + IMPORTANCE_WEIGHT == Decimal("1.30")
    assert Decimal(str(POLICY["relevance_bonus"]["max"])) > Decimal("0")
    assert Decimal(str(POLICY["relevance_bonus"]["relevance_max"])) > Decimal("0")
    assert Decimal(str(POLICY["relevance_bonus"]["interest_max"])) > Decimal("0")
    assert Decimal(str(POLICY["relevance_bonus"]["relevance_max"])) + Decimal(str(POLICY["relevance_bonus"]["interest_max"])) <= Decimal(str(POLICY["relevance_bonus"]["max"]))
    print("self-check: ok")
    return True


def _read_json(path: str):
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    return json.loads(raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("quality_output", nargs="?")
    parser.add_argument("source", nargs="?")
    parser.add_argument("--retry-quality-output")
    parser.add_argument("--importance-output")
    parser.add_argument("--relevance-output")
    parser.add_argument("--context")
    parser.add_argument("--relevance-unavailable", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--config-from-base", metavar="BASE_CONFIG_JSON",
                        help="Override tunable policy fields (route, bands, bonus) from a Base config JSON file")
    args = parser.parse_args()
    if args.self_check:
        return 0 if self_check() else 1
    if args.config_from_base:
        try:
            _reload_policy(args.config_from_base)
        except (ValueError, OSError) as exc:
            print(f"warning: base config load failed ({exc}), falling back to policy.json", file=sys.stderr)
            _reload_policy()
    if not args.quality_output or not args.source:
        parser.error("quality_output and source are required")
    if bool(args.relevance_output) != bool(args.context):
        parser.error("--relevance-output and --context must be provided together")
    if args.relevance_unavailable and (args.relevance_output or args.context):
        parser.error("--relevance-unavailable cannot be combined with relevance input")
    result = score(
        _read_json(args.quality_output),
        Path(args.source).read_text(encoding="utf-8"),
        _read_json(args.retry_quality_output) if args.retry_quality_output else None,
        _read_json(args.relevance_output) if args.relevance_output else None,
        Path(args.context).read_text(encoding="utf-8") if args.context else None,
        args.relevance_unavailable,
        _read_json(args.importance_output) if args.importance_output else None,
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
