#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/test_files.py — Web 文件能力测试(纯HTTP, 不耗token)

覆盖: 上传(含中文文件名/重名自动改名) → 目录浏览 → 下载内容一致 →
      敏感文件拒下载(403) → 路径穿越拦截(400)
"""

import subprocess
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent.parent
PORT = 8803
BASE = f"http://127.0.0.1:{PORT}"

passed, failed = [], []


def check(name, cond):
    (passed if cond else failed).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")


def main():
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--port", str(PORT), "--no-open", "--cwd", str(HERE)],
        cwd=str(HERE), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                requests.get(f"{BASE}/api/sessions", timeout=2)
                break
            except Exception:
                time.sleep(0.3)
        else:
            raise RuntimeError("server 启动失败")

        # 1) 上传(中文文件名, 唯一化保证幂等)
        stamp = int(time.time() * 1000)
        fname = f"导入测试_{stamp}.txt"
        content = "内容-中文-123\nsecond line".encode("utf-8")
        r = requests.post(f"{BASE}/api/files/upload",
                          files={"file": (fname, content)}, timeout=30)
        j = r.json()
        check("上传成功且返回相对路径", r.status_code == 200 and j.get("ok")
              and j["path"].startswith(f"uploads/{fname}"))
        saved = j["path"]

        # 2) 重名自动改名
        r2 = requests.post(f"{BASE}/api/files/upload",
                           files={"file": (fname, content)}, timeout=30)
        check("重名自动加序号", r2.json()["path"] != saved and "(1)" in r2.json()["path"])

        # 3) 目录浏览
        r = requests.get(f"{BASE}/api/files", params={"path": "uploads"}, timeout=10)
        names = [it["name"] for it in r.json().get("items", [])]
        check("目录浏览可见上传文件", fname in names)

        # 4) 下载内容一致
        r = requests.get(f"{BASE}/api/files/download", params={"path": saved}, timeout=10)
        disp = r.headers.get("content-disposition", "")
        check("下载内容与上传一致", r.status_code == 200 and r.content == content
              and "filename" in disp)

        # 5) 敏感文件拒下载
        r = requests.get(f"{BASE}/api/files/download", params={"path": "config.json"}, timeout=10)
        check("config.json 拒绝下载(403)", r.status_code == 403)
        r = requests.get(f"{BASE}/api/files", params={"path": "."}, timeout=10)
        names = [it["name"] for it in r.json().get("items", [])]
        check("目录浏览隐藏敏感文件", "config.json" not in names)

        # 6) 路径穿越拦截
        r = requests.get(f"{BASE}/api/files/download",
                         params={"path": "../work-main/README.md"}, timeout=10)
        check("路径穿越被拦(400)", r.status_code == 400)
        r = requests.post(f"{BASE}/api/files/upload",
                          files={"file": ("../../evil.txt", b"x")}, timeout=10)
        check("上传文件名路径成分被剥离", r.status_code == 200
              and "/" not in r.json()["path"].split("uploads/")[-1])
        print(f"[test_files] 通过 {len(passed)} 项, 失败 {len(failed)} 项")
        return 0 if not failed else 1
    finally:
        proc.terminate()
        # 幂等清理: 删除本测试产生的文件
        for p in (HERE / "uploads").glob("导入测试_*.txt"):
            try:
                p.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
