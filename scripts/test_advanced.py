#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/test_advanced.py — 子agent扇出 + 计划模式 测试

单测(零LLM): 子agent只读隔离 / 角色注入+工具白名单 / verify核查员 /
             research扇出与上限 / 计划批准与否决 / 分步执行与中途转向
E2E(真实LLM): research 并行调查 / 角色扇出+核查 / --plan 计划 / --stepwise 分步
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
    """脚本化模型: 按预设序列返回消息, 并记录收到的messages/tools."""

    def __init__(self, seq):
        self.seq = list(seq)
        self.calls = 0
        self.seen = []
        self.usage = {"calls": 0, "prompt_tokens": 0,
                      "completion_tokens": 0, "total_tokens": 0}

    def chat(self, messages, tools=None):
        self.calls += 1
        self.usage["calls"] += 1
        self.seen.append({"msgs": messages, "tools": tools})
        return self.seq[min(self.calls - 1, len(self.seq) - 1)]


def tool_call(name, args):
    return {"id": f"c_{name}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


# ---------------- 单测 ----------------

def unit_subagent_readonly():
    core.set_root(HERE)
    reg = core.build_registry()
    client = FakeClient([
        {"role": "assistant", "content": "",
         "tool_calls": [tool_call("write_file", {"path": "x.txt", "content": "1"}),
                        tool_call("list_dir", {"path": "."})]},
        {"role": "assistant", "content": "结论: 目录内容已核实。"},
    ])
    core.run_subagent(client, reg, "调查目录", CFG, name="unit")
    check("子agent返回最终结论", client.seq[1]["content"] in
          ("结论: 目录内容已核实。",))
    sub_files = sorted((HERE / "sessions").glob("sub_*_unit.jsonl"),
                       key=lambda p: p.stat().st_mtime)
    ok = False
    if sub_files:
        txt = sub_files[-1].read_text("utf-8")
        ok = "未注册的工具: write_file" in txt
    check("子agent注册表为只读(write_file被拒)", ok)


def unit_roles_tools():
    core.set_root(HERE)
    reg = core.build_registry()
    client = FakeClient([{"role": "assistant", "content": "结论: 完成。"}])
    core.run_subagent(client, reg, "任务A", CFG, name="role1",
                      role="代码审计员: 只关注代码质量与结构",
                      tools=["grep", "list_dir"])
    sysmsg = client.seen[0]["msgs"][0]["content"]
    check("角色注入子agent系统提示", "代码审计员" in sysmsg)
    names = sorted(t["function"]["name"] for t in client.seen[0]["tools"] or [])
    check("工具白名单生效(仅grep/list_dir)", names == ["grep", "list_dir"])
    r = core.run_subagent(client, reg, "任务B", CFG, name="role2",
                          tools=["no_such_tool"])
    check("白名单无交集时报错回灌", "无交集" in r)


def unit_verify():
    core.set_root(HERE)
    core._REG_REF = core.build_registry()
    calls = []
    orig = core.run_subagent

    def fake(client, registry, task, cfg, name="sub", role="", tools=None):
        calls.append({"task": task, "role": role, "name": name})
        return f"结论:{task[:8]}"

    core.run_subagent = fake
    try:
        r = core.t_research([{"task": "问题一", "role": "审计员"},
                             {"task": "问题二"}], verify=True)
        check("verify追加核查员调用", any(c["name"] == "verify"
                                        and "核查" in c["role"] for c in calls))
        check("结果含角色标注与[核查]节",
              "角色: 审计员" in r and "[核查]" in r)
        check("任务对象解析(2子任务+1核查)", len(calls) == 3)
    finally:
        core.run_subagent = orig


def unit_research_fanout():
    core.set_root(HERE)
    core._REG_REF = core.build_registry()
    orig = core.run_subagent

    def fake_sub(client, registry, task, cfg, name="sub", role="", tools=None):
        return f"结论-{task}"

    core.run_subagent = fake_sub
    try:
        r = core.t_research(["甲任务", "乙任务", "丙任务"])
        check("research 扇出汇总(3任务)",
              "[子任务1] 甲任务" in r and "结论-丙任务" in r)
        r = core.t_research(["a", "b", "c", "d", "e", "f"])
        check("research 硬上限4个任务", "[子任务5]" not in r and "[子任务4]" in r)
        check("research 接受单字符串",
              "[子任务1]" in core.t_research("单个字符串也接受"))
    finally:
        core.run_subagent = orig


def unit_plan_mode():
    core.set_root(HERE)
    reg = core.build_registry()
    tr = core.Transcript(Path(tempfile.mkdtemp()) / "t.jsonl")
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
    client2 = FakeClient([{"role": "assistant", "content": "1. 某计划"}])
    events2 = []
    answer2 = core.plan_and_run(client2, reg, core.YoloPolicy(),
                                lambda p: False, tr,
                                [{"role": "system", "content": "s"}],
                                "任务X", CFG, emit=lambda k, d: events2.append(k))
    check("计划否决后不执行",
          answer2.startswith("[计划被用户否决]") and client2.calls == 1
          and "plan_rejected" in events2)


def unit_stepwise():
    core.set_root(HERE)
    reg = core.build_registry()
    plan_text = "1. 第一步看目录\n2. 第二步数文件\n3. 第三步写结论\n预计轮次: 3"
    tr = core.Transcript(Path(tempfile.mkdtemp()) / "t.jsonl")
    client = FakeClient([
        {"role": "assistant", "content": plan_text},
        {"role": "assistant", "content": "s1"},
        {"role": "assistant", "content": "s2"},
        {"role": "assistant", "content": "s3"},
    ])
    history = [{"role": "system", "content": "s"}]
    events = []

    def sc(i, n, st, ans):
        if i == 1:
            return "continue", "后续跳过无关文件"
        return "continue", ""

    answer = core.plan_and_run(client, reg, core.YoloPolicy(), lambda p: True,
                               tr, history, "总任务", CFG,
                               emit=lambda k, d: events.append(k),
                               stepwise=True, step_confirm=sc)
    check("分步执行完成全部3步", answer == "s3" and client.calls == 4)
    check("step事件流(steps + 3x step_done)",
          "steps" in events and events.count("step_done") == 3)
    check("修改指令注入历史",
          any("计划修改指令" in str(m.get("content", ""))
              and "后续跳过" in str(m.get("content", ""))
              for m in history if m.get("role") == "user"))
    client2 = FakeClient([{"role": "assistant", "content": plan_text},
                          {"role": "assistant", "content": "s1"}])
    answer2 = core.plan_and_run(client2, reg, core.YoloPolicy(), lambda p: True,
                                core.Transcript(Path(tempfile.mkdtemp()) / "t2.jsonl"),
                                [{"role": "system", "content": "s"}], "总任务", CFG,
                                emit=lambda k, d: None, stepwise=True,
                                step_confirm=lambda i, n, st, a: ("stop", ""))
    check("step_confirm停止生效",
          "第1步后按用户要求停止" in answer2 and client2.calls == 2)


# ---------------- E2E ----------------

def e2e():
    def run(args, timeout=600):
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

    print("[E2E] 角色扇出 + 核查员")
    out = run(["--yolo", "--session", "adv_roles",
               "用 research 派两个带角色的子agent并行调查: "
               "1) 角色'代码审计员': 统计 plugins 目录下 .py 文件数 "
               "2) 角色'文档检查员': 读 README.md 数快速开始代码块里有几条命令; "
               "开启 verify 核查, 最后汇总两个答案"])
    check("角色扇出+核查完成",
          "-> research(" in out and "[完成]" in out and "核查" in out)

    print("[E2E] 计划模式全流程")
    target = HERE / "plan_test.md"
    target.unlink(missing_ok=True)
    out = run(["--yolo", "--plan", "--session", "adv_plan",
               "统计当前目录下所有 .py 文件的数量, 并把结果写入 plan_test.md"])
    check("计划被生成与批准", "[计划]" in out and "已批准" in out)
    check("按计划执行(文件落盘)", target.exists())
    target.unlink(missing_ok=True)

    print("[E2E] 分步计划(--stepwise)")
    out = run(["--yolo", "--plan", "--stepwise", "--session", "adv_steps",
               "先读 README.md 的前5行了解项目, 再统计当前目录 .py 文件数, "
               "最后一句话总结"])
    check("分步事件与完成",
          "已批准" in out and "步骤" in out and "[完成]" in out)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    args = set(sys.argv[1:])
    if "--e2e-only" not in args:
        print("[单测] 子agent/角色/核查/计划/分步")
        unit_subagent_readonly()
        unit_roles_tools()
        unit_verify()
        unit_research_fanout()
        unit_plan_mode()
        unit_stepwise()
    if "--unit-only" not in args:
        e2e()
    print(f"[test_advanced] 通过 {len(passed)} 项, 失败 {len(failed)} 项")
    sys.exit(0 if not failed else 1)
