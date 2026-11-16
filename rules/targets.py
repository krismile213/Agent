# -*- coding: utf-8 -*-
"""环节（Part）确认目标天数 —— 单一真源与三级来源解析。

为什么要有这个模块
------------------
同一条「环节目标」原先散在三处：数据后台 `rule_config.seg_targets`（可编辑真源）、
`流程复盘/时效确认规则表.xlsx`（Agent 一直在读）、`plugins/workmain.py` 里的裸常量。
2026-11-16 探针（`scripts/probe_rule_drift.py`）实测：Part1 三处给出 75 / 13 / 75，
差 5.8 倍，且**群里数字看不出问题**（当时在途单全部 >120 天，落在遗产区）——
这类缺陷靠人工对表永远发现不了，只能靠单一真源 + 对拍测试防住。

Part 级目标怎么来的
-------------------
Part 是把若干「段」打包后的展示口径，Part 级目标 = 该 Part 内**串行段目标之和**。
并行段不求和：`素材加工→上架` 与 `素材加工→广告` 并行（都记 Part7），
`签样→采购` 与 `取数→签样` 并行（分别落 Part2 / Part1）。

2026-11-16 口径裁决（用户拍板，别再自己改回去）
  · Part1 按 13 收紧 = 提案→取数(1) + 取数→签样(12)；原 xlsx 的 75 是 2026-10-19 之前的旧值。
  · Part8（渠道仓到货检查 15 / 海运 35）**暂不入口径源**，留在本模块常量里。
  · 手机壳 Part1 不折算（壳无「取数」节点，87 天横跨 Part1/Part2），维持 xlsx 值待业务校准。

⚠️ 不含人名：后端 seg_rules.SEGS 带 6 个责任人，Agent 是上云最后一跳，
   这里只取「段名 + 目标天数」，`test_rules_targets.py` 有断言把这个约束钉住。
"""

from __future__ import annotations

import json
from pathlib import Path

#: 默认资产路径（与 work-main 主仓库同层；调用方可覆盖，测试用临时文件覆盖）
DEFAULT_SNAPSHOT = Path(r"C:\Users\dell\Desktop\work-main\资产文件\规则快照.json")
DEFAULT_RULE_XLSX = Path(r"C:\Users\dell\Desktop\work-main\流程复盘\时效确认规则表.xlsx")

#: Part → 组成它的串行段（依据：数据后台 seg_rules.SEGS 的起终点）
PART_SEGS = {
    "手机膜": {
        "Part1": ("提案→取数", "取数→签样"),
        "Part2": ("签样→采购",),
        "Part3": ("采购→本地仓到货",),
        "Part4": ("采购→首次上架",),
        "Part5": ("采购→发货末批",),
        "Part6": ("寄送→拍摄交付", "拍摄交付→素材加工"),
        "Part7": ("素材加工→上架",),
    },
    # 壳无「取数」节点：提案→签样 87 天横跨 Part1/Part2，Part 级折算无意义 → Part1 不折算。
    "手机壳": {
        "Part2": ("签样→采购",),
        "Part3": ("采购→本地仓到货",),
        "Part4": ("采购→首次上架",),
        "Part5": ("采购→发货末批",),
        "Part6": ("寄送→拍摄交付", "拍摄交付→素材加工"),
        "Part7": ("素材加工→上架",),
    },
}

#: Part8(渠道仓到货检查) 目标：按运输方式分。不在快照里（2026-11-16 决定暂不入口径源）。
PART8_TARGET = {"air": 15, "sea": 35}

#: Part9(新单审批) 目标：只看创建≤7天的单，目标 1 天。
PART9_TARGET = 1

#: 最后兜底（连 xlsx 和快照都读不到时用），等于 2026-11-16 的膜口径。
#: ⚠️ 这里写 13 而不是 75：兜底值必须与真源同值，否则"文件一丢反而更稳定地错"。
PART_TARGET_FALLBACK = {
    "Part1": 13, "Part2": 1, "Part3": 8, "Part4": 7,
    "Part5": 22, "Part6": 13, "Part7": 7,
}

#: 手机壳 Part1：待业务重校准，先别动（87 天的推导前提已失效，但不在本次范围）
SHELL_PART1_TARGET = 75

SOURCE_LABEL = {"snapshot": "规则快照", "xlsx": "时效确认规则表", "default": "内置常量"}


def load_snapshot(path=None):
    """读后端导出的规则快照 → (seg_targets dict, 错误说明)。只读，缺失不抛异常。"""
    p = Path(path or DEFAULT_SNAPSHOT)
    try:
        doc = json.loads(p.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None, f"快照不存在: {p}"
    except Exception as e:  # 坏 JSON / 权限 / 编码
        return None, f"快照不可读: {type(e).__name__}: {e}"
    segs = (doc.get("rules") or {}).get("seg_targets") or {}
    if not isinstance(segs, dict) or not segs:
        return None, "快照里没有 seg_targets"
    return segs, ""


def _seg_value(segs, seg):
    """段目标 → int；缺失/不可用/非数字一律返回 None（None ≠ 0）。"""
    item = segs.get(seg)
    if not isinstance(item, dict):
        return None
    if item.get("available") is False:
        return None
    try:
        return int(item.get("target"))
    except (TypeError, ValueError):
        return None


def part_targets_from_segments(seg_targets, line="手机膜"):
    """把段目标折成 Part 级 → (targets dict, 缺失段清单)。

    缺任一子段就**不产出**该 Part（宁可回退到下一级来源，也不拿半截加总充数）。
    """
    mapping = PART_SEGS.get(line, {})
    out, missing = {}, []
    for part, segs in mapping.items():
        vals = [_seg_value(seg_targets, s) for s in segs]
        if any(v is None for v in vals):
            missing.append({"part": part,
                            "segs": [s for s, v in zip(segs, vals) if v is None]})
            continue
        out[part] = sum(vals)
    return out, missing


def _read_rule_xlsx(path, dev_type="常规开发"):
    """读时效确认规则表的某一开发类型 → {Part: 天}；读不到返回 {}。"""
    p = Path(path or DEFAULT_RULE_XLSX)
    if not p.exists():
        return {}
    try:
        import pandas as pd
        df = pd.read_excel(p, dtype=str)
    except Exception:
        return {}
    got = {}
    for _, r in df.iterrows():
        if str(r.get("开发类型", "")).strip() != dev_type:
            continue
        part = str(r.get("Part", "")).strip()
        if not part.startswith("Part"):
            continue
        try:
            got[part.split()[0]] = int(float(r.get("确认目标(天)")))
        except (TypeError, ValueError):
            continue
    return got


def resolve_targets(snapshot_path=None, rule_xlsx=None, line="手机膜"):
    """解析 Part→目标天数 → (targets dict, sources dict)。

    优先级：规则快照（可编辑真源） > 时效确认规则表.xlsx（离线兜底） > 内置常量。
    逐 Part 记录来源，便于在工具输出里如实标注「这个 13 是从哪儿来的」。
    """
    targets = dict(PART_TARGET_FALLBACK)
    targets["Part8"] = PART8_TARGET["air"]
    targets["Part9"] = PART9_TARGET
    sources = {k: "default" for k in targets}
    if line == "手机壳":
        targets["Part1"] = SHELL_PART1_TARGET   # 壳不折算，见模块 docstring
        sources["Part1"] = "default"

    xl = _read_rule_xlsx(rule_xlsx)
    for part, val in xl.items():
        if part in targets:
            targets[part] = val
            sources[part] = "xlsx"

    segs, _err = load_snapshot(snapshot_path)
    if segs:
        derived, _missing = part_targets_from_segments(segs, line)
        for part, val in derived.items():
            targets[part] = val          # 快照优先级最高
            sources[part] = "snapshot"

    # Part8/Part9 不在快照（2026-11-16 决定），来源如实标回 default
    targets["Part8"] = PART8_TARGET["air"]
    targets["Part9"] = PART9_TARGET
    sources["Part8"] = sources["Part9"] = "default"
    return targets, sources


def describe_sources(sources):
    """{'Part1':'snapshot',...} → 'Part1~7=规则快照, Part8/9=内置常量' 一句话。"""
    groups = {}
    for part, src in sorted(sources.items(), key=lambda kv: kv[0]):
        groups.setdefault(src, []).append(part[4:])
    bits = []
    for src, parts in groups.items():
        nums = ",".join(parts)
        bits.append(f"Part{nums}={SOURCE_LABEL.get(src, src)}")
    return " · ".join(bits)
