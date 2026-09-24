#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/smoke_web.py — Web 前端端到端冒烟测试

覆盖: server启动 → SSE订阅 → 只读任务(query_batches) → 写任务触发审批卡片 →
浏览器侧 POST /api/approve → 引擎线程恢复 → 文件落盘校验。
用法: python scripts/smoke_web.py   (需要已配置 config.json 的 API Key)
"""

import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent.parent
PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"
SESSION = "smoke_web"

events: queue.Queue = queue.Queue()


def reader():
    with requests.get(f"{BASE}/api/events?session={SESSION}", stream=True, timeout=600) as r:
        for line in r.iter_lines(chunk_size=1, decode_unicode=True):
            if line and line.startswith("data: "):
                events.put(json.loads(line[6:]))


def wait_event(pred, timeout):
    end = time.time() + timeout
    seen = []
    while time.time() < end:
        try:
            ev = events.get(timeout=1)
        except queue.Empty:
            continue
        seen.append(ev)
        if pred(ev):
            return ev
    raise TimeoutError(f"等待事件超时{timeout}s; 最近收到: "
                       f"{[e.get('kind') for e in seen[-10:]]}")


def main():
    cmd = [sys.executable, "server.py", "--port", str(PORT), "--no-open",
           "--cwd", str(HERE)]
    if "--no-plugins" in sys.argv:
        cmd.append("--no-plugins")
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
        threading.Thread(target=reader, daemon=True).start()
        time.sleep(0.5)

        # 1) 只读任务: 引擎应调用 query_batches 并正常收尾
        requests.post(f"{BASE}/api/chat", json={
            "session": SESSION, "reflect": False,
            "message": "用 list_dir 看一下当前沙箱目录内容, 一句话总结"}, timeout=10)
        wait_event(lambda e: e["kind"] == "tool_call" and e["name"] == "list_dir", 60)
        print("PASS  只读任务调用了 list_dir")
        wait_event(lambda e: e["kind"] == "assistant_delta", 60)
        print("PASS  流式输出(assistant_delta)工作")
        ev = wait_event(lambda e: e["kind"] == "task_end", 180)
        assert ev.get("usage", {}).get("calls", 0) > 0, "task_end 应带用量统计"
        print(f"PASS  只读任务完成 (answer前60字: {ev.get('answer','')[:60]})")

        # 2) 写任务: 应触发审批卡片, 批准后文件落盘
        target = HERE / "web_smoke.txt"
        if target.exists():
            target.unlink()
        requests.post(f"{BASE}/api/chat", json={
            "session": SESSION, "reflect": False,
            "message": "用 write_file 把内容 'web-smoke-ok' 写到 web_smoke.txt"}, timeout=10)
        ev = wait_event(lambda e: e["kind"] == "permission_request", 60)
        assert ev["tool"] == "write_file"
        assert "web_smoke.txt" in ev["preview"], "审批预览应包含目标文件与diff"
        print("PASS  收到审批卡片(含diff预览)")
        r = requests.post(f"{BASE}/api/approve", json={
            "session": SESSION, "id": ev["id"], "approve": True, "always": False}, timeout=10)
        if not r.ok:
            print(f"DEBUG approve响应: {r.status_code} {r.text[:300]}")
        assert r.ok
        wait_event(lambda e: e["kind"] == "task_end", 180)
        ok = target.exists() and "web-smoke-ok" in target.read_text(encoding="utf-8")
        assert ok, "审批通过后文件应写入"
        print("PASS  审批后文件落盘")
        target.unlink()

        print("[smoke_web] 全部通过 ✓")
    finally:
        proc.terminate()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
