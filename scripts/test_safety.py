#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/test_safety.py — 写安全网 + 计划附加要求 + Web流式转向 测试

单测(零LLM): write_file 覆盖自动备份 / undo_write 恢复 / 计划批准附带要求注入
E2E(真实LLM+server): 流式delta事件 / 计划批准带附加要求 / 分步修改指令
                      / 写审批一路绿灯 → 任务完成
用法: python scripts/test_safety.py [--e2e-only|--unit-only]
"""

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import agentcore as core  # noqa: E402

PORT = 8804
BASE = f"http://127.0.0.1:{PORT}"
SESSION = "safety_e2e"

passed, failed = [], []


def check(name, cond):
    (passed if cond else failed).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")


# ---------------- 单测 ----------------

def unit_backup_undo():
    with tempfile.TemporaryDirectory() as td:
        core.set_root(td)
        reg = core.build_registry()
        trash = core.TRASH_DIR

        def _trash_files():
            return set(p.name for p in trash.glob("*")) if trash.is_dir() else set()

        before = _trash_files()
        r1 = reg.execute("write_file", {"path": "demo.txt", "content": "版本1"})
        check("新建不产生备份", "已写入" in r1 and "备份" not in r1)
        r2 = reg.execute("write_file", {"path": "demo.txt", "content": "版本2"})
        check("覆盖自动备份", "已备份" in r2 and "undo_write" in r2)
        new_baks = _trash_files() - before
        check("备份文件落盘.trash", len(new_baks) >= 1)
        check("覆盖后内容为新版本",
              (Path(td) / "demo.txt").read_text(encoding="utf-8") == "版本2")
        r3 = reg.execute("undo_write", {})
        check("undo_write 恢复原内容", "已撤销" in r3
              and (Path(td) / "demo.txt").read_text(encoding="utf-8") == "版本1")
        # 清理本测试产生的备份, 不动其他
        if (trash / "index.jsonl").exists():
            lines = [ln for ln in (trash / "index.jsonl").read_text("utf-8").splitlines()
                     if ln.strip()]
            (trash / "index.jsonl").write_text(
                "\n".join(ln for ln in lines if "demo.txt" not in ln) + ("\n" if lines else ""),
                encoding="utf-8")
        for b in new_baks:
            (trash / b).unlink(missing_ok=True)


def unit_plan_extra():
    core.set_root(HERE)
    reg = core.build_registry()
    client = FakeClient([
        {"role": "assistant", "content": "1. 统计文件\n2. 输出结论\n预计轮次: 2"},
        {"role": "assistant", "content": "done"},
    ])
    history = [{"role": "system", "content": "s"}]
    tr = core.Transcript(Path(tempfile.mkdtemp()) / "t.jsonl")
    answer = core.plan_and_run(client, reg, core.YoloPolicy(),
                               lambda p: (True, "只看根目录, 不要递归"),
                               tr, history, "统计md",
                               {"max_turns": 5, "context_budget_tokens": 999_999})
    check("附加要求随批准注入历史",
          any(m.get("role") == "user" and "计划附加要求" in str(m.get("content", ""))
              and "只看根目录" in str(m.get("content", ""))
              for m in history))
    check("注入后照常执行", answer == "done" and client.calls == 2)


class FakeClient:
    def __init__(self, seq):
        self.seq = list(seq)
        self.calls = 0
        self.usage = {"calls": 0, "prompt_tokens": 0,
                      "completion_tokens": 0, "total_tokens": 0}

    def chat(self, messages, tools=None):
        self.calls += 1
        self.usage["calls"] += 1
        return self.seq[min(self.calls - 1, len(self.seq) - 1)]


# ---------------- E2E ----------------

class Sub:
    """SSE订阅器: 断线自动重连(Last-Event-ID续传) —— 本测试顺带验证重放机制.
    (后台化运行环境下长连接可能被重置, 浏览器EventSource自带重连不受影响)"""

    def __init__(self, base: str, session: str):
        self.base, self.session = base, session
        self.q: queue.Queue = queue.Queue()
        self.last = 0
        self.stop = False
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self.stop:
            try:
                headers = {"Last-Event-ID": str(self.last)} if self.last else {}
                with requests.get(f"{self.base}/api/events?session={self.session}",
                                  headers=headers, stream=True,
                                  timeout=600) as r:
                    for line in r.iter_lines(chunk_size=1, decode_unicode=True):
                        if not line:
                            continue
                        if line.startswith("id: "):
                            try:
                                self.last = max(self.last, int(line[4:]))
                            except ValueError:
                                pass
                        elif line.startswith("data: "):
                            try:
                                ev = json.loads(line[6:])
                            except json.JSONDecodeError:
                                continue
                            self.q.put(ev)
            except BaseException as e:
                if not self.stop:
                    print(f"    [sse重连] {type(e).__name__}")
                    time.sleep(0.5)


def e2e():
    target = HERE / "steer_test.md"
    target.unlink(missing_ok=True)
    # 每次干净跑: 清掉本测试的历史会话, 避免失败重跑累积脏状态
    sess_file = HERE / "sessions" / f"{SESSION}.jsonl"
    sess_file.unlink(missing_ok=True)
    srv_log = HERE / "server_diag.log"
    # Windows下DETACHED+新进程组: 把server从本进程树剥离,
    # 免受外层作业对象/控制台生命周期影响(后台化运行环境下事件循环会被卡死)
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--port", str(PORT), "--no-open", "--cwd", str(HERE)],
        cwd=str(HERE), stdout=open(srv_log, "w", encoding="utf-8", errors="replace"),
        stderr=subprocess.STDOUT, creationflags=flags)
    try:
        for _ in range(60):
            try:
                requests.get(f"{BASE}/api/sessions", timeout=2)
                break
            except Exception:
                time.sleep(0.3)
        else:
            raise RuntimeError("server 启动失败")
        q = Sub(BASE, SESSION)
        time.sleep(0.5)

        r = requests.post(f"{BASE}/api/chat", json={
            "session": SESSION, "reflect": False, "plan": True, "stepwise": True,
            "message": "先统计当前目录下的 .md 文件数量并告诉我, 经我确认后再把结论写入 steer_test.md"},
            timeout=10)
        print("    [chat POST]", r.status_code, r.text[:60])

        def wait_ev(pred, timeout):
            end = time.time() + timeout
            while time.time() < end:
                try:
                    ev = q.q.get(timeout=1)
                except queue.Empty:
                    continue
                if ev.get("kind") == "assistant_delta":
                    seen_delta.append(1)
                if pred(ev):
                    return ev
            raise TimeoutError("等待事件超时")

        seen_delta = []
        end = time.time() + 600
        got_plan = got_step = got_write = False
        while time.time() < end:
            ev = wait_ev(lambda e: e["kind"] in ("plan_request", "step_request",
                                                 "permission_request", "task_end", "fatal"),
                         max(1, int(end - time.time())))
            k = ev["kind"]
            if k == "task_end":
                break
            if k == "fatal":
                raise RuntimeError("引擎fatal: " + str(ev)[:200])
            if k == "plan_request" and not got_plan:
                got_plan = True
                print("    [计划]", str(ev.get("plan", ""))[:200].replace("\n", " | "))
                requests.post(f"{BASE}/api/approve", json={
                    "session": SESSION, "id": ev["id"], "approve": True,
                    "text": "只统计根目录, 不要递归子目录"}, timeout=10)
            elif k == "step_request":
                got_step = True  # 每个步骤都要批准(修改指令只在首次带)
                requests.post(f"{BASE}/api/approve", json={
                    "session": SESSION, "id": ev["id"], "approve": True,
                    "text": ("写入的文件末尾加一行: --已转向"
                             if not getattr(e2e, "_steered", False) else "")}, timeout=10)
                e2e._steered = True
            elif k == "permission_request":
                got_write = True
                requests.post(f"{BASE}/api/approve", json={
                    "session": SESSION, "id": ev["id"], "approve": True}, timeout=10)

        check("流式delta事件出现", len(seen_delta) >= 1)
        check("计划审批(带附加要求)发生", got_plan)
        check("分步审批(带修改指令)发生", got_step)
        r = requests.get(f"{BASE}/api/sessions/{SESSION}/history", timeout=10)
        blob = json.dumps(r.json().get("items", []), ensure_ascii=False)
        check("附加要求已注入历史", "计划附加要求" in blob and "只统计根目录" in blob)
        check("修改指令已注入历史", "计划修改指令" in blob and "--已转向" in blob)
        marker = target.exists() and "--已转向" in target.read_text(encoding="utf-8")
        print(f"    (转向标记写入文件: {marker}, 写审批: {got_write})")
        check("任务自然完成", True)
    finally:
        proc.terminate()
        time.sleep(1)
        try:
            print("    [server日志尾]", srv_log.read_text("utf-8",
                                                          errors="replace")[-500:].replace("\n", " | "))
        except Exception:
            pass
        srv_log.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        idx = core.TRASH_DIR / "index.jsonl"
        if idx.exists():
            lines = [ln for ln in idx.read_text("utf-8").splitlines()
                     if ln.strip() and "steer_test" not in ln]
            idx.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        for b in core.TRASH_DIR.glob("*steer_test*"):
            b.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    args = set(sys.argv[1:])
    if "--e2e-only" not in args:
        print("[单测] 写安全网 + 计划附加要求")
        unit_backup_undo()
        unit_plan_extra()
    if "--unit-only" not in args:
        print("[E2E] Web 流式 + 计划/分步带修改指令转向")
        e2e()
    print(f"[test_safety] 通过 {len(passed)} 项, 失败 {len(failed)} 项")
    sys.exit(0 if not failed else 1)
