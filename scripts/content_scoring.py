#!/usr/bin/env python3
"""content-scoring 计算引擎。

模型只输出六维度 0~10 整数等级及证据、上下文加分、风险扣分；
本脚本据此计算 base_score / context_bonus / risk_penalty / final_score，
并给出决策、路由、文字深度数量与正文指纹。

用法:
  python3 content_scoring.py <model_output.json> [<source.md>]
  cat model_output.json | python3 content_scoring.py - <source.md>
  python3 content_scoring.py --self-check
"""
from __future__ import annotations
import hashlib
import json
import sys
from pathlib import Path

SCORE_VERSION = "2.0"

# 六维度权重, 总和 10.0 = 基础分上限; v2.0 等级改 0~10, 权重等比放大 10/9 保校准
DIMENSION_WEIGHTS = {
    "long_term_value": 2.444,        # 长期价值
    "factual_reliability": 2.0,    # 事实可靠
    "insight_depth": 2.222,          # 洞察深度
    "wisdom_transfer": 1.667,        # 智慧迁移
    "information_efficiency": 1.111, # 信息效率
    "structure_expression": 0.556,   # 结构表达
}
DIMENSION_LABELS = {
    "long_term_value": "长期价值",
    "factual_reliability": "事实可靠",
    "insight_depth": "洞察深度",
    "wisdom_transfer": "智慧迁移",
    "information_efficiency": "信息效率",
    "structure_expression": "结构表达",
}
# 上下文加分单项上限
BONUS_CAPS = {"personal_match": 0.5, "timing_action": 0.3, "scarcity_surprise": 0.2}
# 加分总分上限按 base_score 分档
BONUS_TOTAL_CAP_BY_BASE = [(8.0, 1.0), (7.0, 0.7), (6.0, 0.4), (0.0, 0.2)]
# 风险扣分单项硬上限
PENALTY_CAPS = {"outdated": 1.2, "unsupported_assertion": 1.2, "clickbait": 0.8}
# 决策档位
DECISION_BANDS = [
    (9.0, "rare_intensive_read", "稀缺精读"),
    (8.0, "full_deep_read", "完整深读"),
    (7.0, "selective_deep_read", "选择性深读"),
    (6.0, "quick_read", "快速阅读"),
    (0.0, "skip", "跳过"),
]


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _round1(x):
    return round(x + 1e-9, 1)


def content_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def compute_base_score(dimensions):
    """base_score = Σ(等级/10 × 权重), 上限 10.0。等级 0~10。"""
    total = 0.0
    for key, weight in DIMENSION_WEIGHTS.items():
        level = int(_clamp(dimensions.get(key, {}).get("level", 0), 0, 10))
        total += (level / 10.0) * weight
    return _round1(_clamp(total, 0.0, 10.0))


def bonus_total_cap(base_score):
    for threshold, cap in BONUS_TOTAL_CAP_BY_BASE:
        if base_score >= threshold:
            return cap
    return 0.2


def compute_bonus(base_score, bonus_in):
    """加分: 各项 clamp 到单项上限, 总和 clamp 到 base 分档上限。"""
    bonus_in = bonus_in or {}
    items, raw_total = {}, 0.0
    for key, cap in BONUS_CAPS.items():
        v = _clamp(float(bonus_in.get(key, 0.0) or 0.0), 0.0, cap)
        items[key] = _round1(v)
        raw_total += v
    cap = bonus_total_cap(base_score)
    return {
        "personal_match": items["personal_match"],
        "timing_action": items["timing_action"],
        "scarcity_surprise": items["scarcity_surprise"],
        "total": _round1(_clamp(raw_total, 0.0, cap)),
        "cap": _round1(cap),
        "capped": raw_total > cap,
    }


def compute_penalty(penalty_in):
    penalty_in = penalty_in or {}
    items, total = {}, 0.0
    for key, cap in PENALTY_CAPS.items():
        v = _clamp(float(penalty_in.get(key, 0.0) or 0.0), 0.0, cap)
        items[key] = _round1(v)
        total += v
    return {
        "outdated": items["outdated"],
        "unsupported_assertion": items["unsupported_assertion"],
        "clickbait": items["clickbait"],
        "total": _round1(total),
    }


def decide(final_score):
    for threshold, key, label in DECISION_BANDS:
        if final_score >= threshold:
            return key, label
    return "skip", "跳过"


def route_for(decision_key):
    return "card" if decision_key in ("skip", "quick_read") else "long_read"


def ljg_range(final_score):
    """文字 ljg 数量区间, 对齐 long-read 既有契约。"""
    if final_score >= 9.0:
        return [2, 3]
    if final_score >= 8.5:
        return [1, 2]
    if final_score >= 8.0:
        return [1, 1]
    return [0, 1]


def _is_int_level(v):
    """接受 int(非 bool) 或整数值 float; 拒绝 bool/字符串/None。等级 0~10。"""
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return 0 <= v <= 10
    if isinstance(v, float) and v.is_integer():
        return 0 <= v <= 10
    return False


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_input(model_output):
    findings = []
    if not isinstance(model_output, dict):
        return ["model_output is not an object"]
    dims = model_output.get("dimensions")
    if not isinstance(dims, dict):
        return ["dimensions missing or not an object"]
    for k in DIMENSION_WEIGHTS:
        if k not in dims:
            findings.append(f"dimensions missing: {k}")
            continue
        lvl = dims[k].get("level")
        if not _is_int_level(lvl):
            findings.append(f"dimensions.{k}.level must be int 0..10 (8.0 可, bool 不可)")
    # context_bonus / risk_penalty: null 视作空对象放行; 非 dict 非空值才报错
    cb = model_output.get("context_bonus", {})
    if cb is not None and not isinstance(cb, dict):
        findings.append("context_bonus must be an object")
    elif isinstance(cb, dict):
        for kk in BONUS_CAPS:
            if kk in cb and not _is_number(cb[kk]):
                findings.append(f"context_bonus.{kk} must be a number")
    rp = model_output.get("risk_penalty", {})
    if rp is not None and not isinstance(rp, dict):
        findings.append("risk_penalty must be an object")
    elif isinstance(rp, dict):
        for kk in PENALTY_CAPS:
            if kk in rp and not _is_number(rp[kk]):
                findings.append(f"risk_penalty.{kk} must be a number")
    return findings


def score(model_output, source_text=None):
    """输入模型输出 dict, 返回 scoring_result dict。"""
    findings = validate_input(model_output)
    if findings:
        raise ValueError("invalid model_output: " + "; ".join(findings))
    dimensions = model_output.get("dimensions", {})
    base = compute_base_score(dimensions)
    bonus = compute_bonus(base, model_output.get("context_bonus", {}))
    penalty = compute_penalty(model_output.get("risk_penalty", {}))
    final = _round1(_clamp(base + bonus["total"] - penalty["total"], 0.0, 10.0))
    decision_key, decision_label = decide(final)
    route = route_for(decision_key)
    fp = content_fingerprint(source_text) if source_text else model_output.get("content_fingerprint")
    return {
        "score_version": SCORE_VERSION,
        "content_fingerprint": fp,
        "provisional": bool(model_output.get("provisional", False)),
        "detected_domain": model_output.get("detected_domain"),
        "base_score": base,
        "context_bonus": bonus,
        "risk_penalty": penalty,
        "final_score": final,
        "dimensions": {
            k: {
                "level": int(_clamp(dimensions.get(k, {}).get("level", 0), 0, 10)),
                "label": DIMENSION_LABELS[k],
                "evidence": dimensions.get(k, {}).get("evidence", ""),
            }
            for k in DIMENSION_WEIGHTS
        },
        "confidence": model_output.get("confidence", "medium"),
        "decision": decision_key,
        "decision_label": decision_label,
        "route": route,
        "ljg_range": ljg_range(final) if route == "long_read" else None,
        "ljg_card": final >= 8.0 and route == "long_read",
        "conclusion": model_output.get("conclusion", ""),
        "questions": list(model_output.get("questions", []))[:3],
    }


def self_check():
    cases = []
    hi = {
        "dimensions": {k: {"level": 10, "evidence": "e"} for k in DIMENSION_WEIGHTS},
        "context_bonus": {"personal_match": 0.5, "timing_action": 0.3, "scarcity_surprise": 0.2},
        "risk_penalty": {}, "confidence": "high",
        "conclusion": "c", "questions": ["q1", "q2", "q3"],
    }
    r = score(hi, "src")
    cases.append(("high quality: base=10.0 bonus capped 1.0 final=10.0 rare+long_read",
                  r["base_score"] == 10.0 and r["context_bonus"]["total"] == 1.0
                  and r["final_score"] == 10.0 and r["decision"] == "rare_intensive_read"
                  and r["route"] == "long_read" and r["ljg_range"] == [2, 3]
                  and r["ljg_card"] is True, r))
    mid = {
        "dimensions": {k: {"level": 6, "evidence": "e"} for k in DIMENSION_WEIGHTS},
        "context_bonus": {"personal_match": 0.5, "timing_action": 0.3, "scarcity_surprise": 0.2},
        "risk_penalty": {},
    }
    r = score(mid, "src")
    # base=6/10*10=6.0; base>=6 -> bonus cap 0.4; final=6.4 -> quick_read/card
    cases.append(("mediocre base>=6 caps bonus to 0.4 -> quick_read/card",
                  r["base_score"] == 6.0 and r["context_bonus"]["total"] == 0.4
                  and r["context_bonus"]["capped"] is True
                  and r["final_score"] == 6.4 and r["decision"] == "quick_read"
                  and r["route"] == "card" and r["ljg_range"] is None, r))
    # 无证据断言: 高分被扣分压到 long_read 以下
    pen = {
        "dimensions": {k: {"level": 8, "evidence": "e"} for k in DIMENSION_WEIGHTS},
        "context_bonus": {}, "risk_penalty": {"unsupported_assertion": 1.2},
    }
    r = score(pen, "src")
    # base=8/10*10=8.0; penalty=1.2; final=6.8 -> quick_read/card
    cases.append(("unsupported assertion penalty drops to quick_read",
                  r["base_score"] == 8.0 and r["risk_penalty"]["total"] == 1.2
                  and r["final_score"] == 6.8 and r["decision"] == "quick_read"
                  and r["route"] == "card", r))
    # 输入校验: 缺维度
    bad = {"dimensions": {"long_term_value": {"level": 3}}}
    try:
        score(bad)
        cases.append(("missing dimension raises", False, "no raise"))
    except ValueError:
        cases.append(("missing dimension raises", True, "raised"))
    ok = True
    for name, expect, info in cases:
        status = "ok" if expect else "FAIL"
        if not expect:
            ok = False
        print(f"  [{status}] {name}")
    print("self-check:", "ok" if ok else "FAIL")
    return ok


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(2)
    if args[0] == "--self-check":
        sys.exit(0 if self_check() else 1)
    model_path = args[0]
    source_text = None
    if len(args) >= 2:
        source_text = Path(args[1]).read_text(encoding="utf-8")
    raw = sys.stdin.read() if model_path == "-" else Path(model_path).read_text(encoding="utf-8")
    model_output = json.loads(raw)
    result = score(model_output, source_text)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
