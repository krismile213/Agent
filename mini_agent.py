#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mini_agent.py — CLI 适配器 (引擎见 agentcore.py)

一个引擎(agentcore, 事件回调驱动) + 多个薄前端:
  CLI  = 本文件(input() 权限确认, 控制台输出)
  Web  = server.py(FastAPI + SSE + 网页审批收件箱)

用法:
  python mini_agent.py --selftest                      # 离线自检(不需要API Key)
  python mini_agent.py "统计当前目录下所有py文件的行数"   # 单任务
  python mini_agent.py                                 # 交互模式(/new /usage /tools)
  python mini_agent.py --reflect "..."                 # 任务完成后自检反思
  python mini_agent.py --no-plugins ...                # 禁用全部插件(纯通用模式)
  python mini_agent.py --cwd "任何目录" ...             # 以任何目录为沙箱
  python mini_agent.py --session work --resume ...      # 恢复历史会话
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import agentcore as core
from agentcore import Tool, clip  # noqa: F401  (兼容旧插件 from mini_agent import ...)


# ============================================================
# CLI 权限策略: 交互式 y/n/a 确认
# ============================================================

class InteractivePolicy:
    """read级自动放行; write级逐次确认(可'a'本会话总允许); yolo全放行."""

    def __init__(self, yolo: bool = False):
        self.yolo = yolo
        self.always: set[str] = set()

    def allow(self, tool, kwargs: dict) -> bool:
        if self.yolo or tool.level == "read" or tool.name in self.always:
            return True
        print("  " + tool.preview(kwargs))
        while True:
            ans = input("  允许执行? [y=本次 / a=本会话总允许 / n=拒绝]: ").strip().lower()
            if ans in ("y", "yes"):
                return True
            if ans == "a":
                self.always.add(tool.name)
                return True
            if ans in ("n", "no", ""):
                return False


# ============================================================
# CLI 事件渲染: 把引擎事件打印成原来的样子
# ============================================================

def cli_emit(kind: str, data: dict):
    _live = cli_emit._live  # 流式输出状态(跨调用)
    if kind == "assistant_delta":
        if not _live["on"]:
            print("\n[助手] ", end="", flush=True)
            _live["on"] = True
        print(data["text"], end="", flush=True)
        return
    if _live["on"]:
        print()
        _live["on"] = False
    if kind == "assistant":
        print(f"\n[助手] {data['text']}")
    elif kind == "tool_call":
        print(f"  -> {data['name']}({data['args']})")
    elif kind == "tool_result":
        first = str(data["result"]).splitlines()[0][:200] if data["result"] else ""
        print(f"  <- {first}")
    elif kind == "permission_denied":
        print(f"  [拒绝] {data['name']}")
    elif kind == "injection_suspected":
        print(f"  [安全] 疑似提示注入已隔离(来源:{data['name']}, 标记:{data['marker']})")
    elif kind == "compact":
        print(f"[压缩] 约{data['tokens']}tokens, 已压缩{data['dropped']}条早期消息")
    elif kind == "plan":
        print(f"[计划]\n{clip(data['plan'], 1500)}")
    elif kind == "plan_approved":
        print("[计划] 已批准, 开始执行")
    elif kind == "plan_rejected":
        print(f"[计划] 未执行: {clip(str(data.get('plan', '')), 200)}")
    elif kind == "steps":
        print(f"[分步] 计划拆为 {len(data['steps'])} 步:")
        for i, s in enumerate(data["steps"], 1):
            print(f"  {i}. {s[:80]}")
    elif kind == "step_done":
        print(f"[步骤] {data['step']}/{data['total']} 完成: {clip(data['text'], 80)}")
    elif kind == "plan_stopped":
        print(f"[分步] 计划在第{data['at']}/{data['total']}步后停止")
    elif kind == "reflect":
        print(f"[反思] {clip(data['critique'], 800)}")
    elif kind == "fix_round":
        print(f"[修正] 反思发现问题, 自动修正一轮: {clip(data['critique'], 300)}")
    elif kind == "fatal":
        print(f"[错误] {data['message']}")
    elif kind == "max_turns":
        print("[警告] 达到最大轮数上限")
    # task_start/task_end 由调用方按原格式打印


cli_emit._live = {"on": False}  # 流式输出状态(跨调用)


# ============================================================
# 入口
# ============================================================

def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="mini_agent — 通用agent的CLI前端(引擎: agentcore)")
    ap.add_argument("task", nargs="*", help="任务描述(不带则进入交互模式)")
    ap.add_argument("--cwd", default=os.getcwd(), help="工作沙箱目录(默认当前目录)")
    ap.add_argument("--session", help="会话名(sessions/<名>.jsonl)")
    ap.add_argument("--resume", action="store_true", help="恢复指定会话的历史上下文")
    ap.add_argument("--yolo", action="store_true", help="跳过所有权限确认(风险自负)")
    ap.add_argument("--reflect", action="store_true", help="每个任务完成后做一次自检反思")
    ap.add_argument("--plan", action="store_true",
                    help="计划模式: 先生成执行计划, 人工批准后再执行")
    ap.add_argument("--stepwise", action="store_true",
                    help="分步执行计划: 每步之间暂停, 可继续/停止/输入修改指令")
    ap.add_argument("--no-plugins", action="store_true", help="禁用全部插件(纯通用模式)")
    ap.add_argument("--selftest", action="store_true", help="离线自检(不需要API Key)")
    args = ap.parse_args()
    core.set_root(args.cwd)

    cfg = core.load_config()
    registry = core.build_registry()
    if not args.no_plugins:
        core.load_plugins(registry, cfg)
        try:
            import mcp_bridge
            mcp_bridge.register_mcp_tools(registry, cfg)
        except Exception as e:
            core.log(f"[mcp] 外部工具接入失败(忽略): {type(e).__name__}: {e}")

    if args.selftest:
        sys.exit(core.selftest(registry))

    if not cfg["api_key"]:
        sys.exit("未配置API Key: 复制 config.example.json 为 config.json 并填入, "
                 "或设置环境变量 AGENT_API_KEY")

    name = args.session or datetime.now().strftime("%Y%m%d_%H%M%S")
    transcript = core.Transcript(core.HERE / "sessions" / f"{name}.jsonl")
    history = core.Transcript.load_messages(transcript.path) if args.resume else []
    if args.resume:
        print(f"[会话] 已恢复 {len(history)} 条历史消息")
    history.insert(0, {"role": "system", "content": core.build_system_prompt()})

    client = core.LLMClient(cfg)
    policy = InteractivePolicy(yolo=args.yolo)
    print(f"[启动] model={cfg['model']} 沙箱={core.ROOT} 会话={transcript.path.name}")
    print(f"[工具] {'/'.join(registry.names())}")

    def confirm_plan(plan: str) -> bool:
        if args.yolo:
            print("[计划] (--yolo 自动批准)")
            return True
        return input("  批准执行该计划? [y/n]: ").strip().lower() in ("y", "yes", "")

    def step_confirm_cli(i: int, n: int, step_text: str, last_answer: str):
        if args.yolo:
            return "continue", ""
        raw = input(f"\n  [步骤{i}/{n}完成] 回车=继续下一步 / s=停止"
                    f" / 或直接输入对后续步骤的修改指令: ").strip()
        if raw.lower() in ("s", "stop"):
            return "stop", ""
        if not raw:
            return "continue", ""
        return "continue", raw

    def run_with_reflect(task_text: str) -> str:
        start = len(history)
        if args.plan:
            answer = core.plan_and_run(client, registry, policy, confirm_plan,
                                       transcript, history, task_text, cfg,
                                       emit=cli_emit,
                                       stepwise=args.stepwise,
                                       step_confirm=step_confirm_cli)
        else:
            answer = core.run_task(client, registry, policy, transcript, history,
                                   task_text, cfg, emit=cli_emit)
        if args.reflect and answer and not answer.startswith(("[", "(")):
            answer = core.reflect_and_fix(client, registry, policy, transcript,
                                          history, start, answer, cfg,
                                          emit=cli_emit)
        return answer

    if args.task:
        answer = run_with_reflect(" ".join(args.task))
        print(f"\n[完成] {clip(answer, 2000)}")
    else:
        print("交互模式: 输入任务回车发送; /new清空历史 /usage看用量 /tools列工具; exit退出.")
        while True:
            try:
                line = input("\n你> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            low = line.lower()
            if low in ("exit", "quit", "q", "/exit"):
                break
            if line == "/new":
                history[:] = [history[0]]
                print("[会话] 已清空历史(保留系统提示与项目记忆)")
                continue
            if line == "/usage":
                print(f"[用量] {client.usage_summary()}")
                continue
            if line == "/tools":
                for t in registry._tools.values():
                    tag = "读" if t.level == "read" else "写"
                    print(f"  [{tag}] {t.name} — {t.description.split('.')[0]}")
                continue
            try:
                run_with_reflect(line)
            except KeyboardInterrupt:
                print("\n[中断] 当前任务终止, 会话已保留(--resume 可继续).")

    transcript.log("end", {"reason": "exit", **client.usage})
    print(f"[结束] {client.usage_summary()}")
    transcript.close()


if __name__ == "__main__":
    main()
