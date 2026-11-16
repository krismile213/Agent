"""watch — 联合后台实时监听（旁挂只读，POC）。

设计红线
--------
1. **只读源**：归档库用 `mode=ro` 打开，Part*.xlsx 只 stat 不读内容；
   任何写入只落到本包自己的 `watch/watch.db`。
2. **不动核心**：不 import `daily_brief` / `plugins.workmain` / 数据后台 app；
   口径需要时只复用纯函数（如 `rules.freshness`）。
3. **可测试**：指纹与 diff 是纯函数；引擎接受注入的 sources 与时钟。

入口
----
    python -m watch --once              # 扫一轮（首轮自动建基线，不产事件）
    python -m watch --loop --interval 60
    python -m watch --tail 20
"""

from .fingerprint import fingerprint_row, normalize_row, diff_snapshots, diff_fields
from .model import Change, Event, Snapshot

__all__ = [
    "fingerprint_row",
    "normalize_row",
    "diff_snapshots",
    "diff_fields",
    "Change",
    "Event",
    "Snapshot",
]
