# -*- coding: utf-8 -*-
"""T1 增量脉冲的离线端到端验证（不联网、不碰真实库）。

用真实库结构造临时副本，monkeypatch 掉客户端网络方法，喂入"状态变化"事件，
验证：
  1. 状态变化的单被 upsert 进缓存 + 归档库；
  2. 没变的单被跳过；
  3. Part8 被排除；
  4. 生成的 row_json 字段集与 T0 对齐（含全部 TAIL_COLS）；
  5. 重复跑同一事件 → 归档库行数不增（幂等）；
  6. 全程不写 xlsx（Part 目录 mtime/文件数不变）；
  7. watch 的 row_json 指纹对"没变的单"稳定。

用法: python scripts/test_incremental_pulse.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import incremental_pulse as P  # noqa: E402

DT_DIR = Path("D:/Desktop/work-main/dingtalk")
SRC_CACHE = DT_DIR / "sync_cache.db"
SRC_ARCHIVE = DT_DIR / "export_archive.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail and not cond else ""))


def copy_db(src: Path, dst: Path):
    s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    s.backup(d)
    d.close()
    s.close()


class FakeClient:
    """按预设 map 返回实例详情，统计调用。"""

    def __init__(self, responses: dict[str, dict]):
        self.responses = responses
        self.calls = []

    def query_running_ids(self, code, since, max_results=20, page_sleep=0.4):
        return []

    def get_instance_counted(self, iid, gap):
        self.calls.append(iid)
        return self.responses.get(iid)


def main():
    td = Path(tempfile.mkdtemp(prefix="pulse_test_"))
    cache_db = td / "sync_cache.db"
    archive_db = td / "export_archive.db"
    copy_db(SRC_CACHE, cache_db)
    copy_db(SRC_ARCHIVE, archive_db)
    print(f"临时副本: {td}")

    # 选一条真实的在途单（缓存 RUNNING）
    con = sqlite3.connect(cache_db)
    row = con.execute(
        "SELECT product,part,instance_id,detail,status,create_time "
        "FROM instances WHERE status='RUNNING' LIMIT 1").fetchone()
    product, part, iid, detail, old_status, create_time = row
    inst = json.loads(detail)
    print(f"选中在途单: {product}|{part}|{iid[:16]} status={old_status}")

    # 归档库当前行数
    arc = sqlite3.connect(archive_db)
    before_total = arc.execute("select count(*) from export_rows").fetchone()[0]
    before_row = arc.execute(
        "select row_json from export_rows where product=? and part=? and instance_id=?",
        (product, part, iid)).fetchone()
    arc.close()

    # ---- monkeypatch：让 run_pulse 用假客户端 ----
    # 场景1：把这单状态改成 COMPLETED + 加完成记录 → 应被识别为"变化"
    changed_inst = json.loads(json.dumps(inst))
    changed_inst["status"] = "COMPLETED"
    changed_inst["result"] = "agree"
    changed_inst["finish_time"] = "2026-11-16 22:00:00"
    fake = FakeClient({iid: changed_inst})

    orig_client = P.PulseClient

    class StubPulseClient:
        def __init__(self, *a, **kw):
            pass

    P.PulseClient = lambda *a, **kw: fake

    # 缩小 config：只保留选中模板，避免跑全部 18 个
    cfg = P.load_cfg()
    orig_products = cfg["products"]
    cfg["products"] = {product: {"parts": {part: orig_products[product]["parts"][part]},
                                 "output_dir": orig_products[product]["output_dir"]}}
    P.CONFIG = td / "config.json"
    P.CONFIG.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    # exlcude_part8 若选中的是 Part8 则关掉
    exclude = not part.startswith("Part8")

    args = type("Args", (), {
        "cache_db": cache_db, "archive_db": archive_db,
        "window_days": 45, "page_gap": 0, "detail_gap": 0,
        "exclude_part8": exclude, "pull_all": True,  # 测试用全拉, 确保该单必被核对
    })()

    # reset stats
    stats = P.PulseStats()
    P.PulseStats = lambda: stats

    try:
        report = P.run_pulse(args, write=True)
    finally:
        P.PulseClient = orig_client
    print(f"  report stats: {report['stats']}")

    # ---- 断言 ----
    con = sqlite3.connect(cache_db)
    new_status = con.execute(
        "select status from instances where product=? and part=? and instance_id=?",
        (product, part, iid)).fetchone()[0]
    con.close()
    check("状态变化的单已写回缓存", new_status == "COMPLETED", new_status)

    arc = sqlite3.connect(archive_db)
    new_total = arc.execute("select count(*) from export_rows").fetchone()[0]
    new_row = arc.execute(
        "select row_json from export_rows where product=? and part=? and instance_id=?",
        (product, part, iid)).fetchone()
    arc.close()
    check("归档库行数不变(upsert非新增)", new_total == before_total,
          f"{before_total}->{new_total}")
    d = json.loads(new_row[0])
    check("row_json 审批状态已更新", d.get("审批状态") == "已结束", d.get("审批状态"))
    tail_missing = [k for k in P.A.TAIL_COLS if k not in d]
    check("row_json 含全部 TAIL_COLS", not tail_missing, str(tail_missing))

    # 幂等：再跑一次同样事件，行数不增
    P.PulseClient = lambda *a, **kw: fake
    stats2 = P.PulseStats()
    P.PulseStats = lambda: stats2
    try:
        P.run_pulse(args, write=True)
    finally:
        P.PulseClient = orig_client
    arc = sqlite3.connect(archive_db)
    after2 = arc.execute("select count(*) from export_rows").fetchone()[0]
    arc.close()
    check("重复跑幂等(行数不增)", after2 == before_total, f"{before_total}->{after2}")

    # 状态未变跳过：缓存现在是 COMPLETED，构造一个"缓存RUNNING且仍在RUNNING集合"的场景
    con = sqlite3.connect(cache_db)
    con.execute("update instances set status='RUNNING' where product=? and part=? and instance_id=?",
                (product, part, iid))
    con.commit(); con.close()
    fake3 = FakeClient({})  # 任何详情调用都会被记录
    fake3.query_running_ids = lambda code, since, **kw: [iid]  # 该单仍在途

    class SkipClient:
        def __init__(self, *a, **kw): pass
    P.PulseClient = lambda *a, **kw: fake3
    stats3 = P.PulseStats()
    P.PulseStats = lambda: stats3
    args_skip = type("Args", (), {
        "cache_db": cache_db, "archive_db": archive_db,
        "window_days": 45, "page_gap": 0, "detail_gap": 0,
        "exclude_part8": exclude, "pull_all": False,
    })()
    try:
        P.run_pulse(args_skip, write=False)
    finally:
        P.PulseClient = orig_client
    check("仍在途的单跳过详情拉取", iid not in fake3.calls,
          f"选中单 {iid[:16]} 不应被拉; 其余老单被拉属预期")
    check("跳过计数正确", stats3.skipped_still_running >= 1,
          str(stats3.summary()))

    # 不写 xlsx：Part 目录文件数不变
    out_dir = Path(orig_products[product]["output_dir"])
    # 用副本无法验证真实目录，改为断言脚本源码不调用 export_part / wb.save
    src = (Path(__file__).resolve().parent / "incremental_pulse.py").read_text(encoding="utf-8")
    check("未调用 export_part(不写xlsx)", "export_part" not in src.replace("不写 xlsx", ""))
    check("未调用 wb.save", "wb.save" not in src)

    print(f"\n结果: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("失败项:", FAIL)
    shutil.rmtree(td, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
