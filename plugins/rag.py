# -*- coding: utf-8 -*-
"""
plugins/rag.py — 通用混合检索插件 (BM25 + 向量 + RRF 融合 + LLM 重排)

设计(面试要点):
  - 分块: markdown按标题层级切分, 小块合并, 上限~600字符
  - 双路召回: BM25(精确词/SKU编号强) + 向量(语义泛化), 各取top30
  - 融合: RRF(倒数排名融合, k=60) —— 免调权重的标准做法
  - 重排: LLM列表重排(top12→top_k), 失败自动退回RRF序
  - 语义繁化(query enrichment): 检索前把口语/隐含意图的查询映射到文档规范语义锚点,
    帮助小模型(bge-small)对齐; 原查询+繁化查询双路召回后RRF融合, 不丢词面精度.
    纯本地零依赖, 可开关(rag_enrich, 默认开).
  - 嵌入: 默认本地 fastembed(BAAI/bge-small-zh-v1.5, 512维, ONNX免torch, 免费离线),
    可选远程 OpenAI兼容 /embeddings(z.ai embedding-3); 按chunk hash增量缓存, 只嵌新/变更块
  - 降级: 嵌入不可用→纯BM25+RRF并在结果中注明; 无numpy→同样降级
  - 后端/模型切换→自动全量重建向量(维度不兼容)

配置(config.json, 全部可省略用默认值):
  "rag_sources": [{"name": "work-main文档", "root": "C:\\\\...\\\\work-main",
                    "include": ["资产文件/**/*.md", "流程复盘/**/*.md", "README.md"]}]
  "rag_embed": {
      "backend": "local",                          # local(默认,免费) / remote(付费API)
      "local_model": "BAAI/bge-small-zh-v1.5",     # fastembed 模型名
      "local_cache_dir": "D:\\dev-cache\\fastembed",# 模型缓存(默认D盘)
      "base_url": "...", "model": "embedding-3"    # 仅 backend=remote 时用
  }
  "rag_rerank": true,                              # 关掉省token
  "rag_enrich": true                               # 语义繁化开关(默认开, 本地零成本)
"""

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

import agentcore as core
from mini_agent import Tool, clip  # 兼容层: agentcore.Tool 同对象

try:
    import numpy as np
except ImportError:
    np = None
try:
    import jieba
    from rank_bm25 import BM25Okapi
except ImportError:
    jieba = BM25Okapi = None

import requests

# 本地嵌入(fastembed/ONNX, 无需 torch): 模块级单例, 首次调用才加载
_LOCAL_EMBEDDER = None
_LOCAL_MODEL = "BAAI/bge-small-zh-v1.5"

CACHE_DIR = core.HERE / "kb_cache"
INDEX_JSON = CACHE_DIR / "index.json"
VECS_NPZ = CACHE_DIR / "vecs.npz"
CHUNK_CHARS = 600
CHUNK_MIN = 80
MAX_FILES = 300
MAX_FILE_BYTES = 1_000_000

_state = {"index": None, "vecs": None, "hashes": None}


# ---------------- 分块 ----------------

def _split_md(text: str) -> list:
    """markdown按标题切分: 标题行归属其下方内容; 小块向后合并; 超长块硬切."""
    lines = text.splitlines()
    pieces, cur_title, buf = [], "", []

    def flush():
        nonlocal buf
        body = "\n".join(buf).strip()
        buf = []
        if body:
            pieces.append((cur_title, body))

    for ln in lines:
        m = re.match(r"^(#{1,4})\s+(.*)$", ln)
        if m:
            flush()
            cur_title = m.group(2).strip()[:80]
        buf.append(ln)
    flush()

    merged = []
    for title, body in pieces:
        if merged and len(body) < CHUNK_MIN and len(merged[-1][1]) < CHUNK_CHARS:
            merged[-1] = (merged[-1][0], merged[-1][1] + "\n" + body)
        else:
            while len(body) > CHUNK_CHARS:  # 超长硬切(带少量重叠)
                merged.append((title, body[:CHUNK_CHARS]))
                body = body[CHUNK_CHARS - 80:]
            if body.strip():
                merged.append((title, body))
    return merged


# ---------------- 采集与索引 ----------------

def _collect(sources: list) -> list:
    docs = []
    for src in sources:
        root = Path(src.get("root", "."))
        seen = set()
        # 按 include 模式轮询(round-robin)取文件: 避免海量"资产文件"先吃满
        # MAX_FILES 配额, 把高价值的"流程复盘/docs"等源饿死在截断线外。
        pats = src.get("include", ["**/*.md"])
        buckets = []
        for pat in pats:
            files = [f for f in root.glob(pat)
                     if f.is_file() and not f.name.startswith("~$")
                     and f.stat().st_size <= MAX_FILE_BYTES]
            buckets.append((pat, sorted(files)))  # sorted 保证确定性顺序
        while any(b for _, b in buckets):
            advanced = False
            for pat, bucket in buckets:
                if not bucket:
                    continue
                f = bucket.pop(0)
                advanced = True
                key = str(f.resolve())
                if key in seen:
                    continue
                seen.add(key)
                try:
                    docs.append((f, f.read_text("utf-8", errors="replace")))
                except OSError:
                    continue
                if len(docs) >= MAX_FILES:
                    core.log(f"[rag] 达到文件上限{MAX_FILES}, 截断"
                             f"(按模式轮询, 各源均衡覆盖)")
                    return docs
            if not advanced:
                break
    return docs


def _embed_backend(cfg: dict) -> str:
    """嵌入后端: local(默认, 免费离线) / remote(付费API)."""
    return (cfg.get("rag_embed") or {}).get("backend", "local").lower()


def _embed_model(cfg: dict) -> str:
    emb = cfg.get("rag_embed") or {}
    if _embed_backend(cfg) == "local":
        return emb.get("local_model", _LOCAL_MODEL)
    return emb.get("model", "embedding-3")


def _local_embed(cfg: dict, texts: list) -> list | None:
    """本地嵌入(fastembed + bge-small-zh), 模型单例, 缓存落 D 盘."""
    global _LOCAL_EMBEDDER
    if _LOCAL_EMBEDDER is None:
        try:
            from fastembed import TextEmbedding
        except ImportError:
            core.log("[rag] 本地嵌入不可用: 缺 fastembed (pip install fastembed)")
            return None
        emb = cfg.get("rag_embed") or {}
        cache = emb.get("local_cache_dir", r"D:\dev-cache\fastembed")
        model = _embed_model(cfg)
        # 优先用手动下载好的本地模型目录(specific_model_path), 完全离线、绕开
        # Windows 下 huggingface 下载器的符号链接/XET 校验坑; 目录不存在才回退自带下载
        local_dir = Path(cache) / model.split("/")[-1]
        try:
            if (local_dir / "model_optimized.onnx").exists():
                core.log(f"[rag] 加载本地嵌入模型 {model} (目录 {local_dir})...")
                _LOCAL_EMBEDDER = TextEmbedding(
                    model_name=model, cache_dir=cache,
                    specific_model_path=str(local_dir))
            else:
                core.log(f"[rag] 加载本地嵌入模型 {model} (缓存 {cache}, 在线下载)...")
                _LOCAL_EMBEDDER = TextEmbedding(model_name=model, cache_dir=cache)
        except Exception as e:
            core.log(f"[rag] 本地嵌入模型加载失败({str(e)[:100]}), 降级")
            return None
    try:
        vecs = [list(v) for v in _LOCAL_EMBEDDER.embed(texts)]
        return vecs
    except Exception as e:
        core.log(f"[rag] 本地嵌入推理失败({str(e)[:100]}), 降级")
        return None


def _remote_embed(cfg: dict, texts: list) -> list | None:
    """远程嵌入(原 z.ai embedding-3 逻辑), 保留作可选后端."""
    emb_cfg = cfg.get("rag_embed") or {}
    base = emb_cfg.get("base_url") or cfg["base_url"]
    url = base.rstrip("/") + "/embeddings"
    model = emb_cfg.get("model", "embedding-3")
    headers = {"Authorization": f"Bearer {cfg['api_key']}"}
    out = []
    for i in range(0, len(texts), 32):
        batch = [t[:4000] for t in texts[i:i + 32]]
        for attempt in range(3):
            try:
                r = requests.post(url, json={"model": model, "input": batch},
                                  headers=headers, timeout=60)
                if r.status_code >= 400:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
                data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
                out.extend(d["embedding"] for d in data)
                break
            except Exception as e:
                if attempt == 2:
                    core.log(f"[rag] 嵌入批次失败({e}), 该批降级")
                    out.extend([None] * len(batch))
    return out if out and out[0] is not None else None


def _embed(texts: list, cfg: dict) -> list | None:
    """嵌入分发器: 按配置走本地/远程, 任一不可用统一返回 None 触发纯BM25降级."""
    if _embed_backend(cfg) == "local":
        return _local_embed(cfg, texts)
    return _remote_embed(cfg, texts)


def _build(cfg: dict, force: bool = False) -> str:
    if jieba is None or BM25Okapi is None:
        return "[错误] 缺依赖: pip install rank_bm25 jieba"
    sources = cfg.get("rag_sources")
    if not sources:
        return "[错误] config.json 未配置 rag_sources"
    CACHE_DIR.mkdir(exist_ok=True)

    cur_backend, cur_model = _embed_backend(cfg), _embed_model(cfg)
    old_hash2row = {}
    old_vecs = None
    if not force and VECS_NPZ.exists() and INDEX_JSON.exists():
        try:
            old_meta = json.loads(INDEX_JSON.read_text("utf-8")).get("meta", {})
            # 后端/模型变了 → 旧向量维度不兼容, 丢弃重嵌(否则矩阵乘法崩)
            backend_same = old_meta.get("embed_backend", "remote") == cur_backend
            model_same = old_meta.get("embed_model", "embedding-3") == cur_model
            if not (backend_same and model_same):
                core.log(f"[rag] 嵌入配置变化({old_meta.get('embed_backend','remote')}/"
                         f"{old_meta.get('embed_model','embedding-3')} → "
                         f"{cur_backend}/{cur_model}), 全量重建向量")
            else:
                z = np.load(VECS_NPZ, allow_pickle=False)
                old_vecs, old_hashes = z["vecs"], list(z["hashes"])
                old_hash2row = {h: i for i, h in enumerate(old_hashes)}
        except Exception:
            pass

    docs = _collect(sources)
    chunks = []
    for f, text in docs:
        rel = str(f.resolve())
        for title, body in _split_md(text):
            h = hashlib.md5((rel + "|" + title + "|" + body).encode("utf-8")).hexdigest()
            chunks.append({"src": f.stem[:40], "path": rel,
                           "title": title or f.stem[:60], "hash": h,
                           "text": body})

    new_hashes = [c["hash"] for c in chunks if c["hash"] not in old_hash2row]
    new_texts = [c["title"] + "\n" + c["text"]
                 for c in chunks if c["hash"] not in old_hash2row]
    core.log(f"[rag] {len(docs)}文件 → {len(chunks)}块, 其中新增待嵌入 {len(new_texts)}")

    vecs_new = []
    if new_texts:
        if np is None:
            core.log("[rag] 无numpy, 向量路禁用(纯BM25)")
        else:
            vecs_new = _embed(new_texts, cfg) or []
            if not vecs_new:
                core.log("[rag] 嵌入API不可用, 向量路禁用(纯BM25)")

    # 组装向量矩阵: 保留旧向量 + 追加新向量
    new_map = {h: v for h, v in zip(new_hashes, vecs_new) if v is not None}
    rows = []
    for c in chunks:
        if c["hash"] in old_hash2row:
            rows.append(old_vecs[old_hash2row[c["hash"]]])
        elif c["hash"] in new_map:
            rows.append(np.asarray(new_map[c["hash"]], dtype="float32"))
    has_vec = len(rows) == len(chunks) and rows != []

    meta = {"built": datetime.now().isoformat(timespec="seconds"),
            "files": len(docs), "chunks": len(chunks),
            "dim": int(np.shape(rows[0])[0]) if has_vec and rows else 0,
            "has_vec": has_vec,
            "embed_backend": cur_backend, "embed_model": cur_model,
            "sources": [s.get("name", "?") for s in sources]}
    INDEX_JSON.write_text(json.dumps(
        {"meta": meta, "chunks": chunks,
         "tokens": [list(jieba.cut_for_search(c["title"] + " " + c["text"][:800]))
                    for c in chunks]},
        ensure_ascii=False), encoding="utf-8")
    if has_vec:
        np.savez(VECS_NPZ, vecs=np.stack(rows).astype("float32"),
                 hashes=np.array([c["hash"] for c in chunks]))
    _state.update(index=None, vecs=None, hashes=None)  # 失效缓存
    vec_part = f"启用(dim={meta['dim']})" if has_vec else "未启用(纯BM25)"
    return f"索引构建完成: {len(docs)}文件 / {len(chunks)}块 / 向量{vec_part}"


def _load(cfg: dict):
    if _state["index"] is not None:
        return _state
    if not INDEX_JSON.exists():
        return None
    obj = json.loads(INDEX_JSON.read_text("utf-8"))
    _state["index"] = obj
    if np is not None and VECS_NPZ.exists():
        try:
            z = np.load(VECS_NPZ, allow_pickle=False)
            _state["vecs"] = z["vecs"]
            _state["hashes"] = list(z["hashes"])
        except Exception:
            _state["vecs"] = None
    return _state


def _bm25_rank(query: str, k: int):
    st = _state
    toks = list(jieba.cut_for_search(query))
    bm = BM25Okapi(st["index"]["tokens"])
    scores = bm.get_scores(toks)
    order = sorted(range(len(scores)), key=lambda i: -scores[i])[:k]
    return [(i, float(scores[i])) for i in order if scores[i] > 0]


def _vec_rank(query: str, cfg: dict, k: int):
    if np is None or _state["vecs"] is None:
        return None
    q = _embed([query], cfg)
    if not q or q[0] is None:
        return None
    qv = np.asarray(q[0], dtype="float32")
    qv /= (np.linalg.norm(qv) + 1e-9)
    vecs = _state["vecs"]
    norms = np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9
    sims = (vecs / norms) @ qv
    order = np.argsort(-sims)[:k]
    return [(int(i), float(sims[i])) for i in order]


def _rrf(lists: list, k: int = 60) -> list:
    """Reciprocal Rank Fusion: score = Σ 1/(k+rank)."""
    agg = {}
    for lst in lists:
        for rank, (i, _s) in enumerate(lst, 1):
            agg[i] = agg.get(i, 0.0) + 1.0 / (k + rank)
    return sorted(agg.items(), key=lambda x: -x[1])


# ---------------- 语义繁化 (query enrichment) ----------------
# 小模型(bge-small)对口语/隐含意图的语义对齐弱: "从拿到手机数据到全部到货最慢
# 允许多久" 与文档"从取得手机数据启动到渠道仓到货结束,全程≤60自然日(红线)"语义
# 等价, 但词面和向量都不易命中。检索前把查询里的口语概念映射到文档规范语义锚点,
# 原查询+繁化查询双路召回后 RRF 融合 —— 只注入概念锚点, 不注入答案数字(60/15),
# 保证不造假; 原词面始终保留, 不会稀释精确查询。纯本地零依赖, rag_enrich 可关。
_ENRICH_CONCEPTS = [
    # (触发词正则(任一命中), 注入的规范语义锚点)
    (("全流程", "整条链路", "整个过程", "全程", "总周期", "红线", r"从.*到.*到货"),
     "全流程 整条链路 开发总周期 红线 自然日 上限 全流程汇总表"),
    (("手机数据", "拿到数据", "取得数据", "取数", "立项"),
     "取得手机数据 立项 启动"),
    (("到货", "渠道仓", "全部到货"),
     "渠道仓到货 全流程结束 到货确认"),
    (("最慢", "最晚", "多久", "允许多久", "多少天", "上限", "最迟"),
     "上限 红线 自然日"),
    (("时效", "目标", "几天", "目标天数"),
     "时效目标 目标天数 环节"),
    (("口径", "系统口径", "动作口径"),
     "口径 系统口径 动作口径 选择"),
    (("审批编号", "编号", "文本", "精度"),
     "审批编号 按文本处理 精度 长数字"),
]
_ENRICH_MAX_CHARS = 120


def _enrich_query(query: str, cfg: dict) -> str:
    """把口语查询扩展为规范语义锚点串(保留原词面). 关闭或无命中则原样返回."""
    if not cfg.get("rag_enrich", True):
        return query
    anchors = []
    for triggers, add in _ENRICH_CONCEPTS:
        if any(re.search(t, query) for t in triggers):
            anchors.append(add)
    if not anchors:
        return query
    extra = " ".join(anchors)
    if len(extra) > _ENRICH_MAX_CHARS:
        extra = extra[:_ENRICH_MAX_CHARS]
    return (query + " " + extra).strip()


def _hybrid(cfg: dict, query: str, top: int = 30):
    """BM25 走原查询(护住词面精度), 向量走语义繁化查询(对齐权威文档), RRF 融合.

    实测(6 条评测): 四路等权/繁化路加权都会让原查询的高频词噪声(手机膜/代码审查
    报告)反超权威文档; 而 BM25 保留原查询 + 向量用繁化查询, recall 与语义 top1
    同时全中。繁化查询含原词面, 精确查询也不会丢。

    返回 (fused_top, routes, enriched); routes 值标来源(bm25/vector/both).
    """
    bm = _bm25_rank(query, top)
    q2 = _enrich_query(query, cfg)
    enriched = q2 != query
    # 向量路: 繁化查询优先(语义对齐); 繁化关闭/无命中则退回原查询
    vc = _vec_rank(q2, cfg, top)
    routes = {}
    for i, _ in bm:
        routes[i] = "bm25"
    for i, _ in (vc or []):
        routes[i] = "both" if i in routes else "vector"
    lists = [x for x in (bm, vc) if x]
    return _rrf(lists)[:12], routes, enriched


def _rerank(query: str, cand: list, top_k: int, cfg: dict) -> list:
    """LLM列表重排: 失败/关闭时原序返回."""
    if not cfg.get("rag_rerank", True) or len(cand) <= 1:
        return [i for i, _ in cand]
    chunks = _state["index"]["chunks"]
    body = "\n".join(f"[{n}] {chunks[i]['title']} | {chunks[i]['text'][:300]}"
                     for n, (i, _s) in enumerate(cand, 1))
    try:
        client = core.LLMClient(dict(cfg, temperature=0.0))
        msg = client.chat([
            {"role": "system",
             "content": "你是检索重排器。按与查询的相关性对编号段落降序排序, "
                        "只输出一行逗号分隔的编号序列, 例如: 3,1,2"},
            {"role": "user", "content": f"查询: {query}\n\n段落:\n{body}"}])
        seq = [int(x) for x in re.findall(r"\d+", msg.get("content") or "")][:len(cand)]
        seen, order = set(), []
        for n in seq:
            if 1 <= n <= len(cand) and n not in seen:
                seen.add(n)
                order.append(cand[n - 1][0])
        for i, _s in cand:  # 未提到的按原序补尾
            if i not in order:
                order.append(i)
        return order[:top_k]
    except Exception:
        return [i for i, _ in cand][:top_k]


def _fmt(ids: list, routes: dict, reranked: bool) -> str:
    chunks = _state["index"]["chunks"]
    parts = []
    for n, i in enumerate(ids, 1):
        c = chunks[i]
        parts.append(f"[{n}] {c['title']}  (来源:{c['src']}.md, "
                     f"召回:{routes.get(i, '?')}{', 已重排' if reranked else ''})\n"
                     f"{clip(c['text'], 700)}")
    return "\n\n".join(parts)


# ---------------- 工具 ----------------

def register(registry, cfg: dict) -> int:
    def t_kb_build() -> str:
        return _build(cfg, force=True)

    def t_kb_status() -> str:
        st = _load(cfg)
        if st is None:
            return "索引未构建(首次使用 kb_search 会自动构建, 或显式调用 kb_build)"
        m = st["index"]["meta"]
        emb = f"{m.get('embed_backend','remote')}/{m.get('embed_model','embedding-3')}"
        return (f"索引: {m['files']}文件 / {m['chunks']}块 / "
                f"向量{'启用' if m['has_vec'] else '未启用'}(dim={m.get('dim',0)}, {emb}) "
                f"(构建于 {m['built']}, 来源: {', '.join(m['sources'])})")

    def t_kb_search(query: str, top_k: int = 4) -> str:
        if jieba is None or BM25Okapi is None:
            return "[错误] 缺依赖: pip install rank_bm25 jieba"
        if _load(cfg) is None:
            core.log("[rag] 索引不存在, 自动构建(仅首次)...")
            msg = _build(cfg)
            if not msg.startswith("索引构建完成"):
                return msg
            if _load(cfg) is None:
                return "[错误] 索引构建后仍不可用"
        fused, routes, enriched = _hybrid(cfg, query, 30)
        if not fused:
            return f"无命中(查询: {query[:60]}); 可用 kb_status 查索引覆盖范围"
        ids = _rerank(query, fused, max(1, min(int(top_k), 8)), cfg)
        reranked = cfg.get("rag_rerank", True) and len(fused) > 1
        bm_n = sum(1 for v in routes.values() if v.startswith("bm25"))
        vc_n = sum(1 for v in routes.values() if v.startswith("vector") or v == "both")
        head = (f"混合检索({bm_n}bm25+{vc_n}vec → RRF → "
                f"{'LLM重排' if reranked else 'RRF序'}"
                f"{' · 语义繁化' if enriched else ''}): 查询={query[:60]}")
        return head + "\n\n" + _fmt(ids, routes, reranked)

    registry.register(Tool(
        "kb_search",
        "混合检索知识库(BM25+向量+RRF+LLM重排): 检索业务口径/流程文档/复盘知识. "
        "语义化提问即可, 不必精确匹配关键词.",
        {"type": "object", "properties": {
            "query": {"type": "string", "description": "自然语言查询"},
            "top_k": {"type": "integer", "minimum": 1, "description": "返回条数, 默认4"}},
         "required": ["query"]}, "read", t_kb_search))
    registry.register(Tool(
        "kb_build", "重建知识库索引(增量嵌入, 只嵌入新增/变更块; 写级操作).",
        {"type": "object", "properties": {}, "required": []},
        "write", t_kb_build))
    registry.register(Tool(
        "kb_status", "查看知识库索引状态(规模/向量/构建时间/来源).",
        {"type": "object", "properties": {}, "required": []},
        "read", t_kb_status))
    return 3
