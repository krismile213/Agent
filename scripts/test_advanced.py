#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/test_advanced.py — 子agent扇出 + 计划模式 测试

单测(零LLM): 子agent只读隔离 / research扇出与上限 / 计划批准与否决
E2E(真实LLM): research 并行调查 / --plan 计划模式全流程
用法: python scripts/test_advanced.py [--e2e-only|--unit-only]
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import agentcore as core  # noqa: E402

passed, failed = [], []
CFG = {"max_turns": 5, "context_budget_tokens": 999_999}


def check(name, cond):
    (passed if cond else failed).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")


class FakeClient:
    """脚本化模型: 按预设序列返回消息."""

    def __init__(self, seq):
        self.seq = list(seq)
        self.calls = 0
        self.usage = {"calls": 0, "prompt_tokens": 0,
                      "completion_tokens": 0, "total_tokens": 0}

    def chat(self, messages, tools=None):
        self.calls += 1
        self.usage["calls"] += 1
        return self.seq[min(self.calls - 1, len(self.seq) - 1)]


def tool_call(name, args):
    return {"id": f"c_{name}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


# ---------------- 单测 ----------------

def unit_subagent_readonly():
    core.set_root(HERE)
    reg = core.build_registry()
    # 模型先尝试写工具(应被只读注册表拒绝), 再正常调查, 最后给结论
    client = FakeClient([
        {"role": "assistant", "content": "",
         "tool_calls": [tool_call("write_file", {"path": "x.txt", "content": "1"}),
                        tool_call("list_dir", {"path": "."})]},
        {"role": "assistant", "content": "结论: 目录内容已核实。"},
    ])
    with tempfile.TemporaryDirectory() as td:
        # 子agent会话文件落在 sessions/; 这里只验证返回值, 文件由引擎自管
        answer = core.run_subagent(client, reg, "调查目录", CFG, name="unit")
    check("子agent返回最终结论", answer == "结论: 目录内容已核实。")
    # 只读隔离验证: 用同样的调用直接跑主引擎(有写工具)对比——改为检查子agent的
    # transcript里 write_file 的结果文本
    sub_files = sorted((HERE / "sessions").glob("sub_*_unit.jsonl"),
                       key=lambda p: p.stat().st_mtime)
    ok = False
    if sub_files:
        txt = sub_files[-1].read_text("utf-8")
        ok = "未注册的工具: write_file" in txt and "已注册" not in txt[:200]
    check("子agent注册表为只读(write_file被拒)", ok)


def unit_research_fanout():
    core.set_root(HERE)
    core._REG_REF = core.build_registry()
    orig = core.run_subagent

    def fake_sub(client, registry, task, cfg, name="sub"):
        return f"结论-{task}"

    core.run_subagent = fake_sub
    try:
        r = core.t_research(["甲任务", "乙任务", "丙任务"])
        check("research 扇出汇总(3任务)",
              "[子任务1] 甲任务" in r and "结论-丙任务" in r)
        r = core.t_research(["a", "b", "c", "d", "e", "f"])
        check("research 硬上限4个任务", "[子任务5]" not in r and "[子任务4]" in r)
        r = core.t_research("单个字符串也接受")
        check("research 接受单字符串", "[子任务1]" in r)
    finally:
        core.run_subagent = orig


def unit_plan_mode():
    core.set_root(HERE)
    reg = core.build_registry()
    tr = core.Transcript(Path(tempfile.mkdtemp()) / "t.jsonl")
    # 批准路径
    client = FakeClient([
        {"role": "assistant", "content": "1. 用list_dir查看 → 文件数\n预计轮次: 2"},
        {"role": "assistant", "content": "executed"},
    ])
    history = [{"role": "system", "content": "s"}]
    events = []
    answer = core.plan_and_run(client, reg, core.YoloPolicy(),
                               lambda p: True, tr, history, "统计文件", CFG,
                               emit=lambda k, d: events.append(k))
    check("计划批准后执行", answer == "executed" and client.calls == 2)
    check("计划事件流(plan→plan_approved)",
          events.count("plan") == 1 and "plan_approved" in events)
    check("计划注入任务指令", any("已批准的执行计划" in str(m.get("content", ""))
                                  for m in history if m.get("role") == "user"))
    # 否决路径
    client2 = FakeClient([{"role": "assistant", "content": "1. 某计划"}])
    history2 = [{"role": "system", "content": "s"}]
    events2 = []
    answer2 = core.plan_and_run(client2, reg, core.YoloPolicy(),
                                lambda p: False, tr, history2, "任务X", CFG,
                                emit=lambda k, d: events2.append(k))
    check("计划否决后不执行",
          answer2.startswith("[计划被用户否决]") and client2.calls == 1
          and "plan_rejected" in events2)


# ---------------- E2E ----------------

def e2e():
    def run(args, timeout=420):
        p = subprocess.run([sys.executable, "mini_agent.py"] + args,
                           cwd=str(HERE), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.stdout + p.stderr

    print("[E2E] research 并行扇出")
    out = run(["--yolo", "--session", "adv_research",
               "用 research 工具并行调查两件事: 1) eval/golden.jsonl 里有几个评测用例 "
               "2) plugins/workmain.py 里 register 函数 return 的数字是多少. 汇总答案"])
    check("research 工具被调用", "-> research(" in out)
    check("扇出任务完成", "[完成]" in out and "失败" not in out[-500:])
    print("    (输出尾部:", out.strip().splitlines()[-1][:100], ")")

    print("[E2E] 计划模式全流程")
    target = HERE / "plan_test.md"
    target.unlink(missing_ok=True)
    out = run(["--yolo", "--plan", "--session", "adv_plan",
               "统计当前目录下所有 .py 文件的数量, 并把结果写入 plan_test.md"])
    check("计划被生成与批准", "[计划]" in out and "已批准" in out)
    check("按计划执行(文件落盘)", target.exists())
    if target.exists():
        print("    (plan_test.md 内容:", target.read_text(encoding='utf-8')[:80].replace(chr(10), ' '), ")")
        target.unlink()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    args = set(sys.argv[1:])
    if "--e2e-only" not in args:
        print("[单测] 子agent与计划模式")
        unit_subagent_readonly()
        unit_research_fanout()
        unit_plan_mode()
    if "--unit-only" not in args:
        e2e()
    print(f"[test_advanced] 通过 {len(passed)} 项, 失败 {len(failed)} 项")
    sys.exit(0 if not failed else 1)
