#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
storage.py — 数据层与可观测性后端 (SQLite, WAL)

企业化三件套的存储基座:
  消息表 messages   —— Transcript 的 SQLite 后端(会话持久化, 兼容旧JSONL读取)
  任务表 tasks      —— 每个任务的起止/状态/token用量/父任务(嵌套: 子agent/计划步)
  调用表 llm_calls  —— 每次LLM调用的时延/状态/重试/流式 (trace核心)
  工具表 tool_calls —— 每次工具调用的时长/成败 (按工具的成本与故障归属)
  审计表 approvals  —— 每次写级审批的决定(含转向指令) —— 不可抵赖审计

设计原则: 遥测永不阻塞引擎 —— 所有写路径 try/except 兜底, 失败只打一次日志。
单文件 SQLite(WAL) 支撑单机多进程读; 换 PostgreSQL 时只需实现同接口。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import agentcore as core

HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE / "agent.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
  session TEXT, ts TEXT, kind TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, mode TEXT,
  parent_id INTEGER, started TEXT, ended TEXT, status TEXT,
  answer TEXT, turns INTEGER,
  prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER);
CREATE TABLE IF NOT EXISTS llm_calls(
  ts TEXT, task_id INTEGER, model TEXT, latency_ms INTEGER, status TEXT,
  retries INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER, stream INTEGER);
CREATE TABLE IF NOT EXISTS tool_calls(
  ts TEXT, task_id INTEGER, tool TEXT, level TEXT, ok INTEGER, duration_ms INTEGER);
CREATE TABLE IF NOT EXISTS approvals(
  ts TEXT, session TEXT, kind TEXT, name TEXT, decision TEXT, steer TEXT);
"""

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def get_db(cfg: dict | None = None) -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is not None:
            return _conn
        path = ((cfg or {}).get("storage") or {}).get("path") or str(DEFAULT_DB)
        c = sqlite3.connect(path, check_same_thread=False, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        c.executescript(_SCHEMA)
        c.commit()
        _conn = c
        return c


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


_warned = set()


def _safe(fn):
    """遥测兜底: 出错只告警一次, 永不上抛打断引擎。"""
    def wrap(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            key = fn.__name__
            if key not in _warned:
                _warned.add(key)
                core.log(f"[storage] {key} 记录失败(后续静默): {e}")
            return None
    return wrap


# ---------------- 会话消息(Transcript后端) ----------------

class SqliteTranscript:
    """与旧 Transcript 同接口: log()/load_messages()/close()。
    旧 JSONL 文件在无 SQLite 记录时作为回退读取(平滑迁移)。"""

    def __init__(self, session: str):
        self.session = session
        self._db = get_db()

    @_safe
    def log(self, kind: str, data: dict):
        self._db.execute("INSERT INTO messages VALUES (?,?,?,?)",
                         (self.session, _now(), kind,
                          json.dumps({"msg": data.get("msg")} if kind == "message"
                                     else data, ensure_ascii=False)))
        self._db.commit()

    def close(self):
        pass  # 连接进程级复用

    @staticmethod
    def load_messages(session: str) -> list:
        db = get_db()
        rows = db.execute(
            "SELECT data FROM messages WHERE session=? AND kind='message' "
            "ORDER BY rowid", (session,)).fetchall()
        msgs = []
        for r in rows:
            try:
                m = json.loads(r["data"])["msg"]
                if m.get("role") != "system":
                    msgs.append(m)
            except Exception:
                continue
        if msgs:
            return core.Transcript._patch_dangling(msgs)
        # 回退: 同名旧JSONL(迁移前的历史会话)
        legacy = HERE / "sessions" / f"{session}.jsonl"
        if legacy.exists():
            return core.Transcript._load_jsonl(legacy)
        return []

    @_safe
    def clear(self):
        self._db.execute("DELETE FROM messages WHERE session=?", (self.session,))
        self._db.commit()


# ---------------- 遥测: 任务/调用/工具/审批 ----------------

_ctx = threading.local()  # task_id 线程上下文(子agent线程各自独立)


def current_task_id():
    return getattr(_ctx, "task_id", None)


@_safe
def task_begin(session: str, mode: str = "chat", parent: int | None = None) -> int:
    db = get_db()
    cur = db.execute(
        "INSERT INTO tasks(session,mode,parent_id,started,status) "
        "VALUES (?,?,?,?, 'running')", (session, mode, parent, _now()))
    db.commit()
    _ctx.task_id = cur.lastrowid
    _ctx.task_session = session
    return cur.lastrowid


@_safe
def task_end(status: str, answer: str, turns: int, usage: dict):
    tid = current_task_id()
    if tid is None:
        return
    db = get_db()
    db.execute(
        "UPDATE tasks SET ended=?, status=?, answer=?, turns=?, "
        "prompt_tokens=?, completion_tokens=?, total_tokens=? WHERE id=?",
        (_now(), status, (answer or "")[:2000], turns,
         usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
         usage.get("total_tokens", 0), tid))
    db.commit()
    _ctx.task_id = None


@_safe
def llm_call(model: str, latency_ms: int, status: str, retries: int,
             usage: dict | None, stream: bool):
    db = get_db()
    db.execute("INSERT INTO llm_calls VALUES (?,?,?,?,?,?,?,?,?)",
               (_now(), current_task_id(), model, latency_ms, status, retries,
                (usage or {}).get("prompt_tokens", 0),
                (usage or {}).get("completion_tokens", 0), 1 if stream else 0))
    db.commit()


@_safe
def tool_call(tool: str, level: str, ok: bool, duration_ms: int):
    db = get_db()
    db.execute("INSERT INTO tool_calls VALUES (?,?,?,?,?,?)",
               (_now(), current_task_id(), tool, level, 1 if ok else 0,
                duration_ms))
    db.commit()


@_safe
def approval(session: str, kind: str, name: str, decision: str, steer: str = ""):
    db = get_db()
    db.execute("INSERT INTO approvals VALUES (?,?,?,?,?,?)",
               (_now(), session, kind, name, decision, steer[:500]))
    db.commit()


# ---------------- 查询(报表/指标) ----------------

def q_metrics(hours: int = 24) -> dict:
    db = get_db()
    since = (datetime.now().timestamp() - hours * 3600)
    m = {}
    rows = db.execute("SELECT status, COUNT(*) n FROM tasks GROUP BY status").fetchall()
    m["tasks_by_status"] = {r["status"]: r["n"] for r in rows}
    row = db.execute("SELECT COUNT(*) n, COALESCE(SUM(prompt_tokens),0) p, "
                     "COALESCE(SUM(completion_tokens),0) c FROM llm_calls").fetchone()
    m["llm_calls"], m["prompt_tokens"], m["completion_tokens"] = row["n"], row["p"], row["c"]
    lats = [r["latency_ms"] for r in db.execute(
        "SELECT latency_ms FROM llm_calls WHERE latency_ms IS NOT NULL "
        "ORDER BY latency_ms").fetchall()][-2000:]
    if lats:
        m["llm_latency_avg_ms"] = sum(lats) // len(lats)
        m["llm_latency_p95_ms"] = lats[min(int(len(lats) * 0.95), len(lats) - 1)]
    m["tool_calls"] = {r["tool"]: {"ok": r["ok"], "fail": r["fail"]} for r in db.execute(
        "SELECT tool, SUM(ok) ok, SUM(1-ok) fail FROM tool_calls GROUP BY tool")}
    m["approvals"] = {r["decision"]: r["n"] for r in db.execute(
        "SELECT decision, COUNT(*) n FROM approvals GROUP BY decision")}
    m["top_sessions"] = [r["session"] for r in db.execute(
        "SELECT session, SUM(total_tokens) t FROM tasks GROUP BY session "
        "ORDER BY t DESC LIMIT 5")]
    return m


# ---------------- 可用性: 熔断器 + 限流器(全局单例) ----------------

class Breaker:
    """连续失败熔断: 连续fails次→打开cooldown秒(快速失败)→半开放行一次探测。"""

    def __init__(self, fails: int, cooldown: float):
        self.fails, self.cooldown = fails, cooldown
        self._lock = threading.Lock()
        self._consec = 0
        self._opened_at = 0.0

    def check(self):
        with self._lock:
            if self._consec < self.fails:
                return
            elapsed = time.time() - self._opened_at
            if elapsed < self.cooldown:
                raise core._FatalError(
                    f"LLM熔断器打开中(连续失败{self._consec}次, "
                    f"剩余{int(self.cooldown - elapsed)}s) — 稍后重试")
            self._opened_at = time.time()  # 半开: 放行一次探测

    def record(self, ok: bool):
        with self._lock:
            self._consec = 0 if ok else self._consec + 1
            if self._consec == self.fails:
                self._opened_at = time.time()


class RateLimiter:
    """滑动窗口限流: 每分钟最多rpm次, 超出则等待(有上限, 不无限阻塞)。"""

    def __init__(self, rpm: int, max_wait: float = 30.0):
        self.rpm, self.max_wait = max(1, rpm), max_wait
        self._lock = threading.Lock()
        self._window: list[float] = []

    def acquire(self):
        deadline = time.time() + self.max_wait
        while True:
            with self._lock:
                now = time.time()
                self._window = [t for t in self._window if now - t < 60]
                if len(self._window) < self.rpm:
                    self._window.append(now)
                    return
                wait = min(60 - (now - self._window[0]) + 0.05,
                           deadline - now)
            if wait <= 0:
                raise core._FatalError(
                    f"LLM限流等待超时({self.max_wait}s, 上限{self.rpm}次/分)")
            time.sleep(min(wait, 1.0))


_guards: dict = {}


def guard(cfg: dict):
    """按 base_url+model 维度的全局熔断+限流器(跨会话/跨线程共享)。"""
    key = f"{cfg.get('base_url')}|{cfg.get('model')}"
    if key not in _guards:
        lim = (cfg.get("limits") or {})
        _guards[key] = (Breaker(int(lim.get("breaker_fails", 5)),
                                float(lim.get("breaker_cooldown_s", 120))),
                        RateLimiter(int(lim.get("llm_rpm", 60))))
    return _guards[key]


def _reset(path):
    """测试用: 切换到独立db并重置全部单例状态。"""
    global _conn, DEFAULT_DB, _guards
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
    DEFAULT_DB = Path(path)
    _guards.clear()
