# -*- coding: utf-8 -*-
"""口径内核（rules）—— 纯规则、零业务 IO、不含人名。

设计约束（见 docs/口径内核抽取方案.md）:
  1. 计算函数不读 xlsx / DB / HTTP；取数入口由调用方传进来。
  2. 不携带任何责任人姓名（后端的 SEGS 带 owner，Agent 是上云最后一跳，这里只取结构与目标）。
  3. None ≠ 0：缺失段不参与求和，也不许被当成 1 天。

目前只落了「环节目标天数」一件事；后续 Part→环节名、素材通用判定、
日报表头定位会陆续搬进来，搬一件删一处旧实现。
"""

from .targets import (  # noqa: F401
    PART_SEGS,
    PART8_TARGET,
    PART9_TARGET,
    PART_TARGET_FALLBACK,
    load_snapshot,
    part_targets_from_segments,
    resolve_targets,
    describe_sources,
)

__all__ = [
    "PART_SEGS",
    "PART8_TARGET",
    "PART9_TARGET",
    "PART_TARGET_FALLBACK",
    "load_snapshot",
    "part_targets_from_segments",
    "resolve_targets",
    "describe_sources",
]
