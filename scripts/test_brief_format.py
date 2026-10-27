#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/test_brief_format.py — 简报格式兜底 normalize_brief_md 单测(零 LLM)

背景(2026-10-15 用户反馈): 简报③④节"看不清晰"。
根因是 **markdown 语义**: 连续普通行会被渲染器折叠成一个段落, 逐行但不带列表
标记的 SKU 条目一到钉钉/Web 上就挤成一坨。光靠提示词不可靠(模型经常忘),
故在 daily_brief.normalize_brief_md 里做了确定性兜底, 本文件钉住它的行为。
"""

import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import daily_brief as db  # noqa: E402

FAILS = []


def _brief_file(name: str) -> Path:
    """briefs 按月份归档(2026-10-22 起) → briefs/<YYYY-MM>/<name>; 兼容扁平旧布局。"""
    base = HERE.parent / "briefs"
    p = base / name[:7] / name
    return p if p.exists() else base / name


def check(name, cond, extra=""):
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def bullets(out):
    return [l for l in out.split("\n") if l.startswith("• ")]


def test_bare_sku_lines_get_bullets():
    """③④ 里逐行但不带标记的裸 SKU 条目 → 每条前置 "• "。"""
    out = db.normalize_brief_md(
        "## ③ 最近40天新增SKU\n"
        "G598 Xiaomi Redmi 17 4G/5G（创建09-16，签样09-23）\n"
        "G599 vivo X500 Pro 5G（创建09-28，签样10-12）\n")
    b = bullets(out)
    check("裸条目前置 bullet", len(b) == 2 and b[0].startswith("• G598"), str(b))
    check("条目文字一个字符没动", "（创建09-16，签样09-23）" in b[0])
    check("条目数不变(没漏没并)", out.count("G598") == 1 and out.count("G599") == 1)


def test_inline_joined_entries_split():
    """多个条目被空格串在一行(用户实际看到的形态) → 按 " Gxxx " 边界拆行。"""
    out = db.normalize_brief_md(
        "G598 Xiaomi Redmi 17 4G/5G（创建09-16） G599 vivo X500 Pro 5G（创建09-28） "
        "G600 Xiaomi 18 Pro 5G（创建09-28）")
    check("空格串行拆成 3 条", len(bullets(out)) == 3, str(bullets(out)))
    check("拆开后仍是同一批条目",
          all(s in out for s in ("G598", "G599", "G600")))


def test_semicolon_joined_split():
    """⑤ 环节停滞TOP 常用分号串行 → 也要拆。"""
    out = db.normalize_brief_md(
        "G493 Part6 停滞108天/目标13天（超95天）；G550 Part8 停滞120天/目标35天（超85天）")
    check("分号串行拆成 2 条", len(bullets(out)) == 2, str(bullets(out)))


def test_group_label_inline_entry_split():
    """"**已超期**（17个）：G584 …" → 标签行 + bullet 条目行。"""
    out = db.normalize_brief_md("**已超期**（17个）：G584 Realme NARZO 100x 5G 已超6天[海运]")
    lines = [l for l in out.split("\n") if l.strip()]
    check("组标签单独成行", lines[0] == "**已超期**（17个）", str(lines))
    check("首条目落到 bullet 行", lines[1] == "• G584 Realme NARZO 100x 5G 已超6天[海运]")


def test_no_false_split_in_arrival_brackets():
    """防误拆: [到货: 地区 货件号; …] 里的分号不是条目边界, 货件号必须原样保留。"""
    tail = ("G587 Xiaomi Poco M8 Power 5G 剩1天[空运]（卡Part8）"
            "[到货：亚马逊墨西哥 FBA19PZ6XB8L；美客多墨西哥 77190375；"
            "亚马逊印度 FBA15MGCZGZ8 共14个]")
    out = db.normalize_brief_md(tail)
    check("到货信息整条不被拆断", out.count("\n") == 1 and out.strip().endswith("共14个]"), str(out))
    check("货件号逐字符原样",
          "FBA19PZ6XB8L；美客多墨西哥 77190375；亚马逊印度 FBA15MGCZGZ8 共14个" in out)


def test_dup_h1_title_removed():
    """外层代码已写 "# {tag} {date}", LLM 又多写一遍会重复 → 去重。"""
    out = db.normalize_brief_md("# 午后简报 2026-10-15\n\n## ① 同步状态与数据截至\n同步正常。")
    check("LLM 多写的大标题被去掉", out.count("午后简报") == 0, str(out))
    check("小节标题保留", out.startswith("## ① 同步状态与数据截至"), str(out))


def test_list_markers_unified():
    """"- " 与 "1. " 统一成 "• ", 全文观感一致。"""
    out = db.normalize_brief_md("- G601 Xiaomi 18 Pro Max 5G · Part3\n1. 立即处理G601\n")
    check("'- ' 归一为 '• '", "• G601 Xiaomi 18 Pro Max 5G · Part3" in out, str(out))
    check("'1. ' 归一为 '• '", "• 立即处理G601" in out, str(out))


def test_plain_lines_untouched():
    """普通句/引用/空行/汇总行不许被动到。"""
    src = ("## ① 同步状态与数据截至\n\n同步正常。数据截至时间：2026-10-15 13:57（sync_status）\n\n"
           "> 用量: LLM调用5次\n")
    out = db.normalize_brief_md(src)
    check("普通句原样", "同步正常。数据截至时间：2026-10-15 13:57（sync_status）" in out)
    check("引用块原样", "> 用量: LLM调用5次" in out)
    check("空行结构保留", out.count("\n\n") >= 2, repr(out))


def test_empty_and_none():
    check("None 直通不炸", db.normalize_brief_md(None) is None)
    check("空串直通", db.normalize_brief_md("") == "")


def test_real_brief_idempotent():
    """拿昨天(2026-10-15)真实简报跑一遍: 能改成全 bullet, 且条目数守恒。"""
    p = _brief_file("2026-10-15_pm.md")
    if not p.exists():
        print("  - (跳过) 真实简报夹具不存在")
        return
    raw = p.read_text(encoding="utf-8").replace("\r\n", "\n")
    body = "\n".join(l for l in raw.split("\n")
                     if l.strip() and not l.startswith("> 用量:")
                     and not l.startswith("# 午后简报"))
    out = db.normalize_brief_md(body)
    n_src = body.count("G5") + body.count("G4")
    n_out = out.count("G5") + out.count("G4")
    check("真实简报: SKU 出现次数守恒(没吞条目)", n_src == n_out, f"{n_src} vs {n_out}")
    check("真实简报: ③④ 全部变成 bullet", len(bullets(out)) >= 20, str(len(bullets(out))))
    check("真实简报: 无条目残留裸行(⑤ 组标签除外)",
          all(l.startswith(("##", "•", "**", ">")) or "：" in l or not l.strip()
              or "今日有动态" in l or "同步正常" in l
              for l in out.split("\n")))


def test_section_titles_canonical():
    """2026-10-19 用户反馈"午后有些换行没对齐": 模板说明被抄进小节标题,
    同一节在晨检/午后长短不一 → 一律换成固定短标题。"""
    am = db.normalize_brief_md("## ③ 最近40天新增SKU(滚动40天, 编号+型号+创建日+签样状态)\n")
    pm = db.normalize_brief_md(
        "## ③ 最近40天新增SKU(滚动40天, 编号+型号+创建日+签样状态: 已签样给\"签样MM-DD + "
        "当前环节/已持续/时效\"(直接抄工具给的\"现PartX 已持续N天/时效M天\"), 未签样给"
        "\"未签样N天/目标M天\"并把超目标(⚠️)的排在前面、其余按龄期降序)\n")
    check("晨检长标题 → 固定短标题", am.startswith("## ③ 新增SKU与签样追踪"), am)
    check("午后长标题 → 同一短标题(两轮对齐)", pm.startswith("## ③ 新增SKU与签样追踪"), pm)
    check("①~⑥ 全部有规范标题", all(db.SECTION_TITLES[k] for k in "①②③④⑤⑥"))
    # 无 # 的短行也当标题
    out = db.normalize_brief_md("① 同步状态与数据截至\n同步正常\n")
    check("无#小节标题也被规范", out.startswith("## ① 同步状态与数据截至"), out)


def test_section1_lines_get_bullets():
    """① 里的短数据行不加 bullet, markdown 会折成一段(就是"换行没对齐")."""
    out = db.normalize_brief_md(
        "## ① 同步状态与数据截至\n\n同步正常\n数据截至: 2026-10-19 11:45\n")
    b = bullets(out)
    check("① 两行都变 bullet", len(b) == 2, str(b))
    check("数据截至内容没改", any("数据截至: 2026-10-19 11:45" in x for x in b), str(b))
    # 断供警示句保持普通段落(它是提醒, 不是条目)
    out2 = db.normalize_brief_md(
        "## ① 同步状态与数据截至\n数据已3天未同步，⑤的停滞天数含断供期偏大\n")
    check("断供警示句不加 bullet",
          "数据已3天未同步，⑤的停滞天数含断供期偏大" in out2
          and "• 数据已3天" not in out2, out2)


def test_dup_and_double_bullet():
    """工具行级重复(G558 两遍)只留一条; 已有 "• " 的不能再叠一个 "• "."""
    out = db.normalize_brief_md(
        "## ⑤ 已超期与环节停滞TOP\n"
        "• G558 Part8 停滞119天/目标35天 (超84天)\n"
        "• G558 Part8 停滞119天/目标35天 (超84天)\n"
        "G556 Part8 停滞119天/目标35天 (超84天)\n")
    check("重复条目被去重", out.count("G558") == 1, out)
    check("没有双 bullet", "• •" not in out, out)
    check("裸条目补成单 bullet", "• G556" in out, out)
    # 不同小节里的同名条目不算重复(每节独立去重)
    out2 = db.normalize_brief_md(
        "## ⑤ 已超期与环节停滞TOP\n• G558 停滞119天\n## ⑥ 行动建议\n• G558 停滞119天\n")
    check("跨小节不去重", out2.count("G558") == 2, out2)


def test_compose_daily_merged():
    """晨检 + 午后 → 同一份 briefs/<YYYY-MM>/<日期>.md; 重跑午后只替换午后段。"""
    am = "## ① 同步状态与数据截至\n• 同步正常\n## ③ 新增SKU与签样追踪\n• G601 Xiaomi 18 Pro Max 5G\n"
    pm = "## ① 同步状态与数据截至\n• 与晨检一致\n## ② 今日动态\n• G603 Honor Magic V6 5G 新单\n"
    doc = db.compose_daily("2026-10-19", am_body=am, pm_body=pm,
                           am_label="晨检 09:30", pm_label="午后更新 14:10",
                           am_usage="u1", pm_usage="u2")
    check("只有一份文档(标题是日报)", doc.startswith("# 日报 2026-10-19"), doc[:40])
    check("晨检段在前、午后段在后",
          doc.index("## 晨检 09:30") < doc.index("## 午后更新 14:10"), "order")
    check("六节降到三级标题", "### ① 同步状态与数据截至" in doc, doc[:200])
    check("两段各带用量", doc.count("> 用量:") == 2, str(doc.count("> 用量:")))
    check("段落间有分隔线", "\n---\n" in doc, "sep")

    # 重跑午后: 读回晨检基线 → 再合成, 午后段仍只有一个
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "2026-10-19.md"
        p.write_text(doc, encoding="utf-8")
        morning, label = db.load_morning(p)
        check("能读回晨检基线", "G601" in morning and "G603" not in morning, morning[:60])
        check("读回标题保留时间", label == "晨检 09:30", label)
        doc2 = db.compose_daily("2026-10-19", am_body=morning, pm_body=pm,
                                am_label=label, pm_label="午后更新 14:40")
        check("重跑不叠第二段午后", doc2.count("## 午后更新") == 1, str(doc2.count("## 午后更新")))
        check("重跑后晨检条目没丢", "G601" in doc2, "G601")
        check("重跑后旧午后内容被替换", "14:10" not in doc2, "14:10")


def test_compose_pm_only_when_morning_missing():
    """晨检缺档(任务失败/手动午后首跑) → 午后写成完整快照, 不硬做增量。"""
    doc = db.compose_daily("2026-10-19", pm_body="## ① 同步状态与数据截至\n• 同步正常\n",
                           pm_label="午后补全 17:20", pm_usage="u")
    check("标注晨检缺档", "晨检缺档" in doc, doc[:120])
    check("没有空晨检段", "## 晨检" not in doc, doc[:200])


def test_refine_delta_filters_false_new():
    """② 午后把"晨检 TOP 之外的单"说成新增 → ④⑤ 只许报计数, 带 SKU 的行全部过滤。"""
    baseline = ("• G587 Xiaomi Poco M8 Power 5G 已超3天[空运]\n"
                "• G588 Google Pixel 11 Pro Fold 5G 已超3天[空运]\n")
    pm = ("## ⑤ 已超期与环节停滞TOP\n"
          "• 已超期 22 个 / 停滞 267 单\n"
          "• G587 Xiaomi Poco M8 Power 5G 已超3天[空运]\n"
          "• G586 Realme GT 6 Pro 5G 已超5天[空运]\n")
    out, dropped = db.refine_delta(pm, baseline)
    check("④⑤ 的 SKU 明细被过滤", dropped == 2, str(dropped))
    check("基线内 SKU 不回显", "G587" not in out, out)
    check("基线外的「新增」也不回显(不是真新增)", "G586" not in out, out)
    check("计数行保留", "已超期 22 个 / 停滞 267 单" in out, out)
    # 非 ④⑤ 小节不动(③ 里列同一个 SKU 是正常的)
    out3, d3 = db.refine_delta("## ③ 新增SKU与签样追踪\n• G587 Xiaomi Poco M8 Power 5G\n", baseline)
    check("③ 不被误删", "G587" in out3 and d3 == 0, f"{out3}|{d3}")
    # 全部被过滤 → 补 "与晨检一致"
    out4, _ = db.refine_delta("## ⑤ 已超期与环节停滞TOP\n• G587 Xiaomi Poco M8 Power 5G 已超3天\n", baseline)
    check("空小节补与晨检一致", "• 与晨检一致" in out4, out4)


def test_parse_and_apply_counts():
    """④⑤ 计数由脚本解析 pipeline_alerts 标题行, 不靠模型复述(实测 22 被写成 8)。"""
    tool = ("== 临期预警(剩≤15天, 9个) ==\n"
            "== 已超期(超全流程线, 22个; 另历史遗留69个不列) ==\n"
            "== 环节停滞超目标(段级口径: 距最后更新vs环节目标, 267单, 行级可重复) TOP ==\n")
    c = db.parse_counts(tool)
    check("临期计数", c["warn"] == 9, str(c))
    check("已超期计数(取全流程线, 不是历史遗留)", c["over"] == 22, str(c))
    check("停滞计数", c["stale"] == 267, str(c))

    pm = ("## ④ 临期预警\n• 与晨检一致\n"
          "## ⑤ 已超期与环节停滞TOP\n• 已超期 8 个\n"
          "## ⑥ 行动建议\n• 跟进 G587\n")
    out = db.apply_counts(pm, c, {"warn": 9, "over": 8, "stale": None})
    check("④ 用工具计数", "• 临期 9 个(与晨检一致)" in out, out)
    check("⑤ 总数不被模型写小", "• 已超期 22 个(晨检 8 个, +14)" in out, out)
    check("⑤ 停滞单列一行", "• 环节停滞 267 单(晨检未记录总数)" in out, out)
    check("⑥ 未被改动", "• 跟进 G587" in out, out)
    check("小节之间仍有空行", "\n\n## ⑥" in out, repr(out[-60:]))
    check("④⑤ 标题下不额外空行", "### ④ 临期预警\n• 临期" in out.replace("## ④", "### ④"), out)

    # ⑥ 里"新增的N个"是拿基线行数减出来的假增量 → 去数字保动作
    pm6 = "## ⑥ 行动建议\n• 立即跟进新增的14个超期SKU\n• 处理新增的262个停滞单据\n"
    out6, d6 = db.refine_delta(pm6, "• G587 xxx\n")
    check("⑥ 假增量数字被去掉", "新增的14个" not in out6 and "新增的262个" not in out6, out6)
    check("⑥ 动作对象保留", "跟进新增的超期SKU" in out6, out6)


def test_load_morning_strips_separator():
    """重跑午后不能出现两条 "---"(拆分基线时要丢掉分隔线)。"""
    am = "## ① 同步状态与数据截至\n• 同步正常\n"
    pm = "## ① 同步状态与数据截至\n• 与晨检一致\n"
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "2026-10-19.md"
        p.write_text(db.compose_daily("2026-10-19", am_body=am, pm_body=pm), encoding="utf-8")
        morning, label = db.load_morning(p)
        check("基线里没有分隔线", "---" not in morning, morning)
        doc2 = db.compose_daily("2026-10-19", am_body=morning, pm_body=pm,
                                am_label=label, pm_label="午后更新 14:40")
        check("重排后只有一条分隔线", doc2.count("\n---\n") == 1, str(doc2.count("\n---\n")))


def test_resolve_run_slot_explicit_beats_clock():
    """--pm/--am 显式参数必须压过挂钟: 上午补跑午后简报不能被判成晨检。"""
    am10 = datetime(2026, 10, 20, 10, 0)      # 上午 10 点
    pm23 = datetime(2026, 10, 20, 23, 30)     # 深夜
    check("--pm 在上午也判午后", db.resolve_run_slot(True, False, am10) == (True, "_pm"))
    check("--am 在深夜也判晨检", db.resolve_run_slot(False, True, pm23) == (False, ""))
    try:
        db.resolve_run_slot(True, True, am10)
        check("--pm + --am 互斥抛错", False, "没抛错")
    except ValueError:
        check("--pm + --am 互斥抛错", True)


def test_resolve_run_slot_clock_fallback():
    """都没给参数时保持旧的挂钟行为(向后兼容旧调用/旧计划任务)。"""
    check("12点前→晨检",
          db.resolve_run_slot(False, False, datetime(2026, 10, 20, 9, 30)) == (False, ""))
    check("12点后→午后",
          db.resolve_run_slot(False, False, datetime(2026, 10, 20, 14, 45)) == (True, "_pm"))


def test_pm_label_carries_tag():
    """午后段的标签要带 PM 标记(用户要求"下午的加个pm标签")。"""
    am = "## ① 同步状态与数据截至\n• 同步正常\n"
    pm_body = "## ① 同步状态与数据截至\n• 与晨检一致\n"
    doc = db.compose_daily("2026-10-20", am_body=am, pm_body=pm_body,
                           am_label="晨检 09:30", pm_label="午后更新 14:10 · PM")
    check("午后段标题带 PM", "## 午后更新 14:10 · PM" in doc, doc[:200])
    check("晨检段不带 PM", "## 晨检 09:30" in doc)
    check("更新记录里也带 PM", "午后更新 14:10 · PM" in doc)


def test_scrub_echo_line_dropped():
    """2026-10-22 事故: 模型把提示词"格式铁律"当正文吐出来, 整行必须清掉。"""
    raw = ("## ⑤ 已超期与环节停滞TOP\n\n"
           "• G591 Honor Robot Phone 5G 已超5天[空运]\n"
           "严格遵守格式要求，每个条目独占一行，行首统一 \"• \"，条目必须换行分隔。\n"
           "• G587 Xiaomi Poco M8 已超6天[空运]\n")
    out = db.normalize_brief_md(raw)
    check("回声: 指令原句整行被清掉", "严格遵守格式要求" not in out)
    check("回声: 正常条目不受影响", "• G591" in out and "• G587" in out)


def test_scrub_midline_injection():
    """条目写到一半被注入规则文本: 括号闭合→保留前半条; 不闭合→整行丢。"""
    good = ("## ⑤ 已超期与环节停滞TOP\n"
            "• G589 vivo S2 5G 已超6天[空运] (启动08-17, 卡Part8) 严格遵守格式要求，一行一个 SKU\n")
    out = db.normalize_brief_md(good)
    check("中途注入(括号闭合): 截断保留数据", "G589 vivo S2 5G 已超6天" in out)
    check("中途注入(括号闭合): 注入文本消失", "严格遵守格式要求" not in out)

    bad = ("## ⑤ 已超期与环节停滞TOP\n"
           "• G589 vivo S2 5G 已超6天[空运] [到货: 亚马逊墨西哥 F严格遵守格式要求，一行一个 SKU\n")
    out2 = db.normalize_brief_md(bad)
    check("中途注入(括号未闭合): 整行丢弃, 不留半条货件号",
          "G589" not in out2 and "亚马逊墨西哥 F" not in out2)


def test_scrub_consecutive_repeats():
    """复读 30 遍的同一行只留一条(本次事故的形态)。"""
    line = '⑤ 含两组时, "已超期" 与 "环节停滞TOP" 各自单独成组, 组内仍逐条 "• "。'
    raw = "## ⑤ 已超期与环节停滞TOP\n\n" + "\n\n".join([line] * 30) + "\n"
    out = db.normalize_brief_md(raw)
    check("复读: 30 行重复只留 1 行", out.count("组内仍逐条") <= 1,
          extra=str(out.count("组内仍逐条")))


def test_duplicate_section_header_collapsed():
    """模型重启小节 → 同一小节标题出现两次, 只留第一个。"""
    raw = ("## ⑤ 已超期与环节停滞TOP\n\n已超期 22 个\n• G591 已超5天\n\n"
           "## ⑤ 已超期与环节停滞TOP\n\n环节停滞TOP 228单\n• G493 停滞115天\n")
    out = db.normalize_brief_md(raw)
    check("重复小节标题: 只保留一个", out.count("⑤ 已超期与环节停滞TOP") == 1,
          extra=str(out.count("⑤ 已超期与环节停滞TOP")))
    check("重复小节标题: 两组内容都在", "G591" in out and "G493" in out)


def test_brief_health_signals():
    """体检要能抓住本次三种病征, 干净正文不许误报。"""
    dirty = ("## ⑤ 已超期与环节停滞TOP\n"
             "• G589 vivo S2 5G [到货: 亚马逊墨西哥 F\n"
             "严格遵守格式要求\n"
             "x\nx\nx\n")
    probs = db.brief_health(dirty)
    joined = " ".join(probs)
    check("体检: 抓到提示词回声", "回声" in joined, extra=joined)
    check("体检: 抓到括号未闭合", "括号未闭合" in joined, extra=joined)
    check("体检: 抓到连续重复行", "重复行" in joined, extra=joined)
    check("体检: 空正文报错", db.brief_health("  ") == ["正文为空"])
    clean = ("## ⑤ 已超期与环节停滞TOP\n\n已超期 22 个; 另历史遗留 69 个不列\n"
             "• G591 Honor Robot Phone 5G 已超5天[空运]\n\n"
             "环节停滞TOP 228单 (段级停滞=距最后更新vs环节目标)\n"
             "• G493 Part6 停滞115天/目标13天 (超102天)\n")
    check("体检: 干净正文零问题", db.brief_health(clean) == [],
          extra=str(db.brief_health(clean)))


def test_health_detects_fabricated_skus():
    """2026-10-23 事故: 模型零工具调用, ② 编出三条"今日动态"。

    真实情况是 scan_sync_data 报"今日有动态: 0 单"、Part9 待办是 G598~G601,
    模型却写了 G531/532/533(真实存在, 但是 2026-03-02 就已结束的老单),
    还配了"张三/李四/王五"等虚构人名。体检必须抓出来。
    """
    tool_out = ("数据截至(最新更新时间): 2026-10-22 17:39\n"
                "今日有动态: 0 单\n"
                "== Part9新单待办(创建≤7天且审批中, 2单) ==\n"
                "  G598 Xiaomi Redmi 17 4G/5G (创建 10-22 14:53, 当前负责人: 胡浩明)\n"
                "  G599 vivo X500 Pro 5G (创建 10-19 17:03, 当前负责人: 胡浩明)\n")
    fake = ("## ② 今日动态\n"
            "• SKU G531 · 在Part1(素材加工) · 审批状态：审批中 · 当前负责人：李四\n"
            "• SKU G532 · 在Part2(广告投放) · 审批状态：审批中 · 当前负责人：赵六\n"
            "\n## ③ 新增SKU与签样追踪\n"
            "• G598 Xiaomi Redmi 17 4G/5G (创建 09-16)\n")
    probs = db.brief_health(fake, tool_out)
    joined = " ".join(probs)
    check("编造检测: 抓到工具输出里没有的 SKU", "疑似编造" in joined, extra=joined)
    check("编造检测: 点出 G531", "G531" in joined, extra=joined)
    check("编造检测: 不冤枉 ③ 里的真实 SKU(只看②)", "G598" not in joined, extra=joined)

    ok = ("## ② 今日动态\n"
          "• 今日无新增动态\n"
          "• G598 Xiaomi Redmi 17 4G/5G (创建 10-22 14:53, 当前负责人: 胡浩明)\n"
          "\n## ③ 新增SKU与签样追踪\n")
    check("编造检测: 照抄工具的真实条目零问题", db.brief_health(ok, tool_out) == [],
          extra=str(db.brief_health(ok, tool_out)))
    check("编造检测: 不给工具输出时不判(无从比对)",
          "疑似编造" not in " ".join(db.brief_health(fake)))


_TOOL_OUT = """目录: data/手机膜/手机膜  口径: 全流程线 空运60/海运69(权威表)
数据截至(最新更新时间): 2026-10-21 17:49

== 最近40天新增SKU(09-12~10-22, 8个; 含签样追踪) ==
  G598 Xiaomi Redmi 17 4G/5G (创建 09-16, 签样 09-23, 现Part8 已持续1天/时效15天)
  G602 Samsung Galaxy F70 Pro/M47 5G (创建 10-10, ⚠️未签样 15天/目标12天)
  未签样待跟进 4 个(锚点: 取数/目标12天·或创建/目标87天); 最久 G603(15天)

== 临期预警(剩≤15天, 6个) ==
  G592 Xiaomi Poco F9 Pro/Redmi K100 Pro 5G 剩2天[空运] (启动08-25, 卡Part8) [到货: 美客多墨西哥 77739558；亚马逊墨西哥 FBA19QR4PKLJ 共14个]

== 已超期(超全流程线, 22个; 另历史遗留69个不列) ==
  G591 Honor Robot Phone 5G 已超5天[空运] (启动08-18, 卡Part2)
  G589 vivo S2 5G 已超6天[空运] (启动08-17, 卡Part8) [到货: 亚马逊墨西哥 FBA19PZ6XB8L；美客多墨西哥 77190375 共14个]

== 环节停滞超目标(段级口径: 距最后更新vs环节目标, 228单, 行级可重复) TOP ==
  G493 Part6 停滞115天/目标13天 (超102天)
  G550 Part8 停滞115天/目标35天 (超80天)

== Part9新单待办(创建≤7天且审批中, 3单; 悬挂旧单不在此列) ==
  G600 Xiaomi 18 Pro 5G (创建 10-16 11:23, 当前负责人: 胡浩明)
"""


def test_render_dynamic_section():
    """② 的兜底渲染: 模型编造时按工具输出重建(2026-10-23 事故)。

    夹具刻意按真实顺序拼接(pipeline_alerts 在前、scan_sync_data 在后),
    复现"scan 的缩进行被归到最后一个 == 段"这个串味场景。
    """
    pa = ("数据截至(最新更新时间): 2026-10-22 17:39\n"
          "== 环节停滞超目标(段级口径, 5单) TOP ==\n"
          "  G493 Part6 停滞116天/目标13天 (超103天)\n"
          "\n== Part9新单待办(创建≤7天且审批中, 2单; 悬挂旧单不在此列) ==\n"
          "  G600 Xiaomi 18 Pro 5G (创建 10-16 11:23, 当前负责人: 胡浩明)\n"
          "  G599 vivo X500 Pro 5G (创建 10-19 17:03, 当前负责人: 胡浩明)\n")
    scan0 = ("今日有动态: 0 单\n"
             "停滞≥7天且未完结: 1106 单\n"
             "近120天内停滞TOP:\n"
             "  G549 Part9 停滞119天 (状态:审批中, 最后更新:2026-06-25 10:25)\n")
    out = db.render_dynamic_section(pa + "\n" + scan0)
    check("动态渲染: 0 单写今日无新增动态", "今日无新增动态" in out, extra=str(out))
    check("动态渲染: Part9 计数只算 2 单",
          "Part9新单待办 2 单" in out and "G600" in out and "G599" in out, extra=str(out))
    check("动态渲染: 停滞段没串进 Part9", "G549" not in out, extra=str(out))

    scan_n = ("今日有动态: 1 单(明细如下)\n"
              "  · G598 Xiaomi Redmi 17 4G/5G @ Part8 | 状态:审批中 | "
              "最新节点: 胡浩明|签样确认|2026-10-22 14:53 | 当前负责人:胡浩明\n")
    out2 = db.render_dynamic_section(pa + "\n" + scan_n)
    check("动态渲染: 有明细时逐条照抄",
          "• G598 Xiaomi Redmi 17 4G/5G @ Part8" in out2 and "今日无新增动态" not in out2,
          extra=str(out2))
    check("动态渲染: 不是这套工具输出时返回 None",
          db.render_dynamic_section("随便一段没有今日动态字样的文本") is None)

    md = ("## ② 今日动态\n\n"
          "• SKU G531 · 在Part1(素材加工) · 当前负责人：李四\n\n"
          "## ③ 新增SKU与签样追踪\n\n• G598 …\n")
    new, ok = db.replace_dynamic_section(md, pa + "\n" + scan0)
    check("动态替换: 命中并替换", ok and "G531" not in new, extra=new)
    check("动态替换: ③ 不受影响", "## ③ 新增SKU与签样追踪" in new)
    check("动态替换: 替换后体检通过",
          db.brief_health(new, pa + "\n" + scan0) == [],
          extra=str(db.brief_health(new, pa + "\n" + scan0)))


def test_drop_ghost_action_items():
    """⑥ 顺着编造的 ② 写出的行动建议, 按 SKU 白名单删掉。"""
    tool_out = "  G598 Xiaomi Redmi 17 4G/5G (创建 09-16)\n  G600 Xiaomi 18 Pro 5G\n"
    md = ("## ⑥ 行动建议\n\n"
          "• 立即跟进Part1中G531的审批进度，确保今日完成素材加工审批\n"
          "• 检查Part2中G532的广告投放审批状态\n"
          "• 跟踪 G598 的签样进度\n")
    new, n = db.drop_ghost_skus(md, tool_out)
    check("⑥ 清理: 删掉 2 条假 SKU 建议", n == 2, extra=str(n))
    check("⑥ 清理: G531/G532 消失", "G531" not in new and "G532" not in new, extra=new)
    check("⑥ 清理: 真实 SKU 的建议保留", "G598" in new, extra=new)

    all_fake = ("## ⑥ 行动建议\n\n• 提醒钱七处理 G533 的待审批事项\n")
    new2, n2 = db.drop_ghost_skus(all_fake, tool_out)
    check("⑥ 清理: 全删空时给兜底文案",
          n2 == 1 and "详见 ③④⑤" in new2, extra=new2)

    md6 = ("## ② 今日动态\n\n• 今日无新增动态\n\n"
           "## ⑥ 行动建议\n\n• 提醒钱七处理 G533\n")
    probs = " ".join(db.brief_health(md6, tool_out))
    check("⑥ 体检: 抓到假 SKU", "⑥ 含工具输出里没有的 SKU" in probs, extra=probs)


def test_carry_over_actions():
    """午后 ⑥ 不能把晨检待办清零(2026-10-27 用户反馈的真实场景)。

    晨检要求"今日内完成 G592/G593 的 Part8 审批", 午后只写"无新增动作" ——
    上午的活没落实, 下午反而撤了。
    """
    am = ("## ⑥ 行动建议\n\n"
          "• 立即跟进 G592 和 G593 的 Part8 审批进度，确保今日内完成，避免空运超期扩大\n"
          "• 重点处理 G598 (Xiaomi Redmi 17 4G/5G) 的 Part9 新单待办，当前负责人胡浩明需优先处理\n")
    pm = "## ⑥ 行动建议\n\n• 无新增动作\n"
    tool_out = "  G592 Xiaomi Poco F9 Pro 剩1天[空运] (卡Part8)\n  G598 Xiaomi Redmi 17 (创建 09-16)\n"
    new, n = db.carry_over_actions(pm, am, tool_out)
    check("承接: 补回 2 条", n == 2, extra=str(n))
    check("承接: G592 回来了", "G592" in new, extra=new)
    check("承接: 标了「晨检提出, 仍未推进」", new.count("晨检提出, 仍未推进") == 2, extra=new)
    check("承接: 「无新增动作」被去掉", "无新增动作" not in new, extra=new)

    pm2 = ("## ⑥ 行动建议\n\n"
           "• G592 已推进到 Part9，无需继续催 Part8\n")
    new2, n2 = db.carry_over_actions(pm2, am, tool_out)
    check("承接: 午后已提过 G592 不重复", n2 == 1 and new2.count("G592") == 1, extra=new2)

    # G598 已不在工具输出里(流程走完) → 不该硬挂
    new3, n3 = db.carry_over_actions(pm, am, "  G592 Xiaomi Poco F9 Pro 剩1天[空运]\n")
    check("承接: 数据里查不到的 SKU 不硬补", n3 == 1 and "G598" not in new3, extra=new3)

    new4, n4 = db.carry_over_actions("## ⑥ 行动建议\n\n• 无新增动作\n", am, "")
    check("承接: 没有工具输出时不动", n4 == 0, extra=new4)


def test_render_data_sections():
    """③④⑤ 从工具输出直出: 货件号逐字符保真, 计数行能被 parse_counts 认。"""
    r = db.render_data_sections(_TOOL_OUT)
    check("渲染: 三节齐全", set(r) == {"③", "④", "⑤"}, extra=str(set(r)))
    check("渲染: ③ 条目带 bullet", "• G598 Xiaomi Redmi 17 4G/5G" in r["③"])
    check("渲染: ③ 保留未签样尾注", "未签样待跟进 4 个" in r["③"])
    check("渲染: ④ 货件号逐字符照搬",
          "亚马逊墨西哥 FBA19QR4PKLJ 共14个" in r["④"])
    check("渲染: ④ 带计数行(午后对数用)",
          r["④"].startswith("临期 6 个"), extra=r["④"][:20])
    check("渲染: ④ 计数可被 parse_counts 认出",
          db.parse_counts(r["④"])["warn"] == 6)
    check("渲染: ⑤ 计数行写法对齐 _CNT_OVER",
          r["⑤"].startswith("已超期 22 个; 另历史遗留 69 个不列"), extra=r["⑤"][:40])
    check("渲染: ⑤ 两组都在",
          "• G591" in r["⑤"] and "环节停滞TOP 228单" in r["⑤"] and "• G493" in r["⑤"])
    check("渲染: 货件号没被截断", "FBA19PZ6XB8L" in r["⑤"])
    cnt = db.parse_counts(r["⑤"])
    check("渲染: 计数可被 parse_counts 认出",
          (cnt["over"], cnt["stale"]) == (22, 228), extra=str(cnt))
    check("渲染: 空输入不炸", db.render_data_sections("") == {})


def test_replace_data_sections_overrides_garbage():
    """模型把 ⑤ 写坏(复读+货件号写一半) → 整节被工具渲染版覆盖。"""
    bad = ("## ③ 新增SKU与签样追踪\n\n• G598 乱写\n\n"
           "## ④ 临期预警\n\n• 与晨检一致\n\n"
           "## ⑤ 已超期与环节停滞TOP\n\n"
           "• G589 vivo S2 5G [到货: 亚马逊墨西哥 F严格遵守格式要求，一行一个 SKU\n"
           "⑤ 含两组时, \"已超期\" 与 \"环节停滞TOP\" 各自单独成组, 组内仍逐条 \"• \"。\n")
    out, hit = db.replace_data_sections(bad, _TOOL_OUT)
    check("覆盖: ③④⑤ 三节都被替换", sorted(hit) == ["③", "④", "⑤"], extra=str(hit))
    check("覆盖: 垃圾文本消失", "严格遵守格式要求" not in out and "组内仍逐条" not in out)
    check("覆盖: 半截货件号消失", "亚马逊墨西哥 F\n" not in out and "亚马逊墨西哥 F " not in out)
    check("覆盖: 完整货件号回来了", "FBA19PZ6XB8L" in out)
    check("覆盖: 标题只出现一次", out.count("⑤ 已超期与环节停滞TOP") == 1)


def test_replace_data_sections_inserts_missing():
    """模型漏写 ③⑤(现在允许它不写) → 按序补上, 顺序不乱。"""
    md = ("## ① 同步状态与数据截至\n\n• 同步正常\n\n"
          "## ② 今日动态\n\n• 无新增\n\n"
          "## ④ 临期预警\n\n\n\n"
          "## ⑥ 行动建议\n\n• 跟进 G592\n")
    out, hit = db.replace_data_sections(md, _TOOL_OUT)
    check("补齐: ③⑤ 补上、④ 覆盖", sorted(hit) == ["③", "④", "⑤"], extra=str(hit))
    order = [out.index(f"## {n}") for n in "①②③④⑤⑥"]
    check("补齐: 六节顺序正确", order == sorted(order), extra=str(order))
    check("补齐: ④ 原有内容被覆盖", "G592" in out)


def test_archive_layout_by_month():
    """落盘按月归档: briefs/<YYYY-MM>/<日期>.md, 且兼容未归档的扁平旧文件。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        check("月份取自日期前7位", db.month_of("2026-10-22") == "2026-10",
              db.month_of("2026-10-22"))
        check("规范路径带月目录",
              db.brief_path("2026-10-22", base) == base / "2026-10" / "2026-10-22.md",
              str(db.brief_path("2026-10-22", base)))
        check("文件不存在时返回 None", db.find_brief("2026-10-22", base) is None)
        (base / "2026-10").mkdir()
        (base / "2026-10" / "2026-10-22.md").write_text("x", encoding="utf-8")
        check("月目录里能找到",
              db.find_brief("2026-10-22", base) == base / "2026-10" / "2026-10-22.md")
        (base / "2026-10-23.md").write_text("x", encoding="utf-8")
        check("扁平旧布局仍可读(归档切换过渡)",
              db.find_brief("2026-10-23", base) == base / "2026-10-23.md")


def main():
    print("=" * 60)
    print("[简报格式兜底单测] normalize_brief_md")
    for fn in (test_bare_sku_lines_get_bullets, test_inline_joined_entries_split,
               test_semicolon_joined_split, test_group_label_inline_entry_split,
               test_no_false_split_in_arrival_brackets, test_dup_h1_title_removed,
               test_list_markers_unified, test_plain_lines_untouched,
               test_empty_and_none, test_section_titles_canonical,
               test_section1_lines_get_bullets, test_dup_and_double_bullet,
               test_compose_daily_merged, test_compose_pm_only_when_morning_missing,
               test_refine_delta_filters_false_new, test_parse_and_apply_counts,
               test_load_morning_strips_separator,
               test_real_brief_idempotent,
               test_resolve_run_slot_explicit_beats_clock,
               test_resolve_run_slot_clock_fallback, test_pm_label_carries_tag,
               test_scrub_echo_line_dropped, test_scrub_midline_injection,
               test_scrub_consecutive_repeats, test_duplicate_section_header_collapsed,
               test_brief_health_signals,                test_health_detects_fabricated_skus, test_render_dynamic_section,
               test_drop_ghost_action_items, test_carry_over_actions,
               test_render_data_sections,
               test_replace_data_sections_overrides_garbage,
               test_replace_data_sections_inserts_missing,
               test_archive_layout_by_month):
        print(f"\n== {fn.__name__} ==")
        fn()
    print("\n" + "=" * 60)
    if FAILS:
        print(f"[简报格式兜底单测] {len(FAILS)} 项失败: " + ", ".join(FAILS))
        return 1
    print("[简报格式兜底单测] 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
