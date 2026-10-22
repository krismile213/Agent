#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/test_dingtalk_format.py — 钉钉推送换行/分片单测(零网络/零 LLM)

背景(2026-10-20 用户反馈): 群里收到的晨检简报③④⑤ 又挤成一整行, 且末尾
"美客多墨西哥 771" 被拦腰截断。

两个根因:
  1. **换行**: 简报正文是普通 Markdown, 每个 "• xxx" 独占一行, 但行与行之间
     没有空行。标准 Markdown 里段落内的单个 \\n 只是软换行, 渲染时折成空格;
     而 "•" 也不是列表标记(只有 - * + 是) → 到钉钉就是一条长句。
     修法: dingtalk_push.to_dingtalk_md 把每个非空行变成独立段落。
  2. **截断**: daily_brief 推送时写了 answer[:1800], ⑤ 后半段被硬砍。
     修法: 去掉截断, 改由 dingtalk_push 按段落分片(单片 ≤ MAX_MD_CHARS)。

本文件钉住这两条, 防止回归。
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import dingtalk_push as dp  # noqa: E402

FAILS = []


def _brief_file(name: str) -> Path:
    """briefs 按月份归档(2026-10-22 起) → briefs/<YYYY-MM>/<name>; 兼容扁平旧布局。"""
    base = HERE.parent / "briefs"
    p = base / name[:7] / name
    return p if p.exists() else base / name


def check(name, cond, extra=""):
    print(("  ✓ " if cond else "  ✗ ") + name
          + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def nonempty_lines(txt):
    return [l for l in txt.split("\n") if l.strip()]


def test_bullets_get_blank_line():
    print("\n[1] bullet 逐行 → 独立段落(钉钉才真的换行)")
    src = "### ③ 新增SKU与签样追踪\n• G598 甲\n• G599 乙\n• G600 丙"
    out = dp.to_dingtalk_md(src)
    check("① 条目之间是空行分隔", "• G598 甲\n\n• G599 乙\n\n• G600 丙" in out, out)
    check("② 没有残留单换行粘连",
          all(p.count("• ") == 1 for p in out.split("\n\n") if p.startswith("• ")), out)
    check("③ 标题与首条之间也隔开", "### ③ 新增SKU与签样追踪\n\n• G598" in out, out)
    check("④ 条目数守恒", len(nonempty_lines(out)) == 4,
          f"{len(nonempty_lines(out))}")


def test_plain_line_and_bullet():
    print("\n[2] 非 bullet 行(②的\"今日无新增动态\" / ⑤的计数行)也独立成段")
    src = "已超期 22 个; 另历史遗留 69 个不列\n• G591 已超3天\n• G587 已超4天"
    out = dp.to_dingtalk_md(src)
    check("计数行与首条之间断开", "不列\n\n• G591" in out, out)
    check("三条各自成段", len([p for p in out.split("\n\n") if p.strip()]) == 3, out)


def test_blank_lines_collapsed():
    print("\n[3] 原有多余空行被收敛(不会在群里刷出大段空白)")
    src = "• A\n\n\n\n• B\n  \n• C"
    out = dp.to_dingtalk_md(src)
    check("不出现连续 3 个换行", "\n\n\n" not in out, repr(out))
    check("三个条目都在", len([p for p in out.split("\n\n") if p.strip()]) == 3, out)


def test_table_kept_contiguous():
    print("\n[4] 表格行保持连续(不能被拆成一段一段)")
    src = "| SKU | 状态 |\n| --- | --- |\n| G599 | 预警 |"
    out = dp.to_dingtalk_md(src)
    check("表格三行用单换行相连", "| SKU | 状态 |\n| --- | --- |\n| G599 | 预警 |" in out, out)
    check("表格前后仍与段落隔开", out.count("\n\n") == 0, repr(out))


def test_split_never_cuts_line():
    print("\n[5] 超长正文分片: 每片≤上限, 不切半行, 不丢行")
    lines = [f"• G{600 + i} 这是一个用来测试分片的条目 " + "x" * 60 for i in range(60)]
    src = "\n".join(lines)
    cut = dp.to_dingtalk_md(src)
    chunks = dp.split_md(cut, limit=1000)
    check("① 分片数 > 1", len(chunks) > 1, f"{len(chunks)}")
    check("② 每片都 ≤ 1000 字", all(len(c) <= 1000 for c in chunks),
          [len(c) for c in chunks])
    got = [l for c in chunks for l in nonempty_lines(c)]
    check("③ 行数守恒(没丢条目)", len(got) == len(lines), f"{len(got)}/{len(lines)}")
    check("④ 每行都完整(没被拦腰切)", got == lines, "行内容不一致")
    check("⑤ 拼回去等于原文", "\n\n".join(chunks) == cut.strip(), "拼回不一致")


def test_today_brief_not_truncated():
    print("\n[6] 今天(2026-10-20)的晨检正文不再被 1800 字截断")
    f = _brief_file("2026-10-20.md")
    if not f.exists():
        check("简报文件存在", False, str(f))
        return
    txt = f.read_text("utf-8")
    am = txt.split("## 午后更新")[0]
    body = "\n".join(l for l in am.split("\n")
                     if l.strip() and not l.startswith("# ")
                     and not l.startswith("> 更新记录"))
    cut = dp.to_dingtalk_md(body)
    chunks = dp.split_md(cut)
    check(f"① 晨检正文 {len(body)} 字, 不再被 1800 截断", len(body) > 1800, f"{len(body)}")
    check("② ⑤ 的末条 G585 在消息里", "G585" in cut, "G585 丢了")
    check("③ 那句被砍断的货件号完整", "美客多墨西哥 771" in cut or "77190375" in cut,
          "货件号丢了")
    check("④ 分片数合理(≤3)", len(chunks) <= 3, f"{len(chunks)} 片")
    check("⑤ 每片 ≤ 上限", all(len(c) <= dp.MAX_MD_CHARS for c in chunks),
          [len(c) for c in chunks])


def test_send_payload(monkey=None):
    print("\n[7] send() 组包: 换行已硬化 + 分片标题带 (i/n), 不截断")
    sent = []

    class FakeResp:
        def __init__(self, payload):
            self._p = payload

        def json(self):
            return {"errcode": 0, "errmsg": "ok"}

    class FakeRequests:
        @staticmethod
        def post(url, json=None, timeout=None):
            sent.append(json)
            return FakeResp(json)

    real_req = dp.requests
    real_cfg = dp._load_push_config
    dp.requests = FakeRequests
    dp._load_push_config = lambda: {"webhook": "http://x/send?access_token=t", "secret": ""}
    try:
        ok, msg = dp.send("日报 2026-10-20 · 晨检 09:00", "• A\n• B\n• C")
        check("① 发送成功", ok, msg)
        t = sent[0]["markdown"]["text"]
        check("② 三条各自成段", "• A\n\n• B\n\n• C" in t, repr(t))
        check("③ 标题只有一处", t.count("日报 2026-10-20") == 1, repr(t))

        sent.clear()
        big = "\n".join(f"• G{600 + i} " + "y" * 80 for i in range(80))
        ok, msg = dp.send("日报 长文", big)
        check("④ 长文分成多片", len(sent) > 1, f"{len(sent)} 片")
        check("⑤ 分片标题带 (1/n)",
              f"(1/{len(sent)})" in sent[0]["markdown"]["text"], sent[0]["markdown"]["text"][:40])
        check("⑥ 每片 text ≤ 上限+标题", all(
            len(s["markdown"]["text"]) <= dp.MAX_MD_CHARS + 60 for s in sent),
            [len(s["markdown"]["text"]) for s in sent])
        allsent = "".join(s["markdown"]["text"] for s in sent)
        check("⑦ 末条 G679 仍完整送达", "G679" in allsent, "尾部丢了")
        check("⑧ 片数说明", "片" in msg, msg)
    finally:
        dp.requests = real_req
        dp._load_push_config = real_cfg


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    test_bullets_get_blank_line()
    test_plain_line_and_bullet()
    test_blank_lines_collapsed()
    test_table_kept_contiguous()
    test_split_never_cuts_line()
    test_today_brief_not_truncated()
    test_send_payload()
    print()
    if FAILS:
        print(f"✗ {len(FAILS)} 项失败: {FAILS}")
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
