#!/usr/bin/env python3
"""content-scoring 单元与集成测试。
 
覆盖 8 类用例：高质量高相关、普通但高度相关、过时信息、无证据断言、
标题党、低信息效率、正文不完整(provisional)、评分传递不变(确定性/指纹)。
 
运行：
  python3 scripts/test_content_scoring.py
"""
from __future__ import annotations
import os
import sys
 
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import content_scoring as cs
 
WEIGHTS = cs.DIMENSION_WEIGHTS
DIMS = list(WEIGHTS.keys())
 
 
def _dims(levels):
    """levels: dict dim->level，缺省 0。返回带 evidence 的 dimensions。"""
    return {k: {"level": levels.get(k, 0), "evidence": f"{k} evidence"} for k in DIMS}
 
 
def _expected_base(levels):
    total = 0.0
    for k in DIMS:
        total += (levels.get(k, 0) / 10.0) * WEIGHTS[k]
    return round(total + 1e-9, 1)
 
 
# ---- 用例 ----
 
def test_high_quality_high_relevance():
    """1. 高质量高相关：六维度全 10 + 加分顶格 -> final 10.0 稀缺精读。"""
    mo = {
        "dimensions": _dims({k: 10 for k in DIMS}),
        "context_bonus": {"personal_match": 0.5, "timing_action": 0.3, "scarcity_surprise": 0.2},
        "risk_penalty": {},
        "confidence": "high",
        "conclusion": "稀缺一手体系判断",
        "questions": ["q1", "q2", "q3"],
    }
    r = cs.score(mo, "source-A")
    assert r["base_score"] == 10.0, r["base_score"]
    assert r["context_bonus"]["total"] == 1.0 and r["context_bonus"]["capped"] is False
    assert r["final_score"] == 10.0
    assert r["decision"] == "rare_intensive_read"
    assert r["route"] == "long_read"
    assert r["ljg_range"] == [2, 3]
    assert r["ljg_card"] is True
    assert r["confidence"] == "high"
    assert r["provisional"] is False
    assert len(r["questions"]) == 3
 
 
def test_mediocre_but_highly_relevant():
    """2. 普通但高度相关：base 7.1 + 高相关加分被 cap 到 0.7，final 7.8 selective_deep_read/long_read。"""
    levels = {"long_term_value": 8, "factual_reliability": 8, "insight_depth": 6,
              "wisdom_transfer": 6, "information_efficiency": 8, "structure_expression": 6}
    mo = {
        "dimensions": _dims(levels),
        "context_bonus": {"personal_match": 0.5, "timing_action": 0.3, "scarcity_surprise": 0.2},
        "risk_penalty": {},
        "confidence": "medium",
    }
    r = cs.score(mo, "source-B")
    assert r["base_score"] == _expected_base(levels) == 7.1, r["base_score"]
    # base 7.1 落 7.0~7.9 档，加分上限 0.7；高相关原值 1.0 被 cap
    assert r["context_bonus"]["cap"] == 0.7
    assert r["context_bonus"]["total"] == 0.7 and r["context_bonus"]["capped"] is True
    assert r["final_score"] == 7.8
    assert r["decision"] == "selective_deep_read"
    assert r["route"] == "long_read"
    assert r["ljg_range"] == [0, 1]
    assert r["ljg_card"] is False
 
 
def test_outdated_information():
    """3. 过时信息：base 8.0 被 outdated 1.2 压到 6.8 -> quick_read/card。"""
    mo = {
        "dimensions": _dims({k: 8 for k in DIMS}),
        "context_bonus": {},
        "risk_penalty": {"outdated": 1.2},
        "confidence": "medium",
    }
    r = cs.score(mo, "source-C")
    assert r["base_score"] == 8.0
    assert r["risk_penalty"]["outdated"] == 1.2 and r["risk_penalty"]["total"] == 1.2
    assert r["final_score"] == 6.8
    assert r["decision"] == "quick_read"
    assert r["route"] == "card"
 
 
def test_unsupported_assertion():
    """4. 无证据断言：事实可靠低 + unsupported 扣分把 8.8 压到 7.6 selective_deep_read。"""
    levels = {"long_term_value": 10, "factual_reliability": 4, "insight_depth": 10,
              "wisdom_transfer": 10, "information_efficiency": 10, "structure_expression": 10}
    mo = {
        "dimensions": _dims(levels),
        "context_bonus": {},
        "risk_penalty": {"unsupported_assertion": 1.2},
        "confidence": "low",
    }
    r = cs.score(mo, "source-D")
    assert r["base_score"] == _expected_base(levels) == 8.8, r["base_score"]
    assert r["risk_penalty"]["unsupported_assertion"] == 1.2
    assert r["final_score"] == 7.6
    assert r["decision"] == "selective_deep_read"
    assert r["route"] == "long_read"
    assert r["ljg_range"] == [0, 1]
 
 
def test_clickbait():
    """5. 标题党：base 8.0 被 clickbait 0.8 压到 7.2 selective_deep_read。"""
    mo = {
        "dimensions": _dims({k: 8 for k in DIMS}),
        "context_bonus": {},
        "risk_penalty": {"clickbait": 0.8},
        "confidence": "medium",
    }
    r = cs.score(mo, "source-E")
    assert r["risk_penalty"]["clickbait"] == 0.8
    assert r["final_score"] == 7.2
    assert r["decision"] == "selective_deep_read"
    assert r["route"] == "long_read"
 
 
def test_low_information_efficiency():
    """6. 低信息效率：信息效率 2 拖累 base 到 5.6 -> skip/card。"""
    levels = {"long_term_value": 6, "factual_reliability": 6, "insight_depth": 6,
              "wisdom_transfer": 6, "information_efficiency": 2, "structure_expression": 6}
    mo = {
        "dimensions": _dims(levels),
        "context_bonus": {},
        "risk_penalty": {},
        "confidence": "medium",
    }
    r = cs.score(mo, "source-F")
    assert r["base_score"] == _expected_base(levels) == 5.6, r["base_score"]
    assert r["final_score"] == 5.6
    assert r["decision"] == "skip"
    assert r["route"] == "card"
    assert r["dimensions"]["information_efficiency"]["level"] == 2
 
 
def test_provisional_incomplete_text():
    """7. 正文不完整：provisional=True 必须透传，不得冒充完整评分。"""
    mo = {
        "dimensions": _dims({k: 10 for k in DIMS}),
        "context_bonus": {"personal_match": 0.5, "timing_action": 0.3, "scarcity_surprise": 0.2},
        "risk_penalty": {},
        "confidence": "low",
        "provisional": True,
        "conclusion": "仅基于摘要，需补全文",
        "questions": ["q1"],
    }
    r = cs.score(mo, "source-G")
    assert r["provisional"] is True
    # 评分照常计算，但消费方应据 provisional 谨慎处理
    assert r["final_score"] == 10.0
    assert r["decision"] == "rare_intensive_read"
 
 
def test_scoring_invariance_and_fingerprint():
    """8. 评分传递不变：同一 model_output + 同一正文 -> 完全一致的 scoring_result；
    指纹稳定、随正文变化；score_version 恒定；long-read 侧只消费结果不重评。"""
    mo = {
        "dimensions": _dims({"long_term_value": 8, "factual_reliability": 8, "insight_depth": 8,
                             "wisdom_transfer": 8, "information_efficiency": 8, "structure_expression": 8}),
        "context_bonus": {"personal_match": 0.4, "timing_action": 0.2, "scarcity_surprise": 0.1},
        "risk_penalty": {"unsupported_assertion": 0.3},
        "confidence": "high",
        "conclusion": "稳定结论",
        "questions": ["q1", "q2"],
    }
    src = "invariant-source-text"
    r1 = cs.score(mo, src)
    r2 = cs.score(mo, src)
    # 确定性：两次评分逐字段相等
    assert r1 == r2, "评分非确定性"
    # 指纹 = sha256(正文)[:16]，稳定
    fp = cs.content_fingerprint(src)
    assert r1["content_fingerprint"] == fp
    # 正文变化 -> 指纹变化
    r3 = cs.score(mo, src + "-changed")
    assert r3["content_fingerprint"] != fp
    assert r3["final_score"] == r1["final_score"]  # 分数不受指纹影响
    # score_version 恒定
    assert r1["score_version"] == cs.SCORE_VERSION == "2.0"
    # 未传正文时透传 model_output 的 content_fingerprint
    mo2 = dict(mo)
    mo2["content_fingerprint"] = "preset-fp-1234"
    r4 = cs.score(mo2)
    assert r4["content_fingerprint"] == "preset-fp-1234"
    # 防重复评分契约：fingerprint + score_version 不变即复用同一结果
    assert r1["content_fingerprint"] == r2["content_fingerprint"]
    assert r1["score_version"] == r2["score_version"]
    # long-read 侧消费：final_score 决定 ljg_range，不重新评分
    # base=8.0, bonus cap 1.0(raw 0.7)->total 0.7, penalty 0.3 -> final=8.4
    assert r1["base_score"] == 8.0
    assert r1["context_bonus"]["total"] == 0.7
    assert r1["final_score"] == round(8.0 + 0.7 - 0.3, 1) == 8.4
    assert r1["route"] == "long_read"
    assert r1["ljg_range"] == [1, 1]  # 8.0~8.4 -> 1~1
    assert r1["ljg_card"] is True
 
 
def test_robustness_null_bonus_and_level_types():
    """附加：bonus/penalty 为 null 视作空对象放行(不崩 AttributeError);
    非 dict 非空值/非数值键值抛 ValueError; level 8.0 接受, bool 拒绝。"""
    base_dims = {k: {"level": 6, "evidence": "e"} for k in DIMS}
    # null -> 等同无加分/无扣分, 不抛异常
    for field in ("context_bonus", "risk_penalty"):
        mo = {"dimensions": dict(base_dims), field: None}
        r = cs.score(mo, "s")  # 不应抛 AttributeError
        assert r["final_score"] == 6.0, r["final_score"]
    # 非 dict 非空值 -> ValueError
    for field in ("context_bonus", "risk_penalty"):
        mo = {"dimensions": dict(base_dims), field: "bad"}
        try:
            cs.score(mo, "s")
            assert False, f"{field}='bad' 应抛 ValueError"
        except ValueError:
            pass
        except AttributeError:
            assert False, f"{field}='bad' 不应抛 AttributeError"
    # 已知键非数值 -> ValueError
    mo = {"dimensions": dict(base_dims), "context_bonus": {"personal_match": "high"}}
    try:
        cs.score(mo, "s"); assert False, "非数值加分应抛 ValueError"
    except ValueError:
        pass
    # float level 8.0 接受
    mo = {"dimensions": {k: {"level": 8.0, "evidence": "e"} for k in DIMS},
          "context_bonus": {}, "risk_penalty": {}}
    r = cs.score(mo, "s")
    assert r["base_score"] == 8.0, r["base_score"]
    # bool level 拒绝
    mo = {"dimensions": {k: {"level": True, "evidence": "e"} for k in DIMS},
          "context_bonus": {}, "risk_penalty": {}}
    try:
        cs.score(mo, "s")
        assert False, "bool level 应抛 ValueError"
    except ValueError:
        pass
 
 
def test_input_validation():
    """附加：缺维度或 level 非法应抛 ValueError。"""
    try:
        cs.score({"dimensions": {"long_term_value": {"level": 6}}})
        assert False, "应抛错"
    except ValueError:
        pass
    bad = {"dimensions": {k: {"level": 6} for k in DIMS}}
    bad["dimensions"]["insight_depth"]["level"] = 11  # 越界（0~10）
    try:
        cs.score(bad)
        assert False, "应抛错"
    except ValueError:
        pass
 
 
TESTS = [
    test_high_quality_high_relevance,
    test_mediocre_but_highly_relevant,
    test_outdated_information,
    test_unsupported_assertion,
    test_clickbait,
    test_low_information_efficiency,
    test_provisional_incomplete_text,
    test_scoring_invariance_and_fingerprint,
    test_input_validation,
    test_robustness_null_bonus_and_level_types,
]
 
 
def main():
    passed, failed = 0, 0
    for t in TESTS:
        try:
            t()
            print(f"  [ok] {t.__name__}")
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"  [FAIL] {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  [ERROR] {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed, {len(TESTS)} total")
    return 0 if failed == 0 else 1
 
 
if __name__ == "__main__":
    sys.exit(main())
