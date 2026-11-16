"""watch 的数据模型（无业务依赖，便于单测）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Change:
    """一条字段级变化。old/new 已做展示化截断。"""

    field: str
    old: str
    new: str


@dataclass
class Event:
    """一次检测到的事实。entity 是稳定主键（instance_id / 文件路径 / 货件号）。"""

    detected_at: str
    source: str
    entity: str
    kind: str                  # created | updated | deleted | sync_finished | file_changed
    part: str = ""
    product: str = ""
    changes: list[Change] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        if self.kind == "updated" and self.changes:
            head = ", ".join(f"{c.field}: {c.old} → {c.new}" for c in self.changes[:3])
            more = f" (+{len(self.changes) - 3})" if len(self.changes) > 3 else ""
            return f"[{self.source}] {self.entity} 更新 {head}{more}"
        return f"[{self.source}] {self.entity} {self.kind}"


@dataclass
class Snapshot:
    """某一轮某个源的完整状态：key -> {"hash": str, "data": dict}。

    `meta` 放源级元信息（如最新批次 id、扫描耗时），不进 diff。
    """

    source: str
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    skipped: bool = False
    reason: str = ""

    def __len__(self) -> int:
        return len(self.rows)
