#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_brief.py — 无人值守的每日流程日报(定时任务入口)

设计(无人值守三原则):
  1. 只读注册表: 把写级工具全部摘掉, 挂到计划任务跑也不可能有副作用
  2. 固定任务提示: sync_status 查同步 + 读预警清单 → 汇总简报
  3. 落盘 briefs/<YYYY-MM>/<日期>.md(按月归档); 加 --push 且配置了钉钉webhook则顺带推送

**一天只产出一份文档**(2026-10-19 用户要求"晨检和午后做成一份, 方便管理"):
  - 12 点前: 写 `briefs/<YYYY-MM>/<日期>.md` 的「晨检」段(完整六节快照);
  - 12 点后: 读当天文档里的晨检段当基线, **只生成相对晨检的变化**, 追加
    「午后更新」段; 重跑会替换午后段而不是再写一份, 不会出现两天的重复文件。
  旧版的 `briefs/<日期>_pm.md` 不再生成(历史文件保留)。

配合已有体系: 数据同步本身由 work-main/dingtalk 的计划任务负责,
本脚本只消费其日志与产物, 不重复同步。

Windows 计划任务(已注册, 2026-10-19 核对实际触发: AgentDailyBrief 09:00,
AgentDailyBriefPM 14:20, 动作都是 run_brief.bat; 排在上游同步任务
DingTalkApprovalSync 的 08:40 / 13:59 之后, 午后还刻意错开平台
DataAdmin-AutoPipeline 的 14:10 —— 它要在 14:10 写事中日报, 且撞锁会重试
最多 5 分钟, 2026-10-20 起还会改写真名_HHMMSS 后缀落盘, 简报 14:20 再跑才读得到当天最新版). 本机 schtasks.exe 被安全策略拦,
注册/查询一律走 PowerShell(模块名 ScheduledTasks):
  Get-ScheduledTask -TaskName AgentDailyBrief
  Set-ScheduledTask -TaskName AgentDailyBrief -Trigger (New-ScheduledTaskTrigger -Daily -At "09:00")
"""

import argparse
import re
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agentcore as core  # noqa: E402


# ---------- 落盘布局: 按月份归档(2026-10-22) ----------
#   briefs/2026-10/2026-10-22.md
# 之前是扁平 briefs/<日期>.md, 两个月就 35 个文件混着 task.log 和旧版 _pm.md。
# 归档后按月分目录, task.log 仍留在 briefs/ 顶层(run_brief.bat 重定向到它, 别动)。
BRIEF_DIR = HERE / "briefs"


def month_of(date_str: str) -> str:
    """"2026-10-22" → "2026-10"."""
    return date_str[:7]


def brief_path(date_str: str, base: Path | None = None) -> Path:
    """当天文档的规范路径 briefs/<YYYY-MM>/<YYYY-MM-DD>.md(不保证存在)."""
    return (base or BRIEF_DIR) / month_of(date_str) / f"{date_str}.md"


def find_brief(date_str: str, base: Path | None = None) -> Path | None:
    """按新布局找当天文档; 找不到再看扁平旧布局(兼容未归档的历史文件)."""
    p = brief_path(date_str, base)
    if p.exists():
        return p
    flat = (base or BRIEF_DIR) / f"{date_str}.md"
    return flat if flat.exists() else None


# 简报正文下限(字符): 模板要求 280~480 字, 低于此视为被限流掐空, 不值得推送
BRIEF_MIN_CHARS = 100
# 午后是"只列变化"的增量段, 天然比晨检短(全无变化时可能只有几十字), 单独设下限
PM_MIN_CHARS = 30
# 增量段的软上限: 超过这个长度基本说明模型又重抄了整篇基线, 只记日志提醒不改正文
PM_DELTA_SOFT_MAX = 1200


def ensure_brief_answer(client, history: list, answer: str,
                        min_len: int = BRIEF_MIN_CHARS,
                        sleeps=(20, 40), sleep_fn=time.sleep,
                        hint: str = "", post=None) -> str:
    """空简报兜底(2026-10-10 教训: 免费档 429/1305 过载把最终轮掐空,
    run_task 返回空正文照样写盘推送, 群里收到空壳).

    answer 过短时: 弹掉 history 尾部的空 assistant 消息, 追加"基于工具输出直接
    生成简报"的纯生成指令(不带工具), 按退避间隔重试; 仍短则返回原值, 由调用方
    决定不推送+非零退出 —— 宁可缺今天的简报, 不发空壳占位。
    hint: 重生成指令(午后增量模式要换成"只写变化", 不能用晨检那套 500~1000 字).
    post: 可选 answer -> answer 的确定性收尾(与 run_task 的 output_parser 同一份),
        兜底生成出来的正文也要过一遍, 否则空壳重试出来的东西没走格式校正。
    """
    a = (answer or "").strip()
    if len(a) >= min_len:
        return a
    while (history and history[-1].get("role") == "assistant"
           and not (history[-1].get("content") or "").strip()
           and not history[-1].get("tool_calls")):
        history.pop()  # 空流残留, 留着会误导续生成
    hint = hint or ("工具调用已全部完成。请基于以上工具输出直接输出中文markdown简报"
                    "(约500~1000字, 按任务要求的六节结构; 每个条目独占一行、行首 \"• \"), "
                    "不要再调用任何工具, 不要输出其他内容。")
    for wait_s in sleeps:
        sleep_fn(wait_s)  # 给限流桶留恢复时间, 立刻重试大概率还是 429
        msgs = history + [{"role": "user", "content": hint}]
        try:
            a = (client.chat_stream(msgs).get("content") or "").strip()
        except Exception as e:
            core.log(f"  [空简报兜底] 重生成失败: {e}")
            continue
        if len(a) >= min_len:
            return post(a) if post else a
    return (answer or "").strip()


# ---------- 简报格式兜底(2026-10-15 用户反馈"③④ 挤成一坨看不清") ----------
# 根因是 markdown 语义: **连续普通行会被折叠成一个段落**, 不带列表标记的逐行 SKU
# 条目在钉钉/Web 上一渲染就并成一列。光靠提示词不够可靠(模型经常忘), 故加确定性兜底。
_SKU_G = r"G\d{3,4}"
_SKU_HEAD = re.compile(r"^%s\s" % _SKU_G)                      # 行首就是 SKU 条目
_SPLIT_BY_SPACE = re.compile(r"\s+(?=%s\s)" % _SKU_G)          # "G598 … G599 …" 空格串行
_SPLIT_BY_SEMI = re.compile(r"[；;]\s*(?=%s\s)" % _SKU_G)      # "G493 …；G550 …" 分号串行
_COLON_THEN_SKU = re.compile(r"[:：]\s*(?=%s\s)" % _SKU_G)     # "**已超期**（17个）：G584 …"
_TITLE_DUP = re.compile(r"^\s*#{0,3}\s*(?:晨检|午后)简报\b")
_H1_DUP = re.compile(r"^#\s+\S")          # 正文里的单 # 标题一律丢(文档标题由外层写)
_LISTED = re.compile(r"^\s*(?:•\s*|[-*]\s+|\d+[.)]\s+|#{1,6}\s|>)")

# ---------- 小节标题统一(2026-10-19 用户反馈"午后有些换行没对齐") ----------
# 模板里小节标题后面原本挂着一整段说明, LLM 会把说明一起抄进标题, 于是晨检和
# 午后同一节的标题长短不一(如 ③ 在午后变成一整行"最近40天新增SKU(...签样追踪)").
# 这里用固定短标题覆盖, 保证晨检/午后两段、以及相邻两天的观感完全对齐。
SECTION_TITLES = {
    "①": "① 同步状态与数据截至",
    "②": "② 今日动态",
    "③": "③ 新增SKU与签样追踪",
    "④": "④ 临期预警",
    "⑤": "⑤ 已超期与环节停滞TOP",
    "⑥": "⑥ 行动建议",
}
_SEC_LEAD = re.compile(r"^(#{1,6})\s*")
_SEC_NO = re.compile(r"^([①②③④⑤⑥])")
_WARN_LINE = re.compile(r"^数据已.*未同步")     # ① 顶部的"数据断供"警示句, 保持普通段落


def _canon_section(s: str):
    """识别小节标题行 → (编号, 规范标题)。普通句子(哪怕以①开头)不当标题。"""
    m = _SEC_LEAD.match(s)
    body = s[m.end():].strip() if m else s
    mm = _SEC_NO.match(body)
    if not mm:
        return None
    if not m and len(body) > 40:      # 没有 # 且明显是长句 → 不是标题
        return None
    return mm.group(1), SECTION_TITLES[mm.group(1)]


def _split_sku_entries(s: str) -> list:
    """一行里被空格/分号串起来的多个 SKU 条目拆开, 保留原文文字不动。"""
    parts = [s]
    for pat in (_SPLIT_BY_SPACE, _SPLIT_BY_SEMI):
        nxt = []
        for p in parts:
            nxt.extend(pat.split(p))
        parts = nxt
    return [p.strip() for p in parts if p.strip()]


def repair_brief_answer(client, history: list, answer: str, probs: list,
                        tries: int = 1) -> str:
    """正文出现复读/提示词回声/条目写一半时, 让模型重出一版(2026-10-22 事故)。

    只重试一次: 这类退化多半是单次生成抽风(长上下文+严格格式约束), 重出一次基本就好;
    连续两次还坏说明不是偶发, 再试只是烧 token —— 交给确定性清理兜底 + 日志告警。
    """
    hint = (f"上一版输出有结构问题({'；'.join(probs)}): 出现了复述任务规则、重复行、"
            "或条目写到一半就断。请**重新输出完整六节**, 直接写内容; 不要复述任何规则原文, "
            "每个条目独占一行、行首 \"• \", 方括号必须闭合, 不要调用工具。")
    msgs = list(history) + [{"role": "user", "content": hint}]
    for _ in range(max(1, tries)):
        try:
            a = (client.chat_stream(msgs).get("content") or "").strip()
        except Exception as e:                       # noqa: BLE001
            core.log(f"  [简报修复] 重生成失败: {e}")
            return answer
        if a:
            return a
    return answer


# ---------- 简报回声/复读兜底(2026-10-22 事故) ----------
# 模型把提示词里的"格式铁律"当正文吐了出来, 还把同一句 "⑤ 含两组时…" 复读了 30 遍,
# G589 的到货号写到 "亚马逊墨西哥 F" 就断了。这是模型侧退化(长上下文+严格格式约束),
# 提示词再强调也挡不住, 只能确定性清理:
#   ① 含指令原句的行整行丢; 条目行中途被注入 → 截断到注入点, 截断后括号不闭合就整行丢
#      (半条数据比没有更误导: "亚马逊墨西哥 F" 会被当成真货件号);
#   ② 连续重复行只留一条(复读);
#   ③ 同一小节标题重复出现只留第一个(模型重启小节)。
_ECHO_MARKS = (
    "严格遵守格式要求", "格式铁律", "每个条目独占一行", "条目必须换行分隔",
    "一行一个 SKU", "结尾不要加分号", "括号/方括号要闭合", "各自单独成组",
    "组内仍逐条", "严禁把多个条目", "不要输出其他内容", "不要复述本提示词",
)


def _balanced(s: str) -> bool:
    """方括号/圆括号是否配对(货件号标注 "[到货: …]" 被写断时靠它识别)。"""
    return s.count("[") == s.count("]") and s.count("(") == s.count(")")


# 不能用 \b: 中文也算 \w, "中G531" 里 G 前面就没有词边界(2026-10-23 实测漏抓)
_SKU_RE = re.compile(r"(?<![A-Za-z0-9])G\d{3,4}(?!\d)")


def _section_text(md: str, start: str, end: str | None = None) -> str:
    """取某个小节(如 "②")的正文: 到下一个同层或更高层小节标题为止。

    简报里的节标题在落盘时是 "### ① …"(嵌在 "## 晨检" 之下), 也可能是 "## ① …",
    所以统一按 2~4 级井号匹配。找不到该节返回空串。
    """
    m = re.search(rf"^#{{2,4}}\s*{re.escape(start)}.*$", md, re.M)
    if not m:
        return ""
    tail = md[m.end():]
    stop = (rf"^#{{2,4}}\s*{re.escape(end)}" if end
            else r"^#{2,4}\s*[①②③④⑤⑥⑦⑧⑨]")
    m2 = re.search(stop, tail, re.M)
    return tail[:m2.start()] if m2 else tail


def scrub_brief_artifacts(text: str) -> str:
    """清掉模型复述提示词/复读的行(只动垃圾行, 不改业务文字)。"""
    if not text:
        return text
    out, prev = [], None
    for raw in str(text).replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        s = line.strip()
        hit = next((m for m in _ECHO_MARKS if m in s), None)
        if hit:
            head = s[:s.index(hit)].rstrip(" 　,，;；:：-—")
            # 条目行被中途注入: 截断后若括号仍闭合, 保留前半条; 否则整行丢
            if s.startswith("•") and head.strip("• ").strip() and _balanced(head):
                line = head
            else:
                continue
        if s and s == prev:
            continue                      # 连续复读只留一条
        prev = s or prev
        out.append(line)
    return "\n".join(out)


def brief_health(md: str, tool_out: str | None = None,
                 require_all: bool = False) -> list:
    """结构体检: 返回问题列表(空=健康)。只报不改, 调用方决定重生成/告警。

    三个信号都来自 2026-10-22 实测: 提示词回声、复读行、括号未闭合(数据写一半)。
    传 tool_out 时多查一项「② 是否编造」(2026-10-23 事故: 模型零工具调用, 编出
    G531/532/533 三条"今日动态", 真实是 2026-03-02 就已结束的老单)。
    require_all: 完整快照(晨检)要求六节齐全 —— 2026-10-29 实测模型只写 ①②⑥,
    ④ 因当天"临期 0 个"被渲染逻辑跳过, 简报悄悄少一节且无人报警。午后增量
    只写变化, 不要求六节, 所以默认关闭。
    """
    if not (md or "").strip():
        return ["正文为空"]
    probs = []
    if require_all:
        missing = [n for n in "①②③④⑤⑥"
                   if not re.search(rf"^#{{2,4}}\s*{n}", md, re.M)]
        if missing:
            probs.append(f"缺 {'/'.join(missing)} 节")
    echo = [m for m in _ECHO_MARKS if m in md]
    if echo:
        probs.append(f"含提示词回声({echo[0]})")
    lines = [l.strip() for l in md.split("\n") if l.strip()]
    dup = sum(1 for a, b in zip(lines, lines[1:]) if a == b)
    if dup >= 2:
        probs.append(f"连续重复行 {dup} 处")
    bad = [l for l in lines if l.startswith("•") and not _balanced(l)]
    if bad:
        probs.append(f"{len(bad)} 条括号未闭合(疑似写一半)")
    # ⑤ 必须带两组(已超期 + 环节停滞)且各有条目, 否则等于少报
    m5 = re.search(r"^#{2,4}\s*⑤.*$", md, re.M)
    if m5:
        tail = md[m5.end():]
        m6 = re.search(r"^#{2,4}\s*⑥", tail, re.M)
        sec5 = tail[:m6.start()] if m6 else tail
        if sec5.count("•") == 0:
            probs.append("⑤ 无条目")
        elif "环节停滞" not in sec5:
            probs.append("⑤ 缺环节停滞组")
    # ② 的 SKU 必须是工具真给过的(空 tool_out 说明本次没取到数, 无从比对, 不判)
    # ⑥ 同理: 行动建议必须指向真实存在的 SKU, 不能顺着编造的 ② 一路编下去。
    if tool_out:
        for sec, nxt in (("②", "③"), ("⑥", None)):
            ghost = sorted({s for s in _SKU_RE.findall(_section_text(md, sec, nxt))
                            if s not in tool_out})
            if ghost:
                probs.append(f"{sec} 含工具输出里没有的 SKU({','.join(ghost)}), 疑似编造")
    return probs


def normalize_brief_md(answer: str) -> str:
    """把简报正文规范成"列表可渲染"的形态(只动结构, 不改任何文字):

    0) 小节标题统一成固定的六条(①~⑥), 晨检/午后/隔天都对齐; 大标题去重;
    1) 行首是 SKU 的裸条目 → 前置 "• "(一行一条);
    2) 一行里被空格/分号串起来的多个条目 → 按条目边界拆行;
    3) "组标签：Gxxx …" → 拆成 标签行 + "• Gxxx …" 条目行;
    4) "- / * / 1. " 等列表标记统一成 "• ", 全文观感一致;
    5) ① 节的短数据行("同步正常"/"数据截至: …")也加 "• " —— 否则 markdown 会把
       连续普通行折成一个段落, 这就是用户看到的"换行没对齐";
    6) 小节内完全相同的条目只留一次(工具的行级重复, 如 G558 出现两遍)。
    已经是标题/引用/普通句的行原样保留。
    """
    if not answer:
        return answer
    answer = scrub_brief_artifacts(answer)   # 先清回声/复读(2026-10-22 事故)
    out: list = []
    cur = ""          # 当前小节编号(①..⑥)
    seen_sec: set = set()  # 已出现过的小节(模型重启小节时标题会重复)
    seen: set = set()  # 当前小节已出现的 bullet 原文(去重)

    def emit(txt: str) -> None:
        if txt in seen:
            return
        seen.add(txt)
        out.append(txt)

    for raw in str(answer).replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        s = line.strip()
        if _TITLE_DUP.match(s) or _H1_DUP.match(s):
            continue                                  # 大标题去重(外层统一写)
        if not s:
            out.append(line)
            continue
        sec = _canon_section(s)                       # 小节标题 → 规范短标题
        if sec:
            cur, title = sec
            if cur in seen_sec:
                continue                              # 同一小节重复开头 → 只留第一个
            seen_sec.add(cur)
            seen = set()
            if out and out[-1].strip():
                out.append("")                        # 标题前留空行, 渲染更稳
            out.append(f"## {title}")
            continue
        if _LISTED.match(s):                          # 已有列表/标题/引用 → 统一 bullet
            if s.startswith("•"):
                emit(s)                                # 已是 bullet: 只去重, 不重复加
            elif re.match(r"^\s*[-*]\s+", s):
                emit("• " + s.lstrip("-*").strip())
            elif re.match(r"^\s*\d+[.)]\s+", s):
                emit("• " + re.sub(r"^\s*\d+[.)]\s+", "", s))
            else:
                # 小节标题前补一个空行(源码可读, 渲染也更稳)
                if re.match(r"^#{1,6}\s", s) and out and out[-1].strip():
                    out.append("")
                out.append(line)
            continue
        # 组标签和首条目挤在一行 → 先掰开(标签单独成行时尾冒号就没意义了, 去掉)
        m = _COLON_THEN_SKU.search(s)
        if m and _SKU_HEAD.match(s[m.end():]):
            out.append(s[:m.start() + 1].rstrip().rstrip(":："))
            s = s[m.end():].strip()
        if _SKU_HEAD.match(s):                        # 裸 SKU 条目(可能被串行)
            for it in _split_sku_entries(s):
                emit("• " + it)
            continue
        # ① 的短数据行: 不加 bullet 会被折进同一段(用户报的"换行没对齐")
        if cur == "①" and not _WARN_LINE.match(s):
            emit("• " + s)
            continue
        out.append(line)
    return "\n".join(out).strip() + "\n"


# ---------- 当天唯一一份文档: 晨检快照 + 午后增量(2026-10-19) ----------
AM_MARK = "## 晨检"
PM_MARK = "## 午后更新"
_AM_RE = re.compile(r"^##\s*晨检.*$", re.M)


def _relevel(md: str, level: int = 3) -> str:
    """把规范后的 "## ① …" 降为 "### ① …"(嵌进 "## 晨检/午后更新" 之下)。"""
    return re.sub(r"^##\s+(?=[①②③④⑤⑥])", "#" * level + " ", md, flags=re.M)


def load_morning(path: Path):
    """读当天文档里的晨检段 → (正文, 标题文字)。缺失/为空返回 ("", "晨检")。
    午后用它当对比基线; 顺带走一遍 normalize, 老格式也能被顺手排版。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "", "晨检"
    text = text.replace("\r\n", "\n")
    if PM_MARK in text:                       # 重跑时丢掉上一版午后段
        text = text.split(PM_MARK)[0]
    label = "晨检"
    m = _AM_RE.search(text)
    if m:
        label = m.group(0).lstrip("# ").strip()
        text = text[m.end():]
    keep = [l for l in text.split("\n")
            if not l.startswith("# ") and not l.startswith("> 更新记录")
            and l.strip() != "---"]
    # 注意: "> 用量:" 行要保留 —— 重排午后段时旧的晨检用量不能丢(不传 am_usage)
    body = normalize_brief_md("\n".join(keep))
    return (body if body and body.strip() else ""), label


# 模型把"晨检只列了 TOP N"误当成"新增"(2026-10-19 实测: 晨检列 8 行、总数 22 个,
# 午后写成"从8个增至22个", 还把 TOP 之外的 G586/G582 说成"新增超期")。
# 因此 ④⑤ 只允许报计数: 带 SKU 编号的行一律不回显(晨检段就在同一份文档的上方)。
_DELTA_SECTIONS = ("④", "⑤")
# 模型在 ⑥ 里拿"基线列了几行"做减法(实测: 22-8=14 → "新增的14个超期SKU"),
# 这个增量是假的。⑥ 只留动作对象, 去掉"新增的N个/单"里的数字。
_FAKE_DELTA_NUM = re.compile(r"(新增的?)\s*\d+\s*[个单]")


def refine_delta(pm_md: str, baseline: str):
    """午后增量的确定性校验 → (新正文, 丢掉的条数)。④⑤ 里带 SKU 的行不回显。"""
    base_skus = set(re.findall(_SKU_G, baseline or ""))
    if not base_skus:
        return pm_md, 0
    out: list = []
    cur, dropped = "", 0
    sec_has_bullet = True          # 进入新小节时重置, 用来补 "与晨检一致"
    for line in pm_md.split("\n"):
        s = line.strip()
        sec = _canon_section(s)
        if sec:
            if cur in _DELTA_SECTIONS and not sec_has_bullet:
                out.append("• 与晨检一致")
            cur = sec[0]
            sec_has_bullet = cur not in _DELTA_SECTIONS
            out.append(line)
            continue
        if cur in _DELTA_SECTIONS and s.startswith("• "):
            if re.search(_SKU_G, s):        # 见 docstring: 计数段不列 SKU
                dropped += 1
                continue
            sec_has_bullet = True
        elif cur == "⑥" and s.startswith("• "):
            fixed = _FAKE_DELTA_NUM.sub(r"\1", s)
            if fixed != s:
                dropped += 1
            line = fixed
        out.append(line)
    if cur in _DELTA_SECTIONS and not sec_has_bullet:
        out.append("• 与晨检一致")
    return "\n".join(out), dropped


# ---------- ④⑤ 的计数改成脚本自己算(2026-10-19) ----------
# 模型对"总数"不可靠: 实测把晨检列的 8 行当成总数 8, 而工具口径是 22 个。
# 计数本来就写在 pipeline_alerts 的小节标题里, 直接解析比让模型复述稳。
_CNT_WARN = re.compile(r"临期预警\(剩≤\d+天,\s*(\d+)\s*个\)|临期\s*(\d+)\s*个")
_CNT_OVER = re.compile(r"已超期\(超全流程线,\s*(\d+)\s*个|已超期\s*(\d+)\s*个")
_CNT_STALE = re.compile(r"环节停滞超目标\([^)]*?(\d+)\s*单"
                        r"|环节停滞TOP\s*(\d+)\s*单"
                        r"|停滞\s*(\d+)\s*单")


def _first_int(pat, text):
    m = pat.search(text or "")
    if not m:
        return None
    for g in m.groups():
        if g and g.isdigit():
            return int(g)
    return None


def parse_counts(text: str) -> dict:
    """从 pipeline_alerts 输出(或简报正文)里抽 临期/已超期/停滞 的总数。"""
    return {"warn": _first_int(_CNT_WARN, text),
            "over": _first_int(_CNT_OVER, text),
            "stale": _first_int(_CNT_STALE, text)}


# ---------- ③④⑤ 确定性渲染(2026-10-22) ----------
# 这三节本来就是工具输出的成品行(编号/型号/天数/货件号), 让模型"照抄"是这半个月
# 四起事故的共同根因(挤成一坨/被截断/复读/货件号写一半)。工具每节格式固定:
#   == 标题(含计数) ==
#     <两空格缩进的条目>
#   直接搬, 一个字符都不经模型 —— 数据段从此不可能出错, 也省掉三节的输出 token。
_PA_SEC = re.compile(r"^==\s*(.+?)\s*==$")
_PA_ITEM = re.compile(r"^\s{2,}(\S.*)$")
# 层级自适应: 正文落盘时整段会被降一级(## 晨检 → ### ①), 若只认 "## " 就切不开,
# 结果是 ③④⑤ 被整节追加到文末、原文那几节还在 —— 一份简报里两套 ③④⑤。
_SEC_SPLIT = re.compile(r"^#{2,4}\s*([①②③④⑤⑥])\s*(.*)$", re.M)
_SEC_LEVEL = re.compile(r"^(#{2,4})\s*[①②③④⑤⑥]", re.M)
_SEC_TITLES = {"③": "新增SKU与签样追踪", "④": "临期预警", "⑤": "已超期与环节停滞TOP"}


def _pa_sections(tool_out: str) -> dict:
    """pipeline_alerts 输出 → {节标题: [原始行…]}。"""
    secs, cur = {}, None
    for raw in (tool_out or "").split("\n"):
        line = raw.rstrip()
        m = _PA_SEC.match(line.strip())
        if m:
            cur = m.group(1)
            secs.setdefault(cur, [])
            continue
        if cur is not None and line.strip():
            secs[cur].append(line)
    return secs


def _pa_items(lines) -> tuple:
    """条目行(两空格缩进) → (条目列表, 尾注列表)。尾注如「未签样待跟进 4 个…」。"""
    items, notes = [], []
    for l in lines:
        m = _PA_ITEM.match(l)
        if not m:
            continue
        t = m.group(1).strip()
        (notes if t.startswith("未签样待跟进") else items).append(t)
    return items, notes


def render_data_sections(tool_out: str) -> dict:
    """从工具输出渲染 ③④⑤。一节都取不到 → {}（调用方保留模型原文）。

    ⑤ 的计数行写法必须与 `_CNT_OVER` 对齐（"已超期 22 个"）——午后靠它对数。
    """
    secs = _pa_sections(tool_out)
    if not secs:
        return {}

    def pick(*keys):
        for k, v in secs.items():
            if any(x in k for x in keys):
                return k, v
        return None, None

    # ⚠️ 段存在就必须出节, 哪怕条目为 0 —— 2026-10-29 实测: 当天"临期预警 0 个",
    # 旧写法 `if items` 直接跳过 ④, 简报从六节变成五节, 读者以为漏报了一节。
    # "0 个" 本身是有效结论(今天没有临期), 要写出来, 不能靠标题消失来表达。
    out = {}
    k, v = pick("新增SKU")
    if k:
        items, notes = _pa_items(v)
        out["③"] = "\n".join([f"• {i}" for i in items] + notes) if items \
            else "• 近 40 天无新增 SKU"

    k, v = pick("临期预警")
    if k:
        items, _ = _pa_items(v)
        warn = _first_int(_CNT_WARN, k)
        # 带计数行: 午后要靠它对数, 否则只能写"晨检未记录总数"(2026-10-20 的噪音)
        head = [f"临期 {warn} 个"] if warn is not None else []
        body = [f"• {i}" for i in items] or ["• 无临期 SKU"]
        out["④"] = "\n".join(head + body)

    k, v = pick("已超期")
    if k:
        items, _ = _pa_items(v)
        over = _first_int(_CNT_OVER, k)
        legacy = re.search(r"另历史遗留\s*(\d+)\s*个", k)
        head = f"已超期 {over} 个" if over is not None else "已超期"
        if legacy:
            head += f"; 另历史遗留 {legacy.group(1)} 个不列"
        body = [f"• {i}" for i in items] or ["• 无超期 SKU"]
        out["⑤"] = "\n".join([head] + body)

    k, v = pick("环节停滞")
    if k:
        items, _ = _pa_items(v)
        stale = _first_int(_CNT_STALE, k)
        g2 = (f"环节停滞TOP {stale}单 (段级停滞=距最后更新vs环节目标)"
              if stale is not None else "环节停滞TOP (段级停滞=距最后更新vs环节目标)")
        body = g2 + "\n" + ("\n".join(f"• {i}" for i in items) if items
                            else "• 无停滞超目标环节")
        out["⑤"] = (out["⑤"] + "\n\n" + body) if out.get("⑤") else body
    return out


def render_dynamic_section(tool_out: str) -> str | None:
    """从工具输出确定性渲染 ② 今日动态 —— 只在模型编造时当兜底用。

    来源两处(与提示词允许模型抄的范围一致):
      - scan_sync_data 的今日动态明细: "· G598 xx @ Part1 | 状态:… | 最新节点:… | 当前负责人:…"
      - pipeline_alerts 的 Part9新单待办: "G598 xx (创建 10-22 14:53, 当前负责人: 胡浩明)"
    取不到"今日有动态"字样 → None(说明不是这套工具输出, 调用方保留原文)。
    """
    m = re.search(r"^今日有动态:\s*(\d+)\s*单", tool_out or "", re.M)
    if not m:
        return None
    cnt = int(m.group(1))
    items = [l.strip()[2:].strip() for l in (tool_out or "").split("\n")
             if l.strip().startswith("· ") and "@" in l]
    if not items:
        items = ["今日无新增动态"] if cnt == 0 else [f"今日有动态 {cnt} 单(明细未展开)"]
    # Part9 待办只给一行汇总: 明细在 ③ 已经逐条列过, 不在这里重复铺开
    # 注意 tool_out 是 pipeline_alerts + scan_sync_data 拼起来的, 后者的缩进行会被
    # _pa_sections 归到最后一个 "==" 段里 —— 用标题自带的"N单"截断, 别让停滞行串味。
    secs = _pa_sections(tool_out)
    p9_title, p9_lines = next(
        ((k, v) for k, v in secs.items() if k.startswith("Part9新单待办")), ("", []))
    p9_items, _ = _pa_items(p9_lines)
    m_cnt = re.search(r"(\d+)\s*单", p9_title)
    if m_cnt:
        p9_items = p9_items[:int(m_cnt.group(1))]
    skus = [mm.group(1) for it in p9_items
            if (mm := re.match(r"(G\d{3,4})\b", it))]
    if skus:
        owners = sorted(set(re.findall(r"当前负责人:\s*([^\s,)]+)", " ".join(p9_items))))
        who = f"(负责人 {'、'.join(owners)})" if owners else ""
        items.append(f"Part9新单待办 {len(skus)} 单: {'、'.join(skus)}{who}")
    return "\n".join(f"• {i}" for i in items)


def replace_dynamic_section(md: str, tool_out: str) -> tuple:
    """把 ② 整段换成工具渲染版(丢弃模型编的条目)。返回 (新正文, 是否替换)。"""
    body = render_dynamic_section(tool_out)
    if not body or not (md or "").strip():
        return md, False
    m = re.search(r"^#{2,4}\s*②.*$", md, re.M)
    if not m:
        return md, False
    tail = md[m.end():]
    m2 = re.search(r"^#{2,4}\s*[③④⑤⑥⑦⑧⑨]", tail, re.M)
    end = m.end() + (m2.start() if m2 else len(tail))
    return md[:m.end()] + "\n\n" + body + "\n\n" + md[end:], True


def drop_ghost_skus(md: str, tool_out: str) -> tuple:
    """删掉 ⑥ 里引用了「工具输出中不存在的 SKU」的条目 —— 编造残留的兜底。

    2026-10-23 的 ⑥ 顺着编造的 ② 写出"提醒钱七处理 G533 的待审批事项",
    人和状态都是假的。② 重建后 ⑥ 仍会留着这些条目, 只能按 SKU 白名单删。
    返回 (新正文, 删掉的条数)。
    """
    if not tool_out or not (md or "").strip():
        return md, 0
    sec = _section_text(md, "⑥")
    if not sec.strip():
        return md, 0
    keep, dropped = [], 0
    for ln in sec.split("\n"):
        s = ln.strip()
        if s.startswith("•") and any(g not in tool_out for g in _SKU_RE.findall(s)):
            dropped += 1
            continue
        keep.append(ln)
    if not dropped:
        return md, 0
    if not any(l.strip().startswith("•") for l in keep):
        keep = ["", "• 详见 ③④⑤ 的明细与计数"]
    idx = md.find(sec)
    if idx < 0:
        return md, 0
    return md[:idx] + "\n".join(keep) + md[idx + len(sec):], dropped


def carry_over_actions(pm_md: str, am_md: str, tool_out: str) -> tuple:
    """午后 ⑥ 必须承接晨检待办 —— 晨检提过、SKU 仍在流程里的, 没写就补回来。

    2026-10-27 用户反馈: 晨检 ⑥ 要求"今日内完成 G592/G593 的 Part8 审批",
    午后 ⑥ 却写"• 无新增动作"。上午的活没落实, 下午反而清零, 群里会误判成没事。
    规则只能管住听话的模型, 这里按数据兜一道: 只要 SKU 还在工具输出里
    (pipeline_alerts 只列在途/异常单), 就认定它没做完, 继续挂在 ⑥ 上。
    返回 (新正文, 补回的条数)。
    """
    am_sec = _section_text(am_md or "", "⑥")
    pm_sec = _section_text(pm_md or "", "⑥")
    if not am_sec.strip() or not pm_sec.strip():
        return pm_md, 0
    am_items = [l.strip() for l in am_sec.split("\n") if l.strip().startswith("•")]
    pm_items = [l.strip() for l in pm_sec.split("\n") if l.strip().startswith("•")]
    pm_skus = {s for l in pm_items for s in _SKU_RE.findall(l)}
    carry = []
    for it in am_items:
        skus = _SKU_RE.findall(it)
        if not skus:
            continue                       # 没有 SKU 的泛泛建议不承接(无法核对进展)
        if any(s in pm_skus for s in skus):
            continue                       # 午后已经提过, 不重复
        if not any(s in (tool_out or "") for s in skus):
            continue                       # 数据里查不到 → 大概率已结束, 不硬挂
        carry.append(it.lstrip("• ").rstrip("。") + " (晨检提出, 仍未推进)")
    if not carry:
        return pm_md, 0
    # "无新增动作"是清零式占位, 有承接条目时必须去掉, 否则自相矛盾
    kept = [l for l in pm_items if "无新增动作" not in l]
    new_sec = "\n".join(kept + [f"• {c}" for c in carry])
    idx = (pm_md or "").find(pm_sec)
    if idx < 0:
        return pm_md, 0
    # 前面必须补 "\n\n": pm_sec 自带前导换行, 直接拼会让第一条粘在标题行尾被吃掉
    return pm_md[:idx] + "\n\n" + new_sec + pm_md[idx + len(pm_sec):], len(carry)


def replace_data_sections(md: str, tool_out: str) -> tuple:
    """把 ③④⑤ 正文换成工具渲染版；模型漏写的节按序补上。

    返回 (新正文, 被替换/补上的节编号列表)。渲染取不到 → 原样返回。
    """
    rendered = render_data_sections(tool_out)
    if not rendered or not (md or "").strip():
        return md, []
    parts = _SEC_SPLIT.split(md)
    head, blocks = parts[0], []
    for i in range(1, len(parts), 3):
        blocks.append([parts[i], parts[i + 1],
                       parts[i + 2] if i + 2 < len(parts) else ""])
    hit = []
    for b in blocks:
        if b[0] in rendered:
            b[2] = "\n" + rendered[b[0]].rstrip() + "\n"
            hit.append(b[0])
    for num in sorted(rendered):
        if num not in [b[0] for b in blocks]:
            blocks.append([num, _SEC_TITLES.get(num, ""),
                           "\n" + rendered[num].rstrip() + "\n"])
            hit.append(num)
    blocks.sort(key=lambda b: "①②③④⑤⑥".index(b[0]))
    # 沿用原文的标题层级: 落盘段落是 ### 级, 重拼成 ## 会让同一份文档里层级跳变
    m = _SEC_LEVEL.search(md)
    lvl = m.group(1) if m else "##"
    body = "\n".join([head.rstrip()] + [f"{lvl} {n} {t}".rstrip() + "\n" + c
                                        for n, t, c in blocks])
    return body.strip() + "\n", hit


def _count_line(label: str, now, base, unit: str = "个") -> str:
    if now is None:
        return f"• {label} 个数以工具为准(本次未取到)"
    if base is None:
        return f"• {label} {now} {unit}(晨检未记录总数)"
    if base == now:
        return f"• {label} {now} {unit}(与晨检一致)"
    diff = now - base
    return f"• {label} {now} {unit}(晨检 {base} {unit}, {diff:+d})"


def apply_counts(pm_md: str, now: dict, base: dict) -> str:
    """把 ④⑤ 两节内容换成脚本算出的计数行(不列 SKU)。"""
    if now.get("warn") is None and now.get("over") is None:
        return pm_md
    repl = {
        "④": [_count_line("临期", now.get("warn"), base.get("warn"))],
        "⑤": [_count_line("已超期", now.get("over"), base.get("over")),
               _count_line("环节停滞", now.get("stale"), base.get("stale"), "单")],
    }
    lines = pm_md.split("\n")
    out: list = []
    i = 0
    while i < len(lines):
        sec = _canon_section(lines[i].strip())
        if sec and sec[0] in repl:
            out.append(lines[i])
            out.extend(repl[sec[0]])          # 紧跟标题, 与其他节一样不留空行
            i += 1
            while i < len(lines) and not _canon_section(lines[i].strip()):
                i += 1
            if i < len(lines):
                out.append("")            # 与下一节标题之间留空行
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)



def compose_daily(today: str, *, am_body: str = "", pm_body: str = "",
                  am_usage: str = "", pm_usage: str = "",
                  am_label: str = "晨检 09:30",
                  pm_label: str = "午后更新") -> str:
    """把晨检/午后拼成当天唯一的 briefs/<YYYY-MM>/<日期>.md。

    只有晨检 → 只写晨检段; 午后再跑 → 晨检段原样保留(只重排格式) + 追加午后段;
    重跑午后 → 替换午后段, 不会在同一份文档里叠出两段。
    """
    if am_body and pm_body:
        rec = f"> 更新记录: {am_label}(完整快照) · {pm_label}(只列相对晨检的变化)"
    elif pm_body:
        rec = f"> 更新记录: {pm_label}(晨检缺档, 本节为完整快照)"
    else:
        rec = f"> 更新记录: {am_label}(完整快照)"
    chunks = [f"# 日报 {today}\n\n{rec}\n"]
    if am_body:
        chunks.append(f"## {am_label}\n\n{_relevel(am_body).strip()}\n")
        if am_usage:
            chunks[-1] = chunks[-1].rstrip() + f"\n\n> 用量: {am_usage}\n"
    if pm_body:
        chunks.append(f"---\n\n## {pm_label}\n\n{_relevel(pm_body).strip()}\n")
        if pm_usage:
            chunks[-1] = chunks[-1].rstrip() + f"\n\n> 用量: {pm_usage}\n"
    return "\n\n".join(c.rstrip() for c in chunks).rstrip() + "\n"


def build_readonly_registry(cfg: dict) -> core.Registry:
    """只保留只读工具的注册表 —— 无人值守的安全底线."""
    reg = core.build_registry()
    core.load_plugins(reg, cfg)
    dropped = [n for n, t in list(reg._tools.items()) if t.level != "read"]
    for n in dropped:
        del reg._tools[n]
    core.log(f"[晨检] 无人值守模式: 摘除写级工具 {len(dropped)} 个, "
             f"保留只读 {len(reg.names())} 个")
    return reg


PREFETCH_TOOLS = ("sync_status", "scan_sync_data", "pipeline_alerts")


def prefetch_tool_data(registry) -> dict:
    """预先执行只读工具, 把实时数据直接交给模型。

    为什么不让模型自己调: 2026-10-23 实测, glm-4.5-flash 在"③④⑤ 不用你写条目"
    的提示下会整段跳过取数(1 次 LLM 调用、0 次工具调用), 直接编出三条"今日动态"。
    否定式指令压过了"必须取数", 靠提示词不可靠 —— 代码先取好塞给它最稳。
    """
    data = {}
    for name in PREFETCH_TOOLS:
        try:
            data[name] = registry.execute(name, {})
        except Exception as e:                  # noqa: BLE001
            data[name] = f"[取数失败: {e}]"
            print(f"[预取] {name} 失败: {e}")
    return data


def prefetch_prompt(data: dict) -> str:
    """把预取结果拼成提示词片段(接在任务说明之后)。"""
    if not data:
        return ""
    head = ("\n\n**以下实时数据已由系统取好, 直接引用即可 —— 不需要你再调用 "
            "sync_status / scan_sync_data / pipeline_alerts。**"
            "要查单个 SKU 的路线明细可以调 query_sku_route。\n")
    return head + "".join(f"\n### {k} 输出\n{v}\n" for k, v in data.items())


TASK_TEMPLATE = """今日{tag}({date})。数据口径: data/手机膜/手机膜 自动同步目录(重点手机膜)。
**实时数据已由系统取好, 附在本提示词末尾的 "### xxx 输出" 里, 直接引用即可, 不要重复调工具。**

你只负责三件事: ①同步结论、②今日动态、⑥行动建议。③④⑤ 的条目由系统从工具输出
逐字填充(你只留标题), 不要去写。

口径:
- sync_status 的 "[结论]" 行是同步是否正常的唯一判定(✅/❌/⏳); 数据截至久未推进≠
  同步故障(周末无审批自然停在前一场)。禁止推断日志里没有的故障原因。
- scan_sync_data 的「今日有动态」是 ② 的唯一来源: 有明细就逐条给
  SKU + 型号 + Part + 最新节点 + 当前负责人; 写 "0 单" 就写 "• 今日无新增动态"。
- pipeline_alerts 的「Part9新单待办」可作为 ② 的补充, 工具只给 4 个字段, 不要补。
- ⑥ ≤3 条, 必须指向上面真实出现过的 SKU: 谁、卡在哪个环节、要做什么、什么时候前完成。

**不要编造**: SKU、型号、人名、时间戳、Part 名称逐字来自工具输出; 工具没给的宁可不写。
格式不用操心 —— 结构、列表符号、标题都由代码统一, 写平实的短句即可。
"""


# 午后增量: 只写"相对晨检的变化", 不重复快照 —— 一份文档两次更新, 不刷屏.
PM_TEMPLATE = """现在是当日午后更新({date} {hhmm})。今天上午已出过完整晨检快照(见末尾
【晨检基线】), 这次要的是**相对晨检的变化**, 供同一份日报的午后段落。

**实时数据已由系统取好, 附在本提示词末尾, 直接引用, 不要重复调工具。**

你只负责: ② 09:30 之后的新动态、⑥ 行动建议。变化的判断口径:
- ② 有新动态就逐条写 "SKU 型号 · Part/环节 · 最新审批节点+时间 · 当前负责人";
  没有就写 "• 09:30 后无新增动态"。
- ⑥ 必须承接晨检待办: 对着当前数据把晨检 ⑥ 的每条核一遍 —— 涉及的 SKU 还在
  审批中/仍卡原环节, 就原样保留并标 "(晨检提出, 仍未推进)"; 确实推进了写明到了哪一步。
  这一组不许因为"下午没新变化"就清空。再补因今天新增动态/新增SKU而多出来的动作(≤2 条)。
- ④⑤ 的总数由系统按工具输出校正, 你不要写数字; 不要列 SKU(基线段就在上方)。

**不要编造**, 不要背景解释, 不写"总体平稳"这类套话。格式由代码统一, 写短句即可。

【晨检基线】
{baseline}
【基线结束】"""


def resolve_run_slot(pm_flag: bool, am_flag: bool, now=None) -> tuple:
    """决定本次运行是晨检还是午后 → (is_pm, suffix)。

    显式参数优先(--pm / --am); 两个都没给才退回挂钟推断(兼容旧调用)。

    为什么不能只看挂钟: 挂钟推断会在两种情况下判错段, 而且都是静默的 ——
      a) 午后任务失败后延到次日上午补跑 → 被判成晨检, 午后内容写进晨检段,
         把当天真正的晨检快照顶掉;
      b) 晨检任务拖过 12:00 才跑完 → 被判成午后, 正常午后段被覆盖。
    计划任务各自带 --am / --pm 就与"几点运行"彻底解耦。
    """
    if pm_flag and am_flag:
        raise ValueError("--pm and --am are mutually exclusive")
    if pm_flag or am_flag:
        return bool(pm_flag), "_pm" if pm_flag else ""
    now = now or datetime.now()
    pm = now.hour >= 12
    return pm, "_pm" if pm else ""


def main():
    # 计划任务经 cmd >> task.log 重定向: 不 reconfigure 时 stdout 走 cp936。
    # 只 reconfigure stdout 会让 traceback(走 stderr)仍是 GBK, 日志混编码成乱码,
    # 故两个流一起改成 UTF-8(2026-10-15 修)。
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="mini_agent 每日晨检简报")
    ap.add_argument("--push", action="store_true", help="若配置了钉钉webhook则推送")
    ap.add_argument("--pm", action="store_true",
                    help="显式声明本次为午后简报(不再看挂钟时间)")
    ap.add_argument("--am", action="store_true",
                    help="显式声明本次为晨检(不再看挂钟时间)")
    args = ap.parse_args()
    if args.pm and args.am:
        sys.exit("--pm 与 --am 互斥, 只能给一个")

    cfg = core.load_config()
    if not cfg["api_key"]:
        sys.exit("未配置 API Key")

    workmain = cfg.get("workmain_root") or r"C:\Users\dell\Desktop\work-main"
    core.set_root(workmain)  # 沙箱指向 work-main, 才能读预警文件

    registry = build_readonly_registry(cfg)
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    hhmm = now.strftime("%H:%M")
    # am/pm 以显式参数为准; 两个都没给才退回挂钟推断(兼容旧调用)。
    # 计划任务各自带 --am/--pm, 就与"几点运行"彻底解耦(详见 resolve_run_slot)。
    pm, suffix = resolve_run_slot(args.pm, args.am, now)
    pm_tag = " · PM" if pm else ""      # 午后的显式标记(标题/推送都用它, 方便机器 grep)

    day_file = brief_path(today)
    day_file.parent.mkdir(parents=True, exist_ok=True)

    # 午后先看当天文档里有没有可用的晨检快照: 有就只写增量, 没有就补一份完整版
    # (find_brief 兼容未归档的扁平旧文件, 保证归档切换当天也能接上晨检基线)
    prev = find_brief(today)
    if prev and prev != day_file:      # 归档切换过渡: 把平铺的当天文件搬进月目录, 免得出现两份
        try:
            day_file.parent.mkdir(parents=True, exist_ok=True)
            prev.replace(day_file)
            print(f"[归档] 当天文件搬入月目录: {prev.name} → {month_of(today)}/")
            prev = day_file
        except OSError as e:
            print(f"[归档] 搬迁跳过({e}), 本次仍读 {prev.name}")
    morning, am_label = (load_morning(prev) if pm and prev else ("", "晨检"))
    delta = pm and len(morning.strip()) >= BRIEF_MIN_CHARS
    if pm:
        tag = "午后更新" if delta else "午后补全"
    else:
        tag = "晨检"
        am_label = f"晨检 {hhmm}"

    prefetched = {}
    if delta:
        task = PM_TEMPLATE.format(date=today, hhmm=hhmm, baseline=morning)
        min_chars = PM_MIN_CHARS
        prefetched = prefetch_tool_data(registry)     # 午后同样先取数, 别让模型凭基线猜
        task += prefetch_prompt(prefetched)
        print(f"[{tag}] 增量模式: 基线来自 {day_file.name} 的「{am_label}」段")
    else:
        task = TASK_TEMPLATE.format(date=today, tag=tag)
        min_chars = BRIEF_MIN_CHARS
        prefetched = prefetch_tool_data(registry)     # 代码取好数, 不指望模型自觉
        task += prefetch_prompt(prefetched)
        if pm:
            print(f"[{tag}] 当天无可用晨检快照, 本次按完整六节生成并落同一份文档")

    # 体检的"合法 SKU 白名单"要含今日动态(来自 scan_sync_data), 否则 ② 里的
    # 真实动态会被误判成编造。白名单少一点只会多拦, 不会漏放。
    tool_out = (prefetched.get("pipeline_alerts", "") + "\n"
                + prefetched.get("scan_sync_data", ""))
    client = core.LLMClient(cfg)

    def finish(md: str) -> str:
        """确定性收尾 —— 简报的格式由代码保证, 不靠模型自己和提示词角力.

        原先是"生成 → 体检 → 有问题再重生成一次 → 还有问题再按工具重建", 模型
        每次都可能换个花样违反(2026-10-22 复读提示词 / 10-23 编 SKU / 10-27 ⑥清零).
        现在改成一次生成 + 一次有上限的修复, 剩下全部交给确定性函数:
          normalize(结构) → ③④⑤ 按工具渲染 → ② 按工具重建 → 删幽灵 SKU
          → (午后) ④⑤ 只留计数、计数按工具校正、⑥ 承接晨检待办。
        这样"最坏情况"也只是文字平淡一点, 不会出现编造/复读/写一半。
        """
        md = normalize_brief_md(md)
        if not delta:
            # ③④⑤ 由工具输出确定性渲染(不经模型): 数据段不可能被写坏/复读/写一半。
            # 复用预取结果, 不再重复执行工具(取不到就保留模型原文, 不阻断落盘)。
            try:
                md, hit = replace_data_sections(md, prefetched.get("pipeline_alerts", ""))
                if hit:
                    print(f"[{tag}] ③④⑤ 确定性渲染: {'/'.join(hit)}")
            except Exception as e:                  # noqa: BLE001
                print(f"[{tag}] ③④⑤ 渲染跳过(保留模型原文): {e}")

        probs = brief_health(md, tool_out or None, require_all=not delta)
        if probs:
            print(f"[{tag}] ⚠️ 正文异常: {'; '.join(probs)} — 修复一次")
            md = repair_brief_answer(client, history, md, probs)
            md = normalize_brief_md(md)
            left = brief_health(md, tool_out or None, require_all=not delta)
            if left:
                print(f"[{tag}] ⚠️ 修复后仍有异常: {'; '.join(left)}")
                # ② 编造不能只告警就放过: 假 SKU 会连 ⑥ 的行动建议一起带偏(2026-10-23
                # 出现了"提醒钱七处理 G533"这种指向虚构人和虚构状态的建议), 直接重建。
                md, fixed = replace_dynamic_section(md, tool_out)
                if fixed:
                    print(f"[{tag}] ② 已按工具输出重建(丢弃模型编造的动态)")
                md, n_drop = drop_ghost_skus(md, tool_out)
                if n_drop:
                    print(f"[{tag}] ⑥ 删除 {n_drop} 条引用不存在 SKU 的建议(编造残留)")
                remain = brief_health(md, tool_out or None, require_all=not delta)
                if (fixed or n_drop) and remain:
                    print(f"[{tag}] ⚠️ 仍异常: {'; '.join(remain)} "
                          f"(已按确定性清理后的正文落盘)")

        if delta:
            md, dropped = refine_delta(md, morning)
            if dropped:
                print(f"[{tag}] 过滤 {dropped} 条 ④⑤ 的 SKU 明细"
                      f"(午后只报计数, 明细见晨检段)")
            try:
                now_counts = parse_counts(prefetched.get("pipeline_alerts", ""))
                if (now_counts.get("over") is not None
                        or now_counts.get("warn") is not None):
                    md = apply_counts(md, now_counts, parse_counts(morning))
                    print(f"[{tag}] ④⑤ 计数按工具校正: 临期 {now_counts.get('warn')} / "
                          f"已超期 {now_counts.get('over')} / "
                          f"停滞 {now_counts.get('stale')}")
            except Exception as e:                  # 取不到就用模型写的, 不影响落盘
                print(f"[{tag}] 计数校正跳过: {e}")
            # 晨检的待办不能因为"下午没新变化"就消失(2026-10-27 用户反馈)
            md, n_carry = carry_over_actions(md, morning, tool_out)
            if n_carry:
                print(f"[{tag}] ⑥ 承接晨检待办 {n_carry} 条(标注「晨检提出, 仍未推进」)")
            if len((md or "").strip()) > PM_DELTA_SOFT_MAX:
                print(f"[{tag}] ⚠️ 增量正文 {len(md)} 字(> {PM_DELTA_SOFT_MAX}), "
                      f"模型可能重抄了基线; 已照写, 下次留意")
        return md

    transcript = core.Transcript(f"brief_{now.strftime('%Y%m%d')}{suffix}")
    history = [{"role": "system", "content": core.build_system_prompt()}]
    # output_parser 在最后一次模型调用之后、落盘之前跑: 见 finish() 的说明。
    answer = core.run_task(
        client, registry, core.YoloPolicy(), transcript, history, task, cfg,
        emit=lambda k, d: print(f"  [{k}] {str(d)[:120]}")
        if k in ("tool_call", "tool_result", "fatal", "warning") else None,
        output_parser=finish)
    transcript.log("end", {"reason": "brief", **client.usage})
    transcript.close()

    # 空壳防线: 模型整篇没写出来(限流掐空)时再催一次, 并同样走确定性收尾,
    # 至少保证 ③④⑤ 的实时数据能落盘 —— 正文不足时仍然不推送(见下方 ret)。
    answer = ensure_brief_answer(client, history, answer, min_len=min_chars,
                                 hint="", post=finish)

    if delta:
        body = compose_daily(today, am_body=morning, pm_body=answer,
                             am_label=am_label, pm_label=f"午后更新 {hhmm}{pm_tag}",
                             pm_usage=client.usage_summary())
    elif pm:
        body = compose_daily(today, pm_body=answer,
                             pm_label=f"午后补全 {hhmm}{pm_tag}",
                             pm_usage=client.usage_summary())
    else:
        body = compose_daily(today, am_body=answer, am_label=am_label,
                             am_usage=client.usage_summary())
    day_file.write_text(body, encoding="utf-8")
    print(f"[{tag}] 日报已写入 {day_file}")
    print(f"[{tag}] {client.usage_summary()}")

    ret = 0
    if args.push:
        if len((answer or "").strip()) < min_chars:
            # 空壳防线: 数据都在(工具轮已成功), 只是生成被限流掐死 —— 不推送不占群,
            # 退出码非0让计划任务面板可见, 人工/下次运行补上
            print(f"[{tag}] ⚠️ 正文不足{min_chars}字(限流掐空), 已落盘但"
                  f"不推送钉钉; 请稍后手动重跑 run_brief.bat 补发")
            ret = 1
        else:
            try:
                import dingtalk_push
                # 不再 [:1800] 硬截断: 截断会把 ⑤ 后半段拦腰砍掉(2026-10-20 实测
                # 砍在 "美客多墨西哥 771"), 长文由 dingtalk_push 按段落自动分片
                ok, msg = dingtalk_push.send(f"日报 {today} · {tag} {hhmm}{pm_tag}",
                                             answer or "(空)")
                print(f"[{tag}] 推送{'成功' if ok else '失败'}: {msg}")
            except Exception as e:
                print(f"[{tag}] 推送异常(不影响落盘): {e}")
    return ret


if __name__ == "__main__":
    sys.exit(main())
