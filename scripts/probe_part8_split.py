# -*- coding: utf-8 -*-
"""Part8「发货 vs 到货」分层探针（**只读**）。

为什么需要它
------------
Part8 是**渠道仓到货检查**，但表里 `审批状态=审批中` 混着两类完全不同的事：
  ① 货还没到（`渠道仓到货时间` 为空）—— 属运输轨，该按发货目标判；
  ② 货到了、没人确认（到货时间为空之外的全部）—— 这才是 Part8 该催的。
现状工具不区分，于是运输中的 SKU 被当成"卡 Part8 已停留 N 天"催错对象
（2026-11-16 实测：84 单 / 21 SKU 属于 ①）。

同时还暴露出一个口径事实：`更新时间` 会被「到货通知 / 批量转交」刷新 ——
2026-03-25 一次批量转交把 151 单刷成同一天，所以"今天 − 更新时间"测的是
**最后一次审批动作**，不是**到货后等待时长**。本探针以到货时间为锚重算。

用法
----
    python scripts/probe_part8_split.py
    python scripts/probe_part8_split.py --line 手机壳 --json D:/data/part8.json
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

WORKMAIN = Path(r"C:\Users\dell\Desktop\work-main")
DATA = WORKMAIN / "data"
STALE_CAP = 120     # 与 plugins/workmain.py::STALE_CAP_DAYS 同值
TARGET = {"air": 15, "sea": 35}


def analyse(line, today=None):
    import pandas as pd
    today = pd.Timestamp(today or pd.Timestamp.today().normalize())
    d = DATA / line / line
    fs = sorted(d.glob("Part8_*.xlsx"))
    if not fs:
        return None
    df = pd.read_excel(fs[-1], dtype=str)
    if "审批状态" not in df.columns:
        return None
    p = df[df["审批状态"].astype(str).str.strip() == "审批中"].copy()
    for col in ("渠道仓到货时间", "创建时间", "更新时间"):
        p[col] = pd.to_datetime(p.get(col), errors="coerce")
    arr = (today - p["渠道仓到货时间"].dt.normalize()).dt.days
    upd = (today - p["更新时间"].dt.normalize()).dt.days

    not_arrived = p["渠道仓到货时间"].isna()
    arrived = ~not_arrived
    buckets = {
        "未到货": not_arrived,
        "已到货·在途(≤15天)": arrived & (arr <= TARGET["air"]),
        "已到货·逾期(16~120天)": arrived & (arr > TARGET["air"]) & (arr <= STALE_CAP),
        "已到货·僵尸(>120天)": arrived & (arr > STALE_CAP),
    }
    tool = upd <= STALE_CAP        # 工具现口径: 更新时间≤120 全部算 Part8 在途
    out = {"file": fs[-1].name, "rows_total": len(df), "pending_rows": int(len(p)),
           "pending_skus": int(p["新品SKU"].nunique()), "buckets": {}}
    for name, mask in buckets.items():
        out["buckets"][name] = {
            "rows": int(mask.sum()),
            "skus": int(p[mask]["新品SKU"].nunique()),
            "示例": sorted(p[mask]["新品SKU"].dropna().astype(str).unique()[:6]),
        }
    od = buckets["已到货·逾期(16~120天)"]
    if od.any():
        w = arr[od]
        out["buckets"]["已到货·逾期(16~120天)"].update(
            {"等待中位": int(w.median()), "等待最长": int(w.max())})
    out["工具口径对比"] = {
        "工具在途(更新≤120)": {"rows": int(tool.sum()),
                          "skus": int(p[tool]["新品SKU"].nunique())},
        "工具算在途但其实未到货": {"rows": int((tool & not_arrived).sum()),
                          "skus": int(p[tool & not_arrived]["新品SKU"].nunique())},
        "工具算在途但其实僵尸": {"rows": int((tool & buckets["已到货·僵尸(>120天)"]).sum())},
        "更新时间被批量转交刷新的单": int(
            (p["更新时间"].dt.strftime("%Y-%m-%d") == "2026-03-25").sum()),
    }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--line", default=None, choices=["手机膜", "手机壳"])
    ap.add_argument("--json", default=None)
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    lines = [a.line] if a.line else ["手机膜", "手机壳"]
    rep = {}
    for line in lines:
        r = analyse(line)
        if not r:
            print(f"{line}: 无 Part8 文件，跳过")
            continue
        rep[line] = r
        print("=" * 66)
        print(f"{line}  {r['file']}")
        print(f"  总行 {r['rows_total']} | 审批中 {r['pending_rows']} 单 / "
              f"{r['pending_skus']} SKU")
        for name, b in r["buckets"].items():
            extra = ""
            if "等待中位" in b:
                extra = f"，到货后中位等 {b['等待中位']} 天 / 最长 {b['等待最长']} 天"
            print(f"    {name:<22} {b['rows']:>5} 单 / {b['skus']:>3} SKU{extra}")
        t = r["工具口径对比"]
        print(f"  -- 工具现口径: 在途 {t['工具在途(更新≤120)']['rows']} 单 / "
              f"{t['工具在途(更新≤120)']['skus']} SKU")
        print(f"     其中未到货(误算 Part8) {t['工具算在途但其实未到货']['rows']} 单 / "
              f"{t['工具算在途但其实未到货']['skus']} SKU; "
              f"僵尸 {t['工具算在途但其实僵尸']['rows']} 单")
        print(f"     更新时间被 2026-03-25 批量转交刷新: "
              f"{t['更新时间被批量转交刷新的单']} 单")
    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        print(f"\n已写 {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
