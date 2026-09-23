#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/test_mcp.py — MCP 双向测试

方向一(server): 用官方 mcp SDK 客户端连自己手写的 mcp_server.py
  initialize → tools/list → tools/call(list_dir / query_sku_route)
方向二(client): mcp_bridge 把"自己的 server"当外部 server 接入 Registry
  注册带前缀的工具 → 经 Registry.execute 跨进程调用
闭环意义: 两个方向共用同一个被测对象, 不依赖任何第三方 MCP server。
"""

import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "scripts"))

passed, failed = [], []


def check(name, cond):
    (passed if cond else failed).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")


def server_direction():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        params = StdioServerParameters(
            command=sys.executable, args=[str(HERE / "mcp_server.py")],
            env={"PYTHONUTF8": "1"})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                info = getattr(init, "server_info", None) or getattr(init, "serverInfo", None)
                check("initialize(手写协议握手成功)",
                      info is not None and getattr(info, "name", "") == "mini-agent-tools")
                lst = await session.list_tools()
                names = [t.name for t in lst.tools]
                check(f"tools/list 返回{len(names)}个工具",
                      "query_sku_route" in names and "save_memory" in names)
                r = await session.call_tool("list_dir", {"path": "."})
                txt = "\n".join(getattr(c, "text", "") for c in r.content)
                check("tools/call list_dir", ".py" in txt)
                r = await session.call_tool("query_sku_route",
                                            {"sku": "G531", "caliber": "action"})
                txt = "\n".join(getattr(c, "text", "") for c in r.content)
                # 只锚定稳定字段, 不锚定会随复盘重跑漂移的业务数值(如停滞天数)
                check("tools/call query_sku_route(插件工具经MCP可用)",
                      "G531" in txt and "卡点Part" in txt and "停滞" in txt)
    asyncio.run(run())


def client_direction():
    import agentcore as core
    import mcp_bridge

    cfg = dict(core.load_config())
    cfg["mcp_servers"] = [{"name": "self", "command": sys.executable,
                           "args": [str(HERE / "mcp_server.py")],
                           "level": "read"}]
    reg = core.build_registry()
    before = set(reg.names())
    n = mcp_bridge.register_mcp_tools(reg, cfg)
    check(f"bridge 接入外部工具 {n} 个", n >= 10)
    check("工具带前缀注册(self_*)", "self_list_dir" in reg.names()
          and "self_query_sku_route" in reg.names())
    r = reg.execute("self_list_dir", {"path": "."})
    check("经Registry跨进程调用 self_list_dir", ".py" in r)
    r = reg.execute("self_query_sku_route", {"sku": "G531"})
    check("经Registry跨进程调用 self_query_sku_route", "Part8" in r)
    r = reg.execute("self_read_file", {"path": "不存在.txt"})
    check("错误回灌保留(isError语义)", "不存在" in r or "[工具错误]" in r)
    import mcp_bridge as mb
    if mb._BRIDGE:
        mb._BRIDGE.shutdown()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print("[方向一] MCP server: SDK客户端 → 手写server")
    server_direction()
    print("[方向二] MCP client: bridge把外部server工具接进Registry")
    client_direction()
    print(f"[test_mcp] 通过 {len(passed)} 项, 失败 {len(failed)} 项")
    sys.exit(0 if not failed else 1)
