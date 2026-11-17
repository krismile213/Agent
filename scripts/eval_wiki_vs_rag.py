#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/eval_wiki_vs_rag.py — 同一评测集对比 Wiki vs RAG

用 scripts/test_rag.py 完全相同的 6 条查询, 对两种架构评同一件事:
  - top-k 是否命中"期望来源文档"
  - 命中页/块里是否"事实可见"
RAG 走 plugins/rag.py 的 _hybrid(真实检索路径), Wiki 走 knowledge_wiki.query_wiki。
只读对比, 不改任何索引/源。输出对比表 + 各自命中率。
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "plugins"))

import agentcore as core  # noqa: E402
import rag  # noqa: E402
import knowledge_wiki as kw  # noqa: E402

# 与 test_rag.py 完全一致的评测集(查询, 期望来源片段, 期望事实)
CASES = [
    ("新品售前全流程的红线是多少自然日", "全流程汇总表", "60"),
    ("Part8 渠道仓到货环节的时效目标是几天", "SOP-新品售前流程操作手册", "15"),
    ("路线复盘系统口径和动作口径怎么选", "使用说明", "系统"),
    ("审批编号为什么要按文本处理", "使用说明", "文本"),
    ("素材寄送拍摄到加工完成的目标是多少天", "SOP-新品售前流程操作手册", "13"),
]
SEMANTIC = ("新品从拿到手机数据到全部到货最慢允许多久", "全流程汇总表", "60")
ALL = CASES + [SEMANTIC]


def rag_search(cfg, q, top_k=5):
    core.load_plugins  # noqa
    rag._load(dict(cfg, rag_rerank=False))
    fused, _routes, _en = rag._hybrid(dict(cfg, rag_rerank=False), q, 30)
    chunks = rag._state["index"]["chunks"]
    return [(chunks[i]["src"] + "|" + chunks[i]["path"], chunks[i]["text"])
            for i, _ in fused[:top_k]]


def wiki_search(q, top_k=5):
    hits = kw.query_wiki(q, top_k)
    return [(h["source"] + "|" + h["path"], h["text"] + " " + " ".join(h["facts"]))
            for h in hits]


def evaluate(name, searcher, cfg):
    is_rag = name.startswith("RAG")
    rows, src_ok, fact_ok, top1_ok = [], 0, 0, 0
    for q, src_mark, fact in ALL:
        hits = searcher(cfg, q) if is_rag else searcher(q)
        src_hit = any(src_mark in s for s, _ in hits)
        fact_hit = any(fact in t for _, t in hits[:3])
        top1_hit = bool(hits) and src_mark in hits[0][0]
        src_ok += src_hit
        fact_ok += fact_hit
        top1_ok += top1_hit
        rows.append((q, "Y" if top1_hit else ".", "Y" if src_hit else ".",
                     "Y" if fact_hit else "."))
    n = len(ALL)
    print(f"\n[{name}] top1命中 {top1_ok}/{n} · 来源召回 {src_ok}/{n} · 事实可见 {fact_ok}/{n}")
    for q, t1, sr, fc in rows:
        print(f"   top1:{t1} 召回:{sr} 事实:{fc}  {q[:26]}")
    return top1_ok, src_ok, fact_ok


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    cfg = core.load_config()
    print("=" * 62)
    print("Wiki vs RAG — 同一评测集(6条) · 检索级对比(无LLM)")
    print("=" * 62)
    if not (kw.WIKI_DIR / "index.json").exists():
        print("[wiki] 索引不存在, 先编译...")
        print("   ", kw.compile_wiki(cfg))
    r = evaluate("RAG (BM25+向量+RRF+语义繁化)", rag_search, cfg)
    w = evaluate("Wiki (编译期固化+整页返回)", wiki_search, cfg)
    print("\n" + "=" * 62)
    print(f"结论: RAG top1 {r[0]}/6 vs Wiki top1 {w[0]}/6 · "
          f"事实可见 RAG {r[2]}/6 vs Wiki {w[2]}/6")
    print("说明: Wiki 用零 embedding / 零模型 / 编译期固化别名达成; "
          "两者不是替代关系, 各有适用域。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
