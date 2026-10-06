#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/test_auth.py — P1 登录鉴权+多用户隔离 离线测试(零 LLM)

覆盖: 未配置=全放行(向后兼容) / 307跳登录页 / 401拦截 / 登录成败 /
      Cookie 与静态 Bearer / 伪造token拒绝 / 用户@会话 命名空间隔离
      (列表/重命名/删除互不可见, legacy会话借道删除被拒) / 审批跨用户404。
requests + uvicorn 守护线程起真实服务, 端口 8813。
"""
import os
import sys
import threading
import time
from pathlib import Path

# ⚠️ 必须在 requests 使用前摘掉沙箱注入的代理: HTTP_PROXY 指向的工具代理
# 会串 keep-alive 连接的响应(Session 复用连接时拿到别处的 404), 纯测试环境假象
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

PORT = 8813
BASE = f"http://127.0.0.1:{PORT}"
fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def wait_up():
    for _ in range(50):
        try:
            requests.get(BASE + "/api/health", timeout=1)
            return
        except Exception:
            time.sleep(0.2)


def main() -> int:
    srv.CFG = {"model": "test-model", "base_url": "http://127.0.0.1:9/v1",
               "api_key": "test-key"}  # LLMClient 初始化需要, 不发起真实请求
    threading.Thread(
        target=lambda: uvicorn.run(srv.app, host="127.0.0.1", port=PORT, log_level="error"),
        daemon=True).start()
    wait_up()

    # ===== 阶段1: 未启用鉴权 → 全放行(向后兼容, 旧测试/本机用法不变) =====
    srv.setup_auth({})
    r = requests.get(BASE + "/api/sessions", timeout=5)
    check("未配置auth: 列表放行", r.status_code == 200)
    r = requests.get(BASE + "/api/me", timeout=5)
    check("未配置auth: me=匿名且auth=false",
          r.status_code == 200 and r.json().get("user") is None
          and r.json().get("auth") is False)

    # ===== 阶段2: 启用鉴权 → 拦截/登录/凭据 =====
    srv.setup_auth({"auth": {"users": {"alice": "pw-a", "bob": "pw-b"},
                             "tokens": ["tok-static-1"], "session_hours": 1,
                             "secret": "test-secret-xyz"}})
    check("setup_auth 开关生效", srv.AUTH["on"] is True)

    r = requests.get(BASE + "/", timeout=5, allow_redirects=False)
    check("未登录 / → 307 跳登录页",
          r.status_code == 307 and r.headers.get("location") == "/login")
    r = requests.get(BASE + "/api/sessions", timeout=5)
    check("未登录 API → 401", r.status_code == 401)
    r = requests.post(BASE + "/api/chat", json={"session": "x", "message": "hi"}, timeout=5)
    check("未登录 chat → 401", r.status_code == 401)
    check("health 保持开放(探针)",
          requests.get(BASE + "/api/health", timeout=5).status_code == 200)
    check("登录页开放", requests.get(BASE + "/login", timeout=5).status_code == 200)

    check("错误密码 401",
          requests.post(BASE + "/api/login",
                        json={"username": "alice", "password": "wrong"}, timeout=5).status_code == 401)
    check("不存在用户 401",
          requests.post(BASE + "/api/login",
                        json={"username": "ghost", "password": "pw-a"}, timeout=5).status_code == 401)
    s_a, s_b = requests.Session(), requests.Session()
    r = s_a.post(BASE + "/api/login", json={"username": "alice", "password": "pw-a"}, timeout=5)
    check("alice 登录成功+发cookie",
          r.status_code == 200 and "agent_token" in s_a.cookies)
    s_b.post(BASE + "/api/login", json={"username": "bob", "password": "pw-b"}, timeout=5)
    check("me=alice",
          s_a.get(BASE + "/api/me", timeout=5).json().get("user") == "alice")
    check("静态 Bearer token 放行",
          requests.get(BASE + "/api/sessions",
                       headers={"Authorization": "Bearer tok-static-1"}, timeout=5).status_code == 200)
    check("错误 Bearer 401",
          requests.get(BASE + "/api/sessions",
                       headers={"Authorization": "Bearer tok-bad"}, timeout=5).status_code == 401)
    check("伪造cookie 401",
          requests.get(BASE + "/api/sessions",
                       cookies={"agent_token": "garbage.sig"}, timeout=5).status_code == 401)

    tok = srv.make_token("alice")
    check("签名token可验", srv.check_token(tok) == "alice")
    p, sig = tok.split(".")
    bad = ("A" if p[0] != "A" else "B") + p[1:]
    check("篡改payload被拒", srv.check_token(bad + "." + sig) is None)
    check("篡改签名被拒", srv.check_token(p + "." + ("0" * len(sig))) is None)

    # ===== 阶段3: 用户@会话 命名空间隔离 =====
    sd = core.HERE / "sessions"
    sd.mkdir(exist_ok=True)
    (sd / "alice@proj.jsonl").write_text('{"role":"user","content":"hi"}\n', encoding="utf-8")
    (sd / "bob@proj.jsonl").write_text('{"role":"user","content":"yo"}\n', encoding="utf-8")
    (sd / "proj.jsonl").write_text('{"role":"user","content":"legacy"}\n', encoding="utf-8")
    try:
        names_a = [i["name"] for i in s_a.get(BASE + "/api/sessions", timeout=5).json()]
        names_b = [i["name"] for i in s_b.get(BASE + "/api/sessions", timeout=5).json()]
        check("alice 只见自己的 proj", names_a == ["proj"], str(names_a))
        check("bob 只见自己的 proj", names_b == ["proj"], str(names_b))

        r = s_a.get(BASE + "/api/sessions/proj/history", timeout=5)
        check("alice 打开同名会话 200(解析到 alice@proj)", r.status_code == 200,
              f"status={r.status_code} body={r.text[:80]}")

        r = s_a.post(BASE + "/api/sessions/proj/rename", json={"new": "proj2"}, timeout=5)
        check("alice rename 200",
              r.status_code == 200 and r.json().get("name") == "proj2",
              f"status={r.status_code} body={r.text[:80]}")
        check("rename 只动 alice 的文件",
              (sd / "alice@proj2.jsonl").exists() and not (sd / "alice@proj.jsonl").exists()
              and (sd / "bob@proj.jsonl").exists(),
              "files=" + ",".join(sorted(p.name for p in sd.glob("*.jsonl")
                                         if "@" in p.name or p.name == "proj.jsonl")))

        r = s_a.delete(BASE + "/api/sessions/proj2", timeout=5)
        check("alice delete 200", r.status_code == 200, f"status={r.status_code}")
        check("delete 只删 alice 的, bob 仍在",
              not (sd / "alice@proj2.jsonl").exists() and (sd / "bob@proj.jsonl").exists())

        r = s_a.delete(BASE + "/api/sessions/proj", timeout=5)
        check("借道删 legacy 会话 404 且未删",
              r.status_code == 404 and (sd / "proj.jsonl").exists(),
              f"status={r.status_code} body={r.text[:80]}")
        r = s_b.delete(BASE + "/api/sessions/proj", timeout=5)
        check("bob 删自己的 proj 200", r.status_code == 200,
              f"status={r.status_code} body={r.text[:80]}")

        # ===== 阶段4: 审批跨用户隔离 =====
        req = srv.PendingReq("t_write_file", "preview")
        req.session = "alice@sec"
        srv.PENDING[req.id] = req
        r = s_b.post(BASE + "/api/approve",
                     json={"session": "sec", "id": req.id, "approve": True}, timeout=5)
        check("bob 审批 alice 的请求 404(且不消费)",
              r.status_code == 404 and req.id in srv.PENDING,
              f"status={r.status_code} body={r.text[:80]}")
        r = s_a.post(BASE + "/api/approve",
                     json={"session": "sec", "id": req.id, "approve": True}, timeout=5)
        check("alice 审批自己的 200",
              r.status_code == 200 and req.id not in srv.PENDING,
              f"status={r.status_code} body={r.text[:80]}")
        req.ev.set()
    finally:
        srv.PENDING.clear()
        for p in ("alice@proj.jsonl", "bob@proj.jsonl", "proj.jsonl"):
            (sd / p).unlink(missing_ok=True)

    srv.setup_auth({})
    print("ALL PASS" if not fails else f"FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
