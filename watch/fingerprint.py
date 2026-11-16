"""行级指纹与快照 diff —— 纯函数，零 IO，单测直接调。

为什么不用 `export_rows.updated_at`
----------------------------------
归档库每轮同步会把全表 `updated_at` 刷成同一时刻（实测 5095 行全是
`2026-11-16T14:06:37`）。它测的是「同步动作」，不是「业务变更」。
拿它判变化 = 每轮都全量误报。本模块只认 `row_json` 里的业务字段。

指纹口径
--------
* 先 normalize：值统一成去掉首尾空白的字符串，None → ""；
* 剔除同步类字段（SYNC_FIELDS），避免上游把同步时刻写进 row_json 时误报；
* 按字段名排序后拼 `k=v`，sha1 —— 与 JSON 键顺序、缩进无关。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from .model import Change

# 同步时刻 / 内部维护字段：即使出现在 row_json 里也不参与指纹。
SYNC_FIELDS = frozenset({
    "updated_at", "_synced_at", "同步时间", "更新时间（系统）", "__row_hash__",
})

# 事件里单个字段值的最大展示长度（防止整段备注把库里塞爆）。
MAX_VAL_LEN = 200


def _stringify(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(v).strip()


def _clip(s: str, limit: int = MAX_VAL_LEN) -> str:
    return s if len(s) <= limit else s[:limit] + "…"


def normalize_row(raw: Any) -> dict[str, str]:
    """把一行业务数据规范成 `字段 -> 字符串`。

    * dict：逐字段 normalize，剔除 SYNC_FIELDS；
    * 非 dict（坏 JSON / 数组）：包成 `__raw__`，仍可参与 diff，不让整轮崩掉。
    """
    if not isinstance(raw, dict):
        return {"__raw__": _stringify(raw)}
    out: dict[str, str] = {}
    for k, v in raw.items():
        key = str(k).strip()
        if not key or key in SYNC_FIELDS:
            continue
        out[key] = _stringify(v)
    return out


def fingerprint_row(row: dict[str, str]) -> str:
    """规范化行的稳定指纹（sha1）。空行也有确定值。"""
    h = hashlib.sha1()
    for k in sorted(row):
        h.update(k.encode("utf-8"))
        h.update(b"\x1f")
        h.update(row[k].encode("utf-8"))
        h.update(b"\x1e")
    return h.hexdigest()


def diff_fields(old: dict[str, str], new: dict[str, str]) -> list[Change]:
    """字段级差异，按字段名排序；值截断到 MAX_VAL_LEN。"""
    out: list[Change] = []
    for k in sorted(set(old) | set(new)):
        ov, nv = old.get(k, ""), new.get(k, "")
        if ov != nv:
            out.append(Change(field=k, old=_clip(ov), new=_clip(nv)))
    return out


def diff_snapshots(
    old_rows: dict[str, dict[str, Any]],
    new_rows: dict[str, dict[str, Any]],
) -> list[tuple[str, str, list[Change], dict[str, str], dict[str, str]]]:
    """两轮快照 → `(key, kind, changes, old_data, new_data)` 列表。

    kind ∈ created / deleted / updated；updated 只带真正变了的字段。
    返回顺序：updated 在前（业务上最关心），然后 created、deleted，各自按 key 排序。
    """
    updated, created, deleted = [], [], []

    for key, new in new_rows.items():
        old = old_rows.get(key)
        if old is None:
            created.append((key, "created", [], {}, new["data"]))
        elif old.get("hash") != new.get("hash"):
            changes = diff_fields(old.get("data") or {}, new.get("data") or {})
            updated.append((key, "updated", changes, old.get("data") or {}, new["data"]))
    for key, old in old_rows.items():
        if key not in new_rows:
            deleted.append((key, "deleted", [], old.get("data") or {}, {}))

    updated.sort(key=lambda t: t[0])
    created.sort(key=lambda t: t[0])
    deleted.sort(key=lambda t: t[0])
    return updated + created + deleted


def stable_iter(rows: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    """按指纹排序的稳定迭代（测试/摘要用）。"""
    return sorted(rows, key=fingerprint_row)
