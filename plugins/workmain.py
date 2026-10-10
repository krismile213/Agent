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
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from mini_agent import Tool, clip

DEFAULT_ROOT = r"C:\Users\dell\Desktop\work-main"

# 口径单一真源: 停滞/在途的「历史遗留」封顶线(天), scan_sync_data 与 pipeline_alerts 共用.
# 2026-10-10 由双口径(scan 90 / pipeline 120)统一为 120 —— 90 封顶曾把 91~120 天的
# 审批中真实挂单(Part8 渠道仓到货等, 探针实测 155 条)错标成"历史遗留"挡在 TOP 之外,
# G550 真实停滞 119 天两边都报不出。>120 天(2025 年悬挂的 Part9 老单等)仍排除。
# 改此值请重跑 scripts/probe_stale_cap.py 量化翻转面; 运行时文案/docstring 均插值本常量。
STALE_CAP_DAYS = 120

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


def _s(v) -> str:
    """pandas 单元格 → 干净字符串; nan/None 一律归一成空串(注意 float('nan') 是 truthy)."""
    s = "" if v is None else str(v).strip()
    return "" if s.lower() in ("nan", "nat", "none") else s


def _latest_parts(d):
    """每个环节(Part前缀, 即文件名最后一个'-'之前的部分)只保留 mtime 最新一份.

    背景: 同步是覆盖式更新(写新文件+删旧文件), 但旧文件被 Excel/WPS 打开时
    删除会失败, 目录里同环节新旧两份并存 → 行级统计双倍. 按前缀取最新可免疫.
    """
    best = {}
    for p in sorted(d.glob("Part*.xlsx")):
        if p.name.startswith("~$"):
            continue
        key = p.stem.rsplit("-", 1)[0] if "-" in p.stem else p.stem
        mt = p.stat().st_mtime
        if key not in best or mt >= best[key][0]:
            best[key] = (mt, p)
    return [p for _, p in sorted((v for v in best.values()), key=lambda t: t[1].name)]


def _part_label(stem: str) -> str:
    """'Part2_新品首单下单-20260928090829' → 'Part2 新品首单下单'."""
    return stem.split("-")[0].replace("_", " ")


def _stale_note(fresh_time, warn_days: int = 2) -> str:
    """数据新鲜度硬判(纯函数, 可测): 数据截至距今 > warn_days 天时返回告警行.

    背景: 停滞天数 = 今天 - 表内更新时间. 上游同步一旦断供, 全表更新时间
    集体停在断供前, 停滞/超期天数会静默虚增断供天数 —— 这行告警防误读.
    fresh_time: 各Part表内最大「更新时间」(pd.Timestamp 或 None).
    """
    if fresh_time is None:
        return "⚠️ 数据新鲜度: 表内无有效更新时间, 停滞/预警数字不可信"
    try:
        now = datetime.now()
        lag = (now - datetime(fresh_time.year, fresh_time.month,
                              fresh_time.day)).days
    except Exception:
        return ""
    if lag > int(warn_days):
        return (f"⚠️ 数据已 {lag} 天未更新(截至 {str(fresh_time)[:16]}), "
                f"以下停滞/超期天数含断供期会偏大, 请先确认同步任务是否正常再采信")
    return ""


def _latest_node(record, fallback_ts: str = "") -> str:
    """从「审批记录」取最后一条节点, 输出 操作人「节点名」MM-DD HH:MM.

    记录格式: 多条用 ';' 分隔, 每条内 '|' 分隔 = 操作人|节点|时间|类型(audit/抄送).
    例: '李杰华|全套样品测试完成|2026-09-28 08:59:11|audit|'
    """
    s = _s(record)
    if not s:
        return f"无节点明细(最后更新 {fallback_ts})" if fallback_ts else "无节点明细"
    items = [x.strip() for x in s.split(";") if x.strip()]
    if not items:
        return f"无节点明细(最后更新 {fallback_ts})" if fallback_ts else "无节点明细"
    f = [x.strip() for x in items[-1].split("|")]
    if len(f) >= 3 and f[1]:
        ts = f[2][:16]
        if len(ts) >= 16 and ts[4] == "-":        # 'YYYY-MM-DD HH:MM' → 去掉年份
            ts = ts[5:]
        who = f[0] or "?"
        return f"{who}「{f[1]}」{ts}"
    return items[-1][:50]


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
                parts = _latest_parts(sub)
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
            age_h = (datetime.now().timestamp() - logs[-1].stat().st_mtime) / 3600
            age_txt = f"最新日志 {logs[-1].name} 更新于 {age_h:.1f} 小时前"
            if age_h > 24:
                age_txt += " ⚠️ 超24小时未滚动, 同步任务可能已停摆"
            out.append(age_txt)
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
        files = _latest_parts(d)
        if not files:
            return f"[错误] {d} 下没有 Part*.xlsx"
        today = pd.Timestamp(datetime.now().strftime("%Y-%m-%d"))
        done_vals = {"已结束", "终止"}
        dist_total, stale_rows, today_rows = {}, [], []
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
                plabel = _part_label(f.stem)
                for _, r in df[m].iterrows():
                    today_rows.append((
                        plabel,
                        _s(r.get(sku_col)) or "?",
                        _s(r.get("新机型号")),
                        _s(r.get("审批状态")) or "?",
                        _latest_node(r.get("审批记录"), _s(r.get("更新时间"))[:16]),
                        _s(r.get("当前负责人")),
                    ))
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
                 f"数据截至(最新更新时间): {str(fresh_time)[:16] if fresh_time is not None else '无'}"]
        stale_warn = _stale_note(fresh_time)
        if stale_warn:
            lines.append(stale_warn)  # 数据过旧时预警行紧跟数据截至, 防误读
        lines.append(f"今日有动态: {today_cnt} 单" + ("(明细如下)" if today_rows else ""))
        for pl, sku, model, st, node, owner in today_rows[:8]:
            mid = f" {model}" if model and model != sku else ""
            tail = f" | 当前负责人:{owner}" if owner else ""
            lines.append(f"  · {sku}{mid} @ {pl} | 状态:{st} | 最新节点: {node}{tail}")
        if len(today_rows) > 8:
            lines.append(f"  …另有 {len(today_rows) - 8} 单今日动态未列出")
        stale_rows.sort(reverse=True)
        zombie = [r for r in stale_rows if r[0] > STALE_CAP_DAYS]
        recent = [r for r in stale_rows if r[0] <= STALE_CAP_DAYS]
        lines.append(f"停滞≥{stale_days}天且未完结: {len(stale_rows)} 单 "
                     f"(口径注: 按单据行不聚合, 天数=今天-最后更新时间(不归零); "
                     f">{STALE_CAP_DAYS}天历史遗留 {len(zombie)} 单(多为悬挂老单), 不列入TOP)")
        lines.append(f"近{STALE_CAP_DAYS}天内停滞TOP (口径=距最后更新天数; 同一SKU在多环节会重复出现):")
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
        f"""四层流程信号(权威表口径): 本周新增SKU / 全流程临期 / 已超期 / 环节停滞超目标.
        全流程线: 空运60/海运69(时效确认规则表·口径说明); 启动日=Part1手机数据取得日期
        (缺省最早创建时间); 在途只看Part1~8; >{STALE_CAP_DAYS}天历史遗留不提醒; 段级为近似口径
        (环节内无进展超确认目标), 精确段口径以路线复盘为准."""
        import pandas as pd
        d = root / "data" / line / line
        if not d.is_dir():
            return f"[错误] 同步目录不存在: {d} (可用 query_batches 查看可用批次)"
        files = _latest_parts(d)
        if not files:
            return "[错误] 无Part文件"
        today = pd.Timestamp(datetime.now().strftime("%Y-%m-%d"))
        monday = today - pd.Timedelta(days=today.weekday())
        targets = _part_targets()
        modes = _sku_modes()
        FULL = {"air": 60, "sea": 69}
        skus = {}   # sku -> {model, start, pending:set, created, last_up}
        seg_rows = []  # (超目标天数, idle, sku, part, target)
        part9_new = []  # (创建时间, sku, model, owner) Part9新单待办: 创建≤7天且审批中
        fresh_time = None  # 全目录最新「更新时间」, 用于数据新鲜度硬判
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
            if updated is not None:
                last = updated.max()
                if pd.notna(last) and (fresh_time is None or last > fresh_time):
                    fresh_time = last  # 全目录最新「更新时间」= 数据截至
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
                        if tgt < idle <= STALE_CAP_DAYS:
                            seg_rows.append((idle - tgt, idle, sku, part, tgt))
                elif part_no == "9" and is_pending and c is not None and pd.notna(c):
                    # 盲区补丁(2026-10-08): Part9 不进在途/段级口径(历史悬挂单全是噪声),
                    # 但新落下的单没人盯 —— 只报创建≤7天的审批中单, 悬挂旧单忽略
                    if (today - c.normalize()).days <= 7:
                        owner = str(r.get("当前负责人", "") or "").strip()
                        if owner.lower() == "nan":
                            owner = ""
                        part9_new.append((c, sku, mv, owner))

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
            if used > STALE_CAP_DAYS:
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
               f"临期阈值{warn_days}天, 在途=Part1~8审批中, >{STALE_CAP_DAYS}天遗留不提醒; "
               f"段级停滞天数=今天-最后更新时间(归零), 对照该环节确认目标(近似, 按单据行可重复; "
               f"注意与scan_sync_data的停滞口径差异: 归零差~1天(封顶线已统一为{STALE_CAP_DAYS}天); "
              f"Part9新单=创建≤7天且审批中, 悬挂旧单不提醒)"]
        stale_warn = _stale_note(fresh_time)
        if stale_warn:
            out.append(stale_warn)  # 数据过旧时先于一切预警, 防断供虚增天数被误读
        out.append(f"数据截至(最新更新时间): {str(fresh_time)[:16] if fresh_time is not None else '无'}")
        out.append(f"\n== 本周新增SKU({monday:%m-%d}起, {len(new_week)}个) ==")
        for c, sku, mv in new_week[:15]:
            out.append(f"  {sku} {mv} (创建 {c:%m-%d})")
        out.append(f"\n== 临期预警(剩≤{warn_days}天, {len(warn_list)}个) ==")
        for left, sku, mv, stuck, s, tag in warn_list[:n]:
            out.append(f"  {sku} {mv} 剩{left}天[{tag}] (启动{s:%m-%d}, 卡{stuck})")
        out.append(f"\n== 已超期(超全流程线, {len(over_list)}个; 另历史遗留{legacy}个不列) ==")
        for over, sku, mv, stuck, s, tag in over_list[:n]:
            out.append(f"  {sku} {mv} 已超{over}天[{tag}] (启动{s:%m-%d}, 卡{stuck})")
        out.append(f"\n== 环节停滞超目标(段级口径: 距最后更新vs环节目标, {len(seg_rows)}单, 行级可重复) TOP ==")
        for over_t, idle, sku, part, tgt in seg_rows[:n]:
            out.append(f"  {sku} {part} 停滞{idle}天/目标{tgt}天 (超{over_t}天)")
        part9_new.sort(key=lambda x: x[0])          # 最老的新单排前, 先到先办
        out.append(f"\n== Part9新单待办(创建≤7天且审批中, {len(part9_new)}单; 悬挂旧单不在此列) ==")
        for c9, sku9, mv9, owner9 in part9_new[:10]:
            out.append(f"  {sku9} {mv9} (创建 {c9:%m-%d %H:%M}, 当前负责人: {owner9 or '—'})")
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
        "今日有动态明细(每条给 SKU·型号·所在Part·审批状态·最新审批节点+操作人+时间·当前负责人)"
        "/停滞超期TOP. line默认手机膜(重点), 后续可传手机壳.",
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
    return 8 + _register_platform(registry, cfg)


# ---------- platform_* · 平台后端只读直连（「AI 助手嵌入」任务书 T06, 2026-10-09） ----------
# 唯一真源 = 平台后端 API(:8000), 禁止读 xlsx 副本 —— 否则与平台页面出现两份实现口径漂移。
# ⚠️ 硬性要求 trust_env=False: 本机环境变量常驻 HTTP_PROXY=http://127.0.0.1:<随机端口>,
#    requests 默认读环境变量代理 → 打给 127.0.0.1 的请求被丢进本机代理 →
#    连不上/被代理 502(表现像"平台没起", 极难排查; 任务书 §5.1 规则4 实测坑)。

try:
    import requests as _requests
except Exception:                       # 环境缺 requests 时工具仍可注册, 调用才降级
    _requests = None

_PLAT_SESSION = None

# 脱敏关键词(小写比较): 命中即整键剔除 —— Agent 是数据上云的最后一跳,
# 平台侧(T10)会脱敏, 这里再脱一遍做双保险(任务书 §5.3)。
_SENS_SUBSTR = ("负责人", "操作人", "创建人", "姓名", "手机", "电话", "联系方式",
                "备注", "owner", "creator", "operator", "phone", "mobile", "remark")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


def _plat_session():
    """复用的平台 HTTP session(全局一个, trust_env=False 只设一次)。"""
    global _PLAT_SESSION
    if _PLAT_SESSION is None:
        s = _requests.Session()
        s.trust_env = False             # 绕过本机代理 —— 硬性要求
        _PLAT_SESSION = s
    return _PLAT_SESSION


def _plat_sanitize(obj):
    """字段级白名单 + 11 位手机号掩码(138****8000)。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if any(s in kl for s in _SENS_SUBSTR):
                continue
            out[str(k)] = _plat_sanitize(v)
        return out
    if isinstance(obj, list):
        return [_plat_sanitize(x) for x in obj]
    if isinstance(obj, str):
        return _PHONE_RE.sub(lambda m: m.group(0)[:3] + "****" + m.group(0)[7:], obj)
    return obj


def _plat_get(base: str, path: str, params: dict | None = None):
    """平台后端 GET。任何失败都返回 (None, 人话错误文本) —— 降级不抛异常,
    模型拿着错误文本可自愈(提示用户稍后重试/改用平台页面), 任务不中断。"""
    if _requests is None:
        return None, "[平台接口不可用] 运行环境缺少 requests, 无法调用平台后端。"
    url = f"{base}/api/{str(path).strip('/')}"
    try:
        r = _plat_session().get(url, params=params or {}, timeout=(3, 30))
    except Exception as e:
        return None, (f"[平台接口不可用] {type(e).__name__}: {e}；"
                      "请确认平台后端已启动(数据后台系统 start.py), 稍后重试。")
    if r.status_code != 200:
        return None, (f"[平台接口不可用] HTTP {r.status_code} ← {path}；"
                      "请稍后重试或改用平台页面确认。")
    try:
        return r.json(), ""
    except Exception:
        return None, "[平台接口不可用] 响应不是 JSON, 平台后端版本可能不匹配。"


def _plat_brief(obj, indent: int = 0, lines: list | None = None,
                budget: int = 72) -> list:
    """平台 JSON → 模型易读的缩进键值/列表文本, 控制总行数(budget)。"""
    if lines is None:
        lines = []
    pad = "  " * indent
    if len(lines) >= budget:
        lines.append(pad + "…(截断)")
        return lines
    if isinstance(obj, dict):
        for k, v in obj.items():
            if len(lines) >= budget:
                lines.append(pad + "…(截断)")
                break
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:")
                _plat_brief(v, indent + 1, lines, budget)
            else:
                lines.append(f"{pad}{k}: {v if v is not None else '—'}")
    elif isinstance(obj, list):
        for x in obj[:48]:
            if len(lines) >= budget:
                lines.append(pad + f"…(截断)")
                break
            if isinstance(x, dict):
                flat = " | ".join(f"{k}={v}" for k, v in x.items()
                                  if not isinstance(v, (dict, list)))
                lines.append(f"{pad}- {flat}" if flat else f"{pad}- (条目)")
                for k, v in x.items():
                    if isinstance(v, (dict, list)) and v:
                        lines.append(f"{pad}  {k}:")
                        _plat_brief(v, indent + 2, lines, budget)
            else:
                lines.append(f"{pad}- {x}")
        else:
            pass
    return lines


def _register_platform(registry, cfg: dict) -> int:
    """注册 10 个 platform_* 只读工具(全部 level=read; 平台写接口一律不接)。"""
    base = str(cfg.get("platform_root") or "http://127.0.0.1:8000").rstrip("/")
    CAT = {"type": "string", "enum": ["手机膜", "手机壳"],
           "description": "品类, 缺省=上下文当前品类或手机膜"}

    def _out(obj, err=""):
        if obj is None:
            return err
        return clip("\n".join(_plat_brief(_plat_sanitize(obj))), 3800)

    def t_platform_batches(category=None) -> str:
        obj, err = _plat_get(base, "batches", {"category": category or ""})
        return _out(obj, err)

    def t_platform_overview(batch: str, category=None) -> str:
        obj, err = _plat_get(base, f"overview/{batch}", {"category": category or ""})
        return _out(obj, err)

    def t_platform_sku_route(sku: str, caliber=None, category=None) -> str:
        obj, err = _plat_get(base, f"review/sku/{sku}",
                             {"category": category or "", "caliber": caliber or ""})
        return _out(obj, err)

    def t_platform_alerts(category=None, priority=None) -> str:
        obj, err = _plat_get(base, "alerts", {"category": category or ""})
        if obj is None:
            return err
        if priority:                     # 平台接口不认 priority, 在工具层按分组键过滤
            pk = str(priority).lower()
            picked = {k: v for k, v in obj.items() if isinstance(v, (dict, list))
                      and pk in str(k).lower()}
            if picked:
                obj = picked
        return _out(obj)

    def t_platform_transport(category=None) -> str:
        obj, err = _plat_get(base, "transport", {"category": category or ""})
        return _out(obj, err)

    def t_platform_records(batch: str, part: str, page=None, pageSize=None,
                           approvalNo=None, status=None, keyword=None,
                           category=None) -> str:
        q = {"category": category or ""}
        for k, v in (("page", page), ("pageSize", pageSize),
                     ("approvalNo", approvalNo), ("status", status),
                     ("keyword", keyword)):
            if v not in (None, ""):
                q[k] = v
        obj, err = _plat_get(base, f"records/{batch}/{part}", q)
        return _out(obj, err)

    def t_platform_inflight_overview(category=None) -> str:
        obj, err = _plat_get(base, "inflight", {"category": category or ""})
        return _out(obj, err)

    def t_platform_inflight_alerts(category=None) -> str:
        obj, err = _plat_get(base, "inflight/alerts", {"category": category or ""})
        return _out(obj, err)

    def t_platform_daily_report(category=None) -> str:
        # 只读列表; 平台的生成(POST)与下载接口一律不调(任务书 §7 边界)
        obj, err = _plat_get(base, "inflight/daily-report", {"category": category or ""})
        return _out(obj, err)

    def t_platform_deltas(days=None, sku=None, category=None) -> str:
        q = {"category": category or ""}
        if days not in (None, ""):
            q["days"] = days
        if sku not in (None, ""):
            q["sku"] = sku
        obj, err = _plat_get(base, "deltas", q)
        return _out(obj, err)

    reg = registry.register
    reg(Tool(
        "platform_batches",
        "列出平台当前品类的所有数据批次(平台后端唯一真源)。"
        "问题涉及'有哪些批次/换批次'时用。",
        {"type": "object", "properties": {"category": CAT}, "required": []},
        "read", t_platform_batches))
    reg(Tool(
        "platform_overview",
        "某批次的平台总览 KPI(单量/时效/异常概览)。'这个批次整体怎么样'时用。",
        {"type": "object", "properties": {"batch": {"type": "string"},
                                          "category": CAT},
         "required": ["batch"]},
        "read", t_platform_overview))
    reg(Tool(
        "platform_sku_route",
        "某 SKU 在平台的路线达标/复盘现状(各 Part 实际vs目标、卡点、三部门终节点)。"
        "用户问'某SKU卡在哪/进度如何'时用。与平台 SKU档案/路线达标页同源。",
        {"type": "object", "properties": {
            "sku": {"type": "string", "description": "SKU编号, 如 G531"},
            "caliber": {"type": "string", "enum": ["action", "system"],
                        "description": "复盘口径, 默认 action"},
            "category": CAT},
         "required": ["sku"]},
        "read", t_platform_sku_route))
    reg(Tool(
        "platform_alerts",
        "平台加急预警明细(P0/P1/P2 + 盯数清单 + 数据新鲜度)。"
        "用户问'今天加急/最急的单'时用。与平台加急预警页同源。",
        {"type": "object", "properties": {
            "category": CAT,
            "priority": {"type": "string", "enum": ["P0", "P1", "P2"],
                         "description": "可选, 只看某一优先级"}},
         "required": []},
        "read", t_platform_alerts))
    reg(Tool(
        "platform_transport",
        "平台货件运输看板(逐票判定 + 渠道/运输方式聚合)。运输闭环/海运空运相关问答用。",
        {"type": "object", "properties": {"category": CAT}, "required": []},
        "read", t_platform_transport))
    reg(Tool(
        "platform_records",
        "平台明细分页查询(按批次+Part, 可按审批号/状态/关键词过滤)。"
        "'找某张单/某状态单据'时用。返回不含创建人/责任人(脱敏)。",
        {"type": "object", "properties": {
            "batch": {"type": "string"}, "part": {"type": "string"},
            "page": {"type": "integer", "minimum": 1},
            "pageSize": {"type": "integer", "minimum": 1, "maximum": 200},
            "approvalNo": {"type": "string"}, "status": {"type": "string"},
            "keyword": {"type": "string"}, "category": CAT},
         "required": ["batch", "part"]},
        "read", t_platform_records))
    reg(Tool(
        "platform_inflight_overview",
        "平台事中总览: 在研 SKU 清单 + KPI(当前停留环节/距超时)。"
        "'在研有哪些/事中整体情况'时用。",
        {"type": "object", "properties": {"category": CAT}, "required": []},
        "read", t_platform_inflight_overview))
    reg(Tool(
        "platform_inflight_alerts",
        "平台事中风险预警: 当日新超期/持续超期/临近超期(85%)。"
        "用户问'哪些在研要超期'时用。",
        {"type": "object", "properties": {"category": CAT}, "required": []},
        "read", t_platform_inflight_alerts))
    reg(Tool(
        "platform_daily_report",
        "列出平台已生成的事中日报(按日期, 只读)。不生成、不下载。",
        {"type": "object", "properties": {"category": CAT}, "required": []},
        "read", t_platform_daily_report))
    reg(Tool(
        "platform_deltas",
        "平台单据增量(未闭环 SKU 新增单据追踪)。"
        "用户问'这周多了哪些单据/最近变化'时用。",
        {"type": "object", "properties": {
            "days": {"type": "integer", "minimum": 1,
                     "description": "统计区间天数, 默认30"},
            "sku": {"type": "string", "description": "可选, 限定某 SKU"},
            "category": CAT},
         "required": []},
        "read", t_platform_deltas))
    return 10
