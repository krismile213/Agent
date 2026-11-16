#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/test_freshness_guard.py — 数据新鲜度三态守卫单测(零 LLM、零网络)

背景(2026-11-16): 用户 14:20 拿到快照去催办, 17:00 对方已办完却不知道 ——
催办基于旧数据 = 催已完成的事。修法是三态守卫(fresh/stale/blocked)。
与既有 test_freshness.py(测天级 _stale_note) 不重叠: 本测测分钟级新鲜度。

钉住四件事:
  1. 纯函数三态边界: 45min 内 fresh / 超 45min stale / 超 8h blocked。
  2. 归档库超越快照(>120s) → blocked, 且 10s 写入顺序差不误伤。
  3. 取不到快照 → blocked, 不降级成「假装新鲜」。
  4. IO 采集 + daily_brief 收尾: blocked 时 ⑥ 整段被替换为暂缓提示; stale 不动。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

RESULTS = []
NOW = datetime(2026, 11, 16, 18, 0, 0)


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))


def test_pure_three_states():
    print("\n== 纯函数三态 ==")
    from rules.freshness import assess_freshness, FRESH, STALE, BLOCKED
    base = datetime(2026, 11, 16, 17, 50)

    v = assess_freshness(base, None, NOW)          # 10min
    check("10min → fresh", v.state == FRESH and v.age_min == 10, f"{v.state}/{v.age_min}")

    v = assess_freshness(NOW - timedelta(minutes=46), None, NOW)
    check("46min → stale", v.state == STALE, v.state)
    check("stale 横幅带数据截至时刻", "数据截至" in v.banner and "催办前" in v.banner, v.banner)

    v = assess_freshness(NOW - timedelta(hours=8, minutes=1), None, NOW)
    check("8h+ → blocked", v.state == BLOCKED and v.blocked, v.state)

    v = assess_freshness(NOW - timedelta(minutes=44), None, NOW)
    check("44min(边界内) → fresh", v.state == FRESH, v.state)


def test_archive_supersede():
    print("\n== 归档库超越快照 ==")
    from rules.freshness import assess_freshness, BLOCKED, FRESH, ARCHIVE_SKEW_SEC
    snap = datetime(2026, 11, 16, 14, 7, 8)
    now = datetime(2026, 11, 16, 14, 20)

    v = assess_freshness(snap, snap + timedelta(seconds=10), now)
    check("库比快照新 10s(同轮写入顺序) → 不误伤", v.state == FRESH, v.state)

    v = assess_freshness(snap, snap + timedelta(seconds=ARCHIVE_SKEW_SEC + 1), now)
    check(f"库比快照新 >{ARCHIVE_SKEW_SEC}s → blocked", v.state == BLOCKED, v.state)
    check("blocked 横幅明确禁催", "禁止催办" in v.banner, v.banner)


def test_no_snapshot_blocks():
    print("\n== 快照缺失 ==")
    from rules.freshness import assess_freshness, BLOCKED
    v = assess_freshness(None, None, NOW)
    check("快照取不到 → blocked(不假装新鲜)", v.state == BLOCKED, v.state)
    check("快照缺失横幅提示 sync_status", "sync_status" in v.banner, v.banner)


def test_brief_guard():
    print("\n== daily_brief 收尾禁催 ==")
    from daily_brief import enforce_freshness_guard
    md = ("## 晨检\n\n### ③ 已超期\n数据\n\n### ⑥ 行动建议\n\n"
          "• 催 G598 走 Part8 渠道仓到货确认\n• 催 G601 走 Part3 本地仓到货\n")
    blocked = "⛔ 快照已被超越: 数据截至 11-16 14:07, 但后台已同步到 11-16 17:00 —— 本次禁止催办"

    out, hit = enforce_freshness_guard(md, blocked)
    check("blocked → 替换 ⑥", hit is True)
    check("⑥ 不再点名 SKU", "G598" not in out.split("⑥ ")[-1], out[-200:])
    check("⑥ 写暂缓提示", "暂不催办" in out, out[-200:])
    check("③ 数据段没被误动", "### ③ 已超期\n数据" in out)

    stale = "⚠️ 数据截至 11-16 17:14(距今 46 分钟), 催办前先在钉钉核对当前状态"
    out2, hit2 = enforce_freshness_guard(md, stale)
    check("stale → 不替换(仍要催)", hit2 is False and out2 == md)

    out3, hit3 = enforce_freshness_guard(md, "无横幅的普通输出")
    check("无横幅 → 不替换", hit3 is False and out3 == md)


def test_workmain_io():
    print("\n== workmain IO 采集 ==")
    import plugins.workmain as wm
    d = Path(r"C:\Users\dell\Desktop\work-main\data\手机膜\手机膜")
    files = sorted(d.glob("Part*.xlsx"))[-3:]
    if not files:
        check("找到 Part*.xlsx 供采集", False, "目录无 Part 文件")
    else:
        snap = wm._freshness_files(files)
        check("快照 mtime 采集为 datetime", isinstance(snap, datetime), str(snap))
        check("快照 mtime 年份合理", snap is not None and snap.year >= 2025, str(snap))
    root = Path(r"C:\Users\dell\Desktop\work-main")
    arch = wm._freshness_archive(root)
    check("归档库 finished_at 采集为 datetime", isinstance(arch, datetime), str(arch))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    test_pure_three_states()
    test_archive_supersede()
    test_no_snapshot_blocks()
    test_brief_guard()
    test_workmain_io()
    n_ok = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{'=' * 46}\n[数据新鲜度守卫单测] {n_ok}/{len(RESULTS)} 通过")
    return 0 if n_ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
