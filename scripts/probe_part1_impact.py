# -*- coding: utf-8 -*-
"""Part1 目标 75 → 13 的影响面对拍（**只读**，不改任何文件）。

用途
----
改口径最怕两件事：① 该翻的没翻（漏判仍然存在）；② 不该翻的翻了（凭空多出一堆告警
把真信号淹掉）。所以改完必须把「旧口径输出」和「新口径输出」逐行 diff 一遍，并在记录级
数出「落在 13~75 判定差区间」的单有多少。

做法
----
在同一进程里跑两遍 pipeline_alerts：第二遍 monkeypatch `rules.targets.resolve_targets`
把 Part1 强行改回 75（模拟改前），其余完全不变，然后逐行比对。

用法
----
    python scripts/probe_part1_impact.py
    python scripts/probe_part1_impact.py --line 手机壳
"""
import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

OLD_PART1 = 75


def _run_tool(line, force_part1=None):
    """跑一次 pipeline_alerts；force_part1 非空时把 Part1 目标改成它（模拟旧口径）。"""
    import importlib
    import rules.targets as rt
    importlib.reload(rt)          # 清掉上一次的 monkeypatch
    if force_part1 is not None:
        orig = rt.resolve_targets

        def patched(*a, **kw):
            t, s = orig(*a, **kw)
            t["Part1"] = force_part1
            return t, s
        rt.resolve_targets = patched

    import plugins.workmain as wm
    importlib.reload(wm)          # 插件模块级常量不受影响，但闭包缓存必须重建
    import agentcore as core
    reg = core.Registry()
    core.load_plugins(reg, core.load_config())
    out = reg.execute("pipeline_alerts", {"line": line, "top": 12})
    return out if isinstance(out, str) else str(out)


def _seg_lines(text):
    """只取「段落正文」行做 diff，跳过口径说明/提示词块（那些不随目标变化）。"""
    keep = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s.startswith("==") or s.startswith("目录:") or s.startswith("口径") \
                or s.startswith("Part编号") or s.startswith("责任人字段") \
                or s.startswith("流程依赖") or s.startswith("两个到货") \
                or s.startswith("环节目标来源") or s.startswith("数据截至"):
            continue
        keep.append(ln)
    return keep


def _record_impact(line):
    """记录级：Part1 审批中单里，停留落在 (13, 75] 的有多少（=旧口径漏判、新口径抓到）。"""
    import datetime as dt
    import pandas as pd
    d = Path(r"C:\Users\dell\Desktop\work-main\data") / line / line
    fs = sorted(d.glob("Part1_*.xlsx"))
    if not fs:
        return None
    df = pd.read_excel(fs[-1], dtype=str)
    if "审批状态" not in df.columns or "更新时间" not in df.columns:
        return None
    sc = next((c for c in df.columns if "SKU" in str(c).upper()), None)
    p = df[df["审批状态"].astype(str).str.strip() == "审批中"]
    today = pd.Timestamp(dt.date.today())
    up = pd.to_datetime(p["更新时间"], errors="coerce")
    idle = (today - up.dt.normalize()).dt.days
    band = p[(idle > 13) & (idle <= OLD_PART1)]
    return {"pending_rows": len(p), "pending_skus": p[sc].nunique(dropna=True) if sc else 0,
            "in_band_rows": len(band),
            "in_band_skus": band[sc].nunique(dropna=True) if sc else 0,
            "band_examples": list(band[sc].dropna().astype(str).str.strip().unique()[:5]) if sc else []}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--line", default="手机膜")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 74)
    print(f"Part1 目标对拍: 改前 75  vs  改后 13   [{a.line}]")
    print("=" * 74)

    old = _run_tool(a.line, force_part1=OLD_PART1)
    new = _run_tool(a.line, force_part1=None)
    if old.startswith("[错误]") or new.startswith("[错误]"):
        print(f"  ⚠️ 工具不可用: {old[:80]} / {new[:80]}")
        return 1

    o, n = _seg_lines(old), _seg_lines(new)
    added = [x for x in n if x not in o]
    removed = [x for x in o if x not in n]
    print(f"\nA. 输出层: 正文 {len(o)} 行 → {len(n)} 行")
    print(f"   新增 {len(added)} 行 / 消失 {len(removed)} 行")
    for x in added[:12]:
        print(f"   + {x.strip()[:110]}")
    for x in removed[:12]:
        print(f"   - {x.strip()[:110]}")

    print("\nB. 记录层（Part1 审批中单）:")
    imp = _record_impact(a.line)
    if not imp:
        print("   (Part1 文件缺失，跳过)")
    else:
        print(f"   审批中 {imp['pending_rows']} 单 / {imp['pending_skus']} SKU")
        print(f"   停留落在 13~75 天（旧口径漏判、新口径抓到）: "
              f"{imp['in_band_rows']} 单 / {imp['in_band_skus']} SKU")
        if imp["band_examples"]:
            print(f"   例: {', '.join(imp['band_examples'])}")

    print("\nC. 结论:")
    if not added and not removed:
        pn = imp["pending_rows"] if imp else 0
        if pn == 0:
            print("   该环节当前没有审批中单据 —— 改口径对它零影响（等新单进来才生效）。")
        elif imp["in_band_rows"] == 0:
            print("   零翻转，但在途单不为 0 —— 它们全落在判定差窗口之外"
                  f"（{imp['pending_rows']} 单，其中带 SKU 的 {imp['pending_skus']} 个）。")
            print("   这是**潜伏缺陷已修**，不是'没修'：14~75 天的新单以前会报'还剩 61 天'，")
            print("   现在会按 13 天判超期。")
    else:
        print("   有翻转，逐条核对上方 + / - 行是否都该翻（新增=以前漏判的，消失=不该出现的）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
