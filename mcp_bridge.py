#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcp_bridge.py — 给 agent 接入外部 MCP server 的工具(反向)

外部 server 进程(stdio)长驻在一个后台 asyncio 线程里; 引擎(同步)通过
run_coroutine_threadsafe 调用, 对 Registry 来说外部工具和本地工具无差别。

config.json 配置:
  "mcp_servers": [
    {"name": "self", "command": "python", "args": ["mcp_server.py"], "level": "read"}
  ]
  - name: 工具前缀(防重名), 注册为 <name>_<tool>
  - level: 外部工具默认 "write"(需确认), 明确只读的 server 可设 "read"
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import agentcore as core

_HERE = Path(__file__).resolve().parent


class Bridge:
    """后台事件循环线程 + 若干长驻的 MCP stdio 会话."""

    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.sessions: dict = {}      # name -> ClientSession
        self._cms: list = []          # 持有打开的 async 上下文, 关闭时逆序退出
        self._lock = threading.Lock()

    # ---- 生命周期 ----

    def start(self):
        readied = threading.Event()

        def _run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.call_soon(readied.set)
            self.loop.run_forever()

        threading.Thread(target=_run, daemon=True, name="mcp-bridge").start()
        readied.wait(10)

    def _submit(self, coro, timeout=180):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout)

    # ---- 连接与调用 ----

    def connect(self, name: str, command: str, args: list, env: dict | None):
        """启动一个外部 server 并完成 initialize + list_tools, 返回工具清单."""
        return self._submit(self._connect(name, command, args, env or {}))

    async def _connect(self, name: str, command: str, args: list, env: dict):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        merged = dict(env)
        merged.setdefault("PYTHONUTF8", "1")
        params = StdioServerParameters(command=command, args=args, env=merged)
        cm = stdio_client(params)
        read, write = await cm.__aenter__()
        self._cms.append(cm)
        scm = ClientSession(read, write)
        session = await scm.__aenter__()
        self._cms.append(scm)
        await session.initialize()
        self.sessions[name] = session
        return await session.list_tools()

    def call(self, server: str, tool: str, kwargs: dict, timeout: int = 300) -> str:
        res = self._submit(self._call(server, tool, kwargs), timeout)
        return res

    async def _call(self, server: str, tool: str, kwargs: dict) -> str:
        session = self.sessions[server]
        result = await session.call_tool(tool, arguments=kwargs or {})
        parts = []
        for c in (getattr(result, "content", None) or []):
            text = getattr(c, "text", None)
            if text:
                parts.append(text)
        if getattr(result, "isError", False):
            return "[工具错误][外部MCP] " + "\n".join(parts)
        return "\n".join(parts) if parts else "(外部工具无文本输出)"

    def shutdown(self):
        try:
            self._submit(self._shutdown(), 15)
        except Exception:
            pass
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)

    async def _shutdown(self):
        for cm in reversed(self._cms):
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass
        self._cms.clear()
        self.sessions.clear()


_BRIDGE: Bridge | None = None


def register_mcp_tools(registry: core.Registry, cfg: dict) -> int:
    """把 config 里 mcp_servers 的工具注册进 Registry. 缺SDK/连不上都只是跳过."""
    global _BRIDGE
    servers = cfg.get("mcp_servers") or []
    if not servers:
        return 0
    try:
        import mcp  # noqa: F401
    except ImportError:
        core.log("[mcp] 未安装 mcp SDK(pip install mcp), 跳过外部工具接入")
        return 0

    b = Bridge()
    b.start()
    n = 0
    for srv in servers:
        name = srv.get("name") or "mcp"
        level = srv.get("level", "write")
        try:
            tools = b.connect(name, srv.get("command", ""),
                              srv.get("args") or [], srv.get("env") or {})
        except Exception as e:
            core.log(f"[mcp] 服务器 {name} 连接失败(跳过): {type(e).__name__}: {e}")
            continue
        for t in (getattr(tools, "tools", None) or []):

            def make(bridge=b, srv_name=name, tool_name=t.name):
                def call(**kwargs):
                    return bridge.call(srv_name, tool_name, kwargs)
                return call

            desc = (t.description or "").strip()
            registry.register(core.Tool(
                f"{name}_{t.name}",
                f"[外部MCP:{name}] {desc}".strip(),
                getattr(t, "inputSchema", None) or
                {"type": "object", "properties": {}},
                level, make()))
            n += 1
        core.log(f"[mcp] {name}: 接入 {len(tools.tools)} 个工具(前缀 {name}_)")
    if n:
        _BRIDGE = b
        import atexit
        atexit.register(b.shutdown)
    else:
        b.shutdown()
    return n
