#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcp_server.py — 把 agent 的工具注册表暴露成 MCP server (stdio)

有意手写 MCP 协议子集(JSON-RPC 2.0, 按行分帧)而不是用 FastMCP:
  - 零新依赖, 看清协议本质: initialize / tools/list / tools/call / ping
  - 同一份工具函数, 内部(Registry)与外部(MCP客户端)两种暴露, 业务逻辑零复制

用法(供 ZCode / Claude Code 等 MCP 客户端接入):
  command: python  args: [C:\\...\\Agent\\mcp_server.py]
注意: 写级工具经由外部客户端调用时, 权限确认由客户端侧负责(本进程直接执行)。
日志一律走 stderr —— stdout 是协议通道, 不能污染。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agentcore as core  # noqa: E402

PROTOCOL = "2024-11-05"


def rpc_result(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def rpc_error(rid, code, msg):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}


def handle(msg: dict, reg: core.Registry) -> dict | None:
    method = msg.get("method")
    rid = msg.get("id")
    if method == "initialize":
        want = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
        return rpc_result(rid, {
            "protocolVersion": want,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mini-agent-tools", "version": "1.0"}})
    if rid is None:  # notifications(initialized/cancelled等): 不应答
        return None
    if method == "ping":
        return rpc_result(rid, {})
    if method == "tools/list":
        tools = [{"name": t.name, "description": t.description,
                  "inputSchema": t.parameters}
                 for t in reg._tools.values()]
        return rpc_result(rid, {"tools": tools})
    if method == "tools/call":
        p = msg.get("params") or {}
        res = reg.execute(p.get("name", ""), p.get("arguments") or {})
        is_err = res.startswith(("[工具错误]", "[参数错误]"))
        return rpc_result(rid, {"content": [{"type": "text", "text": res}],
                                "isError": bool(is_err)})
    return rpc_error(rid, -32601, f"Method not found: {method}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    core.set_root(Path.cwd())
    # 启动阶段(载插件等)的 core.log 会打 stdout, 而 stdout 是协议通道:
    # 临时重定向到 stderr, 协议开始前还原。
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        cfg = core.load_config()
        reg = core.build_registry()
        core.load_plugins(reg, cfg)
    finally:
        sys.stdout = real_stdout
    print(f"[mcp-server] 就绪, {len(reg.names())} 个工具: "
          f"{', '.join(reg.names())}", file=sys.stderr, flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        out = handle(msg, reg)
        if out is not None:
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
