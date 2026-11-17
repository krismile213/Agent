# -*- coding: utf-8 -*-
"""
plugins/knowledge_wiki.py — LLM Wiki 模式 POC（编译式知识库旁路）

与 rag.py 的本质差异:
  - RAG: query-time 切块 → BM25+向量 检索 → 每次返回碎片 chunk 重新推理, 无沉淀
  - Wiki: compile-time 把精选权威源"预综合"成概念页(固化口语别名 + 事实表 +
          交叉引用), query-time 只读索引 + 整页返回, 零 embedding / 零模型 / 确定性

Karpathy 三操作:
  - ingest  : 编译精选源 → 概念页 markdown + index.json/index.md + log.md
  - query   : 读 index, 加权字面匹配(标题/别名/事实/正文) → 整页返回
  - lint    : 源 hash 漂移 / 同一口径数字冲突 / 孤儿页

设计约束:
  - 旁路: 不 import rag, 不改 rag 主路径, 产物写到独立 wiki/ 目录
  - 只读源: 只读 work-main 的 markdown, 绝不回写
  - 确定性: 编译产物 hash 只 based on 源内容(不含时间戳), 幂等可重跑
  - 不造假: 固化别名只含"概念词/口语说法", 不含任何答案数字(60/15/13)
  - 零依赖: 仅标准库; jieba 可选(有则用于分词, 无则字符 bigram 兜底)

用法:
    import knowledge_wiki as kw
    kw.compile_wiki(cfg)              # 编译(幂等)
    kw.query_wiki("全流程红线多少天")  # 查询 → [{title, facts, text, source, score}]
    kw.lint_wiki()                    # 体检 → [str 问题]
"""

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

try:
    import jieba
except ImportError:
    jieba = None

HERE = Path(__file__).resolve().parent.parent   # Agent 项目根(旁路: 不 import agentcore)
WIKI_DIR = HERE / "wiki"
PAGES_DIR = WIKI_DIR / "pages"
INDEX_JSON = WIKI_DIR / "index.json"
INDEX_MD = WIKI_DIR / "index.md"
LOG_MD = WIKI_DIR / "log.md"
MANIFEST = WIKI_DIR / "manifest.json"

# 精选权威源(相对 work-main root)。wiki 不是全库, 是"少而精"的编译底座。
DEFAULT_SOURCES = [
    "资产文件/新品售前流程动作全流程汇总表.md",
    "Data-Backend-System/docs/SOP-新品售前流程操作手册.md",
    "流程复盘/使用说明.md",
    "Data-Backend-System/docs/单据增量使用说明.md",
    "Data-Backend-System/docs/流程事中与事后评估使用说明.md",
]

# 概念级口语别名规则(通用, 按"标题"判类型; 只固化概念词, 不含答案数字)。
# 这是 wiki 与 query-time 语义繁化的本质差异: 别名在编译期固化进页面, 查询零成本。
_ALIAS_RULES = [
    # 强锚点"红线/总周期"只在真正讲开发总周期的页固化; 这些是全流程页专属口语说法
    (r"红线|总周期|开发总周期", ["整条链路", "开发总周期", "红线",
                              "最慢允许多久", "最晚多久", "多少自然日",
                              "从拿到数据到全部到货", "全程"]),
    # 弱锚点"全流程"是通用词, Part3/汇总表标题都含它, 只给通用别名
    (r"全流程", ["全流程", "多久", "上限", "总周期"]),
    (r"Part\s*\d", ["环节", "时效", "时效目标", "目标天数", "几天", "多少天",
                    "目标", "完成标志"]),
    (r"口径", ["口径", "系统口径", "动作口径", "怎么选", "区别", "选择"]),
    (r"文本|编号|精度|字段", ["审批编号", "按文本处理", "文本", "精度",
                            "为什么", "长数字", "字段"]),
    (r"素材|寄送拍摄|拍摄|加工", ["素材", "寄送拍摄", "拍摄", "加工",
                               "素材加工", "完成", "多少天"]),
]

# 事实断言: 含数字+单位(自然日/工作日/天/nat/work/%)的行, 或"≤N"口径行
_FACT_RE = re.compile(r"(≤|<=|不超过|至少|上限|=)?\s*(\d+)\s*(自然日|工作日|nat|work|天|天\)|个渠道|张\s*sheet|%)")
_HEAD_RE = re.compile(r"^(#{2,4})\s+(.*)$")


def _root(cfg: dict) -> Path:
    srcs = cfg.get("rag_sources") or []
    for s in srcs:
        if s.get("root"):
            return Path(s["root"])
    return Path(".")


def _tokens(text: str) -> list:
    if jieba:
        return [t for t in jieba.lcut_for_search(text) if len(t) >= 2]
    s = re.sub(r"\s+", "", text)
    return [s[i:i + 2] for i in range(len(s) - 1)]


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _aliases_for(title: str) -> list:
    """按标题判概念类型, 固化通用口语别名(不含答案数字)。"""
    a = set()
    title_clean = re.sub(r"[★·（）()【】\[\]：:，,。;；、\s]", " ", title)
    for w in title_clean.split():
        if len(w) >= 2:
            a.add(w)
    for pat, adds in _ALIAS_RULES:
        if re.search(pat, title):
            a.update(adds)
    return sorted(a)


def _facts_for(body: str) -> list:
    """从正文抽事实断言(保留原文行, 供 query 的'事实可见'与 lint 冲突检测)。"""
    facts = []
    seen = set()
    for ln in body.splitlines():
        if _FACT_RE.search(ln) and len(ln.strip()) > 4:
            line = ln.strip().lstrip("|> -·").strip()
            key = line[:40]
            if key not in seen:
                seen.add(key)
                facts.append(line[:160])
    return facts[:12]


def _sections(md: str):
    """按 ##/### 切节; 每节 = 一个概念页候选(标题 + 正文 + 上级标题)。"""
    lines = md.splitlines()
    out, cur_title, parent, buf = [], "", "", []

    def flush():
        body = "\n".join(buf).strip()
        if cur_title and body:
            out.append((parent, cur_title, body))
        buf.clear()

    for ln in lines:
        m = _HEAD_RE.match(ln)
        if m:
            flush()
            level = len(m.group(1))
            if level == 2:
                parent = m.group(2).strip()[:60]
                cur_title = parent
            else:
                cur_title = f"{parent} › {m.group(2).strip()[:60]}"
        else:
            buf.append(ln)
    flush()
    return out


def compile_wiki(cfg: dict, wiki_dir: Path | None = None) -> dict:
    """把精选源编译成概念页。幂等: 内容 hash 不含时间戳, 重跑产物一致。"""
    wd = Path(wiki_dir) if wiki_dir else WIKI_DIR
    pages_dir = wd / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    root = _root(cfg)

    pages, src_hashes = [], {}
    for rel in DEFAULT_SOURCES:
        p = root / rel
        if not p.exists():
            continue
        raw = p.read_text("utf-8", errors="replace")
        src_hashes[rel] = _sha(raw)
        src_name = p.stem[:40]
        for i, (parent, title, body) in enumerate(_sections(raw)):
            aliases = _aliases_for(title)
            facts = _facts_for(body)
            slug = f"{_sha(src_name + '|' + title)[:8]}.md"
            content_hash = _sha(title + "|" + body)
            page = {
                "slug": slug, "title": title, "source": src_name,
                "path": rel, "aliases": aliases, "facts": facts,
                "text": body, "hash": content_hash,
            }
            pages.append(page)
            # 人类可读概念页(frontmatter + 正文 + 事实表); 内容 hash 决定幂等
            fm = (f"---\ntitle: {title}\nsource: {src_name}\n"
                  f"aliases: {json.dumps(aliases, ensure_ascii=False)}\n"
                  f"hash: {content_hash}\n---\n\n")
            ftable = ("\n\n## 事实表\n" + "\n".join(f"- {f}" for f in facts)) if facts else ""
            (pages_dir / slug).write_text(fm + f"# {title}\n\n{body}{ftable}\n", "utf-8")

    idx = {
        "meta": {"built": datetime.now().isoformat(timespec="seconds"),
                 "pages": len(pages), "sources": len(src_hashes),
                 "mode": "compile-time wiki (no embedding)"},
        "pages": pages,
    }
    (wd / "index.json").write_text(json.dumps(idx, ensure_ascii=False), "utf-8")
    (wd / "manifest.json").write_text(
        json.dumps({"sources": src_hashes}, ensure_ascii=False, indent=2), "utf-8")

    # 人类可读目录(index.md): 每条 = 概念 + 固化别名 + 事实数
    lines = ["# Wiki 概念索引", "",
             f"> {len(pages)} 概念页 / {len(src_hashes)} 权威源 · "
             f"编译于 {idx['meta']['built']}", ""]
    for p in pages:
        al = "、".join(p["aliases"][:6])
        lines.append(f"- **{p['title']}** ({p['source']}) — 别名[{al}] · 事实{len(p['facts'])}条")
    (wd / "index.md").write_text("\n".join(lines) + "\n", "utf-8")
    with (wd / "log.md").open("a", encoding="utf-8") as f:
        f.write(f"- {idx['meta']['built']} ingest: {len(src_hashes)}源 → {len(pages)}页\n")
    return idx["meta"]


def _load_index(wd: Path | None = None) -> dict:
    p = (wd or WIKI_DIR) / "index.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text("utf-8"))


def query_wiki(q: str, top_k: int = 3, wiki_dir: Path | None = None) -> list:
    """查询: 读 index, 加权字面匹配, 返回整页。零 embedding / 零模型 / 确定性。"""
    idx = _load_index(wiki_dir)
    pages = idx.get("pages", [])
    if not pages:
        return []
    qt = set(_tokens(q))
    scored = []
    for p in pages:
        title_t = set(_tokens(p["title"]))
        alias_t = set()
        for a in p["aliases"]:
            alias_t |= set(_tokens(a))
        fact_t = set()
        for f in p["facts"]:
            fact_t |= set(_tokens(f))
        body_t = set(_tokens(p["text"][:1200]))
        score = (len(qt & alias_t) * 5 + len(qt & title_t) * 3
                 + len(qt & fact_t) * 2 + len(qt & body_t) * 1)
        # 固化别名的整串命中(如"最慢允许多久")额外加权
        for a in p["aliases"]:
            if len(a) >= 4 and a in q:
                score += 6
        if score > 0:
            scored.append((score, p))
    scored.sort(key=lambda x: -x[0])
    out = []
    for score, p in scored[:top_k]:
        out.append({"title": p["title"], "source": p["source"],
                    "path": p["path"], "facts": p["facts"],
                    "text": p["text"], "score": score})
    return out


def lint_wiki(wiki_dir: Path | None = None, cfg: dict | None = None) -> list:
    """体检: 源 hash 漂移 / 同一口径数字冲突 / 无事实无别名的孤儿页。"""
    wd = Path(wiki_dir) if wiki_dir else WIKI_DIR
    problems = []
    idx = _load_index(wd)
    pages = idx.get("pages", [])
    if not pages:
        return ["wiki 未编译: 缺 index.json"]

    # 1) 源漂移: manifest 记录的 hash vs 当前源文件
    mf_path = wd / "manifest.json"
    if mf_path.exists():
        mf = json.loads(mf_path.read_text("utf-8")).get("sources", {})
        root = _root(cfg or {})
        for rel, h in mf.items():
            p = root / rel
            if not p.exists():
                problems.append(f"源消失: {rel}")
            elif _sha(p.read_text("utf-8", errors="replace")) != h:
                problems.append(f"源漂移(需重编译): {rel}")

    # 2) 数字冲突: 仅当"同一口径主题"(开发总周期红线/同一Part时效)出现多个不同目标值。
    #    注意: 不同 Part 的时效本来就不同(不是冲突), 只有同一主题的权威"目标值"
    #    出现分歧才是真冲突。用主题词归一 → 只比较目标值, 忽略"小时/约X天"等换算值。
    def topic(title: str) -> str | None:
        if "开发总周期" in title or "红线" in title:
            return "开发总周期红线"
        m = re.search(r"(Part\s*\d)", title)
        return m.group(1).replace(" ", "") if m else None

    topic_vals = {}
    for p in pages:
        t = topic(p["title"])
        if not t:
            continue
        for f in p["facts"]:
            if "≤" in f or "全程目标" in f or "目标" in f:
                m = _FACT_RE.search(f)
                if m:
                    topic_vals.setdefault(t, set()).add(m.group(2))
    for t, vals in topic_vals.items():
        # 单一主题(如某个 Part)内出现 >2 个不同目标值视为可疑(容许 1 个换算值)
        if len(vals) > 2:
            problems.append(f"口径数字待核: 主题[{t}] 目标值 {sorted(vals)}")

    # 3) 孤儿页: 既无事实也无有效别名(正文极短且无锚点)
    for p in pages:
        if not p["facts"] and len(p["aliases"]) < 2 and len(p["text"]) < 80:
            problems.append(f"孤儿页: {p['title']}")

    return problems


def _load_cfg() -> dict:
    p = HERE / "config.json"
    return json.loads(p.read_text("utf-8")) if p.exists() else {}


if __name__ == "__main__":
    cfg = _load_cfg()
    print("[wiki] compile:", compile_wiki(cfg))
    for q in ["新品售前全流程的红线是多少自然日", "Part8 渠道仓到货时效目标几天"]:
        print(f"[wiki] query: {q}")
        for h in query_wiki(q, 2):
            print(f"   {h['score']:>3} {h['title']} | {h['facts'][:1]}")
    print("[wiki] lint:", lint_wiki(cfg=cfg) or "OK")
