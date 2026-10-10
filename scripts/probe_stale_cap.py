# -*- coding: utf-8 -*-
"""只读探针: 量化 scan_sync_data 停滞封顶线 90 → 120 的影响面 (2026-10-10 口径统一).

背景: scan_sync_data 与 pipeline_alerts 的停滞天数封顶线曾是双口径 (90 vs 120),
决定统一为 120 (单一真源 workmain.STALE_CAP_DAYS)。本探针改前跑, 回答三问:
  1) 翻转面: 90<天数<=120 区间的单据行有多少条从「历史遗留」转为「可入TOP」?
  2) TOP 变化: 封顶 120 后停滞 TOP8 与 90 封顶相比新增/移出了哪些行?
  3) 边界确认: >120 天的真实遗留仍被排除 (封顶语义不变, 只动分界线), 极端峰值如
     G550 的 125 天仍报不出 —— 这是封顶线的固有语义, 不是漏改。

用法: python scripts/probe_stale_cap.py [line]   (line 默认手机膜)
只读: 只 pd.read_excel, 不写任何文件。
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))          # mini_agent
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))  # workmain

import pandas as pd  # noqa: E402

from workmain import DEFAULT_ROOT, _latest_parts, _part_label  # noqa: E402

DONE = {"已结束", "终止"}


def collect(line: str):
    """与 scan_sync_data 完全同口径收集停滞行: (days, sku, part, 状态, 更新时间)."""
    d = Path(DEFAULT_ROOT) / "data" / line / line
    rows = []
    files = _latest_parts(d)
    if not files:
        print(f"[错误] {d} 下无 Part*.xlsx")
        sys.exit(2)
    today = pd.Timestamp(datetime.now().strftime("%Y-%m-%d"))
    for f in files:
        part = _part_label(f.stem)
        df = pd.read_excel(f, dtype=str)
        up = (pd.to_datetime(df["更新时间"], errors="coerce")
              if "更新时间" in df.columns else None)
        stat = df.get("审批状态")
        if up is None or stat is None:
            continue
        act = df[(~stat.isin(DONE)) & (up < today - pd.Timedelta(days=7))]
        for _, r in act.iterrows():
            try:
                days = int((today - pd.to_datetime(r.get("更新时间"))).days)
            except Exception:
                continue
            sku_col = next((c for c in df.columns if "SKU" in str(c).upper()),
                           "审批单标题")
            rows.append((days, str(r.get(sku_col, "?")).strip(), part,
                         str(r.get("审批状态", "?")), str(r.get("更新时间"))[:16]))
    return rows


def main():
    line = sys.argv[1] if len(sys.argv) > 1 else "手机膜"
    rows = collect(line)
    rows.sort(reverse=True)
    flip = [r for r in rows if 90 < r[0] <= 120]          # 翻转: zombie → recent
    beyond = [r for r in rows if r[0] > 120]              # 仍被排除的遗留
    top90 = [r for r in rows if r[0] <= 90][:8]
    top120 = [r for r in rows if r[0] <= 120][:8]

    print(f"数据源: data/{line}/{line} (只读探针, {datetime.now():%Y-%m-%d %H:%M})")
    print(f"停滞>=7天且未完结总行数: {len(rows)}")
    print(f"\n[1] 翻转面 90<天数<=120: {len(flip)} 条 (由「历史遗留」转入「近TOP池」)")
    for days, sku, part, st, t in flip:
        print(f"    {sku} {part} 停滞{days}天 (状态:{st}, 最后更新:{t})")
    print(f"\n[2] >120 天仍被排除: {len(beyond)} 条 (封顶语义不变)")
    for days, sku, part, st, t in beyond[:6]:
        print(f"    {sku} {part} 停滞{days}天 (状态:{st}, 最后更新:{t})")
    if len(beyond) > 6:
        print(f"    …另有 {len(beyond) - 6} 条")
    print(f"\n[3] 停滞TOP8 对比:")
    print("  90封顶:")
    for days, sku, part, *_ in top90:
        print(f"    {sku} {part} {days}天")
    print("  120封顶:")
    for days, sku, part, *_ in top120:
        print(f"    {sku} {part} {days}天")
    new_in = [r for r in top120 if r not in top90]
    print(f"\n结论: TOP8 新进 {len(new_in)} 行"
          + (": " + "; ".join(f"{r[1]} {r[2]} {r[0]}天" for r in new_in) if new_in else ""))


if __name__ == "__main__":
    main()
