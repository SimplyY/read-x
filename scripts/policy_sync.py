#!/usr/bin/env python3
"""读取并解析精读配置多维表格。

读飞书多维表格「精读配置」表，重建动态字段并校验。
静态字段（versions / dimension_scores / quality_dimensions / importance_weight / evidence_caps / claims / retry）
保留本地 policy 的值。评分运行时使用 Base 快照；本文件的 pull 命令仍可用于人工预览或写回。
写回靠 git 回退；--dry-run 先看 diff 再决定。

Usage:
  python3 scripts/policy_sync.py pull              # 拉取并写回
  python3 scripts/policy_sync.py pull --dry-run    # 只打印 diff，不写
"""
from __future__ import annotations

import argparse
import difflib
import json
import math
import re
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

POLICY_PATH = Path(__file__).parents[1] / ".agents/skills/content-scoring/references/scoring-policy.json"
DEFAULT_BASE_TOKEN = "ASdsbB3Gka9OKNsD7YhcJ9rZnjd"
DEFAULT_TABLE_ID = "tblGb1nXPfdKsPA1"


def _dec_default(o):
    """保留原数字格式：整数 exponent>=0 转 int，否则 float。"""
    if isinstance(o, Decimal):
        return int(o) if o.as_tuple().exponent >= 0 else float(o)
    raise TypeError


def fetch_records(base_token: str, table_id: str):
    """调 lark-cli 读全部记录，返回 (列名顺序, 行数组)。"""
    cmd = [
        "lark-cli", "base", "+record-list",
        "--base-token", base_token, "--table-id", table_id,
        "--as", "user", "--format", "json",
    ]
    resp = json.loads(subprocess.check_output(cmd, text=True))
    if not resp.get("ok"):
        raise RuntimeError(f"lark-cli record-list 失败: {resp.get('error')}")
    data = resp["data"]
    if data.get("has_more"):
        raise RuntimeError("记录分页未处理（配置表不应超过单页），请检查表行数")
    return data["fields"], data["data"]


def _grp(row_dict):
    v = row_dict["配置分组"]
    if isinstance(v, list):
        if len(v) != 1 or not isinstance(v[0], str) or not v[0].strip():
            raise ValueError(f"配置分组非法: {v!r}")
        return v[0]
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"配置分组非法: {v!r}")
    return v


def _number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)) or not math.isfinite(value):
        raise ValueError(f"{field} 必须是有限数字: {value!r}")
    return float(value)


def parse_ljg_text(text):
    """'2-3篇/卡片' -> ([2,3], True)；'1篇/无卡片' -> ([1,1], False)。"""
    m = re.match(r"(\d+)(?:-(\d+))?篇/(卡片|无卡片)$", (text or "").strip())
    if not m:
        raise ValueError(f"精读档位文本值无法解析: {text!r}")
    lo = int(m.group(1))
    hi = int(m.group(2)) if m.group(2) else lo
    return [lo, hi], m.group(3) == "卡片"


def _typed_ljg_fields(row):
    """Prefer typed Base columns, while accepting the legacy 文本值 format."""
    minimum = row.get("ljg_min")
    maximum = row.get("ljg_max")
    card = row.get("ljg_card")
    if minimum is None and maximum is None:
        return parse_ljg_text(row.get("文本值"))
    if not isinstance(card, (bool, type(None))):
        raise ValueError(f"ljg_card 必须是布尔值: {card!r}")
    minimum = _number(minimum, "ljg_min")
    maximum = _number(maximum, "ljg_max")
    if int(minimum) != minimum or int(maximum) != maximum:
        raise ValueError(f"ljg_min/max 必须是整数: {minimum!r}, {maximum!r}")
    result = [int(minimum), int(maximum)]
    typed_card = bool(card)
    legacy = row.get("文本值")
    if legacy:
        legacy_range, legacy_card = parse_ljg_text(legacy)
        if legacy_range != result or legacy_card != typed_card:
            raise ValueError(f"typed 精读字段与文本值不一致: {row.get('配置项')!r}")
    return result, typed_card


def rebuild_policy(records):
    """按配置分组重建动态字段。返回新 policy dict（静态字段来自原文件）。"""
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"), parse_float=Decimal)
    route, relevance_max = {}, None
    quality_bands, priority_bands = [], []
    for r in records:
        for field in ("配置项", "数值", "配置分组", "是否启用"):
            if field not in r:
                raise ValueError(f"配置记录缺少字段: {field}")
        if not isinstance(r["配置项"], str) or not r["配置项"].strip():
            raise ValueError(f"配置项非法: {r['配置项']!r}")
        if not isinstance(r["是否启用"], bool):
            raise ValueError(f"是否启用必须是布尔值: {r['是否启用']!r}")
        if not r.get("是否启用"):
            continue
        g = _grp(r)
        name, num, txt = r["配置项"], r["数值"], r.get("文本值")
        if g == "路由门槛":
            if name == "质量下限":
                route["quality_floor"] = _number(num, "质量下限")
            elif name == "长读门槛":
                route["long_read_threshold"] = _number(num, "长读门槛")
            elif name == "ChatGPT 芒格门槛":
                route["chatgpt_munger_threshold"] = _number(num, "ChatGPT 芒格门槛")
        elif g == "相关性加分":
            if name == "加分上限":
                relevance_max = _number(num, "加分上限")
        elif g == "质量档位":
            rng, card = _typed_ljg_fields(r)
            quality_bands.append({"minimum": _number(num, name), "label": name, "ljg_range": rng, "ljg_card": card})
        elif g == "优先级档位":
            if not isinstance(txt, str) or not txt.strip():
                raise ValueError(f"优先级档位缺少文本值: {name!r}")
            priority_bands.append({"minimum": _number(num, name), "label": txt})

    for bands in (quality_bands, priority_bands):
        bands.sort(key=lambda b: b["minimum"], reverse=True)

    _validate(route, relevance_max, quality_bands, priority_bands)
    old_bonus = policy.get("relevance_bonus", {})
    policy["route"] = route
    policy["relevance_bonus"] = {
        "max": relevance_max,
        "relevance_max": old_bonus.get("relevance_max"),
        "interest_max": old_bonus.get("interest_max"),
    }
    policy["quality_bands"] = quality_bands
    policy["priority_bands"] = priority_bands
    policy.pop("ljg_bands", None)
    return policy


def _validate(route, relevance_max, quality_bands, priority_bands):
    if relevance_max is None or relevance_max <= 0:
        raise ValueError("相关性加分上限缺失或非正")
    if "quality_floor" not in route or "long_read_threshold" not in route:
        raise ValueError("路由门槛不完整")
    route.setdefault("chatgpt_munger_threshold", 8.5)
    if route["quality_floor"] >= route["long_read_threshold"]:
        raise ValueError("质量下限须小于长读门槛")
    if any(route[name] < 0 or route[name] > 10 for name in ("quality_floor", "long_read_threshold", "chatgpt_munger_threshold")):
        raise ValueError("路由门槛不能为负")
    for bands, name in [(quality_bands, "质量"), (priority_bands, "优先级")]:
        if not bands:
            raise ValueError(f"{name}档位为空")
        if bands[-1]["minimum"] > 0:
            raise ValueError(f"{name}档位缺少兜底（minimum<=0）")
    for b in quality_bands:
        if not isinstance(b["ljg_range"], list) or len(b["ljg_range"]) != 2:
            raise ValueError(f"质量档位 ljg_range 非法: {b}")
        if not 0 <= b["ljg_range"][0] <= b["ljg_range"][1]:
            raise ValueError(f"质量档位 ljg_range 越界: {b}")
        if not isinstance(b["ljg_card"], bool):
            raise ValueError(f"质量档位 ljg_card 非布尔: {b}")


def _dump(policy) -> str:
    return json.dumps(policy, ensure_ascii=False, indent=2, default=_dec_default) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["pull"])
    ap.add_argument("--base-token", default=DEFAULT_BASE_TOKEN)
    ap.add_argument("--table-id", default=DEFAULT_TABLE_ID)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cols, rows = fetch_records(args.base_token, args.table_id)
    records = [dict(zip(cols, row)) for row in rows]
    new_policy = rebuild_policy(records)
    new_text = _dump(new_policy)
    old_text = POLICY_PATH.read_text(encoding="utf-8")
    old_norm = _dump(json.loads(old_text, parse_float=Decimal))

    if new_text == old_norm:
        print("无变化，多维表格与 scoring-policy.json 已一致。")
        return
    diff = difflib.unified_diff(
        old_norm.splitlines(keepends=True), new_text.splitlines(keepends=True),
        fromfile="scoring-policy.json (当前)", tofile="scoring-policy.json (拉取后)", n=1)
    sys.stdout.write("".join(diff))
    if args.dry_run:
        print("\n[dry-run] 未写回。")
        return
    POLICY_PATH.write_text(new_text, encoding="utf-8")
    print("\n已写回 scoring-policy.json，可用 git diff 复核、git checkout 回退。")


if __name__ == "__main__":
    main()
