#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/test_brief_format.py — 简报格式兜底 normalize_brief_md 单测(零 LLM)

背景(2026-10-15 用户反馈): 简报③④节"看不清晰"。
根因是 **markdown 语义**: 连续普通行会被渲染器折叠成一个段落, 逐行但不带列表
标记的 SKU 条目一到钉钉/Web 上就挤成一坨。光靠提示词不可靠(模型经常忘),
故在 daily_brief.normalize_brief_md 里做了确定性兜底, 本文件钉住它的行为。
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import daily_brief as db  # noqa: E402

FAILS = []


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
    p = HERE.parent / "briefs" / "2026-10-15_pm.md"
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
    """晨检 + 午后 → 同一份 briefs/<日期>.md; 重跑午后只替换午后段。"""
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
               test_real_brief_idempotent):
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
