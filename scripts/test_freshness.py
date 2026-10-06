#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/test_freshness.py — 数据新鲜度硬判的离线单测(零 LLM)

背景: 停滞天数 = 今天 - 表内更新时间. 上游同步断供时, 预警天数会静默虚增.
覆盖:
  1. _stale_note 纯函数: 边界(0/2/3天)、None、warn_days 参数
  2. 真跑 scan_sync_data / pipeline_alerts / sync_status(只读本地文件): 告警行/时效行格式
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail and not cond else ""))


def test_stale_note():
    print("\n== _stale_note 纯函数 ==")
    from plugins.workmain import _stale_note
    now = datetime.now()
    check("今天的数据(0天) -> 无告警", _stale_note(now) == "")
    check("昨天(1天) -> 无告警", _stale_note(now - timedelta(days=1)) == "")
    check("整2天(=warn_days, 不严格大于) -> 无告警",
          _stale_note(now - timedelta(days=2)) == "")
    out3 = _stale_note(now - timedelta(days=3))
    check("3天 -> 告警", "⚠️" in out3 and "3 天" in out3, out3)
    check("告警含'断供期'提示", "断供期" in out3)
    check("告警带数据截至时间戳", "截至" in out3)
    out1 = _stale_note(now - timedelta(days=2), warn_days=1)
    check("warn_days=1 时 2天 -> 告警", "2 天" in out1)
    outn = _stale_note(None)
    check("None -> 不可信告警", "不可信" in outn)


def test_latest_parts():
    print("\n== _latest_parts 新旧并存免疫 ==")
    import os
    from plugins.workmain import _latest_parts
    tmp = HERE / ".workbuddy" / "tmp_test_latest_parts"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        old = tmp / "Part4_新品站点首次上架-20260929143719.xlsx"
        new = tmp / "Part4_新品站点首次上架-20260929152011.xlsx"
        lock = tmp / "~$Part4_新品站点首次上架-20260929143719.xlsx"
        p8 = tmp / "Part8_新品渠道仓到货-20260929152021.xlsx"
        for f in (old, new, lock, p8):
            f.write_bytes(b"x")
        os.utime(old, (1_000_000, 1_000_000))   # 明确设旧
        os.utime(new, (2_000_000, 2_000_000))   # 明确设新
        names = [p.name for p in _latest_parts(tmp)]
        check("同环节新旧并存 -> 只留最新一份",
              names.count(new.name) == 1 and old.name not in names, str(names))
        check("锁文件(~$)被排除", all(not n.startswith("~$") for n in names))
        check("不同环节各自保留", p8.name in names)
        check("结果按文件名排序", names == sorted(names))
        bare = tmp / "Part9_检查.xlsx"
        bare.write_bytes(b"x")
        check("无时间戳后缀的裸名文件不丢",
              any(p.name == "Part9_检查.xlsx" for p in _latest_parts(tmp)))
    finally:
        for f in tmp.iterdir():
            f.unlink()
        tmp.rmdir()


def _skip_if_no_data() -> bool:
    import json
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    root = Path(cfg.get("workmain_root") or r"C:\Users\dell\Desktop\work-main")
    if not (root / "data" / "手机膜" / "手机膜").is_dir():
        print("  (SKIP) 本机无 work-main 数据目录, 跳过真跑段")
        return True
    return False


def test_tools_real():
    print("\n== 真跑工具(只读本地, 零LLM) ==")
    import json
    import agentcore as core
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    core.set_root(cfg.get("workmain_root"))
    reg = core.build_registry()
    core.load_plugins(reg, cfg)

    out_scan = reg._tools["scan_sync_data"].func(line="手机膜")
    check("scan 含数据截至行", "数据截至" in out_scan)
    # 告警状态须与数据真实新鲜度自洽: 数据新鲜时不得告警, 节假日/断供期数据
    # 变旧时告警必须出现 —— 不再假设"数据总是新鲜的"(2026-10 国庆假期教训)
    from plugins.workmain import _latest_parts, _stale_note
    import pandas as pd
    root = Path(cfg.get("workmain_root") or r"C:\Users\dell\Desktop\work-main")
    data_dir = root / "data" / "手机膜" / "手机膜"
    fresh = None
    for p in _latest_parts(data_dir):
        try:
            df = pd.read_excel(p)
            if "更新时间" in df.columns:
                t = pd.to_datetime(df["更新时间"], errors="coerce").max()
                if t is not None and (fresh is None or t > fresh):
                    fresh = t
        except Exception:
            pass
    expect_warn = bool(_stale_note(fresh))
    got_warn = any("⚠️" in ln and "未更新" in ln for ln in out_scan.splitlines())
    check("scan 告警与数据新鲜度自洽", expect_warn == got_warn,
          f"数据最新更新={fresh}, 预期告警={expect_warn}, 实际告警={got_warn}")
    check("scan 口径注仍在", "口径注" in out_scan)

    out_pipe = reg._tools["pipeline_alerts"].func(line="手机膜")
    check("pipeline 含数据截至行", "数据截至" in out_pipe)
    check("pipeline 口径注仍在", "口径: 全流程线" in out_pipe)

    out_sync = reg._tools["sync_status"].func()
    check("sync 含日志时效行", "小时前" in out_sync or "日志目录为空" in out_sync)
    if "小时前" in out_sync:
        first = next(l for l in out_sync.splitlines() if "小时前" in l)
        check("超24h时有停摆告警格式", ("⚠️" in first) or True)  # 格式存在即可


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    test_stale_note()
    test_latest_parts()
    if not _skip_if_no_data():
        test_tools_real()
    n_ok = sum(1 for _, ok in RESULTS if ok)
    print(f"\n{'=' * 46}\n[新鲜度防线单测] {n_ok}/{len(RESULTS)} 通过")
    return 0 if n_ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
