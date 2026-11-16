"""数据源采集器：只读、失败降级为 skipped，绝不让一轮扫描崩掉。

第一版四个源：
  sqlite_rows   —— 钉钉归档库 export_rows，行级业务字段（主力）
  export_runs   —— 同一库的同步批次，按 started_at 聚合成「一轮同步」事件
  part_files    —— data/**/Part*.xlsx 的 mtime/size（快照产出信号）
  http_transport—— 平台 /api/transport（:8000 没起时自动 skipped，可选）

统一契约：`snapshot() -> Snapshot`
  rows[key] = {"hash": sha1, "data": {字段: 值}, ...源私有字段(part/product)}
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from .fingerprint import fingerprint_row, normalize_row
from .model import Snapshot


def _ro_connect(db: Path, timeout: float = 5.0) -> sqlite3.Connection:
    """只读打开 SQLite（WAL 下也能读到已提交数据）。

    * `mode=ro` 保证不写源库；
    * busy_timeout 让并发同步写库时等一会儿，而不是立刻抛 locked。
    """
    con = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True,
                          timeout=timeout)
    con.execute("PRAGMA busy_timeout=3000")
    return con


def _iso_from_mtime(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S")


class SqliteRowsSource:
    """export_rows 行级监听。

    主键 = `product|instance_id`（instance_id 是钉钉实例 id，全局唯一；
    加 product 是为了防跨品类串号）。
    """

    name = "sqlite_rows"

    def __init__(self, db: Path, table: str = "export_rows"):
        self.db = Path(db)
        self.table = table

    def snapshot(self) -> Snapshot:
        if not self.db.is_file():
            return Snapshot(self.name, skipped=True, reason=f"库不存在: {self.db}")
        try:
            con = _ro_connect(self.db)
            try:
                cur = con.execute(
                    f"SELECT product, part, instance_id, row_json FROM {self.table}")
                rows: dict[str, dict[str, Any]] = {}
                dup = bad_json = 0
                for product, part, iid, row_json in cur:
                    key = f"{product or ''}|{iid or ''}"
                    try:
                        raw = json.loads(row_json) if row_json else {}
                    except (TypeError, ValueError):
                        raw = {"__raw__": row_json}
                        bad_json += 1
                    data = normalize_row(raw)
                    if key in rows:
                        dup += 1
                    rows[key] = {
                        "hash": fingerprint_row(data),
                        "data": data,
                        "part": str(part or ""),
                        "product": str(product or ""),
                    }
            finally:
                con.close()
        except sqlite3.Error as e:
            return Snapshot(self.name, skipped=True, reason=f"sqlite: {e}")

        return Snapshot(self.name, rows=rows, meta={
            "dup_keys": dup, "bad_json": bad_json, "db": str(self.db),
        })


class ExportRunsSource:
    """同步批次监听：把同一 `started_at` 的多个 Part 任务聚成「一轮同步」。

    一轮同步 = 10 个左右 Part 任务共用同一个 started_at，各自 finished_at 不同。
    按 started_at 聚合后，一轮只产一条事件，不会刷屏。
    """

    name = "export_runs"

    def __init__(self, db: Path, limit: int = 40):
        self.db = Path(db)
        self.limit = limit

    def snapshot(self) -> Snapshot:
        if not self.db.is_file():
            return Snapshot(self.name, skipped=True, reason=f"库不存在: {self.db}")
        sql = """
            SELECT started_at,
                   MIN(finished_at)  AS first_finished,
                   MAX(finished_at)  AS last_finished,
                   COUNT(*)          AS part_count,
                   SUM(rows_total)   AS rows_total,
                   SUM(rows_new)     AS rows_new,
                   SUM(rows_updated) AS rows_updated,
                   GROUP_CONCAT(part, '|') AS parts
              FROM export_runs
             GROUP BY started_at
             ORDER BY started_at DESC
             LIMIT ?
        """
        try:
            con = _ro_connect(self.db)
            try:
                cur = con.execute(sql, (self.limit,))
                rows: dict[str, dict[str, Any]] = {}
                latest = ""
                for (started, first_fin, last_fin, pc, rt, rn, ru, parts) in cur:
                    key = str(started or "")
                    if not key:
                        continue
                    data = normalize_row({
                        "started_at": started,
                        "first_finished": first_fin,
                        "last_finished": last_fin,
                        "part_count": pc,
                        "rows_total": rt,
                        "rows_new": rn,
                        "rows_updated": ru,
                        # 排序后拼接，避免 GROUP_CONCAT 顺序抖动造成假变更
                        "parts": ",".join(sorted(set(str(parts or "").split("|")))),
                    })
                    rows[key] = {"hash": fingerprint_row(data), "data": data}
                    latest = max(latest, str(last_fin or ""))
            finally:
                con.close()
        except sqlite3.Error as e:
            return Snapshot(self.name, skipped=True, reason=f"sqlite: {e}")

        return Snapshot(self.name, rows=rows, meta={"latest_finished": latest})


class PartFilesSource:
    """Part*.xlsx 快照文件监听。

    实况目录是 `data/<品类>/<品类>/Part*.xlsx`（两层品类目录）。
    ⚠️ key 必须带品类：手机膜和手机壳都有 `Part1_产品开发`，
    只用环节名做 key 会让两个品类互相覆盖（2026-11-16 实测踩到）。
    """

    name = "part_files"

    def __init__(self, root: Path, pattern: str = "data/*/*/Part*.xlsx"):
        self.root = Path(root)
        self.pattern = pattern

    def _latest_per_part(self) -> dict[str, tuple[str, Path]]:
        """每个 (品类, 环节) 只保留 mtime 最新一份（同环节新旧并存时取新）。"""
        best: dict[str, tuple[float, str, Path]] = {}
        for p in self.root.glob(self.pattern):
            if p.name.startswith("~$") or not p.is_file():
                continue
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            rel = p.relative_to(self.root).parts
            category = rel[-2] if len(rel) >= 2 else ""
            stem = p.stem
            part = stem.rsplit("-", 1)[0] if "-" in stem else stem
            key = f"{category}|{part}"
            if key not in best or mt >= best[key][0]:
                best[key] = (mt, category, p)
        return {k: (v[1], v[2]) for k, v in best.items()}

    def snapshot(self) -> Snapshot:
        if not self.root.is_dir():
            return Snapshot(self.name, skipped=True,
                            reason=f"目录不存在: {self.root}")
        rows: dict[str, dict[str, Any]] = {}
        try:
            for key, (category, p) in self._latest_per_part().items():
                st = p.stat()
                data = normalize_row({
                    "category": category,
                    "file": p.name,
                    "batch": p.parent.name,
                    "mtime": _iso_from_mtime(st.st_mtime),
                    "size": st.st_size,
                })
                rows[key] = {"hash": fingerprint_row(data), "data": data}
        except OSError as e:
            return Snapshot(self.name, skipped=True, reason=f"os: {e}")
        return Snapshot(self.name, rows=rows, meta={"root": str(self.root)})


class HttpTransportSource:
    """平台 /api/transport 货件级监听（可选源）。

    :8000 没启动时返回 skipped —— 不是错误，实时监听允许部分源离线。
    逐票 key = 货件号；无货件号的占位票用 `hub|i` 兜底。
    """

    name = "http_transport"

    def __init__(self, base_url: str, timeout: float = 6.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get_json(self, path: str) -> Any:
        # 本机服务，绕过系统代理（Clash 开着时 127.0.0.1 也不该走代理）
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"{self.base_url}{path}", timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def snapshot(self) -> Snapshot:
        try:
            payload = self._get_json("/api/transport")
        except (URLError, OSError, ValueError, TimeoutError) as e:
            return Snapshot(self.name, skipped=True,
                            reason=f"接口不可用: {type(e).__name__}: {e}")

        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return Snapshot(self.name, skipped=True, reason="返回结构缺 items")

        rows: dict[str, dict[str, Any]] = {}
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            trk = str(it.get("trk") or it.get("货件号") or "").strip()
            hub = str(it.get("hub") or it.get("渠道仓") or "").strip()
            key = trk or f"{hub}|{i}"
            data = normalize_row({
                "hub": hub,
                "ship": it.get("ship") or "",
                "arr": it.get("arr") or "",
                "days": it.get("days"),
                "target": it.get("target"),
                "state": it.get("state") or "",
            })
            rows[key] = {"hash": fingerprint_row(data), "data": data}
        kpi = payload.get("kpi") if isinstance(payload, dict) else None
        return Snapshot(self.name, rows=rows, meta={"kpi": kpi or {}})
