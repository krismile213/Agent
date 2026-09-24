#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eval/run_eval.py — 金标准评测 runner

用途: 每次换模型 / 改 prompt / 改工具描述 / 改引擎逻辑后, 跑一遍回归:
    python eval/run_eval.py                    # 全部套件
    python eval/run_eval.py --suite core       # 只跑核心(不依赖workmain插件)
    python eval/run_eval.py --case arith_tool  # 只跑单用例
    python eval/run_eval.py --model glm-5.3    # 临时换模型对比

设计:
  - 断言式判定(确定性): 工具使用/回答包含/是否自然完成/LLM调用上限, 不用judge模型
  - 每用例独立会话+独立沙箱(cwd可指 fixtures 夹具目录), Yolo 策略跑批
  - MEMORY.md 全程备份/恢复, 评测不污染记忆
  - 报告落 eval/reports/eval_<时间戳>.md, 退出码 0/1 可挂 CI
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import agentcore as core  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent
GOLDEN = EVAL_DIR / "golden.jsonl"
FIXTURES = EVAL_DIR / "fixtures"
REPORTS = EVAL_DIR / "reports"


def load_cases() -> list:
    cases = []
    for line in GOLDEN.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cases.append(json.loads(line))
    return cases


def run_case(case: dict, cfg: dict, registry, policy) -> dict:
    root = HERE if case.get("cwd", "here") == "here" else FIXTURES
    core.set_root(root)
    tr = core.Transcript(f"eval_{case['id']}")
    tr.clear()  # 每次干净重跑
    history = [{"role": "system", "content": core.build_system_prompt()}]
    client = core.LLMClient(cfg)
    events = []

    def emit(kind, data):
        events.append({"kind": kind, **data})

    t0 = time.time()
    mode = case.get("mode", "chat")
    if mode in ("plan", "plan_stepwise"):
        # 评测口径: 计划自动批准; 分步模式步间自动继续(只测机制不测交互)
        answer = core.plan_and_run(
            client, registry, policy, lambda p: True, tr, history,
            case["task"], cfg, emit=emit,
            stepwise=(mode == "plan_stepwise"),
            step_confirm=(lambda i, n, st, a: ("continue", ""))
            if mode == "plan_stepwise" else None)
    else:
        answer = core.run_task(client, registry, policy, tr, history,
                               case["task"], cfg, emit=emit)
    tr.close()
    return {"answer": answer or "", "events": events,
            "usage": dict(client.usage), "dur": round(time.time() - t0, 1)}


def evaluate(case: dict, res: dict) -> list:
    fails = []
    evs, ans = res["events"], res["answer"]
    called = [e["name"] for e in evs if e["kind"] == "tool_call"]
    ch = case.get("checks", {})
    for t in ch.get("tools_used", []):
        if t not in called:
            fails.append(f"缺少工具 {t}")
    if ch.get("tools_used_any") and not any(t in called for t in ch["tools_used_any"]):
        fails.append(f"未使用任一 {ch['tools_used_any']}")
    for t in ch.get("tools_forbidden", []):
        if t in called:
            fails.append(f"禁用工具被调用 {t}")
    for s in ch.get("answer_contains", []):
        if s not in ans:
            fails.append(f"回答缺少 {s!r}")
    if ch.get("answer_contains_any") and not any(s in ans for s in ch["answer_contains_any"]):
        fails.append(f"回答未含任一 {ch['answer_contains_any']}")
    for s in ch.get("answer_not_contains", []):
        if s in ans:
            fails.append(f"回答不应包含 {s!r}")
    finished = (any(e["kind"] == "task_end" for e in evs)
                and not ans.startswith(("[", "(")))
    if ch.get("must_finish", True) and not finished:
        fails.append(f"任务未自然完成(answer={ans[:60]!r})")
    for k in ch.get("events_contains", []):
        if not any(e.get("kind") == k for e in evs):
            fails.append(f"缺少事件 {k}")
    m = ch.get("max_llm_calls")
    if m and res["usage"]["calls"] > m:
        fails.append(f"LLM调用{res['usage']['calls']}次超上限{m}")
    return fails


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="mini_agent 金标准评测")
    ap.add_argument("--suite", choices=["core", "workmain", "all"], default="all")
    ap.add_argument("--case", help="只跑指定 id 的用例")
    ap.add_argument("--model", help="临时覆盖模型名(对比用)")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个")
    args = ap.parse_args()

    cfg = core.load_config()
    if not cfg["api_key"]:
        sys.exit("未配置 API Key, 无法跑评测")
    if args.model:
        cfg["model"] = args.model

    registry = core.build_registry()
    core.load_plugins(registry, cfg)
    policy = core.YoloPolicy()
    has_wm = registry.get("query_sku_route") is not None

    cases = load_cases()
    if args.suite != "all":
        cases = [c for c in cases if c["suite"] == args.suite]
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
    if args.limit:
        cases = cases[:args.limit]

    mem = HERE / "MEMORY.md"  # 评测期间隔离记忆
    backup = mem.read_text("utf-8") if mem.exists() else None
    rows, skipped, t0 = [], 0, time.time()
    try:
        for c in cases:
            if c["suite"] == "workmain" and not has_wm:
                print(f"SKIP  {c['id']}  (workmain 插件不可用)")
                skipped += 1
                continue
            print(f"RUN   {c['id']}  {c['task'][:40]}")
            try:
                res = run_case(c, cfg, registry, policy)
            except RuntimeError as e:  # 网络抖动等: 该用例重试一次, 仍败则记FAIL继续
                print(f"  [重试] 运行异常({str(e)[:80]}), 该用例重试一次")
                try:
                    res = run_case(c, cfg, registry, policy)
                except RuntimeError as e2:
                    res = {"answer": "", "events": [],
                           "usage": {"calls": 0, "prompt_tokens": 0,
                                     "completion_tokens": 0, "total_tokens": 0},
                           "dur": 0.0, "fails_override": f"运行异常: {str(e2)[:120]}"}
            fails = res.pop("fails_override", None) or evaluate(c, res)
            ok = not fails
            rows.append({"case": c["id"], "ok": ok, "fails": fails, **res})
            mark = "PASS" if ok else "FAIL"
            print(f"  {mark}  LLM调用{res['usage']['calls']}次 "
                  f"{res['usage']['total_tokens']:,}tokens {res['dur']}s")
            for f in fails:
                print(f"       - {f}")
    finally:
        if backup is None:
            mem.unlink(missing_ok=True)
        else:
            mem.write_text(backup, encoding="utf-8")
        core.set_root(HERE)

    n_ok = sum(1 for r in rows if r["ok"])
    total_tok = sum(r["usage"]["total_tokens"] for r in rows)
    dur = round(time.time() - t0, 1)
    score = f"{n_ok}/{len(rows)}"

    REPORTS.mkdir(exist_ok=True)
    rp = REPORTS / f"eval_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    lines = [f"# 评测报告 {datetime.now().strftime('%Y-%m-%d %H:%M')}",
             "",
             f"- 模型: `{cfg['model']}`  套件: {args.suite}  "
             f"通过: **{score}**(跳过{skipped})  总token: {total_tok:,}  用时: {dur}s",
             "",
             "| 用例 | 结果 | LLM调用 | tokens | 失败断言 |",
             "|---|---|---|---|---|"]
    for r in rows:
        fails = "<br>".join(r["fails"]) if r["fails"] else ""
        lines.append(f"| {r['case']} | {'✅' if r['ok'] else '❌'} | "
                     f"{r['usage']['calls']} | {r['usage']['total_tokens']:,} | {fails} |")
    lines += ["", f"> 失败用例的完整事件流见 `eval/sessions/eval_<id>.jsonl`"]
    rp.write_text("\n".join(lines), encoding="utf-8")

    print(f"\n[评测] 通过 {score}, 跳过 {skipped}, 总token {total_tok:,}, "
          f"用时 {dur}s\n[评测] 报告: {rp}")
    return 0 if n_ok == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
