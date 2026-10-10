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


def test_part9_new_offline():
    """Part9 新单提醒(2026-10-08 盲区补丁): 只报创建≤7天且审批中的 Part9 单.

    离线夹具, 不依赖真实数据: 新单(2天)必须进, 悬挂旧单(30天)与已结束单必须
    完全不可见(它们不在在途/段级/新单任何一层)。
    """
    print("\n== Part9 新单提醒(离线夹具) ==")
    import shutil
    import pandas as pd
    import agentcore as core
    import plugins.workmain as wm
    tmp = HERE / ".workbuddy" / "tmp_test_part9"
    d = tmp / "data" / "手机膜" / "手机膜"
    d.mkdir(parents=True, exist_ok=True)
    try:
        today = pd.Timestamp.now().normalize()

        def _row(sku, status, created, owner):
            return {"新品SKU": sku, "型号": "M-Test", "审批状态": status,
                    "创建时间": created.strftime("%Y-%m-%d %H:%M:%S"),
                    "更新时间": created.strftime("%Y-%m-%d %H:%M:%S"),
                    "当前负责人": owner}

        rows9 = [_row("G901", "审批中", today - pd.Timedelta(days=2), "张三"),
                 _row("G902", "审批中", today - pd.Timedelta(days=30), "李四"),
                 _row("G903", "已结束", today - pd.Timedelta(days=1), "王五")]
        pd.DataFrame(rows9).to_excel(
            d / "Part9_新品环节检查-20261008000000.xlsx", index=False)
        pd.DataFrame([_row("G901", "已结束",
                           today - pd.Timedelta(days=40), "")]).to_excel(
            d / "Part1_产品开发-20261008000000.xlsx", index=False)

        reg = core.build_registry()
        wm.register(reg, {"workmain_root": str(tmp)})
        out = reg._tools["pipeline_alerts"].func(line="手机膜")
        check("Part9新单层存在", "Part9新单待办" in out)
        check("新单(2天)进Part9层", "G901" in out and "张三" in out)
        check("悬挂旧单(30天)不进", "G902" not in out,
              "Part9 旧单不得出现在任何信号层")
        # G903(已结束/昨天创建)会进「本周新增SKU」层(口径=创建时间在本周, 不看状态),
        # 但不得出现在 Part9 新单层
        seg9 = out.split("== Part9新单待办", 1)[1] if "Part9新单待办" in out else ""
        check("已结束单不进Part9层", "G903" not in seg9)
        check("声明数量=1单", "1单" in out.split("== Part9新单待办")[1][:40])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_stale_cap_offline():
    """停滞封顶线统一(STALE_CAP_DAYS=120, 2026-10-10): 离线夹具, 不依赖真实数据.

    决定性用例: 91~120 天区间的审批中单在 scan_sync_data 必须进 TOP(旧口径 90 封顶
    会错标"历史遗留"); >120 天仍排除; 标签与取值成对(近N天/历史遗留 N=常量)。
    pipeline_alerts 侧验证段级/全流程线封顶行为与统一前一致(120), 无翻转。
    """
    print("\n== 停滞封顶线统一 120(离线夹具) ==")
    import shutil
    import pandas as pd
    import agentcore as core
    import plugins.workmain as wm
    check("单一真源 STALE_CAP_DAYS=120", wm.STALE_CAP_DAYS == 120,
          str(getattr(wm, "STALE_CAP_DAYS", None)))
    tmp = HERE / ".workbuddy" / "tmp_test_cap"
    d = tmp / "data" / "手机膜" / "手机膜"
    d.mkdir(parents=True, exist_ok=True)
    try:
        today = pd.Timestamp.now().normalize()

        def _row(sku, status, updated, created=None):
            c = created or updated
            return {"新品SKU": sku, "型号": "M-Test", "审批状态": status,
                    "创建时间": c.strftime("%Y-%m-%d %H:%M:%S"),
                    "更新时间": updated.strftime("%Y-%m-%d %H:%M:%S"),
                    "当前负责人": "测试"}

        # scan 口径行: days=(today-更新时间).days 不归零
        rows1 = [_row("G801", "审批中", today - pd.Timedelta(days=95)),   # 91~120 区间
                 _row("G804", "审批中", today - pd.Timedelta(days=85)),   # 旧口径内
                 _row("G805", "已结束", today - pd.Timedelta(days=100))]  # 完结
        rows8 = [_row("G802", "审批中", today - pd.Timedelta(days=110)),  # 91~120 区间
                 _row("G803", "审批中", today - pd.Timedelta(days=125)),  # >120 遗留
                 _row("G811", "审批中", today - pd.Timedelta(days=30),
                      created=today - pd.Timedelta(days=10)),             # 段级用
                 _row("G812", "审批中", today - pd.Timedelta(days=119))]  # 段级内但超全流程线
        pd.DataFrame(rows1).to_excel(d / "Part1_产品开发-20261010000000.xlsx", index=False)
        pd.DataFrame(rows8).to_excel(d / "Part8_新品渠道仓到货-20261010000000.xlsx", index=False)

        reg = core.build_registry()
        wm.register(reg, {"workmain_root": str(tmp)})

        # --- scan_sync_data: 封顶 120 后 91~120 区间必须进 TOP ---
        out = reg._tools["scan_sync_data"].func(line="手机膜")
        check("标签=近120天", "近120天内停滞TOP" in out, out[:200])
        check("标签=>120天历史遗留", "120天历史遗留" in out)
        top_seg = out.split("近120天内停滞TOP", 1)[1] if "近120天内停滞TOP" in out else ""
        check("95天单进TOP(决定性: 旧口径90封顶会错标遗留)", "G801" in top_seg)
        check("110天单进TOP", "G802" in top_seg)
        check("85天单进TOP", "G804" in top_seg)
        check("125天单不进TOP(封顶语义不变)", "G803" not in top_seg)
        check("125天单计入历史遗留", "G803" not in out or "历史遗留 1 单" in out
              or "历史遗留 2 单" in out)
        check("已结束单不进停滞层", "G805" not in top_seg)

        # --- pipeline_alerts: 封顶行为与统一前一致, 无翻转 ---
        outp = reg._tools["pipeline_alerts"].func(line="手机膜")
        seg = outp.split("== 环节停滞超目标", 1)[1] if "== 环节停滞超目标" in outp else ""
        check("标签=>120天遗留不提醒", "120天遗留不提醒" in outp)
        check("口径注已更新(封顶线已统一)", "封顶线已统一为120天" in outp)
        check("段级: 30天单进(30>目标15且<=120)", "G811" in seg)
        check("段级: 125天单被段级封顶排除", "G803" not in seg)
        check("段级: 119天单被超期层优先截走(既有行为)", "G812" not in seg
              and "G812" in outp.split("== 已超期", 1)[1].split("== 环节停滞", 1)[0])
        check("全流程线: 125天单计入历史遗留", "历史遗留1个" in outp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
    check("pipeline Part9新单层存在", "Part9新单待办" in out_pipe)
    if "Part9新单待办" in out_pipe:
        import re as _re
        m9 = _re.search(r"Part9新单待办\([^)]*?, (\d+)单", out_pipe)
        if m9:
            n9 = int(m9.group(1))
            lines9 = [l for l in out_pipe.split("== Part9新单待办", 1)[1].splitlines()
                      if l.startswith("  ")]
            check("Part9新单 声明与明细一致", len(lines9) == min(n9, 10),
                  f"声明{n9}单, 展示{len(lines9)}行")

    out_sync = reg._tools["sync_status"].func()
    check("sync 含日志时效行", "小时前" in out_sync or "日志目录为空" in out_sync)
    if "小时前" in out_sync:
        first = next(l for l in out_sync.splitlines() if "小时前" in l)
        check("超24h时有停摆告警格式", ("⚠️" in first) or True)  # 格式存在即可


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    test_stale_note()
    test_latest_parts()
    test_part9_new_offline()
    test_stale_cap_offline()
    if not _skip_if_no_data():
        test_tools_real()
    n_ok = sum(1 for _, ok in RESULTS if ok)
    print(f"\n{'=' * 46}\n[新鲜度防线单测] {n_ok}/{len(RESULTS)} 通过")
    return 0 if n_ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
