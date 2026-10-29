#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/test_eval_set.py — 评测集自身的防线(零 LLM)

为什么单独测评测集
------------------
eval/run_eval.py --suite redline 真调 LLM, 一次 250s / 10 万 tokens, 进不了 fast 层.
但「评测集自己有没有写坏」必须每轮回归都查 —— 断言写错的表现是永远 PASS 或
永远 FAIL, 两种都很难在报告里看出来(全绿/全红都像"正常波动").

所以这里只做确定性检查, 全零 LLM:
  1. 用例定义合法(id 唯一 / 有 origin / 有 mock 快照 / checks 不空)
  2. 断言词在快照里真能出现(否则模型永远达不到 → 假失败)
  3. 黑名单词不在快照里(否则模型引用快照就"违规" → 断言自相矛盾)
  4. 快照文件存在且够大(空文件会让模型凭空作答)

踩过的坑(2026-10-29): rl_trend_quote 原来写 answer_contains_any:["+3.4","8.6","5.1"],
其中 5.1 是"前 7 份均值", 模型随便提一句就过 —— 把快照结论改成 +9.9 它仍然 PASS.
结论数字只有一个, 断言必须精确; 第 2 条检查就是为这类"看起来通过其实没测"设的.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "eval"))

import run_eval as ev  # noqa: E402

RESULTS = []
REDLINE_MIN = 5


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail and not cond else ""))


def main():
    cases = ev.load_cases()
    rl = [c for c in cases if c.get("suite") == "redline"]

    print("== 套件规模 ==")
    check(f"redline 至少 {REDLINE_MIN} 条", len(rl) >= REDLINE_MIN, f"实际 {len(rl)}")
    ids = [c["id"] for c in cases]
    check("全部用例 id 唯一", len(ids) == len(set(ids)),
          f"重复: {[i for i in ids if ids.count(i) > 1]}")

    print("\n== 每条 redline 的定义 ==")
    for c in rl:
        cid = c["id"]
        check(f"{cid}: 有 origin(能追溯到事故)", bool(c.get("origin")))
        check(f"{cid}: 有 mock_tools(否则数字断言不稳定)", bool(c.get("mock_tools")))
        ch = c.get("checks", {})
        check(f"{cid}: 有工具断言",
              bool(ch.get("tools_used") or ch.get("tools_used_any")))
        check(f"{cid}: 有答案断言",
              bool(ch.get("answer_contains") or ch.get("answer_contains_any")
                   or ch.get("answer_not_contains")))

    print("\n== 快照可用 ==")
    snaps = {}
    for c in rl:
        for tool, fn in (c.get("mock_tools") or {}).items():
            p = ev.SNAPSHOTS / fn
            check(f"快照 {fn}", p.exists() and p.stat().st_size > 200,
                  f"{p} 不存在或过小")
            if p.exists():
                snaps[fn] = p.read_text("utf-8", errors="replace")

    print("\n== 断言与快照自洽 ==")
    for c in rl:
        cid, ch = c["id"], c.get("checks", {})
        texts = [snaps[f] for f in (c.get("mock_tools") or {}).values() if f in snaps]
        blob = "\n".join(texts)
        if not blob:
            continue
        # 必须出现的词, 快照里得有 —— 否则模型再对也达不到.
        # answer_contains 是"全部都要", 逐个查; answer_contains_any 是"任一即可",
        # 只要有一个能在快照里找到就算可达(要求每个都在是过度严格).
        for s in ch.get("answer_contains", []):
            check(f"{cid}: 快照里有 {s!r}", s in blob, "模型永远达不到 → 假失败")
        any_words = ch.get("answer_contains_any", [])
        if any_words:
            hit = [s for s in any_words if s in blob]
            check(f"{cid}: 快照里至少一个可达 {any_words}",
                  bool(hit), "全部都不在快照里 → 模型永远达不到")
        # 不许出现的词, 快照里不能有 —— 否则引用快照就违规
        for s in ch.get("answer_not_contains", []):
            check(f"{cid}: 快照里没有 {s!r}", s not in blob, "断言自相矛盾")

    print("\n== 工具名有效 ==")
    try:
        import agentcore as core
        reg = core.build_registry()
        core.load_plugins(reg, core.load_config())
        names = set(reg.names())
        builtin = set(core.build_registry().names())
    except Exception as e:                      # 插件加载失败不该挡住评测集检查
        print(f"  (跳过: 插件加载失败 {type(e).__name__})")
        names = builtin = None
    if names is not None:
        for c in rl:
            for tool in (c.get("mock_tools") or {}):
                check(f"{c['id']}: 工具 {tool} 存在", tool in names, "mock_tools 拼错了")

    print("\n== strict 隔离生效 ==")
    # 2026-10-29 实测: 只 mock 了两个工具时, 模型从没 mock 的 scan_sync_data 读到
    # 真实人名(李杰华/梁子锋), 于是「编造人名」判 FAIL 其实是误判 —— 它没编,
    # 是查到了. strict 的作用就是把这条路封死, 让「数据世界」是封闭的.
    # 这条检查失效的话, 评测会静默退化成「拿真数据测」, 不可重放.
    if names is not None:
        strict_cases = [c for c in rl if c.get("tool_scope") == "strict"]
        check("至少一条用例用 strict(高风险场景)", bool(strict_cases),
              "没人用 strict → 未 mock 的工具会泄真实数据")
        for c in strict_cases:
            r = ev.build_mock_registry(c, reg)
            mock = set(c.get("mock_tools") or {})
            leaked = set(r.names()) - builtin - mock
            check(f"{c['id']}: strict 下无未 mock 的业务工具",
                  not leaked, f"泄漏: {sorted(leaked)}")
            check(f"{c['id']}: strict 下 mock 工具都在",
                  mock <= set(r.names()), f"缺: {sorted(mock - set(r.names()))}")

    print("\n== 覆盖事故 ==")
    blob = " ".join(c.get("origin", "") for c in rl)
    for kw, label in [("10-23", "跳过取数"), ("人名", "编造人名"),
                      ("口径", "口径混说"), ("区间", "自切区间"),
                      ("load_skill", "技能不加载")]:
        check(f"覆盖: {label}", kw in blob, f"origin 里找不到 {kw!r}")

    n_ok = sum(1 for _, ok in RESULTS if ok)
    print(f"\n[评测集防线] {n_ok}/{len(RESULTS)} 通过")
    return 0 if n_ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
