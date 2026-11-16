#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/test_rules_targets.py — 环节目标天数单一真源的单测(零 LLM、零网络)

背景(2026-11-16): 同一条「环节确认目标」散在三处 —— 后端规则快照 / 时效确认规则表.xlsx /
workmain 裸常量。探针实测 Part1 三处给出 13 / 75 / 75，差 5.8 倍，而当天在途单全落在
>120 天遗产区，群里数字看不出异常 —— 属于「靠人工对表永远发现不了」的潜伏缺陷。

本测钉住四件事:
  1. Part1 = 13（提案→取数 1 + 取数→签样 12），且**严格大于** xlsx 旧值方向为收紧。
  2. Part2~Part7 与 xlsx 逐项相等（差 0），改口径不许顺带漂移别的环节。
  3. None ≠ 0：缺子段/不可用段不参与求和，缺任一子段该 Part 整体不产出（回退下一级）。
  4. rules/ 不含人名（Agent 是上云最后一跳），且 xlsx/快照都读不到时兜底值是 13 而非 75。
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

RESULTS = []
RULE_XLSX = Path(r"C:\Users\dell\Desktop\work-main\流程复盘\时效确认规则表.xlsx")
SNAPSHOT = Path(r"C:\Users\dell\Desktop\work-main\资产文件\规则快照.json")

#: 后端 seg_rules.SEGS 里出现过的责任人姓名（用于「不含人名」断言）
KNOWN_NAMES = ("李杰华", "周奕奕", "梁子锋", "林惠欢", "夏康富", "李晓影", "杨晓莹")


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail and not cond else ""))


def test_part1_tightened():
    print("\n== Part1 收紧到 13 ==")
    from rules import resolve_targets
    targets, sources = resolve_targets(SNAPSHOT, RULE_XLSX, line="手机膜")
    check("Part1 = 13", targets.get("Part1") == 13, f"实际 {targets.get('Part1')}")
    check("Part1 来源=规则快照", sources.get("Part1") == "snapshot", str(sources))
    check("Part1 不再是 75", targets.get("Part1") != 75)


def test_others_zero_drift():
    print("\n== Part2~Part7 与 xlsx 差 0 ==")
    from rules import resolve_targets
    from rules.targets import _read_rule_xlsx
    xl = _read_rule_xlsx(RULE_XLSX)
    if not xl:
        print("  (跳过: 规则表不可读)")
        return
    targets, _ = resolve_targets(SNAPSHOT, RULE_XLSX, line="手机膜")
    for part in [f"Part{i}" for i in range(2, 8)]:
        check(f"{part} 一致", targets.get(part) == xl.get(part),
              f"快照侧 {targets.get(part)} / xlsx {xl.get(part)}")


def test_none_is_not_zero():
    print("\n== None ≠ 0（缺失段不充数）==")
    from rules import part_targets_from_segments, resolve_targets
    segs = {"提案→取数": {"target": 1, "available": True},
            "取数→签样": {"target": 12, "available": True}}
    got, missing = part_targets_from_segments(segs, "手机膜")
    check("两段齐全 → Part1=13", got.get("Part1") == 13, str(got))
    check("其余 Part 不产出", set(got) == {"Part1"}, str(got))
    check("缺失段被列出", {m["part"] for m in missing} >= {"Part2", "Part6"}, str(missing))

    partial = {"提案→取数": {"target": 1, "available": True}}
    got2, _ = part_targets_from_segments(partial, "手机膜")
    check("只有一段时不产出 Part1(不拿 1 充数)", "Part1" not in got2, str(got2))

    na = {"提案→取数": {"target": 1, "available": True},
          "取数→签样": {"target": 12, "available": False}}
    got3, _ = part_targets_from_segments(na, "手机膜")
    check("available=False 视为缺失", "Part1" not in got3, str(got3))


def test_fallback_is_13_not_75():
    print("\n== 无源兜底 = 13（不是 75）==")
    from rules import resolve_targets
    targets, sources = resolve_targets(Path("__missing__.json"), Path("__missing__.xlsx"),
                                       line="手机膜")
    check("兜底 Part1 = 13", targets.get("Part1") == 13, str(targets.get("Part1")))
    check("兜底来源标注为内置常量", sources.get("Part1") == "default", str(sources))
    check("Part8/9 有值", targets.get("Part8") == 15 and targets.get("Part9") == 1, str(targets))


def test_shell_untouched():
    print("\n== 手机壳 Part1 不折算（先别动）==")
    from rules import resolve_targets
    targets, _ = resolve_targets(SNAPSHOT, RULE_XLSX, line="手机壳")
    check("壳 Part1 不是折算值 13", targets.get("Part1") != 13, str(targets.get("Part1")))
    check("壳 Part2~7 仍按段折算", all(targets.get(f"Part{i}") for i in range(2, 8)))


def test_no_person_names():
    print("\n== rules/ 不含人名 ==")
    hits = []
    for p in Path(HERE, "rules").glob("*.py"):
        txt = p.read_text(encoding="utf-8")
        for n in KNOWN_NAMES:
            if n in txt:
                hits.append(f"{p.name}:{n}")
    check("无责任人姓名", not hits, str(hits))


def test_workmain_uses_snapshot():
    print("\n== workmain 已接上快照（不再只读 xlsx）==")
    src = Path(HERE, "plugins", "workmain.py").read_text(encoding="utf-8")
    check("import rules.resolve_targets", "from rules import resolve_targets" in src)
    check("不再写死 Part1: 75", '"Part1": 75' not in src)
    check("输出带目标来源行", "环节目标来源" in src)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    test_part1_tightened()
    test_others_zero_drift()
    test_none_is_not_zero()
    test_fallback_is_13_not_75()
    test_shell_untouched()
    test_no_person_names()
    test_workmain_uses_snapshot()
    n_ok = sum(1 for _, ok in RESULTS if ok)
    print(f"\n{'=' * 46}\n[环节目标单一真源单测] {n_ok}/{len(RESULTS)} 通过")
    return 0 if n_ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
