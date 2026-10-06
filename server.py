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
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
import uuid
import webbrowser
from collections import deque
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse,
                               StreamingResponse)
from pydantic import BaseModel

import agentcore as core

LOOP: asyncio.AbstractEventLoop | None = None
CFG: dict = {}
SESS: dict[str, "SessionState"] = {}
PENDING: dict[str, "PendingReq"] = {}
EVLOG_MAX = 500  # 每会话保留的最近事件数(SSE断线重放窗口)
MAX_UPLOAD = 50 * 1024 * 1024  # 上传大小上限50MB

# ---------- 登录鉴权(P1): HMAC签名Cookie + 可选静态Bearer + 用户@会话命名空间 ----------
# config.json → "auth": {"users": {"名": "密码"}, "users_sha256": {"名": "<hex>"},
#                        "tokens": ["脚本Bearer"], "session_hours": 72, "secret": ""}
# users/tokens 全空 = 关闭鉴权(本机模式, 行为与旧版完全一致)。

AUTH: dict = {"on": False, "users": {}, "sha": {}, "tokens": set(),
              "hours": 72, "secret": ""}
COOKIE = "agent_token"
SEP = "@"  # 会话命名空间分隔符(用户@会话): @ 在 Windows 文件名合法, 且清洗后的会话名必不含@


def setup_auth(cfg: dict) -> dict:
    """从配置装载鉴权; 任一凭据源非空即启用。secret 不配则随机生成(重启后登录态失效)."""
    a = cfg.get("auth") or {}
    AUTH["users"] = {str(k): str(v) for k, v in (a.get("users") or {}).items()
                     if k and v}
    AUTH["sha"] = {str(k): str(v).lower() for k, v in
                   (a.get("users_sha256") or {}).items() if k and v}
    AUTH["tokens"] = {str(t) for t in (a.get("tokens") or []) if t}
    AUTH["hours"] = max(1, int(a.get("session_hours") or 72))
    AUTH["secret"] = str(a.get("secret") or "")
    AUTH["on"] = bool(AUTH["users"] or AUTH["sha"] or AUTH["tokens"])
    if AUTH["on"] and not AUTH["secret"]:
        AUTH["secret"] = secrets.token_hex(32)
    return AUTH


def _sign(b: bytes) -> str:
    return hmac.new(AUTH["secret"].encode(), b, hashlib.sha256).hexdigest()


def make_token(user: str) -> str:
    payload = json.dumps({"u": user, "exp": int(time.time()) + AUTH["hours"] * 3600},
                         separators=(",", ":")).encode()
    b = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return b + "." + _sign(b.encode())


def check_token(tok: str) -> "str | None":
    """校验签名token(防篡改+防过期), 返回用户名或None."""
    try:
        b, sig = tok.rsplit(".", 1)
        if not hmac.compare_digest(_sign(b.encode()), sig):
            return None
        d = json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)))
        return str(d["u"]) if int(d.get("exp") or 0) >= time.time() else None
    except Exception:
        return None


def verify_login(user: str, pwd: str) -> bool:
    if user in AUTH["users"]:
        return hmac.compare_digest(AUTH["users"][user], pwd or "")
    if user in AUTH["sha"]:
        return hmac.compare_digest(
            AUTH["sha"][user],
            hashlib.sha256((pwd or "").encode()).hexdigest())
    return False  # 不存在的用户同样走 compare_digest 时间近似


def current_user(request: "Request") -> "str | None":
    """从 Cookie 或 Authorization: Bearer 解出登录身份."""
    if not AUTH["on"]:
        return None
    h = request.headers.get("authorization") or ""
    if h.startswith("Bearer "):
        t = h[7:].strip()
        if t in AUTH["tokens"]:
            return "token"
        u = check_token(t)
        if u:
            return u
    ck = request.cookies.get(COOKIE)
    return check_token(ck) if ck else None


class PendingReq:
    """一次待审批的操作(写工具或执行计划): 引擎线程 wait(), 审批端点 set()."""

    def __init__(self, name: str, preview: str):
        self.id = uuid.uuid4().hex[:12]
        self.tool_name = name
        self.preview = preview
        self.session = ""
        self.ev = threading.Event()
        self.approved = False
        self.always = False
        self.steer_text = ""  # 批准时附带的修改指令/附加要求


class WebPolicy:
    """网页权限策略: 只读放行; 写操作推审批卡片并阻塞等待浏览器响应;
    external级(对外发送)双重确认 —— 第一张卡片确认草稿, 第二张确认实际发送,
    且不支持"总允许"(对外发送不能有一次放行终身的口子)."""

    def __init__(self, sess: "SessionState"):
        self.sess = sess

    def _ask(self, tool_name: str, preview: str, allow_always: bool) -> "PendingReq":
        """推一张审批卡片并阻塞等待浏览器响应(返回未pop的req对象)."""
        import storage
        req = PendingReq(tool_name, preview)
        req.session = self.sess.name
        PENDING[req.id] = req
        self.sess.emit_threadsafe("permission_request",
                                  {"id": req.id, "tool": tool_name,
                                   "preview": preview})
        req.ev.wait()  # 阻塞引擎线程直到浏览器审批(本地单用户, 不设超时)
        storage.approval(self.sess.name, "tool", tool_name,
                         "allowed" if req.approved else "denied")
        if allow_always and req.always:
            self.sess.always.add(tool_name)
        return req

    def allow(self, tool, kwargs: dict) -> bool:
        if tool.level == "read" or tool.name in self.sess.always:
            return True
        if tool.level == "external":
            req1 = self._ask(tool.name, tool.preview(kwargs), allow_always=False)
            if not req1.approved:
                return False
            req2 = self._ask(tool.name,
                             "【第二次确认 · 对外发送】草稿已确认, 即将实际发出"
                             "(不可撤回):\n" + tool.preview(kwargs)[:1200],
                             allow_always=False)
            req2.always = False  # external 不吃"总允许"
            return req2.approved
        req = self._ask(tool.name, tool.preview(kwargs), allow_always=True)
        return req.approved


class WebPlanConfirmer:
    """计划模式的网页审批: 复用审批收件箱机制, 引擎线程等待浏览器批准计划."""

    def __init__(self, sess: "SessionState"):
        self.sess = sess

    def __call__(self, plan: str):
        import storage
        req = PendingReq("执行计划", plan[:4000])
        req.session = self.sess.name
        PENDING[req.id] = req
        self.sess.emit_threadsafe("plan_request",
                                  {"id": req.id, "plan": plan[:4000]})
        req.ev.wait()
        storage.approval(self.sess.name, "plan", "执行计划",
                         "allowed" if req.approved else "denied",
                         req.steer_text if req.approved else "")
        if req.approved and req.steer_text:
            return True, req.steer_text  # 批准+附加要求(引擎注入历史)
        return req.approved


class WebStepConfirmer:
    """分步执行的网页审批: 每步完成后 允许=继续 / 拒绝=停止.
    (修改指令走"拒绝→发新消息转向", 文本注入留待后续)"""

    def __init__(self, sess: "SessionState"):
        self.sess = sess

    def __call__(self, i: int, n: int, step_text: str, last_answer: str):
        req = PendingReq(f"计划步骤 {i}/{n}", f"已完成: {step_text[:500]}\n\n"
                                              f"允许=继续第{i + 1}步 / 拒绝=停止(可发新消息转向)")
        req.session = self.sess.name
        PENDING[req.id] = req
        self.sess.emit_threadsafe("step_request",
                                  {"id": req.id, "step": i, "total": n,
                                   "text": step_text[:500]})
        req.ev.wait()
        import storage
        storage.approval(self.sess.name, "step", f"步骤{i}/{n}",
                         "continue" if req.approved else "stop", req.steer_text)
        if req.approved:
            return "continue", req.steer_text  # 批准+可选修改指令(注入后续步骤)
        return "stop", ""


class SessionState:
    def __init__(self, name: str):
        self.name = name
        self.transcript = core.Transcript(name)
        self.history = core.Transcript.load_messages(name)
        self.history.insert(0, {"role": "system",
                                "content": core.build_system_prompt()})
        self.client = core.LLMClient(CFG)
        self.always: set[str] = set()
        self.running = False
        self.subs: set[asyncio.Queue] = set()
        self.seq = 0                       # 事件流水号(SSE断线重放用)
        self.evlog: deque = deque(maxlen=EVLOG_MAX)
        self.cancel = threading.Event()    # 任务停止标志
        self.inbox = core.InstructionInbox()  # 任务中途追加指令(轮/工具边界注入)

    def dispatch(self, ev: dict):
        """在事件循环线程调用: 编号→留档→扇出给所有SSE订阅者.
        流式delta不留档(重放时由完整assistant事件代替, 防止重放日志爆炸)."""
        self.seq += 1
        item = (self.seq, ev)
        if ev.get("kind") != "assistant_delta":
            self.evlog.append(item)
            log_kind = ev.get("kind")
            if log_kind:
                core.log(f"[dispatch] {log_kind} seq={self.seq} subs={len(self.subs)}")
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


# ---------- 鉴权中间件(纯ASGI): 除开放清单外全部要求登录; / 未登录跳登录页 ----------
# ⚠️ 不用 @app.middleware("http")(BaseHTTPMiddleware): 它对 scope/state 的包装
#    会导致 POST/GET 之间 request.state.user 丢失(实测), 且与 SSE 流式相性差。
OPEN_PATHS = {"/api/login", "/api/health", "/login"}


class AuthGate:
    """纯 ASGI 鉴权中间件: 身份写入 scope['state']['user'], 端点经 request.state 读."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            user = None
            if AUTH["on"] and scope["method"] != "OPTIONS" \
                    and scope["path"] not in OPEN_PATHS:
                user = current_user(Request(scope))
                if not user:
                    if scope["path"] == "/":
                        resp = RedirectResponse("/login", status_code=307)
                    else:
                        resp = JSONResponse({"error": "未登录或登录已过期"}, 401)
                    await resp(scope, receive, send)
                    return
            scope.setdefault("state", {})["user"] = user
        await self.app(scope, receive, send)


app.add_middleware(AuthGate)


class LoginBody(BaseModel):
    username: str
    password: str = ""


@app.post("/api/login")
async def login(body: LoginBody):
    if not AUTH["on"]:  # 未启用鉴权: 空实现保兼容
        return {"ok": True, "user": None}
    u = body.username.strip()
    if not verify_login(u, body.password):
        return JSONResponse({"error": "用户名或密码错误"}, status_code=401)
    resp = JSONResponse({"ok": True, "user": u})
    resp.set_cookie(COOKIE, make_token(u), max_age=AUTH["hours"] * 3600,
                    httponly=True, samesite="lax")
    return resp


@app.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/api/me")
async def me(request: Request):
    return {"user": getattr(request.state, "user", None), "auth": AUTH["on"]}


@app.get("/login")
async def login_page():
    return FileResponse(core.HERE / "static" / "login.html")


class ChatBody(BaseModel):
    session: str
    message: str
    reflect: bool = False
    plan: bool = False
    stepwise: bool = False


class ApproveBody(BaseModel):
    session: str
    id: str
    approve: bool
    always: bool = False
    text: str = ""  # 批准时附带的修改指令/附加要求(计划/步骤用)


def get_sess(name: str) -> SessionState:
    """入参应为已清洗并绑定用户的会话id(一律经 _scope 产出)."""
    if name not in SESS:
        SESS[name] = SessionState(name)
    return SESS[name]


def _scope(request: "Request", name: str) -> str:
    """把请求里的会话名绑定到当前登录用户的命名空间 —— 多用户隔离的唯一收口."""
    name = "".join(c for c in (name or "").strip() if c.isalnum() or c in "-_") or "web"
    if AUTH["on"]:
        u = getattr(request.state, "user", None)
        if u:
            return f"{u}{SEP}{name}"
    return name


def _sess_file(sid: str) -> Path:
    return core.HERE / "sessions" / f"{sid}.jsonl"


@app.get("/")
async def index():
    return FileResponse(core.HERE / "static" / "index.html")


@app.get("/api/sessions")
async def list_sessions(request: Request):
    """会话列表 = SQLite主存储 ∪ 旧JSONL(按最近活动排序).
    顺带修复: 旧版只扫 jsonl, SQLite 会话重启后从侧栏消失.
    鉴权开启时按 用户@会话 命名空间过滤; brief_* 为定时简报机生会话, 不入侧栏."""
    out: dict = {}
    prefix = f"{request.state.user}{SEP}" if AUTH["on"] else ""

    def visible(sid: str) -> bool:
        if sid.startswith("brief_"):
            return False
        return sid.startswith(prefix) if AUTH["on"] else SEP not in sid

    try:
        import storage
        for r in storage.get_db().execute(
                "SELECT session s, MAX(ts) m, COUNT(*) n FROM messages "
                "GROUP BY session"):
            sid = r["s"] or ""
            if not visible(sid):
                continue
            try:
                mt = datetime.fromisoformat(str(r["m"])).timestamp()
            except Exception:
                mt = 0.0
            out[sid] = {"_mt": mt, "size": int(r["n"]),
                        "name": sid.split(SEP, 1)[1] if SEP in sid else sid,
                        "mtime": datetime.fromtimestamp(mt).strftime("%m-%d %H:%M")
                        if mt else ""}
    except Exception:
        pass
    d = core.HERE / "sessions"
    if d.is_dir():
        for f in d.glob("*.jsonl"):
            sid = f.stem
            if not visible(sid):
                continue
            st = f.stat()
            if sid not in out or out[sid]["_mt"] < st.st_mtime:
                out[sid] = {"_mt": float(st.st_mtime), "size": st.st_size,
                            "name": sid.split(SEP, 1)[1] if SEP in sid else sid,
                            "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%m-%d %H:%M")}
    return [{k: v for k, v in o.items() if k != "_mt"}
            for o in sorted(out.values(), key=lambda x: x["_mt"], reverse=True)]


@app.get("/api/sessions/{name}/history")
async def session_history(request: Request, name: str):
    s = get_sess(_scope(request, name))
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
async def chat(body: ChatBody, request: Request):
    s = get_sess(_scope(request, body.session))
    if s.running:
        return JSONResponse({"error": "该会话有任务正在运行: 可直接输入追加指令(不打断执行), 或点停止后再发新任务"}, 409)
    if not body.message.strip():
        return JSONResponse({"error": "消息为空"}, 400)
    if TASK_SEM is not None and not TASK_SEM.acquire(blocking=False):
        return JSONResponse({"error": "服务并发已满(任务排队中), 请稍后再试"}, 503)
    s.cancel.clear()
    s.running = True
    s.send("running", {"value": True})
    core.log(f"[work] session={s.name} plan={body.plan} stepwise={body.stepwise} "
             f"history={len(s.history)}")

    def work():
        try:
            start = len(s.history)
            if body.plan:
                answer = core.plan_and_run(s.client, REG, WebPolicy(s),
                                           WebPlanConfirmer(s), s.transcript,
                                           s.history, body.message, CFG,
                                           emit=s.emit_threadsafe, cancel=s.cancel,
                                           stepwise=body.stepwise,
                                           step_confirm=WebStepConfirmer(s)
                                           if body.stepwise else None,
                                           inject=s.inbox)
            else:
                answer = core.run_task(s.client, REG, WebPolicy(s), s.transcript,
                                       s.history, body.message, CFG,
                                       emit=s.emit_threadsafe, cancel=s.cancel,
                                       inject=s.inbox)
            if body.reflect and answer and not answer.startswith(("[", "(")):
                core.reflect_and_fix(s.client, REG, WebPolicy(s), s.transcript,
                                     s.history, start, answer, CFG,
                                     emit=s.emit_threadsafe, cancel=s.cancel,
                                     inject=s.inbox)
        except Exception as e:  # 引擎级异常兜底, 释放会话
            s.emit_threadsafe("fatal", {"message": f"{type(e).__name__}: {e}"})
        finally:
            s.running = False
            s.emit_threadsafe("running", {"value": False,
                                          "usage": dict(s.client.usage)})
            if TASK_SEM is not None:
                TASK_SEM.release()

    threading.Thread(target=work, daemon=True, name=f"agent-{body.session}").start()
    return {"ok": True}


@app.post("/api/stop")
async def stop(body: StopBody, request: Request):
    """停止正在运行的任务: 引擎在轮/工具边界优雅退出;
    若有未决审批, 一并按拒绝解决(解除引擎线程阻塞)."""
    sid = _scope(request, body.session)
    s = SESS.get(sid)
    stopping = False
    if s and s.running:
        stopping = True
        s.cancel.set()
        for rid in [r for r, v in PENDING.items() if v.session == sid]:
            req = PENDING.pop(rid)
            req.approved = False
            req.ev.set()
            s.send("permission_resolved", {"id": rid, "approved": False})
    return {"ok": True, "stopping": stopping}


@app.post("/api/approve")
async def approve(body: ApproveBody, request: Request):
    sid = _scope(request, body.session)
    req = PENDING.get(body.id)
    if req is None or req.session != sid:  # 不存在/不属于当前用户(含跨用户)一律404
        return JSONResponse({"error": "审批请求不存在或已处理"}, 404)
    PENDING.pop(body.id)
    req.approved = body.approve
    req.always = body.always
    req.steer_text = (body.text or "").strip()
    req.ev.set()  # 唤醒阻塞中的引擎线程
    s = SESS.get(sid)
    if s:
        s.send("permission_resolved", {"id": body.id, "approved": body.approve})
    return {"ok": True}


class BatchApproveBody(BaseModel):
    session: str
    ids: list[str]
    approve: bool
    text: str = ""


@app.post("/api/approve_batch")
async def approve_batch(body: BatchApproveBody, request: Request):
    """批量审批: 一次解决同会话的多个待决请求(逐个唤醒阻塞的引擎线程).
    missing 返回已失效的id(引擎侧已解决, 如点过停止)."""
    sid = _scope(request, body.session)
    s = SESS.get(sid)
    resolved, missing = [], []
    for rid in body.ids:
        req = PENDING.pop(rid, None)
        if req is None or req.session != sid:
            missing.append(rid)
            continue
        req.approved = body.approve
        req.always = False  # 批量操作不授予"总允许"
        req.steer_text = (body.text or "").strip()
        req.ev.set()
        resolved.append(rid)
        if s:
            s.send("permission_resolved", {"id": rid, "approved": body.approve})
    return {"ok": True, "resolved": resolved, "missing": missing}


@app.get("/api/pending")
async def list_pending(request: Request, session: str):
    """当前会话未决审批清单 —— 前端刷新/换会话后据此重建审批卡片."""
    sid = _scope(request, session)
    return {"items": [{"id": r.id, "tool": r.tool_name, "preview": r.preview}
                      for r in PENDING.values() if r.session == sid]}


class InjectBody(BaseModel):
    session: str
    text: str


@app.post("/api/enqueue")
async def enqueue(body: InjectBody, request: Request):
    """任务运行中追加指令: 不打断执行, 引擎在轮/工具边界自动注入历史.
    (任务未运行时报错 —— 那种情况直接走 /api/chat 发新任务)"""
    sid = _scope(request, body.session)
    s = SESS.get(sid)
    text = (body.text or "").strip()
    if not text:
        return JSONResponse({"error": "指令为空"}, 400)
    if not s or not s.running:
        return JSONResponse({"error": "任务未在运行, 直接发送即可"}, 400)
    s.inbox.add(text)
    s.send("instruction_queued", {"text": text[:200]})
    return {"ok": True}


# ---------- 文件能力: 导入 / 浏览 / 下载 (全部锁在沙箱内) ----------

def _sensitive(p: Path) -> bool:
    low = p.name.lower()
    return (p.name == "config.json" or low.endswith(".env")
            or "credential" in low or "secret" in low or "password" in low)


@app.post("/api/files/upload")
async def upload_file(file: UploadFile = File(...)):
    """导入文件到沙箱 uploads/ (重名自动加序号), 之后在消息里用相对路径引用."""
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        return JSONResponse({"error": f"文件超上限({MAX_UPLOAD // 1024 // 1024}MB)"}, 413)
    fname = Path(file.filename or "upload.bin").name  # 去掉任何路径成分
    if not fname or fname.startswith("."):
        fname = "upload_" + datetime.now().strftime("%H%M%S") + ".bin"
    d = core.safe_path("uploads")
    d.mkdir(parents=True, exist_ok=True)
    target = d / fname
    i = 1
    while target.exists():
        target = d / f"{target.stem}({i}){target.suffix}"
        i += 1
    target.write_bytes(data)
    relp = target.relative_to(core.ROOT).as_posix()
    return {"ok": True, "path": relp, "size": len(data)}


@app.get("/api/files")
async def list_files(path: str = "."):
    try:
        p = core.safe_path(path)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, 400)
    if not p.is_dir():
        return JSONResponse({"error": f"不是目录: {path}"}, 400)
    items = []
    try:
        entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    except OSError as e:
        return JSONResponse({"error": str(e)}, 400)
    for ch in entries[:200]:
        if ch.name in core.EXCLUDE_DIRS:
            continue
        if ch.is_dir():
            items.append({"name": ch.name,
                          "path": ch.relative_to(core.ROOT).as_posix(),
                          "is_dir": True, "size": 0, "mtime": ""})
        else:
            if _sensitive(ch):
                continue
            st = ch.stat()
            items.append({"name": ch.name,
                          "path": ch.relative_to(core.ROOT).as_posix(),
                          "is_dir": False, "size": st.st_size,
                          "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%m-%d %H:%M")})
    return {"items": items}


@app.get("/api/files/download")
async def download_file(path: str):
    try:
        p = core.safe_path(path)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, 400)
    if not p.is_file():
        return JSONResponse({"error": f"文件不存在: {path}"}, 404)
    if _sensitive(p):
        return JSONResponse({"error": "该文件含敏感信息, 禁止下载"}, 403)
    return FileResponse(p, filename=p.name)


@app.get("/api/files/raw")
async def raw_file(path: str):
    """内联预览(无 Content-Disposition 附件头): 前端产物预览抽屉用. 同样的敏感/穿越拦截."""
    try:
        p = core.safe_path(path)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, 400)
    if not p.is_file():
        return JSONResponse({"error": f"文件不存在: {path}"}, 404)
    if _sensitive(p):
        return JSONResponse({"error": "该文件含敏感信息, 禁止预览"}, 403)
    return FileResponse(p)  # 不带 filename → 浏览器按扩展名内联渲染


class RenameBody(BaseModel):
    new: str


def _clean_session_name(name: str) -> str:
    return "".join(c for c in name.strip() if c.isalnum() or c in "-_")


@app.post("/api/sessions/{name}/rename")
async def rename_session(request: Request, name: str, body: RenameBody):
    sid = _scope(request, name)
    s = SESS.get(sid)
    if s and s.running:
        return JSONResponse({"error": "任务运行中, 暂不能重命名"}, 409)
    new = _clean_session_name(body.new)
    if not new:
        return JSONResponse({"error": "新名称为空"}, 400)
    src = _sess_file(sid)
    dst = _sess_file(_scope(request, new))
    if not src.exists():
        return JSONResponse({"error": f"会话不存在: {name}"}, 404)
    if new != name and dst.exists():
        return JSONResponse({"error": f"目标会话名已存在: {new}"}, 409)
    src.rename(dst)
    if sid in SESS and not SESS[sid].running:
        SESS.pop(sid, None)  # 已加载的状态按旧名失效, 下次打开按新名重建
    return {"ok": True, "name": new}


@app.delete("/api/sessions/{name}")
async def delete_session(request: Request, name: str):
    sid = _scope(request, name)
    s = SESS.get(sid)
    if s and s.running:
        return JSONResponse({"error": "任务运行中, 暂不能删除"}, 409)
    f = _sess_file(sid)
    if not f.exists():
        return JSONResponse({"error": f"会话不存在: {name}"}, 404)
    f.unlink()
    SESS.pop(sid, None)
    try:  # 遥测镜像同步清理(失败不影响主功能)
        import storage
        db = storage.get_db()
        db.execute("DELETE FROM messages WHERE session=?", (sid,))
        db.commit()
    except Exception:
        pass
    return {"ok": True}


@app.get("/api/health")
async def health():
    """健康检查(容器/负载均衡探针)."""
    return {"status": "ok", "model": CFG.get("model"),
            "sessions": len(SESS),
            "running": sum(1 for s in SESS.values() if s.running)}


@app.get("/api/metrics")
async def metrics():
    """Prometheus 文本格式指标(可直接被 scrape; 数值源=SQLite遥测)."""
    import storage
    m = storage.q_metrics()
    lines = ["# HELP agent_tasks_total 任务总数(按状态)",
             "# TYPE agent_tasks_total counter"]
    for st, n in m.get("tasks_by_status", {}).items():
        lines.append(f'agent_tasks_total{{status="{st}"}} {n}')
    lines += ["# HELP agent_llm_calls_total LLM调用总数",
              "# TYPE agent_llm_calls_total counter",
              f'agent_llm_calls_total {m.get("llm_calls", 0)}',
              "# HELP agent_llm_tokens_total token用量",
              "# TYPE agent_llm_tokens_total counter",
              f'agent_llm_tokens_total{{kind="prompt"}} {m.get("prompt_tokens", 0)}',
              f'agent_llm_tokens_total{{kind="completion"}} {m.get("completion_tokens", 0)}',
              "# HELP agent_llm_latency_p95_ms LLM调用P95时延",
              "# TYPE agent_llm_latency_p95_ms gauge",
              f'agent_llm_latency_p95_ms {m.get("llm_latency_p95_ms", 0)}']
    if "llm_latency_avg_ms" in m:
        lines.append(f'agent_llm_latency_avg_ms {m["llm_latency_avg_ms"]}')
    for tool, c in m.get("tool_calls", {}).items():
        lines.append(f'agent_tool_calls_total{{tool="{tool}",status="ok"}} {c["ok"]}')
        lines.append(f'agent_tool_calls_total{{tool="{tool}",status="fail"}} {c["fail"]}')
    for d, n in m.get("approvals", {}).items():
        lines.append(f'agent_approvals_total{{decision="{d}"}} {n}')
    return StreamingResponse(iter([ln + "\n" for ln in lines]),
                             media_type="text/plain; version=0.0.4")


@app.get("/api/events")
async def events(request: Request, session: str):
    s = get_sess(_scope(request, session))
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
TASK_SEM: threading.BoundedSemaphore | None = None  # 全局并发闸门(main里按配置创建)


def main():
    global CFG
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="mini_agent Web前端(FastAPI+SSE+网页审批)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1",
                    help="绑定地址(默认127.0.0.1仅本机; 容器内用0.0.0.0)")
    ap.add_argument("--cwd", default=os.getcwd(), help="工作沙箱目录(默认当前目录)")
    ap.add_argument("--no-plugins", action="store_true", help="禁用全部插件")
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    core.set_root(args.cwd)
    CFG = core.load_config()
    setup_auth(CFG)
    global TASK_SEM
    TASK_SEM = threading.BoundedSemaphore(
        int((CFG.get("limits") or {}).get("max_concurrent_tasks", 3)))
    if not args.no_plugins:
        core.load_plugins(REG, CFG)
        try:
            import mcp_bridge
            mcp_bridge.register_mcp_tools(REG, CFG)
        except Exception as e:
            core.log(f"[mcp] 外部工具接入失败(忽略): {type(e).__name__}: {e}")

    url = f"http://127.0.0.1:{args.port}"
    print(f"[web] model={CFG['model']} 沙箱={core.ROOT} 工具={len(REG.names())}个")
    print(f"[web] 鉴权: {'启用(用户数 %d, 未配secret则重启后需重新登录)' % len(AUTH['users']) if AUTH['on'] else '未启用(本机模式; 上网前在 config.json 配置 auth.users)'}")
    print(f"[web] serving on {url}  (Ctrl+C 停止)")
    if not args.no_open:
        threading.Timer(1.2, webbrowser.open, [url]).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
