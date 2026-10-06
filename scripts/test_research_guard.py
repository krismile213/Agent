#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/test_research_guard.py — research 扇出三道防线的离线单测(零 LLM 成本)

覆盖:
  1. 截断修复: [核查] 段单独预留预算, 正文再长也不会把核查挤掉
  2. 引用机器可查: 无来源标记的整块/数字要点行被标 [无依据]
  3. 存疑即隔离: 核查员标 存疑/证伪 的条目原地降级+移文末隔离区
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import agentcore as core  # noqa: E402

RESULTS = []


def check(name: str, cond: bool, detail: str = ""):
    RESULTS.append((name, bool(cond)))
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail and not cond else ""))


# ---------- 1. 引用机器可查: _mark_unsourced ----------

def test_mark_unsourced():
    print("\n== _mark_unsourced ==")
    # 整块无来源
    block = "结论: 库存充足。\n- 共 152 件在途\n- 负责人已确认"
    out = core._mark_unsourced(block)
    check("整块无来源标记 -> 块级预警", out.startswith("⚠️[无依据-整块未带来源]"))
    # 有来源标记, 但数字要点行缺来源 -> 行级标注
    block2 = ("结论: 数据已核实。\n"
              "- A 料共 152 件 (来源: inventory.csv:12)\n"
              "- B 料共 80 件\n"
              "建议: 补货 30 件。")
    out2 = core._mark_unsourced(block2)
    check("有标记块不被整块预警", not out2.startswith("⚠️["))
    check("缺来源的数字要点被行级标注",
          "- [无依据] B 料共 80 件" in out2)
    check("带来源的要点行不动", "- A 料共 152 件 (来源: inventory.csv:12)" in out2)
    check("无数字的建议行不误伤", "建议: 补货 30 件。" in out2
          and "[无依据] 建议" not in out2)
    # 中文冒号来源也认
    block3 = "结论: ok\n- 产量 3000 (来源: 报表.xlsx:5)"
    check("中文冒号来源可识别", not core._mark_unsourced(block3).startswith("⚠️["))
    # source: 英文标记也认
    block4 = "结论: ok\n- output 42 (source: run.log:3)"
    check("英文 source 标记可识别", not core._mark_unsourced(block4).startswith("⚠️["))
    # 带来源但数字行也带来源, 无行级标注
    check("原文无额外污染", "[无依据]" not in core._mark_unsourced(block3))


# ---------- 2. 核查裁决解析: _parse_verdicts ----------

def test_parse_verdicts():
    print("\n== _parse_verdicts ==")
    txt = ("#1: 通过 — 数字与原始表一致\n"
           "#2: 存疑 — 价格 3.8 未见来源表\n"
           "#3:证伪：与工具输出矛盾\n"
           "总结论: 2 条有问题。")
    vd = core._parse_verdicts(txt)
    check("三条全部解析", set(vd) == {1, 2, 3})
    check("通过判定", vd[1][0] == "通过")
    check("存疑+原因", vd[2][0] == "存疑" and "3.8" in vd[2][1])
    check("证伪(中文冒号/无破折号)", vd[3][0] == "证伪")
    check("乱文本不崩且为空", core._parse_verdicts("核查完成, 都还行。") == {})
    dup = core._parse_verdicts("#1: 通过\n#1: 存疑 — 后翻")
    check("同条目重复取最后一次", dup[1][0] == "存疑")


# ---------- 3. 存疑即隔离: _quarantine ----------

def _mk_parts():
    return [f"[子任务{i}] 任务{i}\n结论{i}: 数字 {i * 11} (来源: f{i}.csv:1)"
            for i in (1, 2, 3)]


def test_quarantine():
    print("\n== _quarantine ==")
    parts = _mk_parts()
    out = core._quarantine(parts, "#2: 存疑 — 数字无法复现")
    check("无问题条目原地不动", out[0].startswith("[子任务1]") and out[2].startswith("[子任务3]"))
    check("存疑条目原地降级为占位", "⚠️核查存疑" in out[1] and "数字 22" not in out[1])
    check("原文移到隔离区", any("子任务2 原文" in p and "数字 22" in p for p in out))
    check("隔离区有显著边界", any("隔离区" in p for p in out))
    out2 = core._quarantine(_mk_parts(), "#3: 证伪 — 与工具输出相反")
    check("证伪标记", "❌核查证伪" in out2[2])
    check("无存疑时原样返回", core._quarantine(_mk_parts(), "#1: 通过\n#2: 通过") == _mk_parts())
    check("空裁决不崩不变", core._quarantine(_mk_parts(), "") == _mk_parts())


# ---------- 4. 截断修复: _assemble_research ----------

def test_assemble():
    print("\n== _assemble_research (截断bug) ==")
    big_body = [f"[子任务{i}] " + "正" * 4200 for i in (1, 2, 3)]  # ~12.7k 字符
    vb = "[核查]\n#1: 存疑 — 数字虚高\n#2: 通过\n#3: 证伪 — 与台账相反"
    out = core._assemble_research(big_body, vb)
    check("正文超长时核查段仍在(修复点)", "#3: 证伪" in out and "[核查]" in out)
    check("核查段近乎完整保留", out.endswith(vb) or out.rstrip().endswith(vb))
    plain = core._assemble_research(big_body, None)
    check("无核查时维持 10k 上限", len(plain) <= 10_000 + 30, f"len={len(plain)}")
    long_vb = "[核查]\n" + "查" * 5_000
    out3 = core._assemble_research([f"[子任务1] {'正' * 500}"], long_vb)
    check("核查段自身封顶 3000", len(long_vb) > 3000 and len(out3) <= 12_100)
    out4 = core._assemble_research(big_body, "[核查]\n短")
    check("总预算不超 ~12k", len(out4) <= 12_100)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    test_mark_unsourced()
    test_parse_verdicts()
    test_quarantine()
    test_assemble()
    n_ok = sum(1 for _, ok in RESULTS if ok)
    print(f"\n{'=' * 46}\n[research 防线单测] {n_ok}/{len(RESULTS)} 通过")
    return 0 if n_ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
