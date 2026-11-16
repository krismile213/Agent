"""轮询引擎：采集 → 与上轮快照 diff → 落事件 → 存新快照。

首轮行为（重要）
----------------
源没有历史 state 时不产业务事件（否则第一次启动就是 5095 条「新增」，
等于噪音），只写基线。需要看基线规模用 `--emit-baseline`，事件 kind 标
`baseline`，与真实新增区分开。

幂等
----
同一源同一轮快照，hash 全等 → 不产事件、不重写 state（省 IO）。
所以 `--once` 连跑两次，第二次必然是 0 事件。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable

from .fingerprint import diff_snapshots
from .model import Event, Snapshot
from .store import EventStore

TS_FMT = "%Y-%m-%dT%H:%M:%S"


def _ts(dt: datetime) -> str:
    return dt.strftime(TS_FMT)


@dataclass
class ScanResult:
    started_at: str
    finished_at: str
    scanned: int = 0
    events: list[Event] = field(default_factory=list)
    baseline: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def counts(self) -> dict[str, int]:
        out = {"created": 0, "updated": 0, "deleted": 0,
               "sync_finished": 0, "baseline": 0}
        for e in self.events:
            out[e.kind] = out.get(e.kind, 0) + 1
        return out

    def line(self) -> str:
        c = self.counts
        skipped = f" skip={len(self.skipped)}" if self.skipped else ""
        base = f" baseline={len(self.baseline)}" if self.baseline else ""
        return (f"{self.finished_at} scanned={self.scanned} "
                f"new={c['created']} upd={c['updated']} del={c['deleted']} "
                f"sync={c['sync_finished']}{base}{skipped}")


def _event_kind(source: str, kind: str) -> str:
    """源语义映射：export_runs 的「新增批次」= 一轮同步完成。"""
    if source == "export_runs" and kind == "created":
        return "sync_finished"
    return kind


def _payload_for(source: str, data: dict[str, str]) -> dict:
    if source == "part_files":
        return {"file": data.get("file", ""), "batch": data.get("batch", "")}
    if source == "export_runs":
        return {"last_finished": data.get("last_finished", ""),
                "part_count": data.get("part_count", ""),
                "rows_total": data.get("rows_total", "")}
    return {}


class WatchEngine:
    def __init__(self, store: EventStore, sources: Iterable,
                 clock: Callable[[], datetime] = datetime.now,
                 emit_baseline: bool = False):
        self.store = store
        self.sources = list(sources)
        self.clock = clock
        self.emit_baseline = emit_baseline

    def scan_once(self) -> ScanResult:
        started = self.clock()
        ts = _ts(started)
        res = ScanResult(started_at=ts, finished_at=ts)

        for src in self.sources:
            name = getattr(src, "name", src.__class__.__name__)
            try:
                snap: Snapshot = src.snapshot()
            except Exception as e:                       # 采集器自身异常不拖垮整轮
                res.errors[name] = f"{type(e).__name__}: {e}"
                continue

            if snap.skipped:
                res.skipped[name] = snap.reason or "skipped"
                continue

            res.scanned += len(snap)
            old = self.store.load_state(name)

            # ---- 首轮：只建基线 ----
            if not old and not self.store.has_state(name):
                self.store.save_state(name, snap.rows, ts)
                res.baseline.append(name)
                if self.emit_baseline:
                    for key, row in snap.rows.items():
                        res.events.append(Event(
                            detected_at=ts, source=name, entity=key,
                            kind="baseline",
                            part=str(row.get("part", "")),
                            product=str(row.get("product", "")),
                            payload=_payload_for(name, row.get("data") or {}),
                        ))
                continue

            diffs = diff_snapshots(old, snap.rows)
            for key, kind, changes, old_data, new_data in diffs:
                row = snap.rows.get(key) or {}
                res.events.append(Event(
                    detected_at=ts,
                    source=name,
                    entity=key,
                    kind=_event_kind(name, kind),
                    part=str(row.get("part", "")),
                    product=str(row.get("product", "")),
                    changes=changes,
                    payload=_payload_for(name, new_data or old_data),
                ))

            # 无变化不重写 state，避免每轮 5095 行无谓写入
            if diffs:
                self.store.save_state(name, snap.rows, ts)

        if res.events:
            self.store.append_events(res.events)

        finished = self.clock()
        res.finished_at = _ts(finished)
        c = res.counts
        self.store.record_run(
            started_at=res.started_at, finished_at=res.finished_at,
            scanned=res.scanned, created=c["created"], updated=c["updated"],
            deleted=c["deleted"],
            skipped=";".join(f"{k}:{v}" for k, v in res.skipped.items()),
            error=";".join(f"{k}:{v}" for k, v in res.errors.items()),
        )
        return res

    def run_forever(self, interval: float = 60.0,
                    on_result: Callable[[ScanResult], None] | None = None,
                    max_rounds: int | None = None) -> list[ScanResult]:
        rounds: list[ScanResult] = []
        n = 0
        try:
            while max_rounds is None or n < max_rounds:
                res = self.scan_once()
                rounds.append(res)
                if on_result:
                    on_result(res)
                n += 1
                if max_rounds is not None and n >= max_rounds:
                    break
                time.sleep(interval)
        except KeyboardInterrupt:
            pass
        return rounds
