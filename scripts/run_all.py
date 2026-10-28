#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/run_all.py — 一键跑全部测试

分层:
  --tier fast  (默认, 不耗token): selftest + 全部单测
  --tier full  (耗token, ~15分钟): fast + MCP双向 + Web冒烟 + 升级E2E +
                                   高级能力E2E + 金标准评测(23用例)
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
    ("安全拦截单测", [sys.executable, "scripts/test_security.py", "--unit-only"]),
    ("人工干预三件套", [sys.executable, "scripts/test_human_intervention.py"]),
    ("research防幻觉防线", [sys.executable, "scripts/test_research_guard.py"]),
    ("数据新鲜度防线", [sys.executable, "scripts/test_freshness.py"]),
    ("失败模式复盘单测", [sys.executable, "scripts/test_session_analysis.py"]),
    ("空简报防线", [sys.executable, "scripts/test_brief_guard.py"]),
    ("简报格式兜底", [sys.executable, "scripts/test_brief_format.py"]),
    ("钉钉推送换行/分片", [sys.executable, "scripts/test_dingtalk_format.py"]),
    ("Web新增端点", [sys.executable, "scripts/test_web_features.py"]),
    ("Web前端装配防线", ["node", "scripts/test_web_ui.js"]),
    ("分层记忆+断点续跑", [sys.executable, "scripts/test_memory_ckpt.py"]),
    ("登录鉴权+多用户", [sys.executable, "scripts/test_auth.py"]),
    ("平台上下文注入", [sys.executable, "scripts/test_plat_context.py"]),
    ("平台只读工具", [sys.executable, "scripts/test_platform_tools.py"]),
    ("事中日报本地只读", [sys.executable, "scripts/test_inflight_report.py"]),
    ("Web渲染器", ["node", "scripts/test_web_md.js"]),
    ("企业化三件套单测", [sys.executable, "scripts/test_enterprise.py", "--unit-only"]),
    ("RAG检索质量", [sys.executable, "scripts/test_rag.py"]),
    ("任务契约四件套", [sys.executable, "scripts/test_task_contract.py"]),
]
FULL_EXTRA = [
    ("文件接口", [sys.executable, "scripts/test_files.py"]),
    ("安全网与转向", [sys.executable, "scripts/test_safety.py"]),
    ("注入防御E2E", [sys.executable, "scripts/test_security.py", "--e2e-only"]),
    ("企业化E2E(metrics)", [sys.executable, "scripts/test_enterprise.py", "--e2e-only"]),
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
        # 子进程输出一律落文件再转发, 不走管道: (1) 宿主的 safe-delete shim 在
        # 管道/特殊 stdout 下会把正常退出改写成 SystemExit(1), 秒挂一片; (2) 输出
        # 落盘后失败时能直接回看现场, 不用重跑。
        import tempfile
        with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as fh:
            p = subprocess.run(cmd, cwd=str(HERE), stdout=fh,
                               stderr=subprocess.STDOUT)
            fh.seek(0)
            sys.stdout.write(fh.read())
        dur = round(time.time() - t, 1)
        results.append((name, "PASS" if p.returncode == 0 else "FAIL", dur))
        if p.returncode != 0:
            # 一定要带耗时: 正常套件不可能 2 秒就失败, 秒挂基本是"根本没跑起来"
            # (解释器找不到 / 依赖缺失 / 输出重定向踩到宿主的 shim), 和业务失败
            # 是两类问题, 分开看才判得准。
            print(f"!!! {name} 失败 (exit={p.returncode}, {dur}s)")

    n_ok = sum(1 for _, s, _ in results if s == "PASS")
    print(f"\n{'=' * 46}\n[总览] tier={args.tier}  "
          f"通过 {n_ok}/{len(results)}  用时 {round(time.time() - t0, 1)}s")
    for name, status, dur in results:
        print(f"  {status:<5} {name} ({dur}s)")
    print("[提示] 手动用例清单见 tests/TESTCASES.md")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
