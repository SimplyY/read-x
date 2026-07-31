#!/usr/bin/env python3
"""Deterministic quality levels from source-linked facts."""
from __future__ import annotations


FACT_KINDS = {
    "evidence_quality": {
        "support_chain", "independent_source", "distinct_method", "process_result",
        "locatable_event", "alternative", "scope_boundary", "replication",
        "symptom_or_diagnosis", "illustrative_only",
    },
    "insight_explanatory": {
        "standard_mechanism", "nonobvious_judgment", "causal_edge", "concept_redefinition",
        "diagnosis_change", "action_change", "feedback_cycle", "second_order",
        "prediction", "validated_prediction",
    },
    "transfer_durability": {
        "criterion", "diagnosis_method", "action_method", "component",
        "cross_context_application", "component_dependency", "scope_boundary",
        "application_result", "generated_judgment",
    },
    "information_efficiency": {
        "advancing", "repetition_group", "nonargument_range", "navigation",
        "compressible_group", "near_incompressible",
    },
}

CLAIM_TYPE_BY_KIND = {
    "support_chain": "empirical", "independent_source": "empirical",
    "distinct_method": "empirical", "process_result": "experiential",
    "locatable_event": "empirical", "alternative": "causal", "scope_boundary": "normative",
    "replication": "empirical", "symptom_or_diagnosis": "experiential",
    "illustrative_only": "experiential", "standard_mechanism": "causal",
    "nonobvious_judgment": "normative", "causal_edge": "causal",
    "concept_redefinition": "normative", "diagnosis_change": "method",
    "action_change": "method", "feedback_cycle": "causal", "second_order": "causal",
    "prediction": "causal", "validated_prediction": "empirical", "criterion": "method",
    "diagnosis_method": "method", "action_method": "method", "component": "method",
    "cross_context_application": "method", "component_dependency": "causal",
    "application_result": "empirical", "generated_judgment": "method",
}


def validate_facts(dimension: str, facts, unit_count: int) -> None:
    if not isinstance(facts, list) or not facts:
        raise RuntimeError(f"{dimension} facts must be a non-empty array")
    allowed = FACT_KINDS[dimension]
    for fact in facts:
        if not isinstance(fact, dict) or fact.get("kind") not in allowed:
            raise RuntimeError(f"{dimension} fact kind is invalid")
        ids = fact.get("unit_ids")
        if not isinstance(ids, list) or not ids or len(ids) != len(set(ids)):
            raise RuntimeError(f"{dimension} fact unit_ids must be unique and non-empty")
        if any(not isinstance(value, int) or not 1 <= value <= unit_count for value in ids):
            raise RuntimeError(f"{dimension} fact unit_id is invalid")
        if fact.get("role") not in {"decisive", "supporting"}:
            raise RuntimeError(f"{dimension} fact role is invalid")


def _facts_by_kind(facts: list[dict], decisive_only=False) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for fact in facts:
        if decisive_only and fact["role"] != "decisive":
            continue
        result.setdefault(fact["kind"], []).append(fact)
    return result


def _unit_total(grouped: dict[str, list[dict]], kind: str) -> int:
    return len({unit_id for fact in grouped.get(kind, []) for unit_id in fact["unit_ids"]})


def _evidence(facts: list[dict]):
    all_facts = _facts_by_kind(facts)
    decisive = _facts_by_kind(facts, True)
    independent = _unit_total(decisive, "independent_source")
    methods = _unit_total(decisive, "distinct_method")
    results = _unit_total(decisive, "process_result")
    events = _unit_total(decisive, "locatable_event")
    chain = _unit_total(decisive, "support_chain")
    if "illustrative_only" in all_facts and not (results or independent or events):
        return 6.0, ["only_illustrative_or_anecdotal"], "只有说明性故事或类比", "缺实质证据链"
    if "replication" in decisive and independent >= 3 and "scope_boundary" in decisive:
        level = 10.0
    elif "replication" in decisive and independent >= 2:
        level = 9.5
    elif independent >= 2 and "alternative" in decisive and "scope_boundary" in decisive:
        level = 9.0
    elif independent >= 2 and (methods >= 2 or results >= 1):
        level = 8.5
    elif events >= 3 or methods >= 3 or independent + methods >= 3:
        level = 8.0
    elif results >= 2 or chain >= 3:
        level = 7.5
    elif results >= 1 or chain >= 2 or events >= 2:
        level = 7.0
    elif chain or events or independent or methods:
        level = 6.5
    else:
        level = 6.0
    if "symptom_or_diagnosis" in all_facts and results == 0:
        level = min(level, 6.5)
    rationale = f"独立来源{independent}、不同方法{methods}、过程结果{results}、可定位事件{events}"
    ceiling = "下一档需要更多独立来源、结果或反证边界"
    return level, [], rationale, ceiling


def _insight(facts: list[dict]):
    decisive = _facts_by_kind(facts, True)
    causal = _unit_total(decisive, "causal_edge")
    feedback = _unit_total(decisive, "feedback_cycle")
    second = "second_order" in decisive
    prediction = "prediction" in decisive
    action = "action_change" in decisive
    diagnosis = "diagnosis_change" in decisive
    concept = "concept_redefinition" in decisive
    if _unit_total(decisive, "validated_prediction") >= 2:
        level = 10.0
    elif "validated_prediction" in decisive:
        level = 9.5
    elif feedback and causal >= 2 and action and (prediction or second):
        level = 9.0
    elif feedback or second or prediction:
        level = 8.5
    elif causal >= 2 and (action or diagnosis):
        level = 8.0
    elif causal >= 2 or (concept and (action or diagnosis)):
        level = 7.5
    elif causal and "nonobvious_judgment" in decisive:
        level = 7.0
    elif causal or concept or "nonobvious_judgment" in decisive:
        level = 6.5
    else:
        level = 6.0
    rationale = f"因果边{causal}、反馈{feedback}、诊断改变{int(diagnosis)}、行动改变{int(action)}"
    ceiling = "下一档需要更完整机制、反馈、二阶影响或预测"
    return level, [], rationale, ceiling


def _transfer(facts: list[dict]):
    decisive = _facts_by_kind(facts, True)
    criteria = _unit_total(decisive, "criterion")
    components = _unit_total(decisive, "component")
    contexts = _unit_total(decisive, "cross_context_application")
    results = _unit_total(decisive, "application_result")
    diagnosis = "diagnosis_method" in decisive
    action = "action_method" in decisive
    dependency = "component_dependency" in decisive
    boundary = "scope_boundary" in decisive
    generated = "generated_judgment" in decisive
    if results >= 3 and contexts >= 3 and generated:
        level = 10.0
    elif results >= 2 and contexts >= 2 and generated:
        level = 9.5
    elif components >= 2 and dependency and boundary and contexts >= 2 and generated:
        level = 9.0
    elif (components >= 2 and dependency and results >= 1) or contexts >= 2:
        level = 8.5
    elif (diagnosis and action) or results >= 2:
        level = 8.0
    elif (diagnosis or action) and (contexts or results):
        level = 7.5
    elif diagnosis or action or criteria >= 3:
        level = 7.0
    elif criteria or components:
        level = 6.5
    else:
        level = 6.0
    rationale = f"稳定判据{criteria}、组件{components}、跨情境{contexts}、应用结果{results}"
    ceiling = "下一档需要诊断行动闭合、组件依赖或跨情境结果"
    return level, [], rationale, ceiling


def _efficiency(facts: list[dict]):
    grouped = _facts_by_kind(facts)
    advancing = _unit_total(grouped, "advancing")
    repetitions = len(grouped.get("repetition_group", []))
    compressible = len(grouped.get("compressible_group", []))
    navigation = "navigation" in grouped
    if "nonargument_range" in grouped:
        return 6.0, ["substantial_nonargument_section"], "存在完整非论证板块", "移除非论证板块后再评密度"
    if "near_incompressible" in grouped and navigation and not repetitions:
        level = 9.0
    elif navigation and advancing >= 4 and not repetitions and not compressible:
        level = 8.5
    elif navigation and advancing >= 3 and repetitions == 0 and compressible <= 1:
        level = 8.0
    elif advancing >= 2 and repetitions <= 1 and compressible <= 1:
        level = 7.5
    elif advancing >= 2 and repetitions + compressible <= 3:
        level = 7.0
    elif advancing:
        level = 6.5
    else:
        level = 6.0
    rationale = f"推进单元{advancing}、重复组{repetitions}、可压缩组{compressible}、导航{int(navigation)}"
    ceiling = "下一档需要更高推进覆盖与更少重复压缩"
    return level, [], rationale, ceiling


SCORERS = {
    "evidence_quality": _evidence,
    "insight_explanatory": _insight,
    "transfer_durability": _transfer,
    "information_efficiency": _efficiency,
}


def score_dimension(dimension: str, facts: list[dict]) -> dict:
    level, disqualifiers, rationale, ceiling = SCORERS[dimension](facts)
    return {
        "level": level, "disqualifiers": disqualifiers,
        "rationale": rationale, "ceiling_reason": ceiling,
    }
