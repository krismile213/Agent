"""事件库：只写 `watch/watch.db`，与源库物理隔离。

三张表：
  events —— 检测到的事实（字段级 changes 存 JSON）
  state  —— 上一轮各源快照（source, key -> hash + data），用于 diff
  runs   —— 每轮扫描的审计（扫描数 / 事件数 / 跳过的源 / 异常）
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from .model import Event

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at  TEXT NOT NULL,
    source       TEXT NOT NULL,
    entity       TEXT NOT NULL,
    kind         TEXT NOT NULL,
    part         TEXT DEFAULT '',
    product      TEXT DEFAULT '',
    changes_json TEXT DEFAULT '[]',
    payload_json TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_ts     ON events(detected_at);
CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source, detected_at);

CREATE TABLE IF NOT EXISTS state (
    source     TEXT NOT NULL,
    key        TEXT NOT NULL,
    hash       TEXT NOT NULL,
    data_json  TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source, key)
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT,
    finished_at TEXT,
    scanned     INTEGER DEFAULT 0,
    created     INTEGER DEFAULT 0,
    updated     INTEGER DEFAULT 0,
    deleted     INTEGER DEFAULT 0,
    skipped     TEXT DEFAULT '',
    error       TEXT DEFAULT ''
);
"""


class EventStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.path, timeout=10)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA busy_timeout=3000")
        self.con.executescript(SCHEMA)
        self.con.commit()

    # ---------------- state ----------------
    def load_state(self, source: str) -> dict[str, dict]:
        out: dict[str, dict] = {}
        cur = self.con.execute(
            "SELECT key, hash, data_json FROM state WHERE source=?", (source,))
        for key, h, data_json in cur:
            try:
                data = json.loads(data_json)
            except (TypeError, ValueError):
                data = {}
            out[key] = {"hash": h, "data": data}
        return out

    def save_state(self, source: str, rows: dict[str, dict], ts: str) -> None:
        """整源替换：先删后插（单事务，5095 行 ~几十毫秒）。

        整源替换是为了让「源里消失的行」一定被清掉 —— 增量 upsert 会留下
        幽灵 state，导致删除事件永远检测不到。
        """
        payload = [
            (source, key, v.get("hash", ""),
             json.dumps(v.get("data") or {}, ensure_ascii=False, sort_keys=True),
             ts)
            for key, v in rows.items()
        ]
        with self.con:
            self.con.execute("DELETE FROM state WHERE source=?", (source,))
            self.con.executemany(
                "INSERT INTO state(source,key,hash,data_json,updated_at) "
                "VALUES(?,?,?,?,?)", payload)

    def has_state(self, source: str) -> bool:
        row = self.con.execute(
            "SELECT 1 FROM state WHERE source=? LIMIT 1", (source,)).fetchone()
        return row is not None

    # ---------------- events ----------------
    def append_events(self, events: Iterable[Event]) -> int:
        rows = []
        for e in events:
            rows.append((
                e.detected_at, e.source, e.entity, e.kind, e.part, e.product,
                json.dumps([c.__dict__ for c in e.changes], ensure_ascii=False),
                json.dumps(e.payload, ensure_ascii=False, sort_keys=True),
            ))
        if not rows:
            return 0
        with self.con:
            self.con.executemany(
                "INSERT INTO events(detected_at,source,entity,kind,part,product,"
                "changes_json,payload_json) VALUES(?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def tail(self, limit: int = 20) -> list[dict]:
        cur = self.con.execute(
            "SELECT id,detected_at,source,entity,kind,part,product,changes_json "
            "FROM events ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for (eid, ts, src, entity, kind, part, product, ch) in cur:
            try:
                changes = json.loads(ch)
            except (TypeError, ValueError):
                changes = []
            out.append({"id": eid, "detected_at": ts, "source": src,
                        "entity": entity, "kind": kind, "part": part,
                        "product": product, "changes": changes})
        return out

    # ---------------- runs ----------------
    def record_run(self, started_at: str, finished_at: str, scanned: int,
                   created: int, updated: int, deleted: int,
                   skipped: str = "", error: str = "") -> None:
        with self.con:
            self.con.execute(
                "INSERT INTO runs(started_at,finished_at,scanned,created,updated,"
                "deleted,skipped,error) VALUES(?,?,?,?,?,?,?,?)",
                (started_at, finished_at, scanned, created, updated,
                 deleted, skipped, error))

    def stats(self, hours: int = 24) -> dict:
        since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
        by_source = {}
        for src, n in self.con.execute(
                "SELECT source, COUNT(*) FROM events WHERE detected_at>=? "
                "GROUP BY source ORDER BY 2 DESC", (since,)):
            by_source[src] = n
        by_kind = {}
        for kind, n in self.con.execute(
                "SELECT kind, COUNT(*) FROM events WHERE detected_at>=? "
                "GROUP BY kind ORDER BY 2 DESC", (since,)):
            by_kind[kind] = n
        state_rows = {}
        for src, n in self.con.execute(
                "SELECT source, COUNT(*) FROM state GROUP BY source"):
            state_rows[src] = n
        last_run = self.con.execute(
            "SELECT started_at,finished_at,scanned,created,updated,deleted,"
            "skipped,error FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "since": since,
            "events_by_source": by_source,
            "events_by_kind": by_kind,
            "state_rows": state_rows,
            "last_run": last_run,
        }

    def reset(self) -> None:
        """清空监听自己的库（不动任何源）。"""
        with self.con:
            self.con.execute("DELETE FROM events")
            self.con.execute("DELETE FROM state")
            self.con.execute("DELETE FROM runs")

    def close(self) -> None:
        self.con.close()
