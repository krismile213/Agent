#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/test_memory_ckpt.py — P1 分层记忆 + 断点续跑 的离线单测(零 LLM)

覆盖:
  1. 分层记忆: save_memory 双写(MEMORY.md + 当天日志) / recall_memory 三种 target /
     _memory_block 注入与截断 / 旧版根 MEMORY.md 迁移
  2. checkpoint: write/list/clear 生命周期 / 损坏文件自清理 /
     run_task 真跑(FakeClient)正常结束与取消路径的落盘/清除 / "崩溃"残留可发现
全部用 tmp 目录, 不碰真实 agent.db 与记忆文件。
"""
import json
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail and not cond else ""))


class _Tmp:
    """把 agentcore 的 HERE/MEM_* 与 storage 的 DB 都指到 tmp, 结束后还原."""

    def __init__(self):
        import agentcore as core
        import storage
        self.core, self.storage = core, storage
        self.dir = Path(tempfile.mkdtemp(prefix="wb_memck_"))
        self._old = (core.HERE, core.ROOT, core.MEM_DIR, core.MEM_LONG, core.MEM_LOG,
                     storage.DEFAULT_DB, storage._conn)
        core.HERE = self.dir
        core.ROOT = self.dir
        core.MEM_DIR = self.dir / "memory"
        core.MEM_LONG = core.MEM_DIR / "MEMORY.md"
        core.MEM_LOG = core.MEM_DIR / "log"
        storage.DEFAULT_DB = self.dir / "agent.db"
        storage._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        core, storage = self.core, self.storage
        (core.HERE, core.ROOT, core.MEM_DIR, core.MEM_LONG, core.MEM_LOG,
         storage.DEFAULT_DB, storage._conn) = self._old


def test_memory_layers():
    print("\n== 分层记忆 ==")
    with _Tmp() as t:
        core = t.core
        core.t_save_memory("对账口径: 分列之和必须等于合计")
        core.t_save_memory("免费档 429 由降级链扛")

        long_txt = core.MEM_LONG.read_text("utf-8")
        check("双写: MEMORY.md 有两行", long_txt.count("- [") == 2)
        today = core.MEM_LOG / f"{datetime.now():%Y-%m-%d}.md"
        log_txt = today.read_text("utf-8")
        check("双写: 当天日志存在且带[HH:MM]",
              today.exists() and log_txt.count("[") >= 2 and "429" in log_txt)

        r = core.t_recall_memory("recent")
        check("recall recent 含日志内容", "429" in r and "对账口径" in r)
        r = core.t_recall_memory(f"{datetime.now():%Y-%m-%d}")
        check("recall 指定日期", "429" in r)
        check("recall 不存在的日期报清晰提示",
              "无" in core.t_recall_memory("2001-01-01"))
        r = core.t_recall_memory("all")
        check("recall all=长期记忆全文", "对账口径" in r)

        # _memory_block 注入与截断
        big = "\n".join(f"- 条目{i} " + "x" * 60 for i in range(120))
        core.MEM_LONG.write_text(big, encoding="utf-8")
        block = core._memory_block()
        check("memory_block 注入且封顶(<=4200字符)", len(block) <= 4_200, f"len={len(block)}")
        check("memory_block 标题更新为 memory/ 路径", "memory/MEMORY.md" in block)

        # 旧版迁移: 根 MEMORY.md → memory/MEMORY.md
        # (换 HERE 时必须连 MEM_* 一起换 —— 生产中 MEM_* 由真实 HERE 导出, 不会变)
        t2dir = Path(tempfile.mkdtemp(prefix="wb_memck2_"))
        old = (core.HERE, core.MEM_DIR, core.MEM_LONG, core.MEM_LOG)
        core.HERE = t2dir
        core.MEM_DIR = t2dir / "memory"
        core.MEM_LONG = core.MEM_DIR / "MEMORY.md"
        core.MEM_LOG = core.MEM_DIR / "log"
        (t2dir / "MEMORY.md").write_text("- [旧] 根目录时代的记忆", encoding="utf-8")
        core._ensure_mem()
        moved = t2dir / "memory" / "MEMORY.md"
        check("迁移: 旧根MEMORY.md 移入 memory/", moved.exists()
              and "根目录时代" in moved.read_text("utf-8")
              and not (t2dir / "MEMORY.md").exists())
        core.HERE, core.MEM_DIR, core.MEM_LONG, core.MEM_LOG = old


class FakeClient:
    """两步: 第一步发 echo 工具调用, 第二步给最终回答."""

    def __init__(self):
        self.calls = 0
        self.usage = {"prompt_tokens": 1, "completion_tokens": 1,
                      "total_tokens": 2}

    def chat(self, history, tools=None):
        self.calls += 1
        if self.calls == 1:
            return {"content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "echo", "arguments": '{"x": "hi"}'}}]}
        return {"content": f"done({self.calls})", "tool_calls": None}


class AllowPolicy:
    def allow(self, tool, kwargs):
        return True


def _mk_registry(core):
    reg = core.Registry()
    reg.register(core.Tool(
        "echo", "回显", {"type": "object", "properties": {
            "x": {"type": "string"}}, "required": ["x"]},
        "read", lambda x: f"echo:{x}"))
    return reg


def test_ckpt_lifecycle():
    print("\n== checkpoint 生命周期 ==")
    with _Tmp() as t:
        core = t.core
        core.write_checkpoint("sess-a", "做个大任务", 3)
        items = core.list_checkpoints()
        check("write→list 可见", len(items) == 1 and items[0]["task"] == "做个大任务"
              and items[0]["turn"] == 3 and items[0]["session"] == "sess-a")

        core.write_checkpoint("sess-a", "做个大任务", 4)  # 轮边界刷新 = 覆盖
        check("同会话刷新不重复", len(core.list_checkpoints()) == 1)

        # 损坏文件自清理
        bad = core.HERE / "sessions" / "broken.ckpt.json"
        bad.write_text("{not json", encoding="utf-8")
        items = core.list_checkpoints()
        check("损坏ckpt被跳过并清除", all(c["session"] != "broken" for c in items)
              and not bad.exists())

        core.clear_checkpoint("sess-a")
        check("clear→list 为空", core.list_checkpoints() == [])


def test_run_task_ckpt():
    print("\n== run_task 集成(FakeClient) ==")
    with _Tmp() as t:
        core = t.core
        reg = _mk_registry(core)
        cfg = {"max_turns": 5, "stream": False, "context_budget_tokens": 10 ** 9}

        # 正常结束: ckpt 先落盘, 结束后清除
        tr = core.Transcript(core.HERE / "sessions" / "ok.jsonl")
        ans = core.run_task(FakeClient(), reg, AllowPolicy(), tr, [],
                            "正常任务", cfg)
        check("正常结束返回最终回答", ans == "done(2)")
        check("正常结束 ckpt 已清除", core.list_checkpoints() == [])
        tr.close()

        # 取消: 预置 cancel → 第0轮即退出, ckpt 清除
        tr = core.Transcript(core.HERE / "sessions" / "cancel.jsonl")
        ev = threading.Event()
        ev.set()
        ans = core.run_task(FakeClient(), reg, AllowPolicy(), tr, [],
                            "会被取消的任务", cfg, cancel=ev)
        check("取消路径返回中断标记", "中断" in ans)
        check("取消路径 ckpt 已清除", core.list_checkpoints() == [])
        tr.close()

        # "崩溃": 只写不清 → list 能发现(续跑的依据)
        core.write_checkpoint("crash@proj", "崩溃时正在跑的任务", 7)
        items = core.list_checkpoints()
        check("崩溃残留可发现", len(items) == 1
              and items[0]["session"] == "crash@proj" and items[0]["turn"] == 7)
        # 会话名含 @ 等 Windows 合法字符, 文件名安全
        check("ckpt 文件名保留 @(多用户命名空间)",
              (core.HERE / "sessions" / "crash@proj.ckpt.json").exists())


def main() -> int:
    test_memory_layers()
    test_ckpt_lifecycle()
    test_run_task_ckpt()
    ok = sum(1 for _, p in RESULTS if p)
    print(f"\n{'=' * 46}\n[分层记忆+断点续跑单测] {ok}/{len(RESULTS)} 通过")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
