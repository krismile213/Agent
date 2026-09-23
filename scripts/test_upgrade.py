#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/test_upgrade.py — 多轮对话增强的测试

单元部分(不需要API Key):
  1) save_memory 工具 + MEMORY.md 注入系统提示
  2) 悬空 tool_calls 修复(被中断会话可安全 --resume)

E2E部分(需要API Key, 启动真实server):
  3) SSE 断线重放: Last-Event-ID 重连后补发错过的事件
  4) 任务停止: /api/stop 后引擎优雅退出(steering基础)
用法: python scripts/test_upgrade.py [--e2e-only|--unit-only]
"""

import json
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

PORT = 8801
BASE = f"http://127.0.0.1:{PORT}"
SESSION = "upgrade_e2e"

passed, failed = [], []


def check(name, cond):
    (passed if cond else failed).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")


# ---------------- 单元 ----------------

def unit_memory():
    import agentcore as core
    core.set_root(HERE)
    reg = core.build_registry()
    mem = HERE / "MEMORY.md"
    backup = mem.read_text("utf-8") if mem.exists() else None
    try:
        r = reg.execute("save_memory", {"content": "测试记忆条目-可删除"})
        check("save_memory 写入", "已记入" in r and mem.exists()
              and "测试记忆条目-可删除" in mem.read_text("utf-8"))
        prompt = core.build_system_prompt()
        check("MEMORY.md 注入系统提示", "跨会话记忆" in prompt and "测试记忆条目-可删除" in prompt)
    finally:
        if backup is None:
            mem.unlink(missing_ok=True)
        else:
            mem.write_text(backup, encoding="utf-8")


def unit_dangling():
    import agentcore as core
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "s.jsonl"
        lines = [
            {"kind": "message", "msg": {"role": "user", "content": "t"}},
            {"kind": "message", "msg": {"role": "assistant", "content": "",
             "tool_calls": [
                 {"id": "c1", "function": {"name": "list_dir", "arguments": "{}"}},
                 {"id": "c2", "function": {"name": "grep", "arguments": "{}"}}]}},
            {"kind": "message", "msg": {"role": "tool", "tool_call_id": "c1",
                                        "content": "ok"}},
            {"kind": "message", "msg": {"role": "user", "content": "next"}},
        ]
        f.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines),
                     encoding="utf-8")
        msgs = core.Transcript.load_messages(f)
        synth = [m for m in msgs if m.get("role") == "tool"
                 and m.get("tool_call_id") == "c2" and "中断" in m.get("content", "")]
        check("悬空tool_calls补合成结果", len(synth) == 1)
        idx_asst = next(i for i, m in enumerate(msgs)
                        if m.get("role") == "assistant" and m.get("tool_calls"))
        idx_next = next(i for i, m in enumerate(msgs)
                        if m.get("role") == "user" and m["content"] == "next")
        idx_synth = next(i for i, m in enumerate(msgs)
                         if m.get("role") == "tool" and m.get("tool_call_id") == "c2")
        check("合成结果位置正确(调用之后/新消息之前)",
              idx_asst < idx_synth < idx_next)


# ---------------- E2E ----------------

class SSE:
    def __init__(self, session, last_id=None):
        headers = {"Last-Event-ID": str(last_id)} if last_id is not None else {}
        self.resp = requests.get(f"{BASE}/api/events?session={session}",
                                 headers=headers, stream=True, timeout=600)
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        seq = 0
        try:
            for line in self.resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if line.startswith("id: "):
                    try:
                        seq = int(line[4:])
                    except ValueError:
                        pass
                elif line.startswith("data: "):
                    try:
                        ev = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    self.q.put((seq, ev))
        except Exception:
            pass  # 连接被close()后的正常退出

    def close(self):
        self.resp.close()


def wait_ev(q, pred, timeout):
    end = time.time() + timeout
    seen = []
    while time.time() < end:
        try:
            item = q.get(timeout=1)
        except queue.Empty:
            continue
        seen.append(item)
        if pred(item):
            return item
    raise TimeoutError(f"等待事件超时; 最近: {[(s, e.get('kind')) for s, e in seen[-8:]]}")


def e2e():
    cmd = [sys.executable, "server.py", "--port", str(PORT), "--no-open", "--cwd", str(HERE)]
    proc = subprocess.Popen(cmd, cwd=str(HERE),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                requests.get(f"{BASE}/api/sessions", timeout=2)
                break
            except Exception:
                time.sleep(0.3)
        else:
            raise RuntimeError("server 启动失败")

        # 3) SSE 断线重放
        s1 = SSE(SESSION)
        time.sleep(0.5)
        requests.post(f"{BASE}/api/chat", json={
            "session": SESSION, "reflect": False,
            "message": "用 grep 在沙箱里搜 'def main' 出现在哪些py文件, 一句话总结"}, timeout=10)
        seq_tool, ev_tool = wait_ev(s1.q, lambda it: it[1].get("kind") == "tool_call", 60)
        print(f"  (首连接看到 tool_call seq={seq_tool})")
        s2 = SSE(SESSION, last_id=0)  # 模拟断线重连, 从头补发
        seq_ts, _ = wait_ev(s2.q, lambda it: it[1].get("kind") == "task_start", 30)
        seq_replay, ev_replay = wait_ev(
            s2.q, lambda it: it[1].get("kind") == "tool_call"
            and it[1].get("name") == ev_tool["name"] and it[0] == seq_tool, 30)
        check("SSE重放: 错过的事件按原seq补发", seq_replay == seq_tool and seq_ts is not None)
        wait_ev(s2.q, lambda it: it[1].get("kind") == "task_end", 180)
        check("SSE重放后实时流不受影响", True)
        s1.close()

        # 4) 任务停止
        requests.post(f"{BASE}/api/chat", json={
            "session": SESSION, "reflect": False,
            "message": "逐个用 read_file 读取沙箱里每个 .py 文件的开头50行, 全部读完再总结"}, timeout=10)
        time.sleep(0.3)
        r = requests.post(f"{BASE}/api/stop", json={"session": SESSION}, timeout=10)
        assert r.ok
        got_cancel = False
        try:
            wait_ev(s2.q, lambda it: it[1].get("kind") == "cancelled", 45)
            got_cancel = True
        except TimeoutError:
            pass
        seq_end, ev_end = wait_ev(s2.q, lambda it: it[1].get("kind") == "task_end", 60)
        stopped = got_cancel or "中断" in (ev_end.get("answer") or "")
        check(f"任务停止生效(cancelled事件={'有' if got_cancel else '无'}, "
              f"answer含中断={'是' if '中断' in ev_end.get('answer','') else '否'})", stopped)
        # 停止后能立即发新任务(steering)
        r2 = requests.post(f"{BASE}/api/chat", json={
            "session": SESSION, "reflect": False, "message": "只回复: 收到"}, timeout=10)
        check("停止后可继续新任务(转向)", r2.status_code != 409)
        wait_ev(s2.q, lambda it: it[1].get("kind") == "task_end", 60)
        s2.close()
    finally:
        proc.terminate()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    args = set(sys.argv[1:])
    if "--e2e-only" not in args:
        print("[单元] 记忆与恢复加固")
        unit_memory()
        unit_dangling()
    if "--unit-only" not in args:
        print("[E2E] SSE重放与任务停止")
        e2e()
    print(f"[test_upgrade] 通过 {len(passed)} 项, 失败 {len(failed)} 项")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
