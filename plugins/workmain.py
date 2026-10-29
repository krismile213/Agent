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

# 口径单一真源: 「新增SKU」的观察窗口(天, 滚动) —— pipeline_alerts 的「新增」层用。
# 2026-10-14 由「本周(周一起)」改为「最近一个月(今天往前30天)」: 周窗口只看 0~7 天太窄,
# 漏掉近一月内新增却仍卡在签样/在途的 SKU。
# 2026-10-15 由 30 扩到 40: 覆盖「取数→签样(膜12天)」+ 后续推进, 避免窗口边缘刚签样就掉出。
NEW_SKU_WINDOW_DAYS = 40

# 签样节点口径(单一真源, 与数据后台 seg_rules 对齐): 未签样且超过目标天数即标 ⚠️。
#   手机膜: 取数(Part1 手机数据取得日期) → 签样, 目标 12 天(膜 SEGS「取数→签样」)。
#   手机壳: 提案(Part1 创建时间) → 签样, 目标 87 天(壳无取数节点; 87=原膜 75+12 名义值,
#           膜口径已于 2026-10-19 把「提案→取数」75 收紧为 1，壳的 87 待业务重校准)。
# 判定锚点由数据驱动: info["start"] 有值(膜有取数列)走膜的锚点, 否则走壳的(创建时间)。
SIGN_TARGET_FILM = 12
SIGN_TARGET_SHELL = 87

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


def _is_sign_col(name: str) -> bool:
    """列名是否为「签样时间」类: 膜=Part2「全套样品测试完成时间」,
    壳=Part1/2「三方确样时间」「手机壳确样日期/时间」等(与数据后台 NODES['签样'] 同源)."""
    n = str(name)
    return ("签样" in n) or ("确样" in n) or ("样品" in n and "完成" in n)


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


def _part8_shipments(part_files) -> dict:
    """Part8(到货)表 → {sku: [(到货渠道仓(地区), 货件号), ...]} 去重保序。

    供 pipeline_alerts 给临期/超期条目标注「地区 + 货件号」。缺文件/缺列一律返回 {}。
    """
    import pandas as pd
    f8 = next((f for f in part_files if f.stem.startswith("Part8")), None)
    if f8 is None:
        return {}
    try:
        df = pd.read_excel(f8, dtype=str)
    except Exception:
        return {}
    cols = {str(c).strip(): c for c in df.columns}
    sku_col = next((c for c in df.columns if "SKU" in str(c).upper()), None)
    c_site, c_ship = cols.get("到货渠道仓"), cols.get("货件号")
    if sku_col is None or (c_site is None and c_ship is None):
        return {}
    out: dict = {}
    for _, r in df.iterrows():
        sku = str(r.get(sku_col, "")).strip()
        if not sku or sku.lower() == "nan":
            continue
        site = str(r.get(c_site, "")).strip() if c_site is not None else ""
        no = str(r.get(c_ship, "")).strip() if c_ship is not None else ""
        site = "" if site.lower() == "nan" else site
        no = "" if no.lower() == "nan" else no
        if not site and not no:
            continue
        pair = (site, no)
        lst = out.setdefault(sku, [])
        if pair not in lst:
            lst.append(pair)
    return out


def _ship_tag(ship_map: dict, sku: str, limit: int = 3) -> str:
    """把某 SKU 的 (地区, 货件号) 列表渲染成一行标注; 超过 limit 个折叠为「共N个」。"""
    lst = ship_map.get(sku)
    if not lst:
        return ""
    shown = "；".join(" ".join(p for p in pair if p) for pair in lst[:limit])
    more = f" 共{len(lst)}个" if len(lst) > limit else ""
    return f" [到货: {shown}{more}]"


def _stage_of(info: dict, targets: dict, modes: dict, sku: str, today) -> str:
    """已签样 SKU 的「最新状态」: 当前环节 + 已持续N天 + 时效M天; 无在途段则「已闭环」。"""
    import pandas as pd
    pend = info.get("pending") or set()
    if not pend:
        return "已闭环"
    part = "Part" + max(pend)
    tgt = targets.get(part, 1)
    if part == "Part8" and modes.get(sku) == "sea":
        tgt = 35
    last = info.get("last_up")
    if last is None or pd.isna(last):
        return f"现{part} 时效{tgt}天"
    days = int((today - last.normalize()).days)
    return f"现{part} 已持续{days}天/时效{tgt}天"


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
            return (f"[错误] 商品资料文件不存在: {f}。"
                    "处理: 检查 config.json 的 workmain_root 是否指向正确目录; "
                    "不要猜 SKU 信息, 直接告诉用户「商品资料库不可用」。")
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

    def _sync_conclusion(log_path) -> str:
        """从同步日志解析结论行: ✅成功/❌失败/⏳进行中. 防 LLM 脑补故障原因."""
        import re as _re
        try:
            lines = log_path.read_text("utf-8", errors="replace").splitlines()
        except OSError:
            return ("[错误] 同步日志不可读(钉钉同步任务可能没在本机跑过)。"
                    "处理: 改用 scan_sync_data 看数据本身的新鲜度; "
                    "向用户说明「查不到同步日志」, 不要编造同步状态。")
        if not lines:
            return "⚠️ 日志为空, 无法判定"
        err_lines = [l for l in lines if "ERROR" in l or "Traceback" in l]
        ends = [l for l in lines if "同步结束" in l]
        age_min = (datetime.now().timestamp() - log_path.stat().st_mtime) / 60
        if not ends and age_min < 15:
            return ("⏳ 同步可能仍在进行: 日志 %.0f 分钟前刚更新, 尚无'同步结束'行, "
                    "暂勿判定成败" % age_min)
        if not ends:
            return "❌ 日志无'同步结束'行(疑似中断), 需人工查看日志尾部"
        last = ends[-1]
        m = _re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", last)
        ts = m.group(1) if m else "时间未知"
        mf = _re.search(r"导出 (\d+) 个文件", last)
        files = mf.group(1) if mf else "?"
        if err_lines:
            return (f"❌ 最近同步 {ts} 有 {len(err_lines)} 条 ERROR "
                    f"(末条: {err_lines[-1][:120]}), 需人工排查")
        # 成功场次里若有 ERROR 也如实说
        return f"✅ 最近同步成功: {ts} (导出{files}个文件, 全程无 ERROR)"

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
            out.append("[结论] " + _sync_conclusion(logs[-1]))
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
            return (f"[错误] {d} 下没有 Part*.xlsx。"
                    "处理: 多半是今天的同步任务还没跑完 —— ① 先看 sync_status 确认同步状态; "
                    "② 明确告诉用户「数据尚未同步, 暂无今日数据」, 不要拿历史数据冒充今天; "
                    "③ 如需近期情况, 用 inflight_report 读最近一份事中日报并注明日期。")
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
        f"""四层流程信号(权威表口径): 最近{NEW_SKU_WINDOW_DAYS}天新增SKU(含签样追踪: 已签样给
        「签样日+当前环节/已持续/时效」, 未签样给龄期并超目标标⚠️) / 全流程临期 / 已超期 /
        环节停滞超目标. 临期与超期条目附 Part8「到货渠道仓(地区)+货件号」.
        全流程线: 空运60/海运69(时效确认规则表·口径说明); 启动日=Part1手机数据取得日期
        (缺省最早创建时间); 在途只看Part1~8; >{STALE_CAP_DAYS}天历史遗留不提醒; 段级为近似口径
        (环节内无进展超确认目标), 精确段口径以路线复盘为准."""
        import pandas as pd
        d = root / "data" / line / line
        if not d.is_dir():
            return f"[错误] 同步目录不存在: {d} (可用 query_batches 查看可用批次)"
        files = _latest_parts(d)
        if not files:
            return ("[错误] 无Part文件(同步目录里还没有 Part*.xlsx)。"
                    "处理: ① 先看 sync_status 确认同步是否跑过; "
                    "② 直接告诉用户「数据尚未同步」, 不要用历史数据冒充今天; "
                    "③ 近期情况可读 inflight_report(昨日事中日报), 引用时注明日期。")
        today = pd.Timestamp(datetime.now().strftime("%Y-%m-%d"))
        since = today - pd.Timedelta(days=NEW_SKU_WINDOW_DAYS)
        targets = _part_targets()
        modes = _sku_modes()
        ship = _part8_shipments(files)   # sku -> [(到货渠道仓, 货件号)] 供临期/超期标注
        FULL = {"air": 60, "sea": 69}
        skus = {}   # sku -> {model, start, pending:set, created, last_up, sign}
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
            sign_col = next((c for c in df.columns if _is_sign_col(str(c))), None)
            signs = (pd.to_datetime(df[sign_col], errors="coerce")
                     if sign_col else None)
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
                                             "last_up": None, "sign": None})
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
                if signs is not None:
                    sg = signs.iloc[i]
                    # 签样取最早一次(重复列/多轮确认时以首次签样为准)
                    if sg is not None and pd.notna(sg) and (info["sign"] is None or sg < info["sign"]):
                        info["sign"] = sg
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

        new_skus = sorted((info["created"], sku, info["model"], info["sign"])
                          for sku, info in skus.items()
                          if info["created"] is not None and info["created"] >= since)
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
        out.append(f"\n== 最近{NEW_SKU_WINDOW_DAYS}天新增SKU({since:%m-%d}~{today:%m-%d}, "
                   f"{len(new_skus)}个; 含签样追踪) ==")
        wait_sign = []          # (未签样龄, sku) 供汇总
        for c, sku, mv, sign in new_skus[:15]:
            if sign is not None and pd.notna(sign):
                # 已签样: 不再只给日期, 追踪「当前环节 + 已持续 + 时效」(用户 2026-10-15 要求)
                st = _stage_of(skus[sku], targets, modes, sku, today)
                out.append(f"  {sku} {mv} (创建 {c:%m-%d}, 签样 {sign:%m-%d}, {st})")
                continue
            # 未签样: 锚点=取数(膜有手机数据取得日期)/创建(缺取数列时, 壳亦如此);
            # 目标 12 天(取数→签样)/87 天(提案→签样, 87=原膜 75+12 名义值·待重校准) —— 超目标标 ⚠️
            info = skus[sku]
            has_qs = info["start"] is not None
            anchor = info["start"] if has_qs else c
            tgt = SIGN_TARGET_FILM if has_qs else SIGN_TARGET_SHELL
            age = int((today - anchor.normalize()).days)
            flag = "⚠️" if age > tgt else ""
            out.append(f"  {sku} {mv} (创建 {c:%m-%d}, {flag}未签样 {age}天/目标{tgt}天)")
            wait_sign.append((age, sku))
        if wait_sign:
            wait_sign.sort(reverse=True)
            out.append(f"  未签样待跟进 {len(wait_sign)} 个(锚点: 取数/目标12天·或创建/目标87天); "
                       f"最久 {wait_sign[0][1]}({wait_sign[0][0]}天)")
        out.append(f"\n== 临期预警(剩≤{warn_days}天, {len(warn_list)}个) ==")
        for left, sku, mv, stuck, s, tag in warn_list[:n]:
            out.append(f"  {sku} {mv} 剩{left}天[{tag}] (启动{s:%m-%d}, 卡{stuck})"
                       + _ship_tag(ship, sku))
        out.append(f"\n== 已超期(超全流程线, {len(over_list)}个; 另历史遗留{legacy}个不列) ==")
        for over, sku, mv, stuck, s, tag in over_list[:n]:
            out.append(f"  {sku} {mv} 已超{over}天[{tag}] (启动{s:%m-%d}, 卡{stuck})"
                       + _ship_tag(ship, sku))
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

    # ---------- 事中日报(平台产物 xlsx, 本地只读) ----------

    def t_inflight_report(line: str = "手机膜", date=None) -> str:
        f"""读平台产物《事中日报》xlsx(数据后台系统每日 09:00 生成) → 结构化摘要。

        纯本地 pandas 只读, 不依赖平台后端在线 → level=read, 无人值守晨/午报
        注册表(build_readonly_registry 留 read 剔 write)自动保留, 可进定时链路。

        输出: 在研SKU状态总表(状态/当前停留/已停留/目标/距超时/预警段/核心原因)
              + 超期明细 + 风险预警 + 异常上报清单 + 豁免备查(SKU级, 不带原因原文)。
        date 缺省读最新一份; 表头在第3行(row0=标题, row1=空), 与平台
        reporting.py 写入结构一致。

        口径(引用时必须注明来源): 环节名是**平台侧 segment**(如 素材加工→广告),
        与 scan_sync_data / pipeline_alerts 的 **Part 编号**口径不是一套,
        同一SKU的两个数字可能不一致; 当天上游未同步时日报数字会停在上一场。

        脱敏纪律(与平台 API 对齐): xlsx 的「责任人」列是真实姓名, 而平台
        outbound_guard 词典没有这一条、API 也从不返回该字段 —— 本工具只读所需列,
        **从不读 责任人 / 豁免原因**, 自由文本(型号/核心原因)过手机号掩码。
        甘特图是 227 列宽表, 不做文本化(看图去平台页面), 本工具不解析。
        """
        import pandas as pd
        reports = (root / "data" / str(line) / str(line) / "reports").resolve()
        try:
            reports.relative_to(root.resolve())   # 防 line 参数路径逃逸
        except ValueError:
            return f"[错误] 非法品类名: {line!r} (仅支持 手机膜/手机壳)"
        if not reports.is_dir():
            return (f"[错误] 事中日报目录不存在: {reports}\n"
                    "事中日报由数据后台系统每日 09:00 生成; 今天尚未生成时可用 "
                    "date 指定更早日期, 或用 platform_daily_report 查已有清单。")
        cands = sorted(reports.glob("事中日报_*.xlsx"))
        if not cands:
            return (f"[错误] {reports} 下没有 事中日报_*.xlsx"
                    "(平台每日 09:00 生成; 上游未同步时会缺当天一份)")

        # 撞锁改名约定（平台侧 2026-10-20）：Excel/WPS 占用正式名时，平台会
        # 写真名_HHMMSS 后缀落盘。同一天可能两份：正式名(可能是旧的) + 后缀名(新)。
        # 选文件必须「先 8 位日期、同日期比 mtime」——若按文件名末段比大小，
        # "20261020" > "1530" 恒成立，被占用写不进去的旧正式名会赢，简报静默
        # 读到旧数据。日期从 stem 里抓 8 位数字，后缀名/正式名一视同仁。
        def _stamp(p):
            m = re.search(r"(\d{8})", p.stem)
            return m.group(1) if m else ""

        def _recency(p):
            return (p.stat().st_mtime, p.name)

        if date not in (None, ""):
            digits = re.sub(r"\D", "", str(date))
            avail = ", ".join(re.sub(r"^事中日报_", "", p.stem)
                              for p in cands[-8:])
            if len(digits) != 8:
                return (f"[错误] date 需为 YYYYMMDD 或 YYYY-MM-DD, 收到 {date!r}; "
                        f"不传则读最新一份。已有(最近8份): {avail}")
            hit = [p for p in cands if _stamp(p) == digits]
            if not hit:
                return (f"[错误] 没有 {line} {digits[:4]}-{digits[4:6]}-{digits[6:]} "
                        f"的事中日报。已有(最近8份): {avail}; 不传 date 读最新一份。")
            f = max(hit, key=_recency)
        else:
            f = max(cands, key=lambda p: (_stamp(p),) + _recency(p))

        try:
            xl = pd.ExcelFile(f)
        except Exception as e:
            return f"[错误] 打不开 {f.name}: {e} (文件可能正被 Excel/WPS 占用)"

        def _sheet(sn):
            """header=2: row0 标题 / row1 空 / row2 表头(平台写入结构, 勿改)。"""
            try:
                return pd.read_excel(xl, sheet_name=sn, header=2, dtype=str)
            except Exception:
                return None     # 旧版日报可能缺某张表 → 跳过该节, 不抛异常

        def _rows(df):
            """有效数据行: 丢全空行与「（无预警段）」一类占位行。"""
            if df is None or df.empty:
                return []
            keep = []
            for _, r in df.iterrows():
                vals = [_s(v) for v in r.tolist()]
                if any(vals) and not vals[0].startswith("（无"):
                    keep.append(r)
            return keep

        def _num(v):
            try:
                return int(float(_s(v)))
            except (TypeError, ValueError):
                return -1

        def _free(v):
            """自由文本: 只过手机号掩码(_plat_sanitize 对 str 仅掩码, 不动业务内容)。"""
            return _plat_sanitize(_s(v))

        try:
            t0 = pd.read_excel(xl, sheet_name=0, header=None, nrows=1, dtype=str)
            title = _free(t0.iloc[0, 0])
        except Exception:
            title = ""
        # day 兜底自拼：与选文件同口径抓 8 位日期（撞锁改名的 _HHMMSS 后缀
        # 不能被当成日期）。标题行(row0)自带日期，这里只在缺标题时兜底。
        m8 = re.search(r"(\d{8})", f.stem)
        d8 = m8.group(1) if m8 else f.stem.rsplit("_", 1)[-1]
        day = f"{d8[:4]}-{d8[4:6]}-{d8[6:]}" if len(d8) == 8 else d8
        # 标题行(row0)自带「品类·日期·生成时间」; 缺标题才自拼(旧版日报可能没有)
        out = [title or f"流程事中日报 · {line} · {day}",
               f"来源: data/{line}/{line}/reports/{f.name} (平台每日09:00生成; "
               "当天上游未同步则数字停在上一场)",
               "口径注: 环节名为平台侧segment口径(如 素材加工→广告), 与 scan_sync_data/"
               "pipeline_alerts 的Part编号口径不同, 同一SKU数字可能不一致, 引用请注明来源。"]

        # ① 在研SKU状态总表 —— 超期优先, 同状态按已停留降序
        rows = _rows(_sheet("在研SKU状态总表"))
        if rows:
            dist = {}
            for r in rows:
                k = _s(r.get("状态")) or "?"
                dist[k] = dist.get(k, 0) + 1
            rows.sort(key=lambda r: (0 if _s(r.get("状态")) == "超期" else 1,
                                     -_num(r.get("已停留(天)"))))
            out.append("")
            out.append(f"— 在研状态总表 ({len(rows)} SKU: "
                       + " / ".join(f"{k}{v}" for k, v in sorted(dist.items())) + ") —")
            for r in rows[:30]:
                sku = _s(r.get("SKU"))
                model = _free(r.get("型号"))
                mid = f" {model}" if model and model != sku else ""
                seg = _s(r.get("当前停留环节")) or "?"
                dept = _s(r.get("部门"))
                tail = []
                left = _s(r.get("距超时(天)"))
                if left and left != "—":
                    tail.append(f"距超时:{left}")
                warn = _s(r.get("预警段数"))
                if warn and warn != "0":
                    tail.append(f"预警段{warn}")
                multi = _s(r.get("多轮提交"))
                if multi and multi != "—":
                    tail.append(f"多轮:{multi}")
                cause = _free(r.get("核心原因"))
                if cause and cause != "—":
                    tail.append(f"原因:{cause}")
                # 人工登记（2026-10-15）：跟着日期走的在研备注（真源=
                # 资产文件\事中日报人工登记.xlsx）。「暂时不弄了」这类是催办前必读的
                # 业务决策，必须带给模型 —— 但它会影响"要不要催"的判断。
                note = _free(r.get("人工登记"))
                if note:
                    tail.append(f"人工登记:{note}")
                out.append(f"  {sku}{mid} | {_s(r.get('状态')) or '?'} | "
                           f"{seg}({dept}) 已{_s(r.get('已停留(天)'))}/"
                           f"目标{_s(r.get('目标(天)'))}天"
                           + (" | " + " | ".join(tail) if tail else ""))
            if len(rows) > 30:
                out.append(f"  …另有 {len(rows) - 30} 个SKU未列出"
                           "(全量去平台「事中日报」页)")

        # ② 超期明细
        rows = _rows(_sheet("超期明细"))
        if rows:
            rows.sort(key=lambda r: -_num(r.get("超期(天)")))
            out.append("")
            out.append(f"— 超期明细 ({len(rows)} 段; 进行中且已进行>目标) —")
            for r in rows[:15]:
                out.append(f"  {_s(r.get('SKU'))} {_s(r.get('部门'))} "
                           f"{_s(r.get('环节'))} 已进行{_s(r.get('已进行(天)'))}/"
                           f"目标{_s(r.get('目标(天)'))}天 → "
                           f"超期{_s(r.get('超期(天)'))}天")
            if len(rows) > 15:
                out.append(f"  …另有 {len(rows) - 15} 段")

        # ③ 风险预警
        rows = _rows(_sheet("风险预警"))
        out.append("")
        if rows:
            rows.sort(key=lambda r: -_num(r.get("已进行(天)")))
            out.append(f"— 风险预警 ({len(rows)} 段; 已进行>目标×预警系数, 未到目标) —")
            for r in rows[:15]:
                out.append(f"  {_s(r.get('SKU'))} {_s(r.get('部门'))} "
                           f"{_s(r.get('环节'))} 已进行{_s(r.get('已进行(天)'))}/"
                           f"目标{_s(r.get('目标(天)'))}天 "
                           f"(预警线{_s(r.get('预警线(天)'))}天)")
            if len(rows) > 15:
                out.append(f"  …另有 {len(rows) - 15} 段")
        else:
            out.append("— 风险预警 (本次无预警段) —")

        # ④ 异常上报清单
        rows = _rows(_sheet("异常上报清单"))
        if rows:
            dist = {}
            for r in rows:
                k = _s(r.get("类型")) or "?"
                dist[k] = dist.get(k, 0) + 1
            rows.sort(key=lambda r: -_num(r.get("超期(天)")))
            out.append("")
            out.append(f"— 异常上报 ({len(rows)} 条: "
                       + " / ".join(f"{k}{v}" for k, v in sorted(dist.items())) + ") —")
            for r in rows[:15]:
                out.append(f"  [{_s(r.get('类型'))}] {_s(r.get('SKU'))} "
                           f"{_s(r.get('部门'))} {_s(r.get('环节'))} "
                           f"已进行{_s(r.get('已进行(天)'))}天/"
                           f"超期{_s(r.get('超期(天)'))}天")
            if len(rows) > 15:
                out.append(f"  …另有 {len(rows) - 15} 条")

        # ⑤ 豁免备查 —— 只到 SKU 级(豁免类型); 「原因」列含登记人姓名, 与平台 API
        #    一致不返回, 要看全量去平台页面。
        rows = _rows(_sheet("豁免备查"))
        out.append("")
        if rows:
            out.append(f"— 豁免备查 ({len(rows)} 个SKU已移出追踪) —")
            for r in rows[:15]:
                out.append(f"  {_s(r.get('SKU'))} {_free(r.get('型号'))} · "
                           f"{_s(r.get('豁免类型'))}")
            if len(rows) > 15:
                out.append(f"  …另有 {len(rows) - 15} 个")
            out.append("  (不返回「原因」原文: 该列含登记人姓名, 平台API同样不返回)")
        else:
            out.append("— 豁免备查 (无) —")

        return clip("\n".join(out), 3800)

    def t_pipeline_trend(days: int = 14, line: str = "手机膜") -> str:
        """流程趋势: 按日对比 在研/超期/进行中/预警, 并给出较上一份的变化。

        为什么需要它: 其余工具都是「当前快照」, 问"这周比上周超期多了多少"只能
        让模型自己调两次 inflight_report 再心算 —— 算错了没人校验。这里把多次
        取数的结果做成确定性汇总, 模型只负责解读。

        数据源: 平台每日 09:00 生成的《事中日报》归档(reports/事中日报_YYYYMMDD.xlsx),
        纯本地只读, 不依赖平台后端在线。周末/节假日没有日报 → 该日缺行, 不是 0。
        口径: 平台侧 segment, 与 pipeline_alerts 的 Part 编号口径不同, 引用须注明。
        """
        reports = (root / "data" / str(line) / str(line) / "reports").resolve()
        try:
            reports.relative_to(root.resolve())     # 防 line 参数路径逃逸
        except ValueError:
            return f"[错误] 非法品类名: {line!r} (仅支持 手机膜/手机壳)"
        if not reports.is_dir():
            return (f"[错误] 事中日报目录不存在: {reports}。"
                    "趋势依赖日报归档; 目录不存在说明平台还没为这个品类生成过日报。")

        # 同一天可能有两份(正式名 + Excel 占用时的 _HHMMSS 后缀), 取 mtime 最新那份
        best = {}
        for p in reports.glob("事中日报_*.xlsx"):
            m = re.search(r"(\d{8})", p.stem)
            if not m:
                continue
            k = m.group(1)
            if k not in best or p.stat().st_mtime > best[k].stat().st_mtime:
                best[k] = p
        if not best:
            return (f"[错误] {reports} 下没有可解析的 事中日报_*.xlsx。"
                    "处理: 日报由平台每日 09:00 生成; 目录为空说明平台任务没跑过, "
                    "改用 pipeline_alerts 看当前快照。")

        n = max(2, min(int(days or 14), 60))
        keys = sorted(best)[-n:]
        rows, skipped = [], []
        for k in keys:
            txt = t_inflight_report(line=line, date=k)
            m = re.search(r"在研状态总表\s*\((\d+)\s*SKU:([^)]*)\)", txt)
            if not m:                       # 当天文件损坏/结构变了 → 跳过但不静默
                skipped.append(k)
                continue
            kv = {}
            for label in ("超期", "进行中", "预警"):
                mm = re.search(rf"{label}\s*(\d+)", m.group(2))
                kv[label] = int(mm.group(1)) if mm else None
            rows.append((k, int(m.group(1)), kv))

        if not rows:
            return (f"[错误] {line} 近 {n} 天的日报都读不出「在研状态总表」。"
                    "处理: 直接看最新一份(inflight_report 不传 date), 或改用 pipeline_alerts。")

        def cell(v):
            return "—" if v is None else str(v)

        # 平台在 2026-10 中旬后不再输出「预警」分组 → 整列为 None。
        # 留着只会让读者以为"预警一直是 0", 干脆整列省略并说明。
        show_warn = any(kv.get("预警") is not None for _, _, kv in rows)
        head = "日期        在研   超期   进行中" + ("  预警" if show_warn else "") + "   超期变化"
        out = [f"流程趋势 · {line} · 近 {n} 天(实际 {len(rows)} 份日报)", head]
        prev = None
        for k, total, kv in rows:
            over = kv.get("超期")
            if prev is None or over is None:
                delta = "—"
            else:
                d = over - prev
                delta = "持平" if d == 0 else f"{d:+d}"
            line_txt = (f"{k[4:6]}-{k[6:]}      {total:>4}  {cell(over):>4}  "
                        f"{cell(kv.get('进行中')):>5}")
            if show_warn:
                line_txt += f"  {cell(kv.get('预警')):>4}"
            line_txt += f"   {delta}"
            out.append(line_txt)
            prev = over

        first, last = rows[0], rows[-1]
        d_over = (last[2].get("超期") or 0) - (first[2].get("超期") or 0)
        span = f"{first[0][4:6]}-{first[0][6:]} → {last[0][4:6]}-{last[0][6:]}"
        out.append(f"\n区间对比({span}): 在研 {first[1]} → {last[1]} 个; "
                   f"超期 {first[2].get('超期')} → {last[2].get('超期')} 个({d_over:+d})")
        if not show_warn:
            out.append("注: 这批日报的汇总行没有「预警」分组, 故不列该列(不是预警为 0)")
        if skipped:
            out.append(f"注: {'/'.join(s[4:6] + '-' + s[6:] for s in skipped)} 的日报结构异常, 已跳过")
        # 按「份数」切而不是按日历切: 周末没有日报, 按日历切会让两段长度不等、
        # 口径错位(2026-10-29 实测模型就是这么算错的)。这里直接给出结论行, 让它照抄。
        if len(rows) >= 4:
            half = min(7, len(rows) // 2)
            recent, prev_rows = rows[-half:], rows[-2 * half:-half]
            r_over = [kv.get("超期") for _, _, kv in recent if kv.get("超期") is not None]
            p_over = [kv.get("超期") for _, _, kv in prev_rows if kv.get("超期") is not None]
            if r_over and p_over:
                ra, pa_ = sum(r_over) / len(r_over), sum(p_over) / len(p_over)
                diff = ra - pa_
                rpk = max(recent, key=lambda r: r[2].get("超期") or -1)
                ppk = max(prev_rows, key=lambda r: r[2].get("超期") or -1)
                out.append(
                    f"最近{len(recent)}份 vs 前{len(prev_rows)}份(按份数切, 避开缺日错位): "
                    f"超期均值 {pa_:.1f} → {ra:.1f} ({diff:+.1f}); "
                    f"峰值 {ppk[2].get('超期')}({ppk[0][4:6]}-{ppk[0][6:]}) → "
                    f"{rpk[2].get('超期')}({rpk[0][4:6]}-{rpk[0][6:]}); "
                    f"最新 {recent[-1][2].get('超期')}({recent[-1][0][4:6]}-{recent[-1][0][6:]})")
        out.append("口径: 平台事中日报 segment 口径, 与 pipeline_alerts 的 Part 编号口径不同, "
                   "引用须注明来源。缺行日期是周末/节假日(平台不出日报), 不是 0。")
        # 模型会自己按日期切"本周/上周"再相减, 缺日一错位结论就反了(2026-10-29 实测:
        # 说成"从5个增加到4个, 净减少1个")。把正确姿势写在数据里, 比写进系统提示有效。
        out.append("引用: 「本周vs上周」直接抄上面那行「最近N份 vs 前N份」, 不要自己按日期切; "
                   "要别的窗口就用 days 参数重调, 引「区间对比」行。")
        return clip("\n".join(out), 3800)

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
        f"四层流程信号(权威表口径): 最近{NEW_SKU_WINDOW_DAYS}天新增SKU(含签样追踪, 已签样给"
        "「签样日+当前环节/已持续/时效」/未签样给龄期并超目标标⚠️) / 全流程临期(空运60·海运69, "
        "附到货地区+货件号) / 已超期(附到货地区+货件号) / "
        "环节停滞超目标(近似段级). 目标值动态读 时效确认规则表.xlsx; "
        "每条带SKU/型号/卡点Part/剩余天数, 可直接用于催办. line默认手机膜.",
        {"type": "object", "properties": {
            "line": {"type": "string", "enum": ["手机膜", "手机壳"]},
            "warn_days": {"type": "integer", "minimum": 1, "description": "临期阈值天数, 默认15"},
            "top": {"type": "integer", "minimum": 1, "description": "每层列出条数, 默认8"}},
         "required": []}, "read", t_pipeline_alerts))
    registry.register(Tool(
        "inflight_report",
        "读平台产物《事中日报》xlsx(每日09:00生成) → 结构化摘要: 在研SKU状态总表"
        "(状态/当前停留/已停留/目标/距超时/预警段/核心原因/人工登记) + 超期明细 + 风险预警 + "
        "异常上报清单 + 豁免备查(SKU级)。纯本地pandas只读, 不依赖平台后端在线, "
        "无人值守链路可用。口径为平台侧segment(如 素材加工→广告), 与 pipeline_alerts "
        "的Part口径不同, 引用须注明来源; 责任人字段按脱敏纪律不返回。"
        "「人工登记」是跨日期延续的在研备注(如'暂时不弄了'), 催办前必读, 优先级高于"
        "系统算出的超期天数。line默认手机膜; date可传 YYYYMMDD/YYYY-MM-DD 读历史某天, 缺省读最新。",
        {"type": "object", "properties": {
            "line": {"type": "string", "enum": ["手机膜", "手机壳"],
                     "description": "品类, 缺省手机膜"},
            "date": {"type": "string",
                     "description": "可选, YYYYMMDD 或 YYYY-MM-DD; 缺省读最新一份"}},
         "required": []}, "read", t_inflight_report))
    registry.register(Tool(
        "pipeline_trend",
        "流程趋势(近N天): 按日对比 在研/超期/进行中/预警 的数量, 并给出较上一份的"
        "变化与区间对比。用于「这周比上周超期多了多少」「最近趋势怎么样」这类"
        "跨日期问题 —— 其余工具都是当前快照, 靠模型自己多次取数再心算容易错。"
        "数据源为平台每日09:00生成的事中日报归档, 纯本地只读不依赖平台后端在线。"
        "口径为平台segment, 与 pipeline_alerts 的Part口径不同, 引用须注明; "
        "周末/节假日无日报则该日缺行(不是0)。line默认手机膜; days默认14(2~60)。"
        "**引用纪律**: 要「本周vs上周」这类对比时, 用 days=7 和 days=14 各调一次, "
        "直接引用输出末尾的「区间对比」行; 不要自己按日期切分区间再相减 —— "
        "日报缺日会让切分口径错位, 算出来是错的(2026-10-29 实测过)。",
        {"type": "object", "properties": {
            "days": {"type": "integer", "minimum": 2, "maximum": 60,
                     "description": "回看天数, 默认14"},
            "line": {"type": "string", "enum": ["手机膜", "手机壳"],
                     "description": "品类, 缺省手机膜"}},
         "required": []}, "read", t_pipeline_trend))
    registry.register(Tool(
        "run_route_check",
        "对指定SKU重跑路线复盘脚本并返回单SKU报告尾部(写级操作, 输出隔离到agent_tmp). "
        "caliber: action/system; mode: air/sea(后勤运输口径).",
        {"type": "object", "properties": {
            "sku": {"type": "string", "description": "SKU编号, 如 G531"},
            "caliber": {"type": "string", "enum": ["action", "system"]},
            "mode": {"type": "string", "enum": ["air", "sea"]}},
         "required": ["sku"]}, "write", t_run_route_check))
    return 9 + _register_platform(registry, cfg)


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
# 词典口径 = 平台侧 backend/app/outbound_guard.py::SENSITIVE_KEY_SUBSTR 的逐条对齐(14→57);
# 纪律: 只增不减 —— 子串匹配没有兜底, 删任一条都会让"一整类字段名"裸奔。
#
# 🔴 纪律(最容易犯的错, 会有人来"好心"加 name): **禁止加入裸 `name`**。
#    实测真实响应里 `name / l4Name / partName / pendingPartName / _partName` 是
#    合法业务键(批次名 / Part 名 / 段名), 裸 `name` 一次误伤 5 个键 → 直接把助手问瞎。
#    name 族只收具体复合词(username / customername / realname / fullname)。
_SENS_SUBSTR = (
    # —— 原有 14 条(必须保留) ——
    "负责人", "操作人", "创建人", "姓名", "手机", "电话", "联系方式",
    "备注", "owner", "creator", "operator", "phone", "mobile", "remark",
    # —— 上一轮 9 条 ——
    "联系", "邮箱", "email", "mail", "微信", "wechat", "身份证", "idcard", "contact",
    # —— 本轮新增 34 条(对齐平台侧, 真实数据取证) ——
    "executor",
    "申请人", "审核人", "审批人", "经办人", "对接人", "主管",
    "收货人", "收件人", "寄件人", "处理人", "提交人", "跟进人", "执行人",
    "用户", "客户",
    "地址", "座机", "固话", "传真",
    "handler", "approver", "applicant", "receiver", "consignee", "shipper",
    "addr", "tel", "fax", "qq",
    "username", "customername", "realname", "fullname",
)
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


def _plat_reason(e) -> str:
    """把底层异常压成一句话原因 —— 完整堆栈贴给用户是噪音, 模型还容易原样转述
    (2026-10-29 实测: 回复里整段贴了 HTTPConnectionPool(...) Max retries exceeded)."""
    s = str(e)
    m = re.search(r"port=(\d+)", s)
    where = f"端口 {m.group(1)}" if m else "端口"
    name = type(e).__name__
    if "Timeout" in name:
        return f"平台后端响应超时({where})"
    if "Connection" in name or "Connection" in s:
        return f"连不上平台后端({where} 无响应)"
    return f"{name}"


# 平台不通时的降级指引: 不写这段, 模型只会说"稍后重试"然后两手空空收场,
# 而本地同步数据其实能给出近似答案(2026-10-29 实测)。
_PLAT_FALLBACK = (
    "处理方式: ① 直说平台服务当前不可用(数据后台系统 start.py 未启动), 绝不编造数据; "
    "② 若问题可用本地同步数据近似回答, 改用 pipeline_alerts(流程信号) / "
    "inflight_report(事中日报) / query_sku_route(单SKU) 给替代答案, 并标注"
    "「平台接口不可用, 以下为本地同步数据, 口径可能不同」; "
    "③ 不要再调其它 platform_* 工具 —— 同一个后端, 都会失败。"
)


def _plat_get(base: str, path: str, params: dict | None = None):
    """平台后端 GET。任何失败都返回 (None, 人话错误文本) —— 降级不抛异常,
    模型拿着错误文本可自愈: 先告知用户, 再降级到本地数据工具给替代答案。"""
    if _requests is None:
        return None, ("[平台接口不可用] 运行环境缺少 requests, 无法调用平台后端。"
                      + _PLAT_FALLBACK)
    url = f"{base}/api/{str(path).strip('/')}"
    try:
        r = _plat_session().get(url, params=params or {}, timeout=(3, 30))
    except Exception as e:
        return None, f"[平台接口不可用] {_plat_reason(e)}。" + _PLAT_FALLBACK
    if r.status_code != 200:
        return None, (f"[平台接口不可用] HTTP {r.status_code} ← /api/{path}。"
                      + _PLAT_FALLBACK)
    try:
        return r.json(), ""
    except Exception:
        return None, ("[平台接口不可用] 响应不是 JSON, 平台后端版本可能不匹配。"
                      + _PLAT_FALLBACK)


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
