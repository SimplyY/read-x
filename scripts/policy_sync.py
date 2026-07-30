#!/usr/bin/env python3
"""精读配置多维表格 -> scoring-policy.json 单向同步。

读飞书多维表格「精读配置」表，重建动态字段，校验后写回 scoring-policy.json。
静态字段（versions / dimension_scores / quality_dimensions / evidence_caps / claims / retry）
保留不动。policy.json 是运行时唯一真值；多维表格是飞鱼手机编辑面板，评分热路径不碰飞书。
写回靠 git 回退；--dry-run 先看 diff 再决定。

Usage:
  python3 scripts/policy_sync.py pull              # 拉取并写回
  python3 scripts/policy_sync.py pull --dry-run    # 只打印 diff，不写
"""
from __future__ import annotations

import argparse
import difflib
import json
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
    return v[0] if isinstance(v, list) else v


def parse_ljg_text(text):
    """'2-3篇/卡片' -> ([2,3], True)；'1篇/无卡片' -> ([1,1], False)。"""
    m = re.match(r"(\d+)(?:-(\d+))?篇/(卡片|无卡片)$", (text or "").strip())
    if not m:
        raise ValueError(f"精读档位文本值无法解析: {text!r}")
    lo = int(m.group(1))
    hi = int(m.group(2)) if m.group(2) else lo
    return [lo, hi], m.group(3) == "卡片"


def rebuild_policy(records):
    """按配置分组重建动态字段。返回新 policy dict（静态字段来自原文件）。"""
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"), parse_float=Decimal)
    route, relevance_max = {}, None
    quality_bands, priority_bands = [], []
    for r in records:
        if not r.get("是否启用"):
            continue
        g = _grp(r)
        name, num, txt = r["配置项"], r["数值"], r.get("文本值")
        if g == "路由门槛":
            if name == "质量下限":
                route["quality_floor"] = float(num)
            elif name == "长读门槛":
                route["long_read_threshold"] = float(num)
        elif g == "相关性加分":
            if name == "加分上限":
                relevance_max = float(num)
        elif g == "质量档位":
            rng, card = parse_ljg_text(txt)
            quality_bands.append({"minimum": float(num), "label": name, "ljg_range": rng, "ljg_card": card})
        elif g == "优先级档位":
            priority_bands.append({"minimum": float(num), "label": txt})

    for bands in (quality_bands, priority_bands):
        bands.sort(key=lambda b: b["minimum"], reverse=True)

    _validate(route, relevance_max, quality_bands, priority_bands)
    policy["route"] = route
    policy["relevance_bonus"] = {"max": relevance_max}
    policy["quality_bands"] = quality_bands
    policy["priority_bands"] = priority_bands
    policy.pop("ljg_bands", None)
    return policy


def _validate(route, relevance_max, quality_bands, priority_bands):
    assert relevance_max is not None and relevance_max > 0, "相关性加分上限缺失或非正"
    assert "quality_floor" in route and "long_read_threshold" in route, "路由门槛不完整"
    assert route["quality_floor"] < route["long_read_threshold"], "质量下限须小于长读门槛"
    for bands, name in [(quality_bands, "质量"), (priority_bands, "优先级")]:
        assert bands, f"{name}档位为空"
        assert bands[-1]["minimum"] <= 0, f"{name}档位缺少兜底（minimum<=0）"
    for b in quality_bands:
        assert isinstance(b["ljg_range"], list) and len(b["ljg_range"]) == 2, f"质量档位 ljg_range 非法: {b}"
        assert 0 <= b["ljg_range"][0] <= b["ljg_range"][1], f"质量档位 ljg_range 越界: {b}"
        assert isinstance(b["ljg_card"], bool), f"质量档位 ljg_card 非布尔: {b}"


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
