#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/test_enterprise.py — 企业化三件套测试: 数据层/可用性/可观测性

单测(零LLM): SQLite会话往返+JSONL回退 / 熔断器 / 限流器 / 遥测记录
E2E(server): /api/health + /api/metrics(Prometheus格式) + 一次真实任务后指标变化
"""

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import agentcore as core  # noqa: E402
import storage  # noqa: E402

PORT = 8807
BASE = f"http://127.0.0.1:{PORT}"
passed, failed = [], []


def check(name, cond):
    (passed if cond else failed).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")


CFG = {"max_turns": 5, "context_budget_tokens": 999_999}


def unit_storage():
    with tempfile.TemporaryDirectory() as td:
        storage._reset(Path(td) / "t.db")
        tr = core.Transcript("sess_ut")
        tr.log("message", {"msg": {"role": "user", "content": "hi"}})
        tr.log("message", {"msg": {"role": "assistant",
                                   "content": "", "tool_calls": [
                                       {"id": "c1", "type": "function",
                                        "function": {"name": "list_dir",
                                                     "arguments": "{}"}}]}})
        tr.log("message", {"msg": {"role": "user", "content": "next"}})
        msgs = core.Transcript.load_messages("sess_ut")
        check("SQLite会话往返+悬空修复",
              len(msgs) == 4 and any(m.get("role") == "tool" and "中断" in
                                     m.get("content", "") for m in msgs))
        tr.clear()
        check("clear后为空", core.Transcript.load_messages("sess_ut") == [])
        # JSONL回退
        legacy = core.HERE / "sessions" / "legacy_ut.jsonl"
        legacy.write_text(json.dumps({"kind": "message",
                                      "msg": {"role": "user", "content": "旧"}},
                                     ensure_ascii=False) + "\n", encoding="utf-8")
        check("旧JSONL回退读取",
              any(m.get("content") == "旧" for m in
                  core.Transcript.load_messages("legacy_ut")))
        legacy.unlink()
        storage._reset(HERE / "agent.db")


def unit_breaker():
    with tempfile.TemporaryDirectory() as td:
        storage._reset(Path(td) / "t.db")
        br, _ = storage.Breaker(fails=2, cooldown=0.6), None
        try:
            br.check()
            br.record(False)
            br.check()
            br.record(False)
            try:
                br.check()
                raised = False
            except core._FatalError:
                raised = True
            check("连续失败触发熔断(快速失败)", raised)
            time.sleep(0.7)
            br.check()  # 半开放行
            br.record(True)
            br.check()
            check("冷却后半开恢复", True)
        finally:
            storage._reset(HERE / "agent.db")


def unit_ratelimit():
    with tempfile.TemporaryDirectory() as td:
        storage._reset(Path(td) / "t.db")
        rl = storage.RateLimiter(rpm=2, max_wait=0.3)
        rl.acquire()
        rl.acquire()
        t0 = time.time()
        try:
            rl.acquire()
            raised = False
        except core._FatalError:
            raised = True
        check("超限快速失败(有界等待)", raised and time.time() - t0 < 2)
        storage._reset(HERE / "agent.db")


def unit_telemetry():
    with tempfile.TemporaryDirectory() as td:
        storage._reset(Path(td) / "t.db")
        storage.task_begin("ut_sess")
        storage.llm_call("test-model", 120, "ok", 0,
                         {"prompt_tokens": 10, "completion_tokens": 5}, True)
        storage.tool_call("grep", "read", True, 8)
        storage.tool_call("run_python", "write", False, 30)
        storage.approval("ut_sess", "tool", "write_file", "allowed", "附注")
        storage.task_end("ok", "答案", 2, {"prompt_tokens": 10,
                                           "completion_tokens": 5,
                                           "total_tokens": 15})
        db = storage.get_db()
        check("tasks表落库", db.execute("SELECT COUNT(*) n FROM tasks "
                                        "WHERE status='ok'").fetchone()["n"] >= 1)
        check("llm_calls表落库(带时延)",
              db.execute("SELECT latency_ms FROM llm_calls").fetchone()[0] == 120)
        check("tool_calls成败落库",
              db.execute("SELECT SUM(ok) FROM tool_calls").fetchone()[0] == 1)
        check("审批审计落库(含附注)",
              "allowed" in (db.execute("SELECT decision FROM approvals")
                            .fetchone()[0] or ""))
        m = storage.q_metrics()
        check("q_metrics汇总", m["llm_calls"] >= 1
              and "llm_latency_p95_ms" in m)
        storage._reset(HERE / "agent.db")


def e2e_metrics():
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--port", str(PORT), "--no-open",
         "--cwd", str(HERE)],
        cwd=str(HERE), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                requests.get(f"{BASE}/api/health", timeout=2)
                break
            except Exception:
                time.sleep(0.3)
        else:
            raise RuntimeError("server 启动失败")
        r = requests.get(f"{BASE}/api/health", timeout=5).json()
        check("health端点", r.get("status") == "ok" and "model" in r)
        before = requests.get(f"{BASE}/api/metrics", timeout=5).text
        check("metrics为Prometheus格式",
              "agent_llm_calls_total" in before
              and "# TYPE agent_tasks_total counter" in before)
        # 一次真实小任务 → 指标增长
        requests.post(f"{BASE}/api/chat", json={
            "session": "ent_e2e", "reflect": False,
            "message": "只回复两个字: 就绪"}, timeout=10)
        for _ in range(60):
            time.sleep(1)
            h = requests.get(f"{BASE}/api/sessions/ent_e2e/history",
                             timeout=5).json()
            if not h.get("running"):
                break
        after = requests.get(f"{BASE}/api/metrics", timeout=5).text
        n_calls = lambda t: int([ln for ln in t.splitlines()
                                 if ln.startswith("agent_llm_calls_total ")][0]
                                .split()[-1])
        check("任务后LLM调用指标增长", n_calls(after) > n_calls(before))
        check("任务成功计入tasks指标",
              'agent_tasks_total{status="ok"}' in after)
    finally:
        proc.terminate()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    args = set(sys.argv[1:])
    if "--e2e-only" not in args:
        print("[单测] 存储/熔断/限流/遥测")
        unit_storage()
        unit_breaker()
        unit_ratelimit()
        unit_telemetry()
    if "--unit-only" not in args:
        print("[E2E] health/metrics端点与真实任务指标")
        e2e_metrics()
    print(f"[test_enterprise] 通过 {len(passed)} 项, 失败 {len(failed)} 项")
    sys.exit(0 if not failed else 1)
