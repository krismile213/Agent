#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/test_reflect_fix.py — 反思闭环机制单测(零LLM成本)

用 FakeClient 脚本化模型行为 + monkeypatch reflect, 验证:
  1) _reflect_passed 启发式的边界
  2) 反思不通过时触发修正轮(fix_round事件 + 历史含[反思修正] + 返回修正稿)
  3) 反思通过时不触发修正轮
"""

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import agentcore as core

passed, failed = [], []


def check(name, cond):
    (passed if cond else failed).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")


CFG = {"max_turns": 5, "context_budget_tokens": 999_999}


class FakeClient:
    """脚本化: 第1次返回工具调用, 第2次返回修正稿."""

    def __init__(self):
        self.calls = 0
        self.usage = {"calls": 0, "prompt_tokens": 0,
                      "completion_tokens": 0, "total_tokens": 0}

    def chat(self, messages, tools=None):
        self.calls += 1
        self.usage["calls"] += 1
        if self.calls == 1:
            return {"role": "assistant", "content": "初稿: 41",
                    "tool_calls": [{"id": "t1", "type": "function", "function": {
                        "name": "run_python",
                        "arguments": json.dumps({"code": "print(6*7)"})}}]}
        return {"role": "assistant", "content": "修正稿: 42(来源: run_python)"}


def test_heuristic():
    cases = [
        ("反思通过", True),
        ("检查通过, 无需修正", True),
        ("未发现明显错误", True),
        ("", True),
        ("(反思调用失败: xx)", True),
        ("发现错误结论: 数字未经验证, 需要修正", False),
        ("回答存在错误: 41 应为 42", False),
        ("有依据遗漏了来源标注", False),
        ("整体符合要求", True),  # 模糊 → 默认通过
    ]
    ok = all(core._reflect_passed(c) == exp for c, exp in cases)
    bad = [c for c, exp in cases if core._reflect_passed(c) != exp]
    check(f"通过判定启发式({len(cases)}组边界)", ok)
    if not ok:
        print("       判错的是:", bad)


def _setup():
    core.set_root(HERE)
    registry = core.build_registry()
    policy = core.YoloPolicy()
    td = tempfile.mkdtemp()
    tr = core.Transcript(Path(td) / "t.jsonl")
    return registry, policy, tr


def test_fix_round():
    registry, policy, tr = _setup()
    client = FakeClient()
    history = [{"role": "system", "content": "s"},
               {"role": "user", "content": "6*7=?"},
               {"role": "assistant", "content": "初稿: 41"}]
    events = []
    orig = core.reflect
    core.reflect = lambda *a, **k: "存在错误: 41 未经验证, 需要修正"
    try:
        answer = core.reflect_and_fix(client, registry, policy, tr, history,
                                      1, "初稿: 41", CFG,
                                      emit=lambda k, d: events.append((k, d)))
    finally:
        core.reflect = orig
    check("修正轮被触发(fix_round事件)", any(k == "fix_round" for k, _ in events))
    check("返回修正稿", answer == "修正稿: 42(来源: run_python)")
    check("历史含[反思修正]指令", any(m.get("role") == "user"
                                      and "[反思修正]" in str(m.get("content"))
                                      for m in history))


def test_pass_round():
    registry, policy, tr = _setup()
    client = FakeClient()
    history = [{"role": "system", "content": "s"},
               {"role": "user", "content": "q"},
               {"role": "assistant", "content": "初稿: 41"}]
    events = []
    orig = core.reflect
    core.reflect = lambda *a, **k: "反思通过"
    try:
        answer = core.reflect_and_fix(client, registry, policy, tr, history,
                                      1, "初稿: 41", CFG,
                                      emit=lambda k, d: events.append((k, d)))
    finally:
        core.reflect = orig
    check("反思通过时不触发修正轮", not any(k == "fix_round" for k, _ in events)
          and answer == "初稿: 41" and client.calls == 0)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print("[单测] 反思闭环机制")
    test_heuristic()
    test_fix_round()
    test_pass_round()
    print(f"[test_reflect_fix] 通过 {len(passed)} 项, 失败 {len(failed)} 项")
    sys.exit(0 if not failed else 1)
