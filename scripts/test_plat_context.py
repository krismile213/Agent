#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/test_plat_context.py — AI助手嵌入 T04 平台上下文注入 离线测试(零 LLM)

覆盖: render_plat_ctx(全字段/缺字段/空/非法) / inject_plat_ctx(首插位置/
      每轮替换同一条不累积/内容不变不重复落盘/空上下文不动) /
      /api/chat 读 X-Plat-Context(带头/换头/不带头/非法JSON) /
      审批事件 level 字段(permission/plan/step, T01 契约扩展) /
      审批等待超时防线(无响应按拒绝自动解决, 引擎线程不挂死) /
      /api/pending 返回体 level 字段(刷新重建卡片契约) /
      setup_cors 默认关闭 + 白名单挂载。
requests + uvicorn 守护线程起真实服务, 端口 8815。
"""
import os
import sys
import threading
import time
from pathlib import Path

# ⚠️ 必须在 requests 使用前摘掉沙箱注入的代理(同 test_auth.py)
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
           "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

import requests
import uvicorn

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
os.chdir(HERE)  # 沙箱根 = 仓库根

import server as srv      # noqa: E402
import agentcore as core  # noqa: E402

PORT = 8815
BASE = f"http://127.0.0.1:{PORT}"
fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name
          + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def wait_up():
    for _ in range(50):
        try:
            requests.get(BASE + "/api/health", timeout=1)
            return
        except Exception:
            time.sleep(0.2)


class _FakeTranscript:
    def __init__(self):
        self.logs = []
        self.session = "t_plat_ctx"

    def log(self, kind, data):
        self.logs.append((kind, data))


# ---------- render_plat_ctx ----------

def test_render():
    txt = core.render_plat_ctx({"page": "alerts", "page_label": "加急预警",
                                "category": "手机膜", "batch": "20261008",
                                "selected_sku": ""})
    check("渲染: 标记开头", txt.startswith(core.PLAT_CTX_MARK))
    check("渲染: 页面+key", "「加急预警」页" in txt and "page=alerts" in txt)
    check("渲染: 品类批次", "品类=手机膜" in txt and "当前批次=20261008" in txt)
    check("渲染: 空SKU占位", "选中SKU=(无)" in txt)
    check("渲染: 兜底规则句", "以用户为准" in txt)
    check("渲染: 空dict→空串", core.render_plat_ctx({}) == "")
    check("渲染: None→空串", core.render_plat_ctx(None) == "")
    check("渲染: 非dict→空串", core.render_plat_ctx("x") == "")
    check("渲染: 仅无意义键→空串", core.render_plat_ctx({"foo": 1}) == "")
    f = core.render_plat_ctx({"page": "records",
                              "filters": {"status": "审批中", "空值": ""}})
    check("渲染: filters生效且滤空", "该页筛选" in f and "status=审批中" in f
          and "空值=" not in f)


# ---------- inject_plat_ctx ----------

def test_inject():
    h = [{"role": "system", "content": "SYS"},
         {"role": "user", "content": "hi"}]
    tr = _FakeTranscript()
    core.inject_plat_ctx(h, {"page": "alerts", "category": "手机膜"}, tr)
    check("注入: 插在system块后", len(h) == 3 and h[1]["role"] == "system"
          and h[1]["content"].startswith(core.PLAT_CTX_MARK))
    n1 = len(tr.logs)
    core.inject_plat_ctx(h, {"page": "alerts", "category": "手机膜"}, tr)
    check("注入: 内容不变不动不落盘", len(h) == 3 and len(tr.logs) == n1)
    core.inject_plat_ctx(h, {"page": "routes", "category": "手机壳"}, tr)
    check("注入: 每轮替换同一条", len(h) == 3 and "routes" in h[1]["content"]
          and "手机壳" in h[1]["content"])
    check("注入: 替换只落盘一次", len(tr.logs) == n1 + 1)
    core.inject_plat_ctx(h, None, tr)
    core.inject_plat_ctx(h, {}, tr)
    check("注入: 空上下文完全不动", len(h) == 3 and len(tr.logs) == n1 + 1)
    h2 = [{"role": "user", "content": "a"}]
    core.inject_plat_ctx(h2, {"page": "dashboard"})
    check("注入: 无system时插最前", h2[0]["role"] == "system"
          and h2[0]["content"].startswith(core.PLAT_CTX_MARK))
    h3 = [{"role": "system", "content": "SYS"},
          {"role": "system", "content": core.PLAT_CTX_MARK + " 旧内容"},
          {"role": "user", "content": "a"}]
    core.inject_plat_ctx(h3, {"page": "skus"})
    check("注入: 认得历史里的旧标记并原位替换", len(h3) == 3
          and "skus" in h3[1]["content"])


# ---------- /api/chat 端点级(真实服务) ----------

captured: dict = {}


def _running(sess):
    try:
        r = requests.get(f"{BASE}/api/sessions/{sess}/history", timeout=3)
        return bool((r.json() or {}).get("running"))
    except Exception:
        return True  # 查不到当作还在跑, 继续等


def _wait_idle(sess, key=None, timeout=15):
    """等该会话任务跑完(running=False)。work() 里 captured 赋值先于 running 复位,
    故 idle 即代表捕获值已就绪; key 参数仅为兼容旧调用点。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not _running(sess):
            time.sleep(0.05)
            return
        time.sleep(0.1)


def test_endpoints():
    orig_run, orig_plan = core.run_task, core.plan_and_run

    def fake_run(client, registry, policy, transcript, history, task, cfg, **kw):
        # 与真实 run_task 同款调用: 引擎签名收 plat_ctx 后注入 —— 验证整条组合链
        core.inject_plat_ctx(history, kw.get("plat_ctx"))
        captured["plat_ctx"] = kw.get("plat_ctx")
        captured["ctx_msgs"] = [m for m in history if str(
            m.get("content", "")).startswith(core.PLAT_CTX_MARK)]
        return "ok"

    def fake_plan(*a, **kw):
        captured["plan_ctx"] = kw.get("plat_ctx")
        return "ok"

    core.run_task, core.plan_and_run = fake_run, fake_plan
    try:
        from urllib.parse import quote
        sess = f"t_ctx_{int(time.time())}"
        # 中文值必须 percent-encode(HTTP 头只允许 latin-1, 裸中文在 requests/httpx
        # 转发时就抛 UnicodeEncodeError) —— 模拟平台 BFF 的正确行为
        hdr = {"X-Plat-Context": quote('{"page":"alerts","page_label":"加急预警",'
                                       '"category":"手机膜","batch":"20261008"}')}
        r = requests.post(BASE + "/api/chat",
                          json={"session": sess, "message": "这一页最急的几单"},
                          headers=hdr, timeout=10)
        check("chat: 带头(中文URL编码) 200", r.status_code == 200
              and (r.json() or {}).get("ok") is True)
        _wait_idle(sess, "ctx_msgs")
        ctx = captured.get("plat_ctx")
        check("chat: plat_ctx 透传为 dict", isinstance(ctx, dict)
              and ctx.get("page") == "alerts" and ctx.get("category") == "手机膜")
        msgs = captured.get("ctx_msgs") or []
        check("chat: history恰1条上下文且含品类批次",
              len(msgs) == 1 and "手机膜" in msgs[0]["content"]
              and "20261008" in msgs[0]["content"])

        # 裸 ASCII JSON(无中文)也兼容: 走原样解析分支
        r = requests.post(BASE + "/api/chat",
                          json={"session": sess + "_ascii", "message": "hi"},
                          headers={"X-Plat-Context": '{"page":"skus"}'}, timeout=10)
        _wait_idle(sess + "_ascii", "plat_ctx")
        check("chat: 裸ASCII JSON直接解析", r.status_code == 200
              and isinstance(captured.get("plat_ctx"), dict)
              and captured["plat_ctx"].get("page") == "skus")

        # 同会话换页再发: 历史里仍只有1条, 内容跟随新上下文
        hdr2 = {"X-Plat-Context": quote('{"page":"routes","category":"手机壳"}')}
        requests.post(BASE + "/api/chat",
                      json={"session": sess, "message": "再看这页"},
                      headers=hdr2, timeout=10)
        _wait_idle(sess, "ctx_msgs", timeout=15)
        msgs = captured.get("ctx_msgs") or []
        check("chat: 换头后仍1条且内容更新", len(msgs) == 1
              and "routes" in msgs[0]["content"] and "手机壳" in msgs[0]["content"]
              and "手机膜" not in msgs[0]["content"])

        # 不带头: 行为与现状一致(plat_ctx=None)
        r = requests.post(BASE + "/api/chat",
                          json={"session": sess + "_b", "message": "hi"},
                          timeout=10)
        _wait_idle(sess + "_b", "plat_ctx")
        check("chat: 不带头 plat_ctx=None", r.status_code == 200
              and captured.get("plat_ctx") is None)

        # 非法 JSON 头: 不报错, 静默忽略
        r = requests.post(BASE + "/api/chat",
                          json={"session": sess + "_c", "message": "hi"},
                          headers={"X-Plat-Context": quote("{bad json")}, timeout=10)
        _wait_idle(sess + "_c", "plat_ctx")
        check("chat: 非法JSON头不报错", r.status_code == 200
              and captured.get("plat_ctx") is None)

        # plan 模式同样透传
        r = requests.post(BASE + "/api/chat",
                          json={"session": sess + "_p", "message": "做个计划",
                                "plan": True}, headers=hdr, timeout=10)
        _wait_idle(sess + "_p", "plan_ctx")
        check("chat: plan模式透传 plat_ctx", r.status_code == 200
              and isinstance(captured.get("plan_ctx"), dict)
              and captured["plan_ctx"].get("page") == "alerts")
    finally:
        core.run_task, core.plan_and_run = orig_run, orig_plan


# ---------- 审批事件 level 字段(T01 §3.4 契约扩展) ----------

class _FakeSess:
    def __init__(self, name="t_lvl"):
        self.name = name
        self.evts = []
        self.always = set()

    def emit_threadsafe(self, kind, data):
        self.evts.append((kind, data))
        if kind == "permission_request":
            # 模拟用户秒点"批准": 卡片推出来即置 approved 并 set 事件
            # (wait() 返回 True=已批准), external 才能走到第二次确认
            req = srv.PENDING.get(data.get("id"))
            if req is not None:
                req.approved = True
                req.ev.set()


class _InstantEvent(threading.Event):
    def wait(self, timeout=None):
        return self.is_set()  # 不阻塞; FakeSess 已 set 即视为已批准


class _NeverEvent(threading.Event):
    def wait(self, timeout=None):
        return False  # 永远不应答: 模拟审批方掉线/无响应(超时路径)


class _TimeoutSess:
    """超时场景假会话: 推卡片但不自动批准(审批方掉线), 收 permission_resolved"""

    def __init__(self, name="t_appr_to"):
        self.name = name
        self.evts = []
        self.always = set()

    def emit_threadsafe(self, kind, data):
        self.evts.append((kind, data))

    def send(self, kind, data):
        self.evts.append((kind, data))


def test_levels():
    orig_event = srv.threading.Event
    srv.threading.Event = _InstantEvent
    try:
        s = _FakeSess()
        tool_w = type("T", (), {"name": "t_write", "level": "write",
                                "preview": staticmethod(lambda kw: "预览")})()
        srv.WebPolicy(s).allow(tool_w, {})
        ev = [d for k, d in s.evts if k == "permission_request"]
        check("level: write卡片带level=write", len(ev) == 1
              and ev[0].get("level") == "write")

        s2 = _FakeSess()
        tool_e = type("T", (), {"name": "t_ext", "level": "external",
                                "preview": staticmethod(lambda kw: "草稿")})()
        srv.WebPolicy(s2).allow(tool_e, {})
        ev2 = [d for k, d in s2.evts if k == "permission_request"]
        check("level: external两次确认都external", len(ev2) == 2
              and all(x.get("level") == "external" for x in ev2))

        s3 = _FakeSess()
        srv.WebPlanConfirmer(s3)("1. 干活")
        evp = [d for k, d in s3.evts if k == "plan_request"]
        check("level: plan_request带level=write", len(evp) == 1
              and evp[0].get("level") == "write")

        s4 = _FakeSess()
        srv.WebStepConfirmer(s4)(1, 3, "步骤文本", "")
        evs = [d for k, d in s4.evts if k == "step_request"]
        check("level: step_request带level=write", len(evs) == 1
              and evs[0].get("level") == "write")
    finally:
        srv.threading.Event = orig_event


# ---------- 审批等待超时防线(2026-10-13): 审批方掉线不得挂死引擎线程 ----------

def test_approval_timeout():
    """_wait_approval: wait() 返回 False(=无响应超时)时按拒绝自动解决:
    PENDING 摘除 / 广播 permission_resolved(timeout=True) / 记账 timeout,
    且 external 第一张卡超时即拒、不推第二张(防引擎线程永久占用
    会话 running 与 TASK_SEM 槽位——2026-10-13 平台 BFF SSE 掉线事故)."""
    import storage
    orig_event = srv.threading.Event
    srv.threading.Event = _NeverEvent
    s = _TimeoutSess()
    srv.SESS[s.name] = s
    try:
        tool_w = type("T", (), {"name": "t_to_write", "level": "write",
                                "preview": staticmethod(lambda kw: "预览")})()
        ok = srv.WebPolicy(s).allow(tool_w, {})
        check("超时: write工具按拒绝(allow=False)", ok is False)
        ev = [d for k, d in s.evts if k == "permission_request"]
        check("超时: 只推1张卡", len(ev) == 1)
        check("超时: PENDING已摘除",
              all(d["id"] not in srv.PENDING for d in ev))
        res = [d for k, d in s.evts if k == "permission_resolved"]
        check("超时: 广播permission_resolved(timeout=True)",
              len(res) == 1 and res[0].get("timeout") is True
              and res[0].get("approved") is False)
        row = storage.get_db().execute(
            "SELECT decision FROM approvals WHERE session=? AND name=? "
            "ORDER BY rowid DESC LIMIT 1", (s.name, "t_to_write")).fetchone()
        check("超时: 记账decision=timeout",
              row is not None and row[0] == "timeout")

        # external: 第一张卡超时即拒, 不再推第二张
        s2 = _TimeoutSess("t_appr_to_ext")
        srv.SESS[s2.name] = s2
        tool_e = type("T", (), {"name": "t_to_ext", "level": "external",
                                "preview": staticmethod(lambda kw: "草稿")})()
        ok2 = srv.WebPolicy(s2).allow(tool_e, {})
        ev2 = [d for k, d in s2.evts if k == "permission_request"]
        check("超时: external首卡超时即拒不推第二张",
              ok2 is False and len(ev2) == 1)
    finally:
        srv.threading.Event = orig_event
        srv.SESS.pop(s.name, None)
        srv.SESS.pop("t_appr_to_ext", None)
        srv.PENDING.clear()


# ---------- /api/pending 带 level(T01 契约扩展: 刷新重建卡片无需猜级别) ----------

def test_pending_level():
    # PendingReq 默认 level 兼容旧的两参构造
    check("PendingReq默认level=write",
          srv.PendingReq("t", "p").level == "write")
    check("PendingReq可显式传level",
          srv.PendingReq("t", "p", "external").level == "external")

    import asyncio
    sid = "t_pending_lvl"
    r_w = srv.PendingReq("write_file", "预览W", "write")
    r_w.session = sid
    r_e = srv.PendingReq("dingtalk_send", "预览E", "external")
    r_e.session = sid
    other = srv.PendingReq("tool_other", "预览O", "write")
    other.session = "t_pending_other"
    for r in (r_w, r_e, other):
        srv.PENDING[r.id] = r
    try:
        items = asyncio.run(srv.list_pending(None, session=sid))["items"]
        by_id = {it["id"]: it for it in items}
        check("pending清单仅含本会话", set(by_id) == {r_w.id, r_e.id})
        check("pending项含level字段", all("level" in it for it in items))
        check("pending level值正确",
              by_id[r_w.id].get("level") == "write"
              and by_id[r_e.id].get("level") == "external")
        check("pending保留原有id/tool/preview",
              by_id[r_w.id].get("tool") == "write_file"
              and by_id[r_w.id].get("preview") == "预览W")
    finally:
        srv.PENDING.clear()

    # 防回归: permission_request SSE 载荷仍含 level(旧行为不许被改坏)
    orig_event = srv.threading.Event
    srv.threading.Event = _InstantEvent
    try:
        s = _FakeSess()
        tool_w = type("T", (), {"name": "t_write", "level": "write",
                                "preview": staticmethod(lambda kw: "预览")})()
        srv.WebPolicy(s).allow(tool_w, {})
        ev = [d for k, d in s.evts if k == "permission_request"]
        check("回归: permission_request仍带level", len(ev) == 1
              and ev[0].get("level") == "write"
              and set(ev[0]) == {"id", "tool", "level", "preview"})
    finally:
        srv.threading.Event = orig_event
        srv.PENDING.clear()


# ---------- setup_cors ----------

def test_cors():
    # 默认(空白名单): 不挂载 + 真实服务响应无 CORS 头
    n = len(srv.app.user_middleware)
    srv.setup_cors({"cors_allow_origins": []})
    check("cors: 空白名单不挂载", len(srv.app.user_middleware) == n)
    r = requests.options(BASE + "/api/health", timeout=5)
    check("cors: 默认无CORS头",
          "access-control-allow-origin" not in {k.lower() for k in r.headers})
    # 白名单: 挂到独立 app(全局 app 已被 uvicorn 启动, 不可再 add)
    from fastapi import FastAPI
    fresh = FastAPI()
    srv.setup_cors({"cors_allow_origins": ["http://localhost:5173"]}, fresh)
    check("cors: 白名单挂载+1", len(fresh.user_middleware) == 1)
    srv.setup_cors({"cors_allow_origins": ["http://localhost:5173"]}, fresh)
    check("cors: 重复配置幂等挂2层(行为=每次调用都挂, 由调用方保证只调一次)",
          len(fresh.user_middleware) == 2)


def main() -> int:
    srv.CFG = {"model": "test-model", "base_url": "http://127.0.0.1:9/v1",
               "api_key": "test-key"}
    threading.Thread(
        target=lambda: uvicorn.run(srv.app, host="127.0.0.1", port=PORT,
                                   log_level="error"),
        daemon=True).start()
    wait_up()
    test_render()
    test_inject()
    test_levels()
    test_approval_timeout()
    test_pending_level()
    test_cors()
    test_endpoints()
    print(f"\n== {'全部通过' if not fails else f'失败 {len(fails)} 项'} ==")
    for f in fails:
        print("  FAIL:", f)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
