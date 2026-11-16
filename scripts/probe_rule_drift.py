# -*- coding: utf-8 -*-
"""三源口径漂移探针（**只读**）：规则快照(后端真源) vs 时效确认规则表.xlsx(workmain 现用) vs 代码常量。

为什么需要它
------------
同一条「环节目标天数」现在有三处来源：
  1. 数据后台 `rule_config.seg_targets`（可编辑真源）→ 落成 `资产文件/规则快照.json`（白名单键，只读通道）
  2. `流程复盘/时效确认规则表.xlsx`（Agent `plugins/workmain.py::_part_targets()` 读的是这份）
  3. `plugins/workmain.py` 里的裸常量（SIGN_TARGET_*、FULL 空运/海运、STALE_CAP_DAYS …）
第 2 份是 2026-10-19 之前的口径（Part1=75），第 1 份已是新口径（提案→取数=1 + 取数→签样=12 = 13）。
两者差 5.8 倍 —— 探针存在的意义就是**在改代码之前先把差多少条数出来**（口径内核抽取的第 0 步）。

用法
----
    python scripts/probe_rule_drift.py                # 手机膜 + 手机壳
    python scripts/probe_rule_drift.py --line 手机膜
    python scripts/probe_rule_drift.py --json out.json

输出三段：
  A. 规则级对照：Part → 快照折算值 / xlsx 值 / 差异
  B. 代码常量清单：哪些口径数字还写死在 .py 里（抽取目标）
  C. 记录级影响面：当前在途单里，有多少条「按快照超目标、按 xlsx 还没超」

⚠️ 纪律：只读，不改任何文件；探针自己不做业务判断，折算关系写在 PART_SEGS 里并注明依据。
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WORKMAIN = Path(r"C:\Users\dell\Desktop\work-main")
SNAPSHOT = WORKMAIN / "资产文件" / "规则快照.json"
RULE_XLSX = WORKMAIN / "流程复盘" / "时效确认规则表.xlsx"
DATA_DIR = WORKMAIN / "data"

#: Part → 由哪些「段」折算而来（依据：数据后台 seg_rules.SEGS 的起终点）
#: Part 是把多个段打包后的展示口径，取「该 Part 内各段目标之和」。
PART_SEGS = {
    "手机膜": {
        "Part1": ["提案→取数", "取数→签样"],
        "Part2": ["签样→采购"],
        "Part3": ["采购→本地仓到货"],
        "Part4": ["采购→首次上架"],
        "Part5": ["采购→发货末批"],
        "Part6": ["寄送→拍摄交付", "拍摄交付→素材加工"],
        "Part7": ["素材加工→上架"],
    },
    # 壳无「取数」节点：提案→签样 87 天横跨 Part1/Part2，Part 级折算无意义 → Part1 不折算；
    # Part2~Part7 的段结构与膜相同（seg_rules_shell.SEGS），可以照折算。
    "手机壳": {
        "Part2": ["签样→采购"],
        "Part3": ["采购→本地仓到货"],
        "Part4": ["采购→首次上架"],
        "Part5": ["采购→发货末批"],
        "Part6": ["寄送→拍摄交付", "拍摄交付→素材加工"],
        "Part7": ["素材加工→上架"],
    },
}

#: 代码里写死的口径数字（抽取时应搬进 rules/ 的唯一真源）
HARDCODED = [
    ("plugins/workmain.py", "STALE_CAP_DAYS = 120", "历史遗留封顶线"),
    ("plugins/workmain.py", "NEW_SKU_WINDOW_DAYS = 40", "新增SKU滚动窗口"),
    ("plugins/workmain.py", "SIGN_TARGET_FILM = 12", "膜：取数→签样"),
    ("plugins/workmain.py", "SIGN_TARGET_SHELL = 87", "壳：提案→签样（待校准）"),
    ("plugins/workmain.py", 'FULL = {"air": 60, "sea": 69}', "全流程线"),
    ("plugins/workmain.py", "GENERIC_MATERIAL_SAVE_DAYS = 18", "素材通用省天数"),
    ("daily_brief.py", 'else "• 近 40 天无新增 SKU"', "③ 空态文案（与窗口常量重复）"),
]


def load_snapshot(path: Path):
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None, "快照文件不存在（后端未启动过？跑一次 export_rules_snapshot）"
    except Exception as e:
        return None, f"快照不可读：{type(e).__name__}: {e}"
    rules = doc.get("rules") or {}
    return {"segs": rules.get("seg_targets") or {},
            "loss_only": rules.get("loss_route_limit"),
            "generated_at": doc.get("generated_at"),
            "version": doc.get("rules_version")}, ""


def load_xlsx(path: Path):
    import pandas as pd
    if not path.exists():
        return None, f"规则表不存在：{path}"
    df = pd.read_excel(path, sheet_name=0, dtype=str)
    out = {}
    for _, r in df.iterrows():
        dev = str(r.get("开发类型", "")).strip()
        part = str(r.get("Part", "")).strip()
        val = str(r.get("确认目标(天)", "")).strip()
        if not part.startswith("Part") or not val:
            continue
        try:
            out.setdefault(dev, {})[part] = int(float(val))
        except ValueError:
            continue
    return out, ""


def snap_part_target(snapshot, line, part):
    """把段目标折成 Part 级目标；缺任一子段返回 (None, 缺哪些段)。"""
    segs = snapshot["segs"]
    need = PART_SEGS[line].get(part)
    if need is None:
        return None, ["（该 Part 无段级折算定义）"]
    missing, total = [], 0
    for seg in need:
        if seg not in segs or segs[seg].get("target") is None:
            missing.append(seg)
        else:
            total += int(segs[seg]["target"])
    return (None if missing else total), missing


def latest_part(path_dir: Path, n: int):
    fs = sorted(path_dir.glob(f"Part{n}_*.xlsx"))
    return fs[-1] if fs else None


def scan_pending(line: str, today):
    """{Part: [(sku, 停留天数)]}，只统计审批中的单。"""
    import pandas as pd
    d = DATA_DIR / line / line
    out = {}
    if not d.is_dir():
        return out
    for n in range(1, 10):
        f = latest_part(d, n)
        if f is None:
            continue
        try:
            df = pd.read_excel(f, dtype=str)
        except Exception:
            continue
        sc = next((c for c in df.columns if "SKU" in str(c).upper()), None)
        if sc is None or "审批状态" not in df.columns:
            continue
        rows = []
        for _, r in df.iterrows():
            if str(r.get("审批状态", "")).strip() != "审批中":
                continue
            up = pd.to_datetime(r.get("更新时间"), errors="coerce")
            if pd.isna(up):
                continue
            rows.append((str(r.get(sc, "")).strip().upper(), int((today - up.normalize()).days)))
        if rows:
            out[f"Part{n}"] = rows
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--line", default=None, choices=["手机膜", "手机壳"])
    ap.add_argument("--json", default=None, help="把结构化的差异结果写到这里")
    a = ap.parse_args()

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    lines = [a.line] if a.line else ["手机膜", "手机壳"]
    report = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "lines": {}}

    snap, err = load_snapshot(SNAPSHOT)
    print("=" * 78)
    print("A. 规则级对照（快照=后端真源  vs  xlsx=workmain 现读）")
    print("=" * 78)
    if err:
        print(f"  ⚠️ {err}")
    else:
        print(f"  快照 version={snap['version']} generated_at={snap['generated_at']} "
              f"seg_targets={len(snap['segs'])} 项")
    xl, xerr = load_xlsx(RULE_XLSX)
    if xerr:
        print(f"  ⚠️ {xerr}")

    for line in lines:
        print(f"\n-- {line}")
        entry = {"rule_drift": {}, "impact": {}}
        if not xl:
            continue
        xline = xl.get("常规开发", {})
        parts = [f"Part{i}" for i in range(1, 10)]
        print(f"   {'Part':<7}{'快照折算':>10}{'xlsx':>8}{'差':>8}   结论")
        for p in parts:
            snap_v, missing = (None, ["(no snapshot)"]) if not snap else snap_part_target(snap, line, p)
            xv = xline.get(p)
            if snap_v is None and xv is None:
                continue
            diff = (xv - snap_v) if (snap_v is not None and xv is not None) else None
            verdict = ""
            if snap_v is not None and xv is not None:
                verdict = "一致" if diff == 0 else f"❌ 漂移（xlsx 宽松 {diff} 天）" if diff > 0 else f"❌ 漂移（xlsx 严 {diff} 天）"
            elif missing and missing != ["(no snapshot)"]:
                verdict = f"无法折算：段缺失 {missing}"
            print(f"   {p:<7}{'-' if snap_v is None else snap_v:>10}{'-' if xv is None else xv:>8}"
                  f"{'-' if diff is None else diff:>8}   {verdict}")
            entry["rule_drift"][p] = {"snapshot": snap_v, "xlsx": xv, "diff": diff,
                                      "missing_segs": missing}
        report["lines"][line] = entry

    print()
    print("=" * 78)
    print("B. 仍写死在代码里的口径数字（抽取目标清单）")
    print("=" * 78)
    for f, snippet, meaning in HARDCODED:
        print(f"  {f:<24}{snippet:<42}{meaning}")

    print()
    print("=" * 78)
    print("C. 记录级影响面（同一 Part，两张表判定不同）")
    print("=" * 78)
    for line in lines:
        drift = report["lines"].get(line, {}).get("rule_drift") or {}
        pending = scan_pending(line, today)
        if not pending:
            print(f"\n-- {line}: 未找到在途数据（{DATA_DIR / line}）")
            continue
        print(f"\n-- {line}（今天={today:%Y-%m-%d}）")
        any_hit = False
        for p, rows in sorted(pending.items()):
            d = drift.get(p) or {}
            sv, xv = d.get("snapshot"), d.get("xlsx")
            hit = []
            if sv is not None and xv is not None and sv != xv:
                lo, hi = min(sv, xv), max(sv, xv)
                hit = [(k, n) for k, n in rows if lo < n <= hi]
            tail = ""
            if hit:
                any_hit = True
                tail = (f"  ❌ {len(hit)} 条落在判定差区间({lo}<停留≤{hi}): "
                        + ", ".join(f"{k}({n}天)" for k, n in sorted(hit, key=lambda x: -x[1])[:6])
                        + (" …" if len(hit) > 6 else ""))
            print(f"   在途 {p:<6}{len(rows):>4} 条（快照={sv} / xlsx={xv}）{tail}")
            report["lines"][line]["impact"][p] = {
                "pending": len(rows), "beat": [{"sku": k, "days": n} for k, n in hit]}
        if not any_hit:
            print("   （当前无判定差异命中：可能是这两个值本来就一致，或该 Part 无在途单）")

    if a.json:
        Path(a.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结构化结果已写入 {a.json}")


if __name__ == "__main__":
    main()
