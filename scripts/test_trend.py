#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/test_trend.py — pipeline_trend 趋势工具单测(零 LLM, 零网络)

背景(2026-10-29): 其余工具都是「当前快照」, 问"这周比上周超期多了多少"只能让
模型自己调两次 inflight_report 再心算 —— 算错了没人校验。pipeline_trend 把多次
取数的结果做成确定性汇总, 模型只负责解读。

本文件钉住: 表结构 / 变化列算法 / 缺日不是 0 / 全空列省略 / 口径声明不丢。
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import agentcore as core  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not cond else ""))


def _reg():
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    reg = core.build_registry()
    core.load_plugins(reg, cfg)
    return reg


def _rows(out):
    """取出表格数据行(形如 '10-27        24     8     16   -2')。"""
    rows = []
    for line in out.split("\n"):
        s = line.rstrip()
        if len(s) > 8 and s[2] == "-" and s[:2].isdigit() and s[3:5].isdigit():
            rows.append(s)
    return rows


def test_basic_shape():
    print("\n== 基本结构 ==")
    reg = _reg()
    out = reg.execute("pipeline_trend", {"days": 10})
    check("有标题行", "流程趋势" in out, out[:60])
    check("有表头(日期/在研/超期/进行中)", all(k in out for k in ("日期", "在研", "超期", "进行中")))
    check("有数据行", len(_rows(out)) >= 3, str(len(_rows(out))))
    check("有区间对比", "区间对比" in out, out[-200:])
    check("带口径声明(segment vs Part)", "segment" in out and "Part" in out, out[-200:])
    check("说明缺行不是 0", "不是 0" in out, out[-160:])


def test_delta_column():
    """变化列必须是「较上一份」的差, 且首行为 —、相等写「持平」。"""
    print("\n== 变化列 ==")
    reg = _reg()
    out = reg.execute("pipeline_trend", {"days": 12})
    rows = _rows(out)
    check("首行变化为 —", rows[0].rstrip().endswith("—"), rows[0])
    # 从输出里回读每行的超期数, 自己算一遍差, 和工具给的比对
    ok = True
    prev = None
    for r in rows:
        parts = r.split()
        try:
            over = int(parts[2])
        except (IndexError, ValueError):
            continue
        delta = parts[-1]
        if prev is None:
            prev = over
            continue
        d = over - prev
        want = "持平" if d == 0 else f"{d:+d}"
        if delta != want:
            ok = False
            print(f"     不一致: {r}  期望 {want}")
        prev = over
    check("每行变化 = 超期数之差", ok)


def test_weekend_gap_is_missing_not_zero():
    """缺日报的日期必须整行缺, 不能补一行 0。

    2026-11-16 两次修正:
      ① 原来写死「10-25 缺 / 10-24、10-26 在」, 而工具读的是「最近 N 份」,
         日报一推进窗口就滑走 → 断言必然假失败(窗口已到 10-31~11-16)。
      ② 改成「周日必缺」也是错的 —— 实测 11-01(周日)有日报, 11-08/11-14/11-15 没有,
         平台并不按星期出报。真正的契约是: **表里的每一行都对应磁盘上一份真实日报**,
         没日报的日期不出现(而不是出现一行 0)。
    """
    import datetime as dt

    print("\n== 缺日不是 0 ==")
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    root = Path(cfg.get("workmain_root") or r"C:\Users\dell\Desktop\work-main")
    reports = root / "data" / "手机膜" / "手机膜" / "reports"
    have = set()
    for p in reports.glob("事中日报_*.xlsx"):
        m = re.search(r"(\d{8})", p.stem)
        if m:
            d = m.group(1)
            have.add(f"{d[4:6]}-{d[6:]}")
    check("能找到日报文件(测试前提)", bool(have), str(reports))

    reg = _reg()
    out = reg.execute("pipeline_trend", {"days": 14})
    rows = _rows(out)
    dates = {r[:5] for r in rows}
    check("有数据行", bool(dates), str(out[:120]))
    if not dates or not have:
        return

    ghost = sorted(d for d in dates if d not in have)
    check("表里每一行都有对应日报(没有凭空补的行)", not ghost, str(ghost))
    lo, hi = min(dates), max(dates)
    lost = [d for d in sorted(have) if lo <= d <= hi and d not in dates]
    check("有日报的日期都在表里(没被静默丢行)", not lost, str(lost[:8]))

    # 显式反例: 缺日报的日期不许出现; 且缺日两侧不能出现「全 0」行
    zero_rows = [r for r in rows if re.search(r"\s0\s+0\s+0\b", r)]
    check("没有 0/0/0 的填充行", not zero_rows, str(zero_rows[:3]))


def test_days_clamp():
    print("\n== days 边界 ==")
    reg = _reg()
    small = reg.execute("pipeline_trend", {"days": 1})
    check("days=1 被抬到 2", "近 2 天" in small, small[:40])
    big = reg.execute("pipeline_trend", {"days": 999})
    check("days=999 被压到 60", "近 60 天" in big, big[:40])


def test_week_compare_line():
    """「最近N份 vs 前N份」结论行必须存在且自洽。

    2026-10-29 三轮实测: 只给明细表时模型自己切区间, 得出"从5个增加到4个,
    净减少1个"这种自相矛盾的结论; 把周对比算好放进输出后, 它直接抄且算对了。
    结论: 确定性计算要前移到工具里, 别指望模型对着表格做减法。
    """
    print("\n== 周对比结论行 ==")
    reg = _reg()
    out = reg.execute("pipeline_trend", {"days": 14})
    line = next((l for l in out.split("\n") if "vs 前" in l), "")
    check("有「最近N份 vs 前N份」行", bool(line), out[-300:])
    if not line:
        return
    check("按份数切(说明避开缺日错位)", "按份数切" in line, line[:60])
    m = re.search(r"超期均值\s*([\d.]+)\s*→\s*([\d.]+)\s*\(([+-][\d.]+)\)", line)
    check("均值与差值格式正确", bool(m), line)
    if m:
        a, b, d = float(m.group(1)), float(m.group(2)), float(m.group(3))
        # 均值各自保留 1 位小数, 差值用的是未舍入的原值 → 容差放到 0.15
        check("差值 ≈ 后段均值 - 前段均值", abs((b - a) - d) < 0.15,
              f"{a}→{b} 差 {d}")
    check("带峰值日期", "峰值" in line and "→" in line, line)
    check("给出最新一份的数", "最新" in line, line)
    check("说明怎么引用", "直接抄" in out and "不要自己按日期切" in out, out[-160:])


def test_bad_category():
    print("\n== 非法品类 ==")
    reg = _reg()
    out = reg.execute("pipeline_trend", {"line": "../../etc"})
    check("不抛异常, 返回错误文本", out.startswith("[错误]"), out[:60])
    check("错误带下一步", any(k in out for k in ("改用", "处理", "说明", "仅支持")), out[:80])


def test_warn_column_elision():
    """整列无预警数据时省略该列, 并说明「不是预警为 0」——否则读者会误读。"""
    print("\n== 空列省略 ==")
    reg = _reg()
    out = reg.execute("pipeline_trend", {"days": 3})
    if "预警" in out.split("\n")[1]:
        print("  (SKIP) 近 3 天仍有预警分组, 不适用")
        return
    check("表头不含预警列", "预警" not in out.split("\n")[1], out.split("\n")[1])
    check("有说明不是 0", "不是预警为 0" in out, out[-160:])


def main() -> int:
    test_basic_shape()
    test_delta_column()
    test_weekend_gap_is_missing_not_zero()
    test_days_clamp()
    test_week_compare_line()
    test_bad_category()
    test_warn_column_elision()
    bad = [n for n, ok in RESULTS if not ok]
    print("\n" + "=" * 46)
    if bad:
        print(f"[趋势工具单测] {len(bad)} 项失败: " + ", ".join(bad))
        return 1
    print(f"[趋势工具单测] {len(RESULTS)}/{len(RESULTS)} 通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
