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


def main():
    print("=" * 60)
    print("[简报格式兜底单测] normalize_brief_md")
    for fn in (test_bare_sku_lines_get_bullets, test_inline_joined_entries_split,
               test_semicolon_joined_split, test_group_label_inline_entry_split,
               test_no_false_split_in_arrival_brackets, test_dup_h1_title_removed,
               test_list_markers_unified, test_plain_lines_untouched,
               test_empty_and_none, test_real_brief_idempotent):
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
