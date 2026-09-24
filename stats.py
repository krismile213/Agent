#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stats.py — 运维报表(数据源: agent.db 遥测)

用法:
  python stats.py            # 近7天任务/成本/时延/工具健康
  python stats.py 1          # 近1天
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import storage  # noqa: E402


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    days = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 7
    db = storage.get_db()
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")

    rows = db.execute("SELECT status, COUNT(*) n, COALESCE(SUM(total_tokens),0) t "
                      "FROM tasks WHERE started>=? GROUP BY status", (since,)).fetchall()
    total = sum(r["n"] for r in rows) or 0
    ok = next((r["n"] for r in rows if r["status"] == "ok"), 0)
    toks = sum(r["t"] for r in rows)
    lats = [r["latency_ms"] for r in db.execute(
        "SELECT latency_ms FROM llm_calls WHERE ts>=? AND latency_ms>0 "
        "ORDER BY latency_ms", (since,)).fetchall()]
    print(f"══ 近{days}天 · 任务成功率 {ok}/{total}"
          f" ({(ok / total * 100 if total else 0):.0f}%) ══")
    for r in rows:
        print(f"  {r['status']:<10} {r['n']:>4} 个   tokens {r['t']:>9,}")
    print(f"  token合计 {toks:,}  (约 ¥{toks / 1_000_000 * 2:.2f} @flash价)")
    if lats:
        p95 = lats[min(int(len(lats) * 0.95), len(lats) - 1)]
        print(f"  LLM时延: 平均 {sum(lats)//len(lats)}ms / P95 {p95}ms / 共{len(lats)}次")

    print("══ 工具健康(成败/耗时) ══")
    for r in db.execute("SELECT tool, COUNT(*) n, SUM(ok) ok, AVG(duration_ms) d "
                        "FROM tool_calls WHERE ts>=? GROUP BY tool ORDER BY n DESC "
                        "LIMIT 12", (since,)):
        print(f"  {r['tool']:<18} {r['n']:>3}次  成功{r['ok']:>3}  均{int(r['d'] or 0)}ms")

    print("══ 近期异常任务 ══")
    bad = db.execute("SELECT started, session, status, substr(answer,1,60) a "
                     "FROM tasks WHERE started>=? AND status NOT IN ('ok') "
                     "ORDER BY id DESC LIMIT 8", (since,)).fetchall()
    bad2 = db.execute("SELECT started, session, status, substr(answer,1,60) a "
                      "FROM tasks WHERE status NOT IN ('ok') "
                      "ORDER BY id DESC LIMIT 8").fetchall()
    for r in (bad or bad2):
        print(f"  {r['started'][:16]} [{r['status']}] {r['session'][:20]}: {r['a']}")
    if not (bad or bad2):
        print("  (无)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
