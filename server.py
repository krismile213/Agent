#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py — Web 前端适配器 (引擎见 agentcore.py)

架构: agentcore 引擎跑在工作线程, 通过线程安全的事件回调把进度推给
SSE 订阅者; 写级工具触发审批时, 引擎线程阻塞在 threading.Event 上,
等浏览器 POST /api/approve 后继续 —— 这就是网页版"人工干预"闭环。

运行:
  python server.py                 # 默认 http://127.0.0.1:8765 并自动开浏览器
  python server.py --port 9000 --cwd "任何目录"
  python server.py --no-plugins --no-open
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import uuid
import webbrowser
from collections import deque
from datetime import datetime

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

import agentcore as core

LOOP: asyncio.AbstractEventLoop | None = None
CFG: dict = {}
SESS: dict[str, "SessionState"] = {}
PENDING: dict[str, "PendingReq"] = {}
EVLOG_MAX = 500  # 每会话保留的最近事件数(SSE断线重放窗口)


class PendingReq:
    """一次待审批的写操作: 引擎线程 wait(), 审批端点 set()."""

    def __init__(self, tool, kwargs: dict):
        self.id = uuid.uuid4().hex[:12]
        self.tool_name = tool.name
        self.preview = tool.preview(kwargs)
        self.session = ""
        self.ev = threading.Event()
        self.approved = False
        self.always = False


class WebPolicy:
    """网页权限策略: 只读放行; 写操作推审批卡片并阻塞等待浏览器响应."""

    def __init__(self, sess: "SessionState"):
        self.sess = sess

    def allow(self, tool, kwargs: dict) -> bool:
        if tool.level == "read" or tool.name in self.sess.always:
            return True
        req = PendingReq(tool, kwargs)
        req.session = self.sess.name
        PENDING[req.id] = req
        self.sess.emit_threadsafe("permission_request",
                                  {"id": req.id, "tool": tool.name,
                                   "preview": req.preview})
        req.ev.wait()  # 阻塞引擎线程直到浏览器审批(本地单用户, 不设超时)
        if req.always:
            self.sess.always.add(tool.name)
        return req.approved


class SessionState:
    def __init__(self, name: str):
        self.name = name
        self.transcript = core.Transcript(core.HERE / "sessions" / f"{name}.jsonl")
        self.history = core.Transcript.load_messages(self.transcript.path)
        self.history.insert(0, {"role": "system",
                                "content": core.build_system_prompt()})
        self.client = core.LLMClient(CFG)
        self.always: set[str] = set()
        self.running = False
        self.subs: set[asyncio.Queue] = set()
        self.seq = 0                       # 事件流水号(SSE断线重放用)
        self.evlog: deque = deque(maxlen=EVLOG_MAX)
        self.cancel = threading.Event()    # 任务停止标志

    def dispatch(self, ev: dict):
        """在事件循环线程调用: 编号→留档→扇出给所有SSE订阅者."""
        self.seq += 1
        item = (self.seq, ev)
        self.evlog.append(item)
        for q in list(self.subs):
            try:
                q.put_nowait(item)
            except Exception:
                pass

    def emit_threadsafe(self, kind: str, data: dict):
        """引擎工作线程调用: 转投到事件循环线程."""
        LOOP.call_soon_threadsafe(self.dispatch, {"kind": kind, **data})

    def send(self, kind: str, data: dict):
        self.dispatch({"kind": kind, **data})


app = FastAPI(title="mini_agent web")


@app.on_event("startup")
async def _startup():
    global LOOP
    LOOP = asyncio.get_running_loop()


class ChatBody(BaseModel):
    session: str
    message: str
    reflect: bool = False


class ApproveBody(BaseModel):
    session: str
    id: str
    approve: bool
    always: bool = False


def get_sess(name: str) -> SessionState:
    name = "".join(c for c in name.strip() if c.isalnum() or c in "-_") or "web"
    if name not in SESS:
        SESS[name] = SessionState(name)
    return SESS[name]


@app.get("/")
async def index():
    return FileResponse(core.HERE / "static" / "index.html")


@app.get("/api/sessions")
async def list_sessions():
    out = []
    d = core.HERE / "sessions"
    if d.is_dir():
        for f in sorted(d.glob("*.jsonl"),
                        key=lambda p: p.stat().st_mtime, reverse=True):
            st = f.stat()
            out.append({"name": f.stem,
                        "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%m-%d %H:%M"),
                        "size": st.st_size})
    return out


@app.get("/api/sessions/{name}/history")
async def session_history(name: str):
    s = get_sess(name)
    items = []
    for m in s.history[1:]:
        r = m.get("role")
        if r == "user":
            c = str(m.get("content", ""))
            if c.startswith("[会话压缩]"):
                items.append({"kind": "assistant", "text": "(上下文已压缩) " + c[:150]})
            elif c.startswith("[反思结论]"):
                items.append({"kind": "reflect",
                              "critique": c[len("[反思结论]"):][:500]})
            else:
                items.append({"kind": "user", "text": c})
        elif r == "assistant":
            if (m.get("content") or "").strip():
                items.append({"kind": "assistant", "text": m["content"]})
            for c in m.get("tool_calls") or []:
                fn = c.get("function") or {}
                items.append({"kind": "tool_call", "name": fn.get("name", "?"),
                              "args": (fn.get("arguments") or "")[:200]})
        elif r == "tool":
            items.append({"kind": "tool_result", "name": "",
                          "result": str(m.get("content"))[:1500]})
    return {"items": items, "usage": s.client.usage, "running": s.running}


class StopBody(BaseModel):
    session: str


@app.post("/api/chat")
async def chat(body: ChatBody):
    s = get_sess(body.session)
    if s.running:
        return JSONResponse({"error": "该会话有任务正在运行, 请等待完成或点停止"}, 409)
    if not body.message.strip():
        return JSONResponse({"error": "消息为空"}, 400)
    s.cancel.clear()
    s.running = True
    s.send("running", {"value": True})

    def work():
        try:
            start = len(s.history)
            answer = core.run_task(s.client, REG, WebPolicy(s), s.transcript,
                                   s.history, body.message, CFG,
                                   emit=s.emit_threadsafe, cancel=s.cancel)
            if body.reflect and answer and not answer.startswith(("[", "(")):
                core.reflect_and_fix(s.client, REG, WebPolicy(s), s.transcript,
                                     s.history, start, answer, CFG,
                                     emit=s.emit_threadsafe, cancel=s.cancel)
        except Exception as e:  # 引擎级异常兜底, 释放会话
            s.emit_threadsafe("fatal", {"message": f"{type(e).__name__}: {e}"})
        finally:
            s.running = False
            s.emit_threadsafe("running", {"value": False,
                                          "usage": dict(s.client.usage)})

    threading.Thread(target=work, daemon=True, name=f"agent-{body.session}").start()
    return {"ok": True}


@app.post("/api/stop")
async def stop(body: StopBody):
    """停止正在运行的任务: 引擎在轮/工具边界优雅退出;
    若有未决审批, 一并按拒绝解决(解除引擎线程阻塞)."""
    s = SESS.get(body.session)
    stopping = False
    if s and s.running:
        stopping = True
        s.cancel.set()
        for rid in [r for r, v in PENDING.items() if v.session == body.session]:
            req = PENDING.pop(rid)
            req.approved = False
            req.ev.set()
            s.send("permission_resolved", {"id": rid, "approved": False})
    return {"ok": True, "stopping": stopping}


@app.post("/api/approve")
async def approve(body: ApproveBody):
    req = PENDING.pop(body.id, None)
    if req is None:
        return JSONResponse({"error": "审批请求不存在或已处理"}, 404)
    req.approved = body.approve
    req.always = body.always
    req.ev.set()  # 唤醒阻塞中的引擎线程
    s = SESS.get(body.session)
    if s:
        s.send("permission_resolved", {"id": body.id, "approved": body.approve})
    return {"ok": True}


@app.get("/api/events")
async def events(request: Request, session: str):
    s = get_sess(session)
    try:
        last_id = int(request.headers.get("last-event-id") or 0)
    except ValueError:
        last_id = 0
    q: asyncio.Queue = asyncio.Queue()
    s.subs.add(q)  # 先订阅再快照, 重放与实时之间不丢不重(按seq去重)

    def frame(seq: int, ev: dict) -> str:
        return f"id: {seq}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"

    async def gen():
        try:
            hello = {"kind": "hello", "tools": REG.names(),
                     "model": CFG["model"], "root": str(core.ROOT)}
            yield f"data: {json.dumps(hello, ensure_ascii=False)}\n\n"
            last_sent = last_id
            for seq, ev in list(s.evlog):  # 断线重放窗口
                if seq > last_sent:
                    yield frame(seq, ev)
                    last_sent = seq
            while True:
                seq, ev = await q.get()
                if seq <= last_sent:
                    continue
                last_sent = seq
                yield frame(seq, ev)
        finally:
            s.subs.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


REG = core.build_registry()


def main():
    global CFG
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="mini_agent Web前端(FastAPI+SSE+网页审批)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--cwd", default=os.getcwd(), help="工作沙箱目录(默认当前目录)")
    ap.add_argument("--no-plugins", action="store_true", help="禁用全部插件")
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    core.set_root(args.cwd)
    CFG = core.load_config()
    if not args.no_plugins:
        core.load_plugins(REG, CFG)
        try:
            import mcp_bridge
            mcp_bridge.register_mcp_tools(REG, CFG)
        except Exception as e:
            core.log(f"[mcp] 外部工具接入失败(忽略): {type(e).__name__}: {e}")

    url = f"http://127.0.0.1:{args.port}"
    print(f"[web] model={CFG['model']} 沙箱={core.ROOT} 工具={len(REG.names())}个")
    print(f"[web] serving on {url}  (Ctrl+C 停止)")
    if not args.no_open:
        threading.Timer(1.2, webbrowser.open, [url]).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
