#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/test_watch_core.py — 联合实时监听内核单测（零 LLM、零网络、零真实源写入）

钉住五件事：
  1. 指纹不含 updated_at：源库每轮把全表 updated_at 刷成同一时刻，不能误报
     「5095 行全变了」——这是本模块存在的首要理由。
  2. 字段级 diff：只报真变了的字段，old/new 可读且截断。
  3. 首轮只建基线不产事件；第二轮起新增/变更/删除各归其位。
  4. 幂等：同一快照连扫两次，第二次 0 事件、不重写 state。
  5. 只读红线：扫描前后源库 mtime/size 不变；坏源/离线源降级 skipped 不崩。

全部用临时库/临时目录，不碰生产归档库与 Part 文件。
"""

import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from watch.engine import WatchEngine                      # noqa: E402
from watch.fingerprint import (diff_fields, diff_snapshots,  # noqa: E402
                               fingerprint_row, normalize_row)
from watch.sources import (ExportRunsSource, HttpTransportSource,  # noqa: E402
                           PartFilesSource, SqliteRowsSource)
from watch.store import EventStore                        # noqa: E402

RESULTS = []
NOW = datetime(2026, 11, 16, 22, 0, 0)


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"  [{detail}]" if detail and not cond else ""))


# ---------------------------------------------------------------------------
# 1. 纯函数
# ---------------------------------------------------------------------------
def test_normalize():
    print("\n== normalize / fingerprint ==")
    row = normalize_row({
        "SKU": "  A-1  ",
        "备注": None,
        "更新时间": "2026-11-16T14:06:37",
        "updated_at": "2026-11-16T14:06:37",
        "嵌套": {"b": 2, "a": 1},
    })
    check("去首尾空白", row.get("SKU") == "A-1", row.get("SKU"))
    check("None → 空串", row.get("备注") == "", repr(row.get("备注")))
    check("剔除技术字段 updated_at", "updated_at" not in row, str(list(row)))
    # 实测（2026-11-16 归档库 5095 行）：「更新时间」有 4575 个不同取值，
    # 是钉钉单据的业务时间，不是同步刷的技术字段 —— 必须保留参与指纹。
    check("业务字段「更新时间」保留", "更新时间" in row, str(list(row)))
    check("嵌套 dict 稳定序列化", row.get("嵌套") == '{"a":1,"b":2}', row.get("嵌套"))

    bad = normalize_row(["x", "y"])
    check("非 dict 包成 __raw__ 不崩", bad.get("__raw__") == '["x","y"]', str(bad))


def test_fingerprint_stability():
    print("\n== 指纹稳定性（updated_at 不参与）==")
    a = normalize_row({"SKU": "A", "状态": "审批中", "updated_at": "2026-11-16T14:06:37"})
    b = normalize_row({"状态": "审批中", "SKU": "A", "updated_at": "2026-11-17T09:00:00"})
    check("键顺序不同 + updated_at 变了 → 同指纹",
          fingerprint_row(a) == fingerprint_row(b))
    c = normalize_row({"SKU": "A", "状态": "已完成"})
    check("业务字段变了 → 指纹变", fingerprint_row(a) != fingerprint_row(c))


def test_diff_fields():
    print("\n== 字段级 diff ==")
    old = {"SKU": "A", "状态": "审批中", "备注": "x" * 300}
    new = {"SKU": "A", "状态": "已完成", "备注": "y" * 300, "新增列": "1"}
    ch = diff_fields(old, new)
    fields = [c.field for c in ch]
    check("只报变化字段", set(fields) == {"状态", "备注", "新增列"}, str(fields))
    check("未变字段不出现", "SKU" not in fields, str(fields))
    check("超长值截断", all(len(c.old) <= 201 and len(c.new) <= 201 for c in ch),
          str([(c.field, len(c.old), len(c.new)) for c in ch]))


def test_diff_snapshots():
    print("\n== 快照 diff 三态 ==")
    old = {
        "k1": {"hash": "h1", "data": {"v": "1"}},
        "k2": {"hash": "h2", "data": {"v": "2"}},
    }
    new = {
        "k1": {"hash": "h1x", "data": {"v": "1改"}},
        "k3": {"hash": "h3", "data": {"v": "3"}},
    }
    out = diff_snapshots(old, new)
    kinds = {k: kind for k, kind, *_ in out}
    check("k1 → updated", kinds.get("k1") == "updated", str(kinds))
    check("k2 → deleted", kinds.get("k2") == "deleted", str(kinds))
    check("k3 → created", kinds.get("k3") == "created", str(kinds))
    check("updated 只带 1 个字段变化",
          len([x for x in out if x[0] == "k1"][0][2]) == 1)


# ---------------------------------------------------------------------------
# 2. 源库构造 + 端到端
# ---------------------------------------------------------------------------
def _make_source_db(path: Path):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE export_rows(
            product TEXT, part TEXT, instance_id TEXT,
            row_json TEXT, first_seen TEXT, updated_at TEXT);
        CREATE TABLE export_runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT, finished_at TEXT, product TEXT, part TEXT,
            rows_total INTEGER, rows_new INTEGER, rows_updated INTEGER);
    """)
    import json
    rows = [
        ("手机膜", "Part1_产品开发", "i1",
         json.dumps({"SKU": "A", "状态": "审批中"}, ensure_ascii=False)),
        ("手机膜", "Part1_产品开发", "i2",
         json.dumps({"SKU": "B", "状态": "已完成"}, ensure_ascii=False)),
    ]
    con.executemany(
        "INSERT INTO export_rows VALUES(?,?,?,?,?,?)",
        [(p, part, iid, rj, "2026-09-21T16:26:30", "2026-11-16T14:06:37")
         for p, part, iid, rj in rows])
    con.execute(
        "INSERT INTO export_runs(started_at,finished_at,product,part,rows_total,"
        "rows_new,rows_updated) VALUES(?,?,?,?,?,?,?)",
        ("2026-11-16T13:59:03", "2026-11-16T14:07:15", "手机膜",
         "Part1_产品开发", 2, 0, 2))
    con.commit()
    con.close()


def _bump_updated_at(db: Path, ts: str = "2026-11-16T22:00:00"):
    """模拟同步：全表 updated_at 刷成同一时刻（业务字段可能没变）。"""
    con = sqlite3.connect(db)
    con.execute("UPDATE export_rows SET updated_at=?", (ts,))
    con.commit()
    con.close()


def test_end_to_end():
    print("\n== 端到端：基线 → 变更 → 幂等 → 新增/删除 ==")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "src.db"
        _make_source_db(src)
        store = EventStore(td / "watch.db")
        engine = WatchEngine(store, [SqliteRowsSource(src), ExportRunsSource(src)],
                             clock=lambda: NOW)

        before = (src.stat().st_mtime_ns, src.stat().st_size)
        r1 = engine.scan_once()
        after = (src.stat().st_mtime_ns, src.stat().st_size)

        check("首轮只建基线，不产事件", not r1.events and len(r1.baseline) == 2,
              f"events={len(r1.events)} baseline={r1.baseline}")
        check("首轮扫到 2 行 + 1 轮批次", r1.scanned == 3, str(r1.scanned))
        check("扫描不改源库（mtime/size 不变）", before == after, f"{before} → {after}")

        # 只刷 updated_at，业务字段不动 → 必须 0 事件
        _bump_updated_at(src)
        r2 = engine.scan_once()
        check("全表刷 updated_at → 0 事件（不误报）", not r2.events,
              str([e.summary() for e in r2.events]))

        # 真改一个业务字段 + 新增一行 + 删一行
        con = sqlite3.connect(src)
        con.execute("UPDATE export_rows SET row_json=? WHERE instance_id='i1'",
                    ('{"SKU":"A","状态":"已完成"}',))
        con.execute("INSERT INTO export_rows VALUES(?,?,?,?,?,?)",
                    ("手机膜", "Part1_产品开发", "i3",
                     '{"SKU":"C","状态":"审批中"}',
                     "2026-11-16T22:00:00", "2026-11-16T22:00:00"))
        con.execute("DELETE FROM export_rows WHERE instance_id='i2'")
        con.commit()
        con.close()

        r3 = engine.scan_once()
        by_entity = {e.entity: e for e in r3.events}
        check("业务字段变更 → 1 条 updated",
              by_entity.get("手机膜|i1") is not None
              and by_entity["手机膜|i1"].kind == "updated", str(list(by_entity)))
        ch = by_entity["手机膜|i1"].changes
        check("changes 只含「状态」", [c.field for c in ch] == ["状态"],
              str([c.field for c in ch]))
        check("old/new 正确", ch and ch[0].old == "审批中" and ch[0].new == "已完成",
              str(ch))
        check("新增行 → created", by_entity.get("手机膜|i3") is not None
              and by_entity["手机膜|i3"].kind == "created", str(list(by_entity)))
        check("删除行 → deleted", by_entity.get("手机膜|i2") is not None
              and by_entity["手机膜|i2"].kind == "deleted", str(list(by_entity)))

        r4 = engine.scan_once()
        check("同快照再扫一次 → 幂等 0 事件", not r4.events,
              str([e.summary() for e in r4.events]))
        check("事件已落库且可 tail", len(store.tail(20)) >= 3,
              str(len(store.tail(20))))
        store.close()


def test_sync_finished():
    print("\n== 一轮同步 → 一条 sync_finished ==")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "src.db"
        _make_source_db(src)
        store = EventStore(td / "watch.db")
        engine = WatchEngine(store, [ExportRunsSource(src)], clock=lambda: NOW)
        engine.scan_once()

        # 同一轮同步：started_at 相同、finished_at 各异（真实形态）
        con = sqlite3.connect(src)
        con.executemany(
            "INSERT INTO export_runs(started_at,finished_at,product,part,"
            "rows_total,rows_new,rows_updated) VALUES(?,?,?,?,?,?,?)",
            [("2026-11-16T21:00:00", "2026-11-16T21:07:10", "手机膜",
              "Part1_产品开发", 5, 1, 4),
             ("2026-11-16T21:00:00", "2026-11-16T21:07:12", "手机膜",
              "Part5_新品渠道仓发货", 7, 2, 5)])
        con.commit()
        con.close()

        r = engine.scan_once()
        check("一轮同步聚合为 1 条事件", len(r.events) == 1,
              str([e.summary() for e in r.events]))
        e = r.events[0] if r.events else None
        check("kind = sync_finished", e is not None and e.kind == "sync_finished",
              getattr(e, "kind", "-"))
        check("part_count 聚合为 2",
              e is not None and e.payload.get("part_count") == "2",
              str(getattr(e, "payload", {})))
        store.close()


# ---------------------------------------------------------------------------
# 3. 文件源
# ---------------------------------------------------------------------------
def test_part_files():
    print("\n== Part 文件源：品类隔离 + mtime 变化 ==")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for cat in ("手机膜", "手机壳"):
            d = root / "data" / cat / cat
            d.mkdir(parents=True)
            (d / f"Part1_产品开发-2026111614{0 if cat == '手机膜' else 1}0000.xlsx"
             ).write_bytes(b"x" * 10)

        store = EventStore(root / "watch.db")
        src = PartFilesSource(root)
        engine = WatchEngine(store, [src], clock=lambda: NOW)
        r1 = engine.scan_once()
        check("两个品类各 1 个 Part1（key 带品类，不串号）", len(r1.baseline) == 1
              and r1.scanned == 2, f"scanned={r1.scanned}")

        keys = set(store.load_state("part_files").keys())
        check("key 含品类前缀",
              keys == {"手机膜|Part1_产品开发", "手机壳|Part1_产品开发"}, str(keys))

        # 重写手机膜那份（mtime 变）→ 只该有 1 条 updated
        p = next((root / "data" / "手机膜" / "手机膜").glob("*.xlsx"))
        os.utime(p, (NOW.timestamp() + 60, NOW.timestamp() + 60))
        r2 = engine.scan_once()
        check("mtime 变化 → 1 条事件", len(r2.events) == 1,
              str([e.summary() for e in r2.events]))
        check("事件实体是手机膜", r2.events and r2.events[0].entity.startswith("手机膜|"),
              r2.events[0].entity if r2.events else "-")
        store.close()          # Windows：临时目录清理前必须释放 sqlite 句柄


# ---------------------------------------------------------------------------
# 4. 降级
# ---------------------------------------------------------------------------
def test_degrade():
    print("\n== 坏源降级 ==")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        store = EventStore(td / "watch.db")
        engine = WatchEngine(store, [
            SqliteRowsSource(td / "not-exist.db"),
            PartFilesSource(td / "no-such-dir"),
            HttpTransportSource("http://127.0.0.1:1", timeout=0.3),
        ], clock=lambda: NOW)
        r = engine.scan_once()
        check("三个坏源全部 skipped 不崩", len(r.skipped) == 3 and not r.errors,
              f"skipped={list(r.skipped)} errors={r.errors}")
        check("skipped 有可读原因", all(v for v in r.skipped.values()),
              str(r.skipped))
        check("整轮仍记录审计", store.stats(24)["last_run"] is not None)
        store.close()


def test_emit_baseline():
    print("\n== --emit-baseline ==")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "src.db"
        _make_source_db(src)
        store = EventStore(td / "watch.db")
        engine = WatchEngine(store, [SqliteRowsSource(src)], clock=lambda: NOW,
                             emit_baseline=True)
        r = engine.scan_once()
        check("基线事件 kind=baseline", r.events and all(
            e.kind == "baseline" for e in r.events), str(r.counts))
        check("基线事件数 = 行数", len(r.events) == 2, str(len(r.events)))
        store.close()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    test_normalize()
    test_fingerprint_stability()
    test_diff_fields()
    test_diff_snapshots()
    test_end_to_end()
    test_sync_finished()
    test_part_files()
    test_degrade()
    test_emit_baseline()
    n_ok = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{'=' * 46}\n[联合监听内核单测] {n_ok}/{len(RESULTS)} 通过")
    return 0 if n_ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
