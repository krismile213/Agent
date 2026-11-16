"""命令行入口。

用法示例
--------
    python -m watch --once                     # 扫一轮（首轮建基线）
    python -m watch --once --emit-baseline     # 首轮把全量标成 baseline 事件
    python -m watch --loop --interval 60       # 常驻，每 60s 一轮
    python -m watch --tail 20                  # 最近 20 条事件
    python -m watch --stats                    # 近 24h 统计
    python -m watch --reset --yes              # 清空监听自己的库
"""

from __future__ import annotations

import argparse
import sys

from .config import ALL_SOURCES, build_config, build_sources
from .engine import WatchEngine
from .store import EventStore


def _print_event(e, verbose: bool = False) -> None:
    line = e.summary() if verbose else e.summary()
    print("  " + line)
    if verbose:
        for c in e.changes[3:]:
            print(f"      · {c.field}: {c.old} → {c.new}")


def _cmd_tail(store: EventStore, n: int) -> int:
    rows = store.tail(n)
    if not rows:
        print("(还没有事件。先跑 python -m watch --once 建基线)")
        return 0
    print(f"最近 {len(rows)} 条事件（新的在上）:")
    for r in reversed(rows):
        ch = ", ".join(f"{c['field']}: {c['old']} → {c['new']}"
                       for c in r["changes"][:2])
        more = f" (+{len(r['changes']) - 2})" if len(r["changes"]) > 2 else ""
        print(f"  {r['detected_at']} [{r['source']}] {r['entity']} "
              f"{r['kind']} {ch}{more}")
    return 0


def _cmd_stats(store: EventStore, hours: int) -> int:
    s = store.stats(hours)
    print(f"近 {hours}h（起点 {s['since']}）")
    print(f"  事件按源: {s['events_by_source'] or '无'}")
    print(f"  事件按类型: {s['events_by_kind'] or '无'}")
    print(f"  基线行数: {s['state_rows'] or '无'}")
    run = s["last_run"]
    if run:
        started, finished, scanned, created, updated, deleted, skipped, error = run
        print(f"  最近一轮: {started} → {finished} scanned={scanned} "
              f"new={created} upd={updated} del={deleted}")
        if skipped:
            print(f"    跳过: {skipped}")
        if error:
            print(f"    异常: {error}")
    else:
        print("  最近一轮: 尚未运行")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="watch", description="联合后台实时监听（旁挂只读 POC）")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="只扫一轮")
    mode.add_argument("--loop", action="store_true", help="常驻轮询")
    mode.add_argument("--tail", type=int, metavar="N", help="看最近 N 条事件")
    mode.add_argument("--stats", action="store_true", help="看近 24h 统计")
    mode.add_argument("--reset", action="store_true", help="清空监听库（需 --yes）")

    ap.add_argument("--interval", type=float, default=None, help="轮询间隔秒，默认 60")
    ap.add_argument("--source", action="append", choices=list(ALL_SOURCES),
                    help="只启用指定源（可重复）")
    ap.add_argument("--root", help="work-main 根目录")
    ap.add_argument("--db", help="监听事件库路径")
    ap.add_argument("--platform", help="平台地址，默认 http://127.0.0.1:8000")
    ap.add_argument("--emit-baseline", action="store_true",
                    help="首轮把全量行标成 baseline 事件（默认不产事件）")
    ap.add_argument("--verbose", "-v", action="store_true", help="打印事件明细")
    ap.add_argument("--yes", "-y", action="store_true", help="--reset 的确认")
    ap.add_argument("--max-rounds", type=int, default=None, help="调试：最多跑几轮")
    args = ap.parse_args(argv)

    if args.reset and not args.yes:
        print("拒绝执行：--reset 会清空 watch.db 的事件与基线，加 --yes 确认。")
        return 2

    cfg = build_config(root=args.root, db=args.db, platform=args.platform,
                       interval=args.interval,
                       sources=tuple(args.source) if args.source else None)
    store = EventStore(cfg.db_path)

    if args.reset:
        store.reset()
        print(f"已清空监听库: {cfg.db_path}")
        store.close()
        return 0

    if args.tail is not None:
        rc = _cmd_tail(store, args.tail)
        store.close()
        return rc

    if args.stats:
        rc = _cmd_stats(store, 24)
        store.close()
        return rc

    if not (args.once or args.loop):
        ap.print_help()
        store.close()
        return 1

    sources = build_sources(cfg)
    engine = WatchEngine(store, sources, emit_baseline=args.emit_baseline)

    print(f"监听库: {cfg.db_path}")
    print(f"数据源: {', '.join(cfg.sources)}")
    print(f"work-main: {cfg.workmain_root}")

    def _on_result(res):
        print(res.line())
        for name, reason in res.skipped.items():
            print(f"  - 跳过 {name}: {reason}")
        for name, err in res.errors.items():
            print(f"  ! {name}: {err}")
        for e in res.events[:50]:
            _print_event(e, verbose=args.verbose)
        if len(res.events) > 50:
            print(f"  ... 其余 {len(res.events) - 50} 条已入库，用 --tail 查看")

    if args.once:
        res = engine.scan_once()
        _on_result(res)
        store.close()
        return 0

    print(f"开始轮询，间隔 {cfg.interval:.0f}s（Ctrl+C 停止）")
    try:
        engine.run_forever(cfg.interval, on_result=_on_result,
                           max_rounds=args.max_rounds)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
