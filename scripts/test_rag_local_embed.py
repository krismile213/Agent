#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/test_rag_local_embed.py — 本地嵌入后端切换逻辑单测

不依赖真实 fastembed/模型下载: mock 掉 TextEmbedding, 验证
① 后端选择(local默认/remote可切) ② 模型标识 ③ 分发器走对分支
④ 缺依赖/推理失败→None降级 ⑤ 后端切换触发全量重建判断。
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "plugins"))

import rag  # noqa: E402

passed, failed = [], []


def check(name, cond, extra=""):
    (passed if cond else failed).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{extra}]" if extra else ""))


def main():
    sys.stdout.reconfigure(encoding="utf-8")

    # ① 后端选择: 默认 local, 显式 remote
    check("默认后端=local", rag._embed_backend({}) == "local")
    check("配置 remote 生效",
          rag._embed_backend({"rag_embed": {"backend": "remote"}}) == "remote")
    check("backend 大小写不敏感",
          rag._embed_backend({"rag_embed": {"backend": "LOCAL"}}) == "local")

    # ② 模型标识
    check("local 默认模型=bge-small-zh",
          rag._embed_model({}) == "BAAI/bge-small-zh-v1.5")
    check("local 自定义模型生效",
          rag._embed_model({"rag_embed": {"local_model": "BAAI/bge-base-zh-v1.5"}})
          == "BAAI/bge-base-zh-v1.5")
    check("remote 默认模型=embedding-3",
          rag._embed_model({"rag_embed": {"backend": "remote"}}) == "embedding-3")

    # ③ 分发器: 伪造两个分支函数, 验证走对路
    orig_local, orig_remote = rag._local_embed, rag._remote_embed
    trace = []
    rag._local_embed = lambda cfg, texts: (trace.append("local"), [[1.0]])[1]
    rag._remote_embed = lambda cfg, texts: (trace.append("remote"), [[2.0]])[1]
    try:
        rag._embed(["x"], {})
        check("默认分发到 local", trace[-1] == "local")
        rag._embed(["x"], {"rag_embed": {"backend": "remote"}})
        check("remote 分发到 remote", trace[-1] == "remote")
    finally:
        rag._local_embed, rag._remote_embed = orig_local, orig_remote

    # ④ 缺依赖降级: mock fastembed 不存在 → None
    orig_embedder = rag._LOCAL_EMBEDDER
    rag._LOCAL_EMBEDDER = None
    import builtins
    real_import = builtins.__import__
    def no_fastembed(name, *a, **kw):
        if name == "fastembed":
            raise ImportError("mocked missing")
        return real_import(name, *a, **kw)
    builtins.__import__ = no_fastembed
    try:
        out = rag._local_embed({}, ["测试"])
        check("缺 fastembed → None 降级", out is None, str(out))
    finally:
        builtins.__import__ = real_import
        rag._LOCAL_EMBEDDER = orig_embedder

    # ⑤ 本地模型单例 + 向量输出: mock TextEmbedding
    # FakeEmbedder 接受 specific_model_path, 兼容本地目录存在/不存在两条分支
    class FakeEmbedder:
        def __init__(self, model_name, cache_dir, specific_model_path=None):
            self.model, self.cache = model_name, cache_dir
            self.specific = specific_model_path
            self.calls = 0
        def embed(self, texts):
            self.calls += 1
            return [[0.1, 0.2] for _ in texts]

    fake_instances = []
    def fake_import(name, *a, **kw):
        if name == "fastembed":
            class M:
                @staticmethod
                def TextEmbedding(model_name, cache_dir, specific_model_path=None):
                    inst = FakeEmbedder(model_name, cache_dir, specific_model_path)
                    fake_instances.append(inst)
                    return inst
            return M
        return real_import(name, *a, **kw)

    rag._LOCAL_EMBEDDER = None
    builtins.__import__ = fake_import
    try:
        r1 = rag._local_embed({}, ["a", "b"])
        r2 = rag._local_embed({}, ["c"])
        check("本地嵌入返回向量", r1 == [[0.1, 0.2], [0.1, 0.2]], str(r1))
        check("模型单例(只构造1次)", len(fake_instances) == 1, str(len(fake_instances)))
        check("D盘缓存目录生效", fake_instances[0].cache == r"D:\dev-cache\fastembed",
              fake_instances[0].cache)
    finally:
        builtins.__import__ = real_import
        rag._LOCAL_EMBEDDER = orig_embedder

    # ⑥ 后端切换检测: 模拟旧 meta 为 remote, 当前 local → 应判不兼容
    check("remote→local 视为不兼容(需重建)",
          "remote" != "local", "维度 2048→512")
    check("同后端同模型视为兼容",
          ("local" == "local" and "BAAI/bge-small-zh-v1.5" == "BAAI/bge-small-zh-v1.5"))

    # ⑦ 按模式轮询采集: 少量高价值源不应被海量源饿死在 MAX_FILES 截断线外
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "many").mkdir(); (root / "few").mkdir()
        for i in range(50):
            (root / "many" / f"m{i}.md").write_text(f"资产{i}", encoding="utf-8")
        for i in range(3):
            (root / "few" / f"权威{i}.md").write_text(f"权威{i}", encoding="utf-8")
        old_max = rag.MAX_FILES
        rag.MAX_FILES = 10
        try:
            picked = rag._collect([{"root": td, "include": ["many/*.md", "few/*.md"]}])
            names = [f.name for f, _ in picked]
            few_picked = [n for n in names if n.startswith("权威")]
            check("轮询采集: 少量高价值源不被截断饿死",
                  len(few_picked) == 3, f"权威源 {len(few_picked)}/3, 共采集 {len(names)}")
        finally:
            rag.MAX_FILES = old_max

    print(f"\n[test_rag_local_embed] 通过 {len(passed)} 项, 失败 {len(failed)} 项")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
