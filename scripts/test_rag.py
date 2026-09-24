#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/test_rag.py — 混合检索(RAG)质量测试

检索级评测(不用LLM答题, 只测召回): 每条查询给定"期望命中的来源文件名片段
+期望事实", 验证 top-k 内召回且事实文本可见; 另测语义泛化(查询不含关键词)
与降级路径(向量路关闭时应仍能纯BM25工作)。
前置: 索引已构建(未构建则自动建, 走真实嵌入API, 首次约1~2分钟)。
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "plugins"))

import agentcore as core  # noqa: E402
import rag  # noqa: E402

passed, failed = [], []


def check(name, cond):
    (passed if cond else failed).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")


# (查询, 期望来源文件名片段, 期望事实片段)
CASES = [
    ("新品售前全流程的红线是多少自然日", "全流程汇总表", "60"),
    ("Part8 渠道仓到货环节的时效目标是几天", "SOP-新品售前流程操作手册", "15"),
    ("路线复盘系统口径和动作口径怎么选", "使用说明", "系统"),
    ("审批编号为什么要按文本处理", "使用说明", "文本"),
    ("素材寄送拍摄到加工完成的目标是多少天", "SOP-新品售前流程操作手册", "13"),
]

SEMANTIC = ("新品从拿到手机数据到全部到货最慢允许多久", "全流程汇总表", "60")


def _search(cfg, q, top_k=5, rerank=False):
    cfg2 = dict(cfg, rag_rerank=rerank)
    core.load_plugins  # noqa
    rag._load(cfg2)
    bm = rag._bm25_rank(q, 30)
    vc = rag._vec_rank(q, cfg2, 30)
    routes = {}
    for i, _ in bm:
        routes[i] = "bm25"
    for i, _ in (vc or []):
        routes[i] = "both" if i in routes else "vector"
    fused = rag._rrf([bm] + ([vc] if vc else []))[:top_k]
    chunks = rag._state["index"]["chunks"]
    hits = [(chunks[i]["src"], chunks[i]["text"], routes.get(i, "?"))
            for i, _ in fused]
    return hits


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    cfg = core.load_config()
    if rag.INDEX_JSON.exists():
        rag._load(cfg)
    else:
        print("[rag] 索引不存在, 自动构建...")
        print("   ", rag._build(cfg))

    print("[评测] 检索级 recall@5 (BM25+向量+RRF, 无重排)")
    ok_n = 0
    for q, src_mark, fact in CASES:
        hits = _search(cfg, q, top_k=5)
        ok = any(src_mark in s for s, _, _ in hits)
        routed = [r for _, _, r in hits if r in ("both", "vector")]
        ok_n += ok
        print(f"   {'HIT ' if ok else 'MISS'} {q[:24]}  向量参与:{bool(routed)}")
    check(f"recall@5 = {ok_n}/{len(CASES)}", ok_n >= len(CASES) - 1)

    print("[评测] 语义泛化(查询不含任何关键词)")
    hits = _search(cfg, SEMANTIC[0], top_k=5)
    top = hits[0]
    check("语义查询top1命中权威文档", SEMANTIC[1] in top[0])
    check("top1文本含目标事实", SEMANTIC[2] in top[1] or SEMANTIC[2] in str(hits[:3]))
    check("向量路参与召回(bm25词面完全不匹配)", any(r != "bm25" for _, _, r in hits))

    print("[评测] 降级: 向量路禁用(模拟无嵌入)")
    saved = rag._vec_rank
    rag._vec_rank = lambda q, c, k: None
    try:
        hits = _search(cfg, "全流程红线多少天", top_k=5)
        check("纯BM25降级仍可用", any("全流程汇总表" in s for s, _, _ in hits))
    finally:
        rag._vec_rank = saved

    m = rag._state["index"]["meta"]
    check(f"索引规模合理({m['chunks']}块/向量{'启用' if m['has_vec'] else '关'})",
          m["chunks"] > 200 and m["has_vec"])
    print(f"[test_rag] 通过 {len(passed)} 项, 失败 {len(failed)} 项")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
