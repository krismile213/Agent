#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/test_human_intervention.py — 人工干预三件套单测(零LLM成本)

覆盖:
  1) 中途追加指令: InstructionInbox 线程安全 + run_task 工具间隙注入历史
  2) 外发双确认: WebPolicy(两段审批/不吃总允许) + CLI InteractivePolicy(双确认)
  3) 批量审批: /api/approve_batch + /api/pending + /api/enqueue(直接调端点, 无需HTTP)
  4) dingtalk_send 工具: external 级注册 + 草稿预览 + dry_run + 无人值守摘除
"""

import asyncio
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "scripts"))

import agentcore as core  # noqa: E402
import server as server  # noqa: E402  (端点单测与审批轮询共用)

passed, failed = [], []


def check(name, cond):
    (passed if cond else failed).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")


CFG = {"max_turns": 5, "context_budget_tokens": 999_999}


class FakeClient:
    """第1次返回工具调用, 第2次返回最终回答; 记录每次收到的messages."""

    def __init__(self):
        self.calls = 0
        self.seen = []
        self.usage = {"calls": 0, "prompt_tokens": 0,
                      "completion_tokens": 0, "total_tokens": 0}

    def chat(self, messages, tools=None):
        self.calls += 1
        self.seen.append(json.dumps(messages, ensure_ascii=False))
        if self.calls == 1:
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"id": "t1", "type": "function", "function": {
                        "name": "probe", "arguments": "{}"}}]}
        return {"role": "assistant", "content": "完成"}


def _setup():
    core.set_root(HERE)
    registry = core.Registry()
    registry.register(core.Tool("probe", "探针", {"type": "object",
                                                 "properties": {}}, "read",
                                lambda: "ok"))
    td = tempfile.mkdtemp()
    tr = core.Transcript(Path(td) / "t.jsonl")
    return registry, tr


# ---------------- 1) 中途追加指令 ----------------

def test_inbox_basic():
    ib = core.InstructionInbox()
    check("空收件箱drain为空", ib.drain() == [])
    ib.add("  看看Part8 ")
    ib.add("")            # 空白不入队
    ib.add("再核对总数")
    got = ib.drain()
    check("入队顺序与去空白", got == ["看看Part8", "再核对总数"])
    check("drain后清空", len(ib) == 0)


def test_run_task_inject():
    registry, tr = _setup()
    client = FakeClient()
    ib = core.InstructionInbox()
    ib.add("重点核对Part8的停滞天数")
    history = [{"role": "system", "content": "s"}]
    events = []
    answer = core.run_task(client, registry, core.YoloPolicy(), tr, history,
                           "跑一次检查", CFG,
                           emit=lambda k, d: events.append((k, d)), inject=ib)
    check("任务正常完成", answer == "完成")
    check("发出instruction_injected事件",
          any(k == "instruction_injected" for k, _ in events))
    check("第2轮LLM已看到追加指令",
          client.calls == 2 and "[中途追加指令]" in client.seen[1]
          and "重点核对Part8" in client.seen[1])
    check("注入消息在user边界(历史结构合法)",
          any(m.get("role") == "user" and "[中途追加指令]" in str(m.get("content"))
              for m in history))


def test_run_task_no_inject():
    registry, tr = _setup()
    client = FakeClient()
    history = [{"role": "system", "content": "s"}]
    events = []
    core.run_task(client, registry, core.YoloPolicy(), tr, history, "任务", CFG,
                  emit=lambda k, d: events.append((k, d)))
    check("无追加指令时不注入不发事件",
          not any(k == "instruction_injected" for k, _ in events)
          and "[中途追加指令]" not in client.seen[-1])


# ---------------- 2) 外发双确认 ----------------

def _ext_tool():
    return core.Tool("dingtalk_send", "外发", {"type": "object",
                                               "properties": {}}, "external",
                     lambda **k: "ok",
                     preview_fn=lambda kw: "[外发草稿] 标题: t\n正文: x")


class _FakeSess:
    def __init__(self):
        self.name = "t_ext"
        self.always = set()

    def emit_threadsafe(self, kind, data):
        pass


def _wait_req(session, timeout=5.0):
    """轮询等待引擎线程推入的"新"待决审批(已见过的id跳过, 模拟端点pop后的状态)."""
    dl = time.time() + timeout
    while time.time() < dl:
        for r in list(server.PENDING.values()):
            if r.session == session and r.id not in _wait_req._seen:
                return r
        time.sleep(0.02)
    raise TimeoutError("未等到审批请求")


def _resolve(req, approved):
    """模拟 /api/approve 端点对单个请求的处置(pop + 置位)."""
    _wait_req._seen.add(req.id)
    server.PENDING.pop(req.id, None)
    req.approved = approved
    req.ev.set()


def test_webpolicy_double_confirm():
    import server as server
    _wait_req._seen = set()
    sess = _FakeSess()
    pol = server.WebPolicy(sess)
    out = {}

    def run():
        out["ok"] = pol.allow(_ext_tool(), {"title": "t", "text": "x"})

    th = threading.Thread(target=run, daemon=True)
    th.start()
    r1 = _wait_req(sess.name)
    check("第一段=草稿确认", "外发草稿" in r1.preview)
    _resolve(r1, True)
    r1.always = True          # 恶意场景: 即使前端传了总允许也不该生效
    r2 = _wait_req(sess.name)
    check("第二段=发送确认", "第二次确认" in r2.preview and r2.id != r1.id)
    _resolve(r2, True)
    th.join(5)
    check("两段都批 → 允许", out.get("ok") is True)
    check("external不吃总允许", "dingtalk_send" not in sess.always)
    server.PENDING.clear()


def test_webpolicy_reject_paths():
    import server as server
    _wait_req._seen = set()
    sess = _FakeSess()
    pol = server.WebPolicy(sess)

    # 草稿就拒绝: 只应出现一张卡片
    out = {}

    def run1():
        out["ok"] = pol.allow(_ext_tool(), {"title": "t", "text": "x"})

    th = threading.Thread(target=run1, daemon=True)
    th.start()
    r1 = _wait_req(sess.name)
    _resolve(r1, False)
    th.join(5)
    check("草稿拒绝 → 直接拒绝(无第二段)", out.get("ok") is False
          and all(r.session != sess.name for r in server.PENDING.values()))

    # 草稿过了, 发送前反悔
    def run2():
        out["ok2"] = pol.allow(_ext_tool(), {"title": "t", "text": "x"})

    th = threading.Thread(target=run2, daemon=True)
    th.start()
    r1 = _wait_req(sess.name)
    _resolve(r1, True)
    r2 = _wait_req(sess.name)
    _resolve(r2, False)
    th.join(5)
    check("发送段拒绝 → 不外发", out.get("ok2") is False)
    server.PENDING.clear()


def test_cli_paths():
    import builtins
    from mini_agent import InteractivePolicy

    def run_with(inputs):
        pol = InteractivePolicy()
        it = iter(inputs)
        orig = builtins.input
        builtins.input = lambda *a: next(it)
        try:
            return pol.allow(_ext_tool(), {"title": "t", "text": "x"}), pol
        finally:
            builtins.input = orig

    ok, pol = run_with(["y", "y"])
    check("CLI: 两段y → 允许且不入总允许", ok and "dingtalk_send" not in pol.always)
    ok, _ = run_with(["n"])
    check("CLI: 草稿n → 拒绝", ok is False)
    ok, _ = run_with(["y", "n"])
    check("CLI: 发送段n → 拒绝", ok is False)
    # 回归: write级 'a' 仍入总允许
    pol = InteractivePolicy()
    wtool = core.Tool("write_file", "w", {"type": "object", "properties": {}},
                      "write", lambda **k: "ok")
    it = iter(["a"])
    orig = builtins.input
    builtins.input = lambda *a: next(it)
    try:
        ok = pol.allow(wtool, {})
    finally:
        builtins.input = orig
    check("回归: write级'a'总允许不变", ok and "write_file" in pol.always)


# ---------------- 3) 批量审批 / 追加指令端点 ----------------

def test_endpoints():
    import server as srv
    srv.CFG = core.load_config()   # 模块级导入时CFG为空, 会话构造需要它
    s = srv.get_sess("t_hiv")
    # 批量审批
    reqs = [srv.PendingReq(f"tool{i}", f"预览{i}") for i in range(3)]
    for r in reqs:
        r.session = s.name
        srv.PENDING[r.id] = r
    body = srv.BatchApproveBody(session=s.name,
                                ids=[r.id for r in reqs] + ["ghost"],
                                approve=True, text="")
    res = asyncio.run(srv.approve_batch(body, None))  # 鉴权关闭时request不参与作用域
    check("批量批准2生效+1失效", len(res["resolved"]) == 3
          and res["missing"] == ["ghost"]
          and all(r.approved for r in reqs) and all(r.ev.is_set() for r in reqs))
    check("批量后PENDING清空", not [r for r in srv.PENDING.values()
                                    if r.session == s.name])
    # pending 清单
    r2 = srv.PendingReq("write_file", "预览")
    r2.session = s.name
    srv.PENDING[r2.id] = r2
    items = asyncio.run(srv.list_pending(None, session=s.name))["items"]
    check("pending清单可查", len(items) == 1 and items[0]["id"] == r2.id
          and items[0]["tool"] == "write_file")
    srv.PENDING.clear()
    # enqueue: 未运行报错 / 运行中入队
    err = asyncio.run(srv.enqueue(srv.InjectBody(session=s.name, text="x"), None))
    check("未运行时enqueue报400", err.status_code == 400)
    s.running = True
    ok = asyncio.run(srv.enqueue(srv.InjectBody(session=s.name, text="盯紧G550"), None))
    check("运行中enqueue成功", ok.get("ok") is True)
    check("指令进入引擎收件箱", s.inbox.drain() == ["盯紧G550"])
    s.running = False


# ---------------- 4) dingtalk_send 工具 ----------------

def test_dingtalk_tool():
    reg = core.build_registry()
    t = reg.get("dingtalk_send")
    check("dingtalk_send已注册且为external级",
          t is not None and t.level == "external")
    pv = t.preview({"title": "测试", "text": "hello\n第二行"})
    check("草稿预览含标题与正文", "外发草稿" in pv and "测试" in pv and "第二行" in pv)
    out = reg.execute("dingtalk_send", {"title": "t", "text": "x", "dry_run": True})
    check("dry_run不实际发送", out.startswith("[dry]"))
    # 无人值守安全底线: external 级必须被晨检只读注册表摘除
    from daily_brief import build_readonly_registry
    ro = build_readonly_registry(core.load_config())
    check("晨检只读注册表已摘除external工具",
          "dingtalk_send" not in ro._tools
          and all(tt.level == "read" for tt in ro._tools.values()))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print("[单测] 人工干预三件套(追加指令/外发双确认/批量审批)")
    test_inbox_basic()
    test_run_task_inject()
    test_run_task_no_inject()
    test_webpolicy_double_confirm()
    test_webpolicy_reject_paths()
    test_cli_paths()
    test_endpoints()
    test_dingtalk_tool()
    print(f"[test_human_intervention] 通过 {len(passed)} 项, 失败 {len(failed)} 项")
    sys.exit(0 if not failed else 1)
