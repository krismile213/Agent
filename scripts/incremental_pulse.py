# -*- coding: utf-8 -*-
"""T1 增量脉冲：只拉状态变化的在途单，写归档库，不碰 xlsx / 同步核心。

分层同步框架 docs/分层同步框架.md 的 T1 实现（P2）。设计纪律：
  1. 增量集 = 新版 API(45天窗, statuses=RUNNING) ∪ 缓存在途(status!=COMPLETED)
     —— P1 实测 45 天窗只覆盖 1/4 在途，缺口在 Part8/9 老单，必须取并集。
  2. 只拉"远端状态与本地缓存不同"的单详情（跳过没变的），把调用量压到最小。
  3. 只写 sync_cache.db(详情) 与 export_archive.db(行)，绝不写 Part*.xlsx。
  4. 复用 approval_sync 的 instance_to_row/archive_part/Cache —— 口径零漂移，
     不改 approval_sync.py 一行。
  5. Part8 默认排除（分流给平台 /api/transport）；QPS 退避 + 模板间隔 + 整点规避。
  6. 默认 dry-run，必须 --live 才写库；支持 --cache-db/--archive-db 指向副本做验证。

用法（网络 + 用户站点 requests/openpyxl）：
  python scripts/incremental_pulse.py --dry                      # 只算增量集, 不拉详情不写库
  python scripts/incremental_pulse.py --live                     # 真跑
  python scripts/incremental_pulse.py --live \
      --cache-db <副本>/sync_cache.db --archive-db <副本>/export_archive.db
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
import time
from pathlib import Path

# work-main/dingtalk 目录：复用 approval_sync / dingtalk_client 的现成实现
DT_DIR = Path("D:/Desktop/work-main/dingtalk")
if str(DT_DIR) not in sys.path:
    sys.path.insert(0, str(DT_DIR))

import approval_sync as A          # noqa: E402  (instance_to_row / archive_part / Cache / 常量)
from dingtalk_client import DingTalkClient, DingTalkError  # noqa: E402

DEFAULT_CACHE = DT_DIR / "sync_cache.db"
DEFAULT_ARCHIVE = DT_DIR / "export_archive.db"
CONFIG = DT_DIR / "config.json"

TZ = dt.timezone(dt.timedelta(hours=8))


def now_str() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_cfg() -> dict:
    with open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


def to_ms(d: dt.datetime) -> int:
    return int(d.replace(tzinfo=TZ).timestamp() * 1000)


def qps_safe_sleep(sec: float):
    if sec:
        time.sleep(sec)


class PulseStats:
    def __init__(self):
        self.api_calls = 0
        self.qps_hits = 0
        self.candidates = 0
        self.pulled = 0
        self.skipped_still_running = 0  # 仍在途+缓存也是在途 → 无需拉详情
        self.skipped_unchanged = 0
        self.changed = 0
        self.excluded_part8 = 0
        self.started = dt.datetime.now()

    def summary(self) -> dict:
        return {
            "duration_sec": round((dt.datetime.now() - self.started).total_seconds(), 1),
            "api_calls": self.api_calls,
            "qps_hits": self.qps_hits,
            "candidates": self.candidates,
            "excluded_part8": self.excluded_part8,
            "pulled_detail": self.pulled,
            "skipped_still_running": self.skipped_still_running,
            "skipped_unchanged": self.skipped_unchanged,
            "changed_written": self.changed,
        }


class PulseClient(DingTalkClient):
    """在现成客户端上加 QPS 命中计数与新版 query 的状态过滤封装。"""

    def __init__(self, app_key, app_secret, stats: PulseStats,
                 logger=lambda *a, **k: None, timeout=20):
        super().__init__(app_key, app_secret, interval_sec=0,
                         logger=logger, timeout=timeout)
        self.stats = stats
        self._s = self._session  # 复用父类 session

    def query_running_ids(self, process_code: str, since: dt.datetime,
                          max_results: int = 20, page_sleep: float = 0.4) -> list[str]:
        """新版 query 拉 RUNNING ID（翻到底）。QPS 命中则退避重试本页。"""
        self.token()
        self.stats.api_calls += 1
        ids: list[str] = []
        next_token = 0
        start_ms, end_ms = to_ms(since), to_ms(dt.datetime.now())
        while True:
            body = {"processCode": process_code, "startTime": start_ms,
                    "endTime": end_ms, "nextToken": next_token,
                    "maxResults": max_results, "statuses": ["RUNNING"]}
            qps_safe_sleep(page_sleep)
            try:
                resp = self._s.post(
                    self.NEW_BASE + "/v1.0/workflow/processes/instanceIds/query",
                    headers={"x-acs-dingtalk-access-token": self._token,
                             "Content-Type": "application/json"},
                    json=body, timeout=self.timeout)
                j = resp.json()
            except Exception as e:
                A.log(f"[pulse][warn] query 网络异常: {e}")
                break
            self.stats.api_calls += 1
            if not j.get("success"):
                code = str(j.get("code") or "")
                if "Qps" in code or "Throttling" in code:
                    self.stats.qps_hits += 1
                    A.log(f"[pulse][warn] QPS 限流, 退避 1.2s 重试")
                    qps_safe_sleep(1.2)
                    continue
                A.log(f"[pulse][warn] query 失败 {code}: {str(j.get('message'))[:80]}")
                break
            result = j.get("result") or {}
            batch = result.get("list") or []
            ids.extend(batch)
            next_token = result.get("nextToken")
            if not batch or not next_token:
                break
        return ids

    def get_instance_counted(self, iid: str, gap: float) -> dict | None:
        """老版 get，带 QPS 退避；返回 process_instance dict 或 None。"""
        self.token()
        while True:
            qps_safe_sleep(gap)
            try:
                inst = self.get_instance(iid)
                self.stats.api_calls += 1
                return inst
            except DingTalkError as e:
                msg = str(e)
                if "Qps" in msg or "Throttl" in msg or "900" in msg:
                    self.stats.qps_hits += 1
                    qps_safe_sleep(1.2)
                    continue
                A.log(f"[pulse][warn] get 失败 {iid[:16]}: {msg[:80]}")
                return None


def build_candidates(cache: A.Cache, cfg: dict, product: str, part: str,
                     code: str, running_ids: list[str], window_days: int,
                     exclude_part8: bool, stats: PulseStats) -> list[str]:
    """增量集 = 新版 RUNNING ∪ 缓存在途；Part8 可排除。返回待核对的 ID 列表。"""
    if exclude_part8 and part.startswith("Part8"):
        stats.excluded_part8 += cache.count(product, part)
        return []
    local_unfinished = set(cache.unfinished_ids(product, part))
    cand = set(running_ids) | local_unfinished
    stats.candidates += len(cand)
    return sorted(cand)


def node_map_for(cache: A.Cache, code: str) -> dict:
    return cache.load_nodes(code) if code else {}


def run_pulse(args, write: bool) -> dict:
    cfg = load_cfg()
    stats = PulseStats()
    live = write

    cache = A.Cache(str(args.cache_db))
    archive = A.archive_connect(str(args.archive_db)) if live else None

    # 间隔由分页/详情各自控制, 父类 interval_sec=0; 静默客户端日志, 用 A.log 统一输出
    client = PulseClient(cfg["app_key"], cfg["app_secret"], stats=stats)

    since = dt.datetime.now() - dt.timedelta(days=args.window_days)
    report = {"generated_at": now_str(), "mode": "LIVE" if live else "DRY",
              "window_days": args.window_days, "exclude_part8": args.exclude_part8,
              "per_part": {}, "stats": {}}

    for product, meta in cfg["products"].items():
        parts = meta["parts"]
        for part, code in parts.items():
            # 1) 新版 API 拉 45 天窗 RUNNING
            running = client.query_running_ids(code, since, page_sleep=args.page_gap)
            # 2) 构造增量集
            cand = build_candidates(cache, cfg, product, part, code, running,
                                    args.window_days, args.exclude_part8, stats)
            if not cand:
                report["per_part"][f"{product}|{part}"] = {"candidates": 0}
                continue

            # 3) 逐单：只拉"疑似变化"的单详情
            #    P1 已验证 statuses=RUNNING 反映"此刻确实在途"(反查 5/5 全对)。
            #    因此: 缓存是在途 AND 仍在新版 RUNNING 集合 → 状态没变, 跳过详情;
            #    只有"从 RUNNING 集合消失"或"缓存没有"的单才拉详情确认。
            #    注意: 45天窗外的老在途单(Part8/9)不在 RUNNING 集合, 会被拉详情 —
            #    这是必要的, 因为老单突然办完正是用户要抓的场景。
            nodes = node_map_for(cache, code)
            changed_items = []       # (iid, inst) 状态真的变了
            running_set = set(running)
            for iid in cand:
                row = cache.conn.execute(
                    "SELECT status FROM instances "
                    "WHERE product=? AND part=? AND instance_id=?",
                    (product, part, iid)).fetchone()
                local_status = row[0] if row else None
                if local_status == "COMPLETED" and iid not in running_set:
                    continue  # 缓存已完成且远端也不在途 → 稳定, 跳过
                if (not args.pull_all and local_status in ("RUNNING", "NEW")
                        and iid in running_set):
                    # 仍在途 + 缓存也是在途 → 状态未变, 跳过 (其他字段由 T0 兜底)
                    stats.skipped_still_running += 1
                    continue
                inst = client.get_instance_counted(iid, args.detail_gap)
                if not inst:
                    continue
                stats.pulled += 1
                remote_status = inst.get("status", "")
                if remote_status == local_status:
                    stats.skipped_unchanged += 1
                    continue
                # 状态确实变了 → 准备写
                stats.changed += 1
                changed_items.append((iid, inst))

            entry = {"candidates": len(cand), "running_api": len(running),
                     "changed": len(changed_items),
                     "ids": [x[0][:16] for x in changed_items[:10]]}
            report["per_part"][f"{product}|{part}"] = entry

            # 4) 写入（LIVE）：先 upsert 缓存(姓名/节点映射可用), 再复用 instance_to_row + archive_part
            if live and changed_items:
                rows_built = []
                data = []
                for iid, inst in changed_items:
                    create_time = inst.get("create_time", "")
                    remote_status = inst.get("status", "")
                    detail = json.dumps(inst, ensure_ascii=False)
                    cache.upsert_instance(product, part, iid, create_time,
                                          remote_status, detail)
                    name_of = lambda u: cache.get_user(u) or (u or "")
                    form, tail, warn = A.instance_to_row(
                        inst, name_of, corp_name=cfg.get("corp_name", ""),
                        nodes=nodes, cc_actors=cache.load_cc_actors(iid))
                    rows_built.append((form, tail))
                    data.append((iid, inst))
                # 列顺序：沿用归档库已有列的顺序（从任一行 row_json 取），新增列追加
                cols = _archive_cols(archive, product, part, rows_built)
                fresh, upd = A.archive_part(product, part, data, rows_built, cols,
                                            archive, run_started=now_str())
                cache.commit()
                entry["archived_fresh"] = fresh
                entry["archived_updated"] = upd

    if archive:
        archive.close()
    report["stats"] = stats.summary()
    return report


def _archive_cols(archive, product: str, part: str, rows_built: list) -> list[str]:
    """从归档库现有行推出列顺序；无历史行则用本轮表单键的超集。"""
    row = archive.execute(
        "SELECT row_json FROM export_rows WHERE product=? AND part=? LIMIT 1",
        (product, part)).fetchone()
    cols: list[str] = []
    if row:
        d = json.loads(row[0])
        cols = [k for k in d.keys() if k not in A.TAIL_COLS]
    seen = set(cols)
    for form, _ in rows_built:
        for k in form:
            if k not in seen:
                cols.append(k)
                seen.add(k)
    return cols


def guard_integrity_hour() -> bool:
    """整点前后 1 分钟不跑（钉钉 QPS 建议错开整点）。返回 True 表示应跳过。"""
    m = dt.datetime.now().minute
    return m >= 59 or m <= 1


def main() -> int:
    ap = argparse.ArgumentParser(description="T1 增量脉冲（只写归档库, 不写 xlsx）")
    ap.add_argument("--live", action="store_true", help="真正写库; 默认 dry-run")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--window-days", type=int, default=45)
    ap.add_argument("--page-gap", type=float, default=0.4, help="ID 分页间隔秒")
    ap.add_argument("--detail-gap", type=float, default=0.3, help="详情拉取间隔秒")
    ap.add_argument("--exclude-part8", action="store_true", default=True,
                    help="排除 Part8 (默认排除, 分流给平台 transport)")
    ap.add_argument("--include-part8", action="store_true", help="包含 Part8")
    ap.add_argument("--cache-db", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--archive-db", type=Path, default=DEFAULT_ARCHIVE)
    ap.add_argument("--pull-all", action="store_true",
                    help="拉取全部在途单详情做完整对账(关闭 RUNNING 集合跳过优化)")
    ap.add_argument("--ignore-hour-guard", action="store_true")
    args = ap.parse_args()
    if args.include_part8:
        args.exclude_part8 = False

    if guard_integrity_hour() and not args.ignore_hour_guard:
        A.log("[pulse] 整点前后 1 分钟, 本轮跳过（加 --ignore-hour-guard 强制）")
        return 0

    write = args.live and not args.dry
    A.log(f"[pulse] 启动 mode={'LIVE' if write else 'DRY'} "
          f"cache={args.cache_db} archive={args.archive_db}")
    report = run_pulse(args, write)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
