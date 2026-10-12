#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/test_web_features.py — P0 前端升级新增后端能力的离线测试(零 LLM)

覆盖: /api/files/raw 内联预览(200/敏感403/穿越400/404) + 会话重命名 + 会话删除(含遥测清理)。
用 requests + uvicorn 守护线程起真实服务(免 httpx/TestClient 依赖), 端口 8811。
"""
import json
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


# 本套件的会话测试写在真实 sessions/ 目录(server 的沙箱根), 历史上套件被 kill
# 会留下 __t_*/__t05_* 残留文件, 污染下一轮 rename/delete 断言(409 误判等).
# 启动先扫掉这些**测试产物**(真实会话名均为日期/adv_/brief_/sub_ 前缀, 不会误伤).
_ARTIFACT_GLOBS = ("__t_*", "_t05_*", "proj.jsonl", "proj2.jsonl",
                   "alice@*", "bob@*", "token@*")


def clean_test_artifacts():
    sd = core.HERE / "sessions"
    n = 0
    if sd.is_dir():
        for pat in _ARTIFACT_GLOBS:
            for f in sd.glob(pat):
                try:
                    f.unlink()
                    n += 1
                except OSError:
                    pass
    import storage
    try:
        db = storage.get_db()
        db.execute("DELETE FROM messages WHERE session LIKE '__t05_%'")
        db.commit()
    except Exception:
        pass
    if n:
        print(f"[cleanup] 清除上轮遗留测试产物 {n} 个")


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
    clean_test_artifacts()
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

    # --- 会话双存储: SQLite-only 会话也须能 delete/rename(防回归 95d6bc2) ---
    # 侧栏(/api/sessions)把 SQLite 主存储 ∪ 旧 JSONL 都列出来; 但"只在 SQLite、
    # 无对应 .jsonl"的会话, 旧实现的存在性判定只看 jsonl -> 误判 404 删/改不掉。
    # 这里用一个纯 SQLite 会话(3 行)锁死该回归。
    import storage
    _db = storage.get_db()
    _ren_from, _ren_to = "__t05_db_ren", "__t05_db_ren_to"
    _del_id = "__t05_db_del"
    for _sid in (_ren_from, _del_id):
        for _i in range(3):
            _db.execute("INSERT INTO messages VALUES (?,?,?,?)",
                        (_sid, "2026-01-01T00:00:0%d" % _i, "message",
                         '{"msg":{"role":"user","content":"t05 db %d"}}' % _i))
    _db.commit()
    _cnt = lambda s: _db.execute(
        "SELECT COUNT(*) FROM messages WHERE session=?", (s,)).fetchone()[0]
    try:
        check("SQLite-only 会话确实只在 SQLite(无 jsonl)",
              _cnt(_ren_from) == 3 and not (sd / (_ren_from + ".jsonl")).exists())

        # 1) 侧栏可见
        _names = [it.get("name") for it in
                  requests.get(BASE + "/api/sessions", timeout=5).json()]
        check("SQLite-only 出现在 /api/sessions 列表", _ren_from in _names)

        # 2) rename -> 200 且新名正确; 随后直接查 SQLite: 键真迁移
        r = requests.post(BASE + "/api/sessions/%s/rename" % _ren_from,
                          json={"new": _ren_to}, timeout=5)
        check("SQLite-only rename 200 且新名正确",
              r.status_code == 200 and r.json().get("name") == _ren_to)
        check("SQLite-only rename 后键真迁移(旧0/新3)",
              _cnt(_ren_from) == 0 and _cnt(_ren_to) == 3)

        # 3) delete 另一个 SQLite-only -> 200 且真清除
        r = requests.delete(BASE + "/api/sessions/" + _del_id, timeout=5)
        check("SQLite-only delete 200 且行真清除",
              r.status_code == 200 and _cnt(_del_id) == 0)

        # 4) 不存在者: 删/改都 404
        r1 = requests.delete(BASE + "/api/sessions/_no_such_db_zzz", timeout=5)
        r2 = requests.post(BASE + "/api/sessions/_no_such_db_zzz/rename",
                           json={"new": "_no_such_db_yyy"}, timeout=5)
        check("SQLite-only 不存在: delete 404 / rename 404",
              r1.status_code == 404 and r2.status_code == 404)
    finally:
        _db.execute("DELETE FROM messages WHERE session LIKE '__t05_%'")
        _db.commit()
        for _p in sd.glob("__t05_db_*"):
            _p.unlink(missing_ok=True)
    check("SQLite-only 测试数据已清尾(库内0行)",
          _db.execute("SELECT COUNT(*) FROM messages WHERE session LIKE "
                      "'__t05_%'").fetchone()[0] == 0)

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

    # --- 断点续跑端点 ---
    ck = sd / "__t_ckpt.ckpt.json"
    ck.write_text(json.dumps({"session": "__t_ckpt", "task": "测试中断任务",
                              "turn": 2, "ts": "2026-10-06T09:00:00"},
                             ensure_ascii=False), encoding="utf-8")
    r = requests.get(BASE + "/api/interrupted", timeout=5)
    items = r.json().get("items", [])
    check("interrupted 列出伪造 ckpt",
          any(i.get("session") == "__t_ckpt" for i in items))
    r = requests.post(BASE + "/api/resume", json={"session": "no_such"}, timeout=5)
    check("resume 无 ckpt 404", r.status_code == 404)
    r = requests.post(BASE + "/api/resume/discard",
                      json={"session": "__t_ckpt"}, timeout=5)
    check("discard 200 且 ckpt 已删",
          r.status_code == 200 and not ck.exists())
    r = requests.get(BASE + "/api/interrupted", timeout=5)
    check("discard 后 interrupted 不再列出",
          all(i.get("session") != "__t_ckpt" for i in r.json().get("items", [])))
    bad = sd / "__t_bad.ckpt.json"
    bad.write_text("{broken", encoding="utf-8")
    requests.get(BASE + "/api/interrupted", timeout=5)  # 触发自清理
    check("interrupted 顺带清掉损坏 ckpt", not bad.exists())

    print("ALL PASS" if not fails else f"FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
