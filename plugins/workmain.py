# -*- coding: utf-8 -*-
"""
workmain 插件 — work-main 工作区的领域工具包(可选)

这是插件 API 的第一个示范: 内核不知道 work-main 的存在,
本插件把已有产物(xlsx/日志/知识库)和脚本薄壳封装成工具注入。

插件API: 定义 register(registry, cfg) 并返回注册的工具数即可被内核加载。
  - 从 mini_agent import Tool, clip 复用内核的类型
  - 工具分 read(自动放行) / write(逐次确认) 两级
  - 任何异常都会被内核转成文本回灌模型, 这里不用 try 兜底业务错误

依赖(仅本插件, 内核不需要): pandas openpyxl
  search_kb 另需: rank_bm25 jieba, 且需先在 work-main 下构建索引:
  python agent/kb/build_kb.py
"""

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from mini_agent import Tool, clip

DEFAULT_ROOT = r"C:\Users\dell\Desktop\work-main"

# 路线复盘两套口径 → 汇总xlsx文件 (口径语义: action=动作映射 / system=系统业务时间列)
ROUTE_FILES = {
    "action": "route_路线复盘_复盘文件.xlsx",
    "system": "route_路线复盘_系统口径.xlsx",
}
ROUTE_SCRIPTS = {
    "action": "route_check.py",
    "system": "route_check_sys.py",
}

_kb = None  # search_kb 懒加载单例


def register(registry, cfg: dict) -> int:
    root = Path(cfg.get("workmain_root") or DEFAULT_ROOT)

    def full(rel: str) -> Path:
        return root / rel

    # ---------- 只读工具 ----------

    def t_query_batches() -> str:
        rows = []
        for line in ("手机膜", "手机壳"):
            d = full(f"data/{line}")
            if not d.is_dir():
                continue
            for sub in sorted(d.iterdir()):
                if not sub.is_dir():
                    continue
                parts = [p for p in sub.glob("Part*.xlsx")
                         if not p.name.startswith("~$")]
                if not parts:
                    continue
                newest = max(p.stat().st_mtime for p in parts)
                rows.append(f"{line}/{sub.name}: {len(parts)}个Part, "
                            f"最新文件 {datetime.fromtimestamp(newest):%Y-%m-%d %H:%M}")
        return "\n".join(rows) if rows else "data/ 下未发现含Part*.xlsx的批次目录"

    def t_get_product_info(keyword: str) -> str:
        import pandas as pd
        f = full("资产文件/商品资料.xlsx")
        if not f.exists():
            return f"[错误] 找不到商品资料: {f}"
        df = pd.read_excel(f, header=1, dtype=str)  # 真实表头在第2行
        cols = [c for c in ("SKU", "ASIN", "英文品名", "中文品名", "型号",
                            "类型", "品牌", "供应商", "状态") if c in df.columns]
        k = str(keyword).strip().upper()
        m = df[df["SKU"].astype(str).str.upper().str.contains(k, na=False)
               | df["型号"].astype(str).str.upper().str.contains(k, na=False)]
        if m.empty:
            return f"商品资料中未匹配到: {keyword} (可换SKU编号或手机型号关键词)"
        out = [f"{c}:{'' if pd.isna(r[c]) else str(r[c]).strip()}"
               for _, r in m.head(10).iterrows() for c in cols]
        return f"匹配{len(m)}条(最多显示10条):\n" + "\n".join(out)

    def t_query_sku_route(sku: str, caliber: str = "action") -> str:
        import pandas as pd
        fname = ROUTE_FILES.get(caliber)
        if not fname:
            return "[参数错误] caliber 只支持 action(动作映射口径) 或 system(系统口径)"
        f = full(f"流程复盘/output/{fname}")
        if not f.exists():
            return f"[错误] 复盘产物不存在: {f} (可先用 run_route_check 生成)"
        df = pd.read_excel(f, sheet_name="路线总览", dtype=str)
        s = str(sku).strip().upper()
        m = df[df["SKU"].astype(str).str.upper() == s]
        if m.empty:
            m = df[df["SKU"].astype(str).str.upper().str.startswith(s)]
        if m.empty:
            return f"路线总览({caliber}口径)中未找到SKU: {sku}"
        keys = [c for c in ("SKU", "型号", "开发类型", "复盘有效性", "复盘标记",
                            "流程位置", "卡点Part", "卡点动态", "停滞(天)",
                            "产品段耗时(天)", "产品段判定", "运营段耗时(天)",
                            "运营段判定", "运营耗时(天)", "运营判定",
                            "后勤耗时(天)", "后勤判定", "下一步") if c in df.columns]
        out = []
        for _, r in m.head(3).iterrows():
            out.append(f"== {r.get('SKU')} (口径:{caliber}) ==")
            out += [f"{k}: {'' if pd.isna(r[k]) else str(r[k]).strip()}" for k in keys]
        return "\n".join(out)

    def t_search_kb(query: str, top_k: int = 4) -> str:
        global _kb
        kbdir = full("agent/kb")
        if not kbdir.exists():
            return f"[错误] 未找到知识库目录: {kbdir} (检查workmain_root配置)"
        for p in (str(kbdir), str(kbdir.parent)):
            if p not in sys.path:
                sys.path.insert(0, p)
        try:
            from retrieve import KB
        except ImportError as e:
            return f"[错误] 知识库依赖缺失({e}): pip install rank_bm25 jieba"
        if _kb is None:
            try:
                _kb = KB()
            except FileNotFoundError as e:
                return f"[错误] {e}"
        hits = _kb.search(query, top_k=max(1, min(int(top_k), 8)))
        if not hits:
            return "知识库无命中(注意: 索引可能落后于最新复盘产物)"
        parts = [f"[{i}] {h['title']} ({h['source']}, score {h['score']})\n"
                 f"{clip(h['text'], 900)}"
                 for i, h in enumerate(hits, 1)]
        return "\n\n".join(parts)

    def t_sync_status() -> str:
        ld = full("dingtalk/logs")
        if not ld.is_dir():
            return "[错误] 未找到 dingtalk/logs (检查workmain_root配置)"
        out = []
        logs = sorted(ld.glob("sync_*.log"))
        if logs:
            tail = logs[-1].read_text("utf-8", errors="replace").splitlines()[-12:]
            out.append(f"== {logs[-1].name} 末尾12行 ==\n" + "\n".join(tail))
        rs = ld / "run_stdout.log"
        if rs.exists():
            out.append("== 定时任务run_stdout.log 末尾5行 ==\n"
                       + "\n".join(rs.read_text("utf-8", errors="replace").splitlines()[-5:]))
        return clip("\n\n".join(out), 3000) if out else "日志目录为空"

    def t_scan_sync_data(line: str = "手机膜", stale_days: int = 7, top: int = 8) -> str:
        """实时扫描自动同步目录: 比读旧预警产物新鲜, 数字直接来自最新xlsx."""
        import pandas as pd
        d = root / "data" / line / line
        if not d.is_dir():
            return f"[错误] 同步目录不存在: {d} (可用 query_batches 查看可用批次)"
        files = [p for p in sorted(d.glob("Part*.xlsx")) if not p.name.startswith("~$")]
        if not files:
            return f"[错误] {d} 下没有 Part*.xlsx"
        today = pd.Timestamp(datetime.now().strftime("%Y-%m-%d"))
        done_vals = {"已结束", "终止"}
        dist_total, stale_rows, today_skus = {}, [], set()
        today_cnt, fresh_time, total = 0, None, 0
        for f in files:
            part = f.stem.split("-")[0].split("_")[0]
            try:
                df = pd.read_excel(f, dtype=str)
            except Exception as e:
                return f"[错误] 读取 {f.name} 失败: {e}"
            total += len(df)
            sku_col = next((c for c in df.columns if "SKU" in str(c).upper()),
                           "审批单标题")
            up = (pd.to_datetime(df["更新时间"], errors="coerce")
                  if "更新时间" in df.columns else None)
            stat = df.get("审批状态")
            if stat is not None:
                for k, v in stat.value_counts().to_dict().items():
                    dist_total[k] = dist_total.get(k, 0) + int(v)
            if up is not None:
                last = up.max()
                if pd.notna(last) and (fresh_time is None or last > fresh_time):
                    fresh_time = last
                m = up >= today
                today_cnt += int(m.sum())
                today_skus.update(str(s).strip() for s in df.loc[m, sku_col].dropna()
                                  if str(s).strip() and str(s).strip().lower() != "nan")
                if stat is not None:
                    act = df[(~stat.isin(done_vals))
                             & (up < today - pd.Timedelta(days=int(stale_days)))]
                    for _, r in act.iterrows():
                        try:
                            days = int((today - pd.to_datetime(r.get("更新时间"))).days)
                        except Exception:
                            continue
                        stale_rows.append((days, str(r.get(sku_col, "?")).strip(),
                                           part, str(r.get("审批状态", "?")),
                                           str(r.get("更新时间"))[:16]))
        lines = [f"目录: data/{line}/{line} ({len(files)}个Part, 共{total}单)",
                 "状态分布: " + " / ".join(f"{k}:{v}" for k, v in sorted(dist_total.items())),
                 f"数据截至(最新更新时间): {str(fresh_time)[:16] if fresh_time is not None else '无'}",
                 f"今日有动态: {today_cnt} 单, SKU: {', '.join(sorted(today_skus)[:12]) or '无'}"]
        stale_rows.sort(reverse=True)
        zombie = [r for r in stale_rows if r[0] > 90]
        recent = [r for r in stale_rows if r[0] <= 90]
        lines.append(f"停滞≥{stale_days}天且未完结: {len(stale_rows)} 单 "
                     f"(其中>90天历史遗留 {len(zombie)} 单, 多为待人工填写的Part9, 不列入TOP)")
        lines.append(f"近90天内停滞(建议关注) TOP:")
        for days, sku, part, st, t in recent[:max(1, min(int(top), 15))]:
            lines.append(f"  {sku} {part} 停滞{days}天 (状态:{st}, 最后更新:{t})")
        return clip("\n".join(lines), 3500)

    # 权威表与运输模式缓存(进程内)
    _caches: dict = {}

    def _part_targets() -> dict:
        """从 流程复盘/时效确认规则表.xlsx 动态读 Part→确认目标(天); 表缺失用同值兜底."""
        if "targets" in _caches:
            return _caches["targets"]
        fallback = {"Part1": 75, "Part2": 1, "Part3": 8, "Part4": 7,
                    "Part5": 22, "Part6": 13, "Part7": 7, "Part8": 15, "Part9": 1}
        try:
            import pandas as pd
            df = pd.read_excel(full("流程复盘/时效确认规则表.xlsx"), dtype=str)
            got = {}
            for _, r in df.iterrows():
                p = str(r.get("Part", "")).strip()
                if p.startswith("Part"):
                    try:
                        got[p.split()[0]] = int(float(r.get("确认目标(天)")))
                    except (TypeError, ValueError):
                        pass
            fallback.update({k: v for k, v in got.items() if k in fallback})
        except Exception:
            pass
        _caches["targets"] = fallback
        return fallback

    def _sku_modes() -> dict:
        """SKU→air/sea: 取路线总览(海运货件数>0或口径=sea → sea), 未覆盖默认air."""
        if "modes" in _caches:
            return _caches["modes"]
        modes = {}
        try:
            import pandas as pd
            df = pd.read_excel(full("流程复盘/output/route_路线复盘_复盘文件.xlsx"),
                               sheet_name="路线总览", dtype=str)
            for _, r in df.iterrows():
                sku = str(r.get("SKU", "")).strip().upper()
                if not sku:
                    continue
                try:
                    sea = int(float(r.get("海运货件数") or 0))
                except (TypeError, ValueError):
                    sea = 0
                kj = str(r.get("口径", "")).strip().lower()
                modes[sku] = "sea" if (sea > 0 or kj == "sea") else "air"
        except Exception:
            pass
        _caches["modes"] = modes
        return modes

    def t_pipeline_alerts(line: str = "手机膜", warn_days: int = 15,
                          top: int = 8) -> str:
        """四层流程信号(权威表口径): 本周新增SKU / 全流程临期 / 已超期 / 环节停滞超目标.
        全流程线: 空运60/海运69(时效确认规则表·口径说明); 启动日=Part1手机数据取得日期
        (缺省最早创建时间); 在途只看Part1~8; >120天历史遗留不提醒; 段级为近似口径
        (环节内无进展超确认目标), 精确段口径以路线复盘为准."""
        import pandas as pd
        d = root / "data" / line / line
        if not d.is_dir():
            return f"[错误] 同步目录不存在: {d} (可用 query_batches 查看可用批次)"
        files = [p for p in sorted(d.glob("Part*.xlsx")) if not p.name.startswith("~$")]
        if not files:
            return "[错误] 无Part文件"
        today = pd.Timestamp(datetime.now().strftime("%Y-%m-%d"))
        monday = today - pd.Timedelta(days=today.weekday())
        targets = _part_targets()
        modes = _sku_modes()
        FULL = {"air": 60, "sea": 69}
        skus = {}   # sku -> {model, start, pending:set, created, last_up}
        seg_rows = []  # (超目标天数, idle, sku, part, target)
        for f in files:
            part_no = "".join(ch for ch in f.stem if ch.isdigit())[:1]
            if not part_no:
                continue
            part = f"Part{part_no}"
            try:
                df = pd.read_excel(f, dtype=str)
            except Exception:
                continue
            sku_col = next((c for c in df.columns if "SKU" in str(c).upper()), None)
            if sku_col is None:
                continue
            model_col = next((c for c in df.columns if "型号" in str(c)), None)
            created = (pd.to_datetime(df["创建时间"], errors="coerce")
                       if "创建时间" in df.columns else None)
            updated = (pd.to_datetime(df["更新时间"], errors="coerce")
                       if "更新时间" in df.columns else None)
            starts = None
            if part_no == "1" and "手机数据取得日期" in df.columns:
                starts = pd.to_datetime(df["手机数据取得日期"], errors="coerce")
            for i in range(len(df)):
                r = df.iloc[i]
                sku = str(r.get(sku_col, "")).strip()
                if not sku or sku.lower() == "nan":
                    continue
                info = skus.setdefault(sku, {"model": "", "start": None,
                                             "pending": set(), "created": None,
                                             "last_up": None})
                mv = str(r.get(model_col, "")).strip() if model_col else ""
                if mv and mv.lower() != "nan" and not info["model"]:
                    info["model"] = mv
                is_pending = str(r.get("审批状态", "")) == "审批中"
                c = created.iloc[i] if created is not None else None
                if c is not None and pd.notna(c) and (info["created"] is None or c < info["created"]):
                    info["created"] = c
                if starts is not None:
                    s = starts.iloc[i]
                    if s is not None and pd.notna(s) and (info["start"] is None or s < info["start"]):
                        info["start"] = s
                u = updated.iloc[i] if updated is not None else None
                if u is not None and pd.notna(u) and (info["last_up"] is None or u > info["last_up"]):
                    info["last_up"] = u
                if is_pending and part_no != "9":
                    info["pending"].add(part_no)
                    # 段级近似: 环节内无进展(最后更新)超该Part确认目标; Part8按运输方式
                    if u is not None and pd.notna(u):
                        idle = int((today - u.normalize()).days)
                        tgt = targets.get(part, 1)
                        if part == "Part8" and _sku_modes().get(sku) == "sea":
                            tgt = 35
                        if tgt < idle <= 120:
                            seg_rows.append((idle - tgt, idle, sku, part, tgt))

        new_week = sorted((info["created"], sku, info["model"])
                          for sku, info in skus.items()
                          if info["created"] is not None and info["created"] >= monday)
        warn_list, over_list, legacy = [], [], 0
        for sku, info in skus.items():
            if not info["pending"]:
                continue
            start = info["start"] or info["created"]
            if start is None or pd.isna(start):
                continue
            used = int((today - start).days)
            if used > 120:
                legacy += 1
                continue
            stuck = "Part" + max(info["pending"])
            line_days = FULL.get(modes.get(sku, "air"), 60)
            tag = "海运" if modes.get(sku, "air") == "sea" else "空运"
            if used > line_days:
                over_list.append((used - line_days, sku, info["model"], stuck,
                                  start, tag))
            elif used >= line_days - int(warn_days):
                warn_list.append((line_days - used, sku, info["model"], stuck,
                                  start, tag))
        warn_list.sort()
        over_list.sort()
        over_skus = {s for _, s, *_ in over_list}
        # 段级TOP排除已在超期名单的SKU, 让新信号浮出来
        seg_rows = [r for r in seg_rows if r[2] not in over_skus]
        seg_rows.sort(reverse=True)
        n = max(1, min(int(top), 12))

        out = [f"目录: data/{line}/{line}  口径: 全流程线 空运60/海运69(权威表), "
               f"临期阈值{warn_days}天, 在途=Part1~8审批中, >120天遗留不提醒; "
               f"段级=环节内无进展超确认目标(近似)"]
        out.append(f"\n== 本周新增SKU({monday:%m-%d}起, {len(new_week)}个) ==")
        for c, sku, mv in new_week[:15]:
            out.append(f"  {sku} {mv} (创建 {c:%m-%d})")
        out.append(f"\n== 临期预警(剩≤{warn_days}天, {len(warn_list)}个) ==")
        for left, sku, mv, stuck, s, tag in warn_list[:n]:
            out.append(f"  {sku} {mv} 剩{left}天[{tag}] (启动{s:%m-%d}, 卡{stuck})")
        out.append(f"\n== 已超期(超全流程线, {len(over_list)}个; 另历史遗留{legacy}个不列) ==")
        for over, sku, mv, stuck, s, tag in over_list[:n]:
            out.append(f"  {sku} {mv} 已超{over}天[{tag}] (启动{s:%m-%d}, 卡{stuck})")
        out.append(f"\n== 环节停滞超目标(近似段级, {len(seg_rows)}单) TOP ==")
        for over_t, idle, sku, part, tgt in seg_rows[:n]:
            out.append(f"  {sku} {part} 停滞{idle}天/目标{tgt}天 (超{over_t}天)")
        return clip("\n".join(out), 3800)

    # ---------- 执行工具(write级, 需确认) ----------

    def t_run_route_check(sku: str, caliber: str = "action",
                          mode: str = "air") -> str:
        script = full(f"流程复盘/{ROUTE_SCRIPTS.get(caliber, 'route_check.py')}")
        if not script.exists():
            return f"[错误] 脚本不存在: {script}"
        tmp = full("流程复盘/output/agent_tmp")
        tmp.mkdir(parents=True, exist_ok=True)  # 隔离输出, 防覆盖正式汇总
        cmd = [sys.executable, str(script), "--sku", str(sku).upper(),
               "--mode", str(mode), "--out", str(tmp)]
        try:
            r = subprocess.run(cmd, cwd=str(script.parent), capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=600,
                               env=dict(os.environ, PYTHONUTF8="1"))
        except subprocess.TimeoutExpired:
            return "[错误] route_check 执行超时(600s), 建议缩小SKU范围"
        md = full(f"复盘文件/{str(sku).upper()}/route_路线复盘.md")
        body = ""
        if md.exists():
            body = "\n".join(md.read_text("utf-8", errors="replace").splitlines()[-60:])
        return clip(f"exit={r.returncode}\n{r.stdout[-1500:]}\n{r.stderr[-800:]}"
                    f"\n== 单SKU报告尾部({md.name}) ==\n{body}", 6000)

    # ---------- 注册 ----------

    registry.register(Tool(
        "query_batches", "列出 work-main data/ 下所有数据批次(产品线/批次数/Part数/最新时间).",
        {"type": "object", "properties": {}, "required": []},
        "read", t_query_batches))
    registry.register(Tool(
        "get_product_info", "按SKU编号或手机型号查商品主数据(ASIN/品名/型号/状态等, 表头在第2行).",
        {"type": "object", "properties": {
            "keyword": {"type": "string", "description": "SKU编号(如G531)或型号关键词(如Poco X8)"}},
         "required": ["keyword"]}, "read", t_get_product_info))
    registry.register(Tool(
        "query_sku_route",
        "查某SKU的路线复盘现状(卡点Part/停滞天数/三段耗时判定/下一步). "
        "caliber: action=动作映射口径(默认) / system=系统口径, 两套结论不可混用, 引用需注明.",
        {"type": "object", "properties": {
            "sku": {"type": "string", "description": "SKU编号, 如 G531"},
            "caliber": {"type": "string", "enum": ["action", "system"]}},
         "required": ["sku"]}, "read", t_query_sku_route))
    registry.register(Tool(
        "search_kb", "检索 work-main 本地知识库(复盘报告/口径文档/商品知识的BM25索引).",
        {"type": "object", "properties": {
            "query": {"type": "string", "description": "检索词"},
            "top_k": {"type": "integer", "minimum": 1, "description": "返回条数, 默认4"}},
         "required": ["query"]}, "read", t_search_kb))
    registry.register(Tool(
        "sync_status", "查看钉钉审批数据自动同步的最近日志(定时任务是否成功).",
        {"type": "object", "properties": {}, "required": []},
        "read", t_sync_status))
    registry.register(Tool(
        "scan_sync_data",
        "实时扫描自动同步目录的Part*.xlsx(比预警清单新鲜): 状态分布/数据截至时间/"
        "今日有动态的SKU/停滞超期TOP. line默认手机膜(重点), 后续可传手机壳.",
        {"type": "object", "properties": {
            "line": {"type": "string", "enum": ["手机膜", "手机壳"]},
            "stale_days": {"type": "integer", "minimum": 1, "description": "停滞阈值天数, 默认7"},
            "top": {"type": "integer", "minimum": 1, "description": "停滞TOP条数, 默认8"}},
         "required": []}, "read", t_scan_sync_data))
    registry.register(Tool(
        "pipeline_alerts",
        "四层流程信号(权威表口径): 本周新增SKU / 全流程临期(空运60·海运69) / 已超期 / "
        "环节停滞超目标(近似段级). 目标值动态读 时效确认规则表.xlsx; "
        "每条带SKU/型号/卡点Part/剩余天数, 可直接用于催办. line默认手机膜.",
        {"type": "object", "properties": {
            "line": {"type": "string", "enum": ["手机膜", "手机壳"]},
            "warn_days": {"type": "integer", "minimum": 1, "description": "临期阈值天数, 默认15"},
            "top": {"type": "integer", "minimum": 1, "description": "每层列出条数, 默认8"}},
         "required": []}, "read", t_pipeline_alerts))
    registry.register(Tool(
        "run_route_check",
        "对指定SKU重跑路线复盘脚本并返回单SKU报告尾部(写级操作, 输出隔离到agent_tmp). "
        "caliber: action/system; mode: air/sea(后勤运输口径).",
        {"type": "object", "properties": {
            "sku": {"type": "string", "description": "SKU编号, 如 G531"},
            "caliber": {"type": "string", "enum": ["action", "system"]},
            "mode": {"type": "string", "enum": ["air", "sea"]}},
         "required": ["sku"]}, "write", t_run_route_check))
    return 8
