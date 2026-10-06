#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/test_web_features.py — P0 前端升级新增后端能力的离线测试(零 LLM)

覆盖: /api/files/raw 内联预览(200/敏感403/穿越400/404) + 会话重命名 + 会话删除(含遥测清理)。
用 requests + uvicorn 守护线程起真实服务(免 httpx/TestClient 依赖), 端口 8811。
"""
import os
import sys
import threading
import time
from pathlib import Path

# ⚠️ 摘掉沙箱注入的代理(工具代理会串 keep-alive 连接的响应, 纯测试环境假象)
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

PORT = 8811
BASE = f"http://127.0.0.1:{PORT}"
fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def main() -> int:
    srv.CFG = {"model": "test-model"}
    threading.Thread(
        target=lambda: uvicorn.run(srv.app, host="127.0.0.1", port=PORT, log_level="error"),
        daemon=True).start()
    for _ in range(50):
        try:
            requests.get(BASE + "/api/health", timeout=1)
            break
        except Exception:
            time.sleep(0.2)

    # --- /api/files/raw ---
    r = requests.get(BASE + "/api/files/raw", params={"path": "README.md"}, timeout=5)
    check("raw 200", r.status_code == 200)
    check("raw 内容正确", "Agent" in r.text)
    check("raw 内联(非附件头)",
          "attachment" not in r.headers.get("content-disposition", "").lower())

    r = requests.get(BASE + "/api/files/raw", params={"path": "config.json"}, timeout=5)
    check("raw 敏感文件 403", r.status_code == 403)

    r = requests.get(BASE + "/api/files/raw",
                     params={"path": "../" * 10 + "Windows/win.ini"}, timeout=5)
    check("raw 路径穿越 400", r.status_code == 400)

    r = requests.get(BASE + "/api/files/raw", params={"path": "no_such_file.xyz"}, timeout=5)
    check("raw 不存在 404", r.status_code == 404)

    # --- 会话重命名 ---
    sd = core.HERE / "sessions"
    sd.mkdir(exist_ok=True)
    src = sd / "__t_ren.jsonl"
    src.write_text('{"role":"user","content":"hi"}\n', encoding="utf-8")

    r = requests.post(BASE + "/api/sessions/__t_ren/rename",
                      json={"new": "__t_renamed"}, timeout=5)
    check("rename 200 且返回新名",
          r.status_code == 200 and r.json().get("name") == "__t_renamed")
    check("rename 旧文件消失新文件出现",
          not src.exists() and (sd / "__t_renamed.jsonl").exists())

    r = requests.post(BASE + "/api/sessions/__t_renamed/rename",
                      json={"new": ""}, timeout=5)
    check("rename 空名 400", r.status_code == 400)

    r = requests.post(BASE + "/api/sessions/__t_renamed/rename",
                      json={"new": "README"}, timeout=5)  # README.jsonl 不存在但会被创建名冲突? 不会, README 会话文件不存在 → 允许
    ok_conflict = r.status_code in (200, 409)
    check("rename 语义合法(200/409)", ok_conflict, str(r.status_code))
    # 收尾: 归位成 __t_renamed 供删除测试
    cur_file = sd / ("__t_renamed.jsonl" if r.status_code == 409 else "README.jsonl")
    if cur_file.name != "__t_renamed.jsonl":
        cur_file.rename(sd / "__t_renamed.jsonl")

    r = requests.post(BASE + "/api/sessions/no_such_sess/rename",
                      json={"new": "x"}, timeout=5)
    check("rename 不存在 404", r.status_code == 404)

    # --- 会话删除 ---
    r = requests.delete(BASE + "/api/sessions/__t_renamed", timeout=5)
    check("delete 200", r.status_code == 200)
    check("delete 文件已删", not (sd / "__t_renamed.jsonl").exists())
    r = requests.delete(BASE + "/api/sessions/__t_renamed", timeout=5)
    check("delete 不存在 404", r.status_code == 404)

    # --- 名称清洗: 含路径成分的名字 ---
    (sd / "__t_dirty.jsonl").write_text("{}\n", encoding="utf-8")
    r = requests.post(BASE + "/api/sessions/__t_dirty/rename",
                      json={"new": "../../evil"}, timeout=5)
    cleaned = "".join(c for c in "../../evil" if c.isalnum() or c in "-_")  # "..evila" 形态
    check("rename 恶意名被清洗(不落 sessions 外)",
          r.status_code == 200 and not (core.HERE.parent / "evil.jsonl").exists()
          and not (core.HERE / "evil.jsonl").exists())
    requests.delete(BASE + "/api/sessions/" + cleaned.replace(".", ""), timeout=5)
    for p in sd.glob("__t_*"):
        p.unlink(missing_ok=True)

    print("ALL PASS" if not fails else f"FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
