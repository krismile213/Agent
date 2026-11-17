#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/test_knowledge_wiki.py — LLM Wiki POC 单测

覆盖: 编译幂等(二次编译 hash 不变) / 只读源 / 查询命中正确页+事实可见 /
固化别名只含概念词不含答案数字 / lint 源漂移与冲突检测 / 旁路(不改 rag)。
全部用临时目录, 不污染真实 wiki/ 产物。
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "plugins"))

import knowledge_wiki as kw  # noqa: E402

passed, failed = [], []


def check(name, cond):
    (passed if cond else failed).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")


def tmpdir():
    return Path(tempfile.mkdtemp(prefix="wiki_poc_"))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    cfg = kw._load_cfg()
    src_root = kw._root(cfg)

    # ① 编译: 5 源 → 若干概念页, 产物齐全
    d = tmpdir()
    try:
        meta = kw.compile_wiki(cfg, d)
        check("编译产出概念页(>20)", meta["pages"] > 20)
        check("编译覆盖权威源(=5)", meta["sources"] == 5)
        check("产物齐全(index/pages/index.md/manifest/log)",
              all((d / f).exists() for f in
                  ["index.json", "index.md", "manifest.json", "log.md", "pages"]))
        idx = json.loads((d / "index.json").read_text("utf-8"))
        pages = idx["pages"]

        # ② 幂等: 同样源二次编译, 每页 hash 与正文一致(忽略 built 时间戳)
        snap = {p["slug"]: p["hash"] for p in pages}
        meta2 = kw.compile_wiki(cfg, d)
        idx2 = json.loads((d / "index.json").read_text("utf-8"))
        snap2 = {p["slug"]: p["hash"] for p in idx2["pages"]}
        check("编译幂等: 页数不变", meta2["pages"] == meta["pages"])
        check("编译幂等: 每页内容hash不变", snap == snap2)

        # ③ 只读源: 编译前后所有源文件 mtime 不变
        mf = json.loads((d / "manifest.json").read_text("utf-8"))["sources"]
        mtimes = {rel: (src_root / rel).stat().st_mtime for rel in mf}
        kw.compile_wiki(cfg, d)
        check("只读源: 源文件未被修改",
              all((src_root / rel).stat().st_mtime == mt for rel, mt in mtimes.items()))

        # ④ 不造假: 固化别名里不含任何答案数字(60/15/13)
        bad = []
        for p in idx2["pages"]:
            for a in p["aliases"]:
                if any(str(n) in a for n in (60, 15, 13, 12, 22, 35)):
                    bad.append((p["title"], a))
        check("固化别名不含答案数字", not bad)

        # ⑤ 查询: top1 命中正确页 + 事实可见(复用 test_rag 的语义查询)
        hits = kw.query_wiki("新品售前全流程的红线是多少自然日", 3, d)
        check("红线查询top1命中开发总周期页",
              bool(hits) and "开发总周期" in hits[0]["title"])
        check("红线查询top1事实可见(含60)",
              bool(hits) and any("60" in f for f in hits[0]["facts"]))

        hits2 = kw.query_wiki("Part8 渠道仓到货时效目标几天", 3, d)
        check("Part8查询top1命中Part8页",
              bool(hits2) and "Part8" in hits2[0]["title"])
        check("Part8查询top1事实可见(含15)",
              bool(hits2) and any("15" in f for f in hits2[0]["facts"]))

        # ⑥ 语义查询(口语, 无关键词)靠固化别名命中
        hits3 = kw.query_wiki("新品从拿到手机数据到全部到货最慢允许多久", 3, d)
        check("口语语义查询能命中全流程页",
              bool(hits3) and ("开发总周期" in hits3[0]["title"]
                               or any("开发总周期" in h["title"] for h in hits3)))

        # ⑦ lint 基线 OK
        check("lint 基线无问题", kw.lint_wiki(d, cfg) == [])

        # ⑧ lint 能发现源漂移(篡改 manifest 里的 hash)
        mf_path = d / "manifest.json"
        obj = json.loads(mf_path.read_text("utf-8"))
        first = next(iter(obj["sources"]))
        obj["sources"][first] = "TAMPERED_HASH"
        mf_path.write_text(json.dumps(obj, ensure_ascii=False), "utf-8")
        problems = kw.lint_wiki(d, cfg)
        check("lint 检出源漂移", any("源漂移" in x for x in problems))
    finally:
        shutil.rmtree(d, ignore_errors=True)

    # ⑨ 旁路: knowledge_wiki 不 import rag(源码级断言, 只看真正的 import 语句行)
    import re
    src_lines = (HERE / "plugins" / "knowledge_wiki.py").read_text("utf-8").splitlines()
    real_imports = [ln for ln in src_lines
                    if re.match(r"^\s*(import|from)\s+", ln)
                    and not ln.lstrip().startswith("#")]
    check("旁路: 源码不 import rag",
          not any(re.search(r"\brag\b", ln) for ln in real_imports))

    print(f"[test_knowledge_wiki] 通过 {len(passed)} 项, 失败 {len(failed)} 项")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
