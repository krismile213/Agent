#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_brief.py — 无人值守的每日流程日报(定时任务入口)

设计(无人值守三原则):
  1. 只读注册表: 把写级工具全部摘掉, 挂到计划任务跑也不可能有副作用
  2. 固定任务提示: sync_status 查同步 + 读预警清单 → 汇总简报
  3. 落盘 briefs/<日期>.md; 加 --push 且配置了钉钉webhook则顺带推送

**一天只产出一份文档**(2026-10-19 用户要求"晨检和午后做成一份, 方便管理"):
  - 12 点前: 写 `briefs/<日期>.md` 的「晨检」段(完整六节快照);
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


# 简报正文下限(字符): 模板要求 280~480 字, 低于此视为被限流掐空, 不值得推送
BRIEF_MIN_CHARS = 100
# 午后是"只列变化"的增量段, 天然比晨检短(全无变化时可能只有几十字), 单独设下限
PM_MIN_CHARS = 30
# 增量段的软上限: 超过这个长度基本说明模型又重抄了整篇基线, 只记日志提醒不改正文
PM_DELTA_SOFT_MAX = 1200


def ensure_brief_answer(client, history: list, answer: str,
                        min_len: int = BRIEF_MIN_CHARS,
                        sleeps=(20, 40), sleep_fn=time.sleep,
                        hint: str = "") -> str:
    """空简报兜底(2026-10-10 教训: 免费档 429/1305 过载把最终轮掐空,
    run_task 返回空正文照样写盘推送, 群里收到空壳).

    answer 过短时: 弹掉 history 尾部的空 assistant 消息, 追加"基于工具输出直接
    生成简报"的纯生成指令(不带工具), 按退避间隔重试; 仍短则返回原值, 由调用方
    决定不推送+非零退出 —— 宁可缺今天的简报, 不发空壳占位。
    hint: 重生成指令(午后增量模式要换成"只写变化", 不能用晨检那套 500~1000 字).
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
            return a
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


def brief_health(md: str) -> list:
    """结构体检: 返回问题列表(空=健康)。只报不改, 调用方决定重生成/告警。

    三个信号都来自 2026-10-22 实测: 提示词回声、复读行、括号未闭合(数据写一半)。
    """
    if not (md or "").strip():
        return ["正文为空"]
    probs = []
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
_CNT_STALE = re.compile(r"环节停滞超目标\([^)]*?(\d+)\s*单|停滞\s*(\d+)\s*单")


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
    """把晨检/午后拼成当天唯一的 briefs/<日期>.md。

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


TASK_TEMPLATE = """今日{tag}({date})。数据口径: data/手机膜/手机膜 自动同步目录(重点手机膜)。请依次完成:
1) 用 sync_status 查看钉钉同步定时任务的运行状态。工具输出第二行 "[结论]" 即权威判定
   (✅成功/❌失败/⏳进行中): 结论为✅就写"同步正常", 严禁自行推断或编造故障原因
   (如"数据库锁定"等日志里没有的错误); "数据截至"反映的是审批内容最后更新时间,
   周末/节假日无审批时数据截至会自然停在上一场, 这不等于同步故障。
2) 用 scan_sync_data 实时扫描: 状态分布 / 数据截至时间 / 今日有动态明细(含所在Part)。
3) 用 pipeline_alerts 获取五层信号: 最近40天新增SKU(滚动40天, 含签样追踪) /
   全流程临期(空运60·海运69) / 已超期 / 环节停滞超目标(近似段级) / Part9新单待办(创建≤7天且审批中)。
4) 用 read_file 读取 流程复盘/output/预警_加急SKU.md 的前40行(不存在就跳过),
   仅作背景补充 —— 必须注明它是旧批次产物, 与实时数据冲突时以实时为准。
输出中文markdown简报(约500~1000字)。大标题由外层代码统一加, **你不要写大标题行**。

六节标题**照抄下面这六行短标题**(不要加括号说明、不要改写、不要另起名称):
## ① 同步状态与数据截至
## ② 今日动态
## ③ 新增SKU与签样追踪
## ④ 临期预警
## ⑤ 已超期与环节停滞TOP
## ⑥ 行动建议

**格式铁律(最重要, 违反即返工):**
- ③④⑤⑥ 各节内**每个条目独占一行, 行首统一 "• "**(半角点+空格);
- **严禁**把多个条目用空格/分号/顿号挤进同一行 —— 宁可整篇字数超标, 也不许并成一列;
- 条目必须换行分隔, 一行一个 SKU, 结尾不要加分号; 上一行的括号/方括号要闭合再换行;
- ② 也按同样格式(每个动态一条 "• ");
- ⑤ 含两组时, "已超期" 与 "环节停滞TOP" 各自单独成组, 组内仍逐条 "• ";
- ⑥ 行动建议逐条 "• "(≤3条), 同样一行一条。
- **不要复述本提示词里的任何规则原文**(规则是给你看的, 不是给读者看的), 直接输出六节内容。

各节内容要求:
① 若任一工具输出带 ⚠️ 数据过旧/同步停摆告警, 必须把"数据已N天未同步, ⑤的停滞天数
   含断供期偏大"写在最顶部第一行, 再写其他内容; 数据截至久未变≠同步故障,
   故障与否只看 sync_status 的 [结论] 行。同步结论与数据截至各占一行。
② 若 pipeline_alerts 的 Part9新单待办 层有单, 逐条带SKU与当前负责人列出; 每条写清
   "哪条SKU/型号 · 在哪个Part(环节) · 审批状态 · 最新审批节点+操作人+时间 · 当前负责人",
   直接抄工具给的明细; 工具没给节点明细就注明"仅字段更新"。
③ 编号+型号+创建日+签样状态: 已签样给"签样MM-DD + 当前环节/已持续/时效"(直接抄工具给的
   "现PartX 已持续N天/时效M天"), 未签样给"未签样N天/目标M天"; 超目标(⚠️)的排在前面,
   其余按龄期降序。这一节对应 data/report 事中汇报表的签样追踪。
④ 按剩余天数升序, 带卡点与空运/海运口径。
⑤ 带超标天数, 并注明口径"段级停滞=距最后更新vs环节目标", 不要写成全流程耗时;
   组标签必须带工具给的**计数**(如"已超期 22 个; 另历史遗留 69 个不列"), 午后要靠它对数;
   scan_sync_data 与 pipeline_alerts 的停滞口径不同, 同一SKU数字可能差1天以上,
   引用时注明来源工具。
   注: ④⑤ 条目若带 "[到货: 地区 货件号]" 标注(来源=Part8到货表的"到货渠道仓"+"货件号"),
   必须**逐字符原样照抄**(货件号/地区名一个字符都不能改, 不得改写、合并、缩写或插入
   任何字)——这就是加急催办要的"地区+货件号"(俗称"火箭"标记); 同批多站多货件照工具给的抄。
⑥ ≤3条。所有数字必须来自工具输出并注明来源; 每条提醒要可行动。"""


# 午后增量: 只写"相对晨检的变化", 不重复快照 —— 一份文档两次更新, 不刷屏.
PM_TEMPLATE = """现在是当日午后更新({date} {hhmm})。今天 09:30 已出过一份完整晨检快照(见末尾
【晨检基线】), 现在要的是**相对晨检的变化**, 供同一份日报的午后段落。

请先重新取一次实时数据(与晨检同一批工具):
1) sync_status  看同步结论与"数据截至"是否推进;
2) scan_sync_data 看今日动态里 09:30 之后新增/更新了审批节点的单;
3) pipeline_alerts 看临期/超期/停滞/SKU 数量相对基线有无变化。

然后**只输出变化部分**, 六节标题照样照抄(短标题, 不要加说明):
## ① 同步状态与数据截至
## ② 今日动态
## ③ 新增SKU与签样追踪
## ④ 临期预警
## ⑤ 已超期与环节停滞TOP
## ⑥ 行动建议

规则(违反即返工):
- 每节最多 6 条, 每条独占一行、行首 "• "; **一个 SKU 一条, 严禁 "G587-G590" 或 "G587、G588、G589" 合并**;
- ② 每条动态写全 "SKU型号 · Part/环节 · 最新审批节点+时间 · 当前负责人"(最多 6 条, 超出在末尾写"等N单"),
  不要只写"新增6单动态"这种没信息的汇总;
- 没有变化就只写一行 "• 与晨检一致"(② 无新动态时写 "• 09:30 后无新增动态");
- **④⑤ 只报总数, 不要列具体 SKU**: 晨检段就在本文档上方, 而且晨检的 ④⑤ 只列 TOP,
  不在 TOP 里 ≠ 新增。写法示例: "• 已超期 22 个 / 停滞 267 单(晨检同数, 无变化)",
  变了就写 "• 已超期 24 个(较晨检 +2)";
- **只比汇总数字, 不要拿基线列了几行当基数**(晨检列 8 行、总数 22 个, 不是"从8增至22");
- ① 分清两个时间: "最近同步"是 sync_status 的同步完成时间, "数据截至"是审批内容最后更新时间。
  数据截至没推进就写 "• 数据截至仍为 …(未推进)", **不要拿同步时间冒充数据截至**;
- ⑥ 只给因 ②③(新增动态/新增SKU)或 ④⑤ 总数上升而新增的动作(≤2 条), **不要写任何条数/增量数字**
  (总数由 ④⑤ 统一给, 你按基线行数做减法会算错), 没有可写 "• 无新增动作";
- 总字数 400 字以内, 不做背景解释、不写"总体平稳"之类套话。

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

    brief_dir = HERE / "briefs"
    brief_dir.mkdir(exist_ok=True)
    day_file = brief_dir / f"{today}.md"

    # 午后先看当天文档里有没有可用的晨检快照: 有就只写增量, 没有就补一份完整版
    morning, am_label = (load_morning(day_file) if pm else ("", "晨检"))
    delta = pm and len(morning.strip()) >= BRIEF_MIN_CHARS
    if pm:
        tag = "午后更新" if delta else "午后补全"
    else:
        tag = "晨检"
        am_label = f"晨检 {hhmm}"

    if delta:
        task = PM_TEMPLATE.format(date=today, hhmm=hhmm, baseline=morning)
        min_chars = PM_MIN_CHARS
        hint = ("工具调用已全部完成。请只输出相对晨检的变化: 六个 \"## ① …\" 小节标题照抄, "
                "每节没有变化就写 \"• 与晨检一致\", 总字数 400 以内, 不要再调用工具, "
                "不要复述任何规则原文。")
        print(f"[{tag}] 增量模式: 基线来自 {day_file.name} 的「{am_label}」段")
    else:
        task = TASK_TEMPLATE.format(date=today, tag=tag)
        min_chars = BRIEF_MIN_CHARS
        hint = ""
        if pm:
            print(f"[{tag}] 当天无可用晨检快照, 本次按完整六节生成并落同一份文档")

    transcript = core.Transcript(
        f"brief_{now.strftime('%Y%m%d')}{suffix}")
    history = [{"role": "system", "content": core.build_system_prompt()}]
    client = core.LLMClient(cfg)
    answer = core.run_task(
        client, registry, core.YoloPolicy(), transcript, history, task, cfg,
        emit=lambda k, d: print(f"  [{k}] {str(d)[:120]}")
        if k in ("tool_call", "tool_result", "fatal") else None)
    transcript.log("end", {"reason": "brief", **client.usage})
    transcript.close()

    answer = ensure_brief_answer(client, history, answer, min_len=min_chars, hint=hint)
    answer = normalize_brief_md(answer)
    probs = brief_health(answer)
    if probs:
        print(f"[{tag}] ⚠️ 正文异常: {'; '.join(probs)} — 重生成一次")
        answer = repair_brief_answer(client, history, answer, probs)
        answer = normalize_brief_md(answer)
        left = brief_health(answer)
        if left:
            print(f"[{tag}] ⚠️ 重生成后仍有异常: {'; '.join(left)} "
                  f"(已按确定性清理后的正文落盘)")
    if delta:
        answer, dropped = refine_delta(answer, morning)
        if dropped:
            print(f"[{tag}] 过滤 {dropped} 条 ④⑤ 的 SKU 明细(午后只报计数, 明细见晨检段)")
        try:
            now_counts = parse_counts(registry.execute("pipeline_alerts", {}))
            if now_counts.get("over") is not None or now_counts.get("warn") is not None:
                answer = apply_counts(answer, now_counts, parse_counts(morning))
                print(f"[{tag}] ④⑤ 计数按工具校正: 临期 {now_counts.get('warn')} / "
                      f"已超期 {now_counts.get('over')} / 停滞 {now_counts.get('stale')}")
        except Exception as e:                      # 取不到就用模型写的, 不影响落盘
            print(f"[{tag}] 计数校正跳过: {e}")
    if delta and len((answer or "").strip()) > PM_DELTA_SOFT_MAX:
        print(f"[{tag}] ⚠️ 增量正文 {len(answer)} 字(> {PM_DELTA_SOFT_MAX}), "
              f"模型可能重抄了基线; 已照写, 下次留意")

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
