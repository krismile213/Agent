#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/run_all.py — 一键跑全部测试

分层:
  --tier fast  (默认, 不耗token): selftest + 全部单测
  --tier full  (耗token, ~15分钟): fast + MCP双向 + Web冒烟 + 升级E2E +
                                   高级能力E2E + 金标准评测(14用例)
用法:
  python scripts/run_all.py            # 快速回归(秒级)
  python scripts/run_all.py --tier full
  python scripts/run_all.py --tier full --skip mcp,eval
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent

FAST = [
    ("内核自检", [sys.executable, "mini_agent.py", "--selftest"]),
    ("反思闭环单测", [sys.executable, "scripts/test_reflect_fix.py"]),
    ("多轮增强单测", [sys.executable, "scripts/test_upgrade.py", "--unit-only"]),
    ("高级能力单测", [sys.executable, "scripts/test_advanced.py", "--unit-only"]),
    ("RAG检索质量", [sys.executable, "scripts/test_rag.py"]),
]
FULL_EXTRA = [
    ("文件接口", [sys.executable, "scripts/test_files.py"]),
    ("安全网与转向", [sys.executable, "scripts/test_safety.py"]),
    ("MCP双向", [sys.executable, "scripts/test_mcp.py"]),
    ("Web冒烟(含审批)", [sys.executable, "scripts/smoke_web.py"]),
    ("多轮增强E2E", [sys.executable, "scripts/test_upgrade.py", "--e2e-only"]),
    ("高级能力E2E", [sys.executable, "scripts/test_advanced.py", "--e2e-only"]),
    ("金标准评测", [sys.executable, "eval/run_eval.py"]),
]


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="mini_agent 全量测试入口")
    ap.add_argument("--tier", choices=["fast", "full"], default="fast")
    ap.add_argument("--skip", default="", help="逗号分隔的要跳过的套件名关键词")
    args = ap.parse_args()
    skip = [s.strip() for s in args.skip.split(",") if s.strip()]

    suites = list(FAST) + (FULL_EXTRA if args.tier == "full" else [])
    results, t0 = [], time.time()
    for name, cmd in suites:
        if any(k in name for k in skip):
            results.append((name, "SKIP", 0))
            print(f"[SKIP] {name}")
            continue
        print(f"\n===== {name} =====")
        t = time.time()
        p = subprocess.run(cmd, cwd=str(HERE))
        results.append((name, "PASS" if p.returncode == 0 else "FAIL",
                        round(time.time() - t, 1)))
        if p.returncode != 0:
            print(f"!!! {name} 失败 (exit={p.returncode})")

    n_ok = sum(1 for _, s, _ in results if s == "PASS")
    print(f"\n{'=' * 46}\n[总览] tier={args.tier}  "
          f"通过 {n_ok}/{len(results)}  用时 {round(time.time() - t0, 1)}s")
    for name, status, dur in results:
        print(f"  {status:<5} {name} ({dur}s)")
    print("[提示] 手动用例清单见 tests/TESTCASES.md")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
