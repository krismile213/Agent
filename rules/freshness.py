"""数据新鲜度判定（纯函数，零 IO，可脱离 xlsx / 归档库 / :8000 单测）。

背景（2026-11-16 用户提出）：
  14:20 的简报催办基于 14:07 的快照；17:00 对方办完了，用户不知道，
  按旧快照去催，发现人家早做完了。本质不是催办话术问题，是数据新鲜度问题。

可用的三个时间信号（实测 dingtalk/export_archive.db + xlsx mtime）：
  1. **快照产出时刻** = Part*.xlsx 的 mtime —— 真正"这批数据几点送到 Agent"。
     每次同步都会重写 xlsx，所以 mtime 反映的是"同步跑没跑"。
  2. **归档库最新批次** = export_runs.MAX(finished_at) —— 后台把数据拉到几点。
     若它明显晚于快照 mtime，说明有新数据入库但 xlsx 没导出 → 快照已被超越。
  3. ~~export_rows.updated_at~~ **不可用**：每次同步全表刷成同一时刻（实测 5095 行
     全是 2026-11-16T14:06:37），它测的是"同步动作"不是"单据业务变更"，
     拿它查"某 SKU 有没有新动向"会得到假阳性。别再用。

三态：
  fresh   —— 快照年龄 ≤ FRESH_MAX_MIN，正常出行动建议。
  stale   —— 快照年龄超阈值但归档库也没新批次：数据就是这么旧（可能是同步延迟
              / 周末停供），建议仍要给，但必须印数据截至时刻并提示"催前先核对"。
  blocked —— 快照年龄 > BLOCK_AGE_H，或归档库出现新批次（> ARCHIVE_SKEW_SEC）：
              催办会指向过期状态 → 禁止生成行动建议，⑥ 段替换为警示。

注意：外部 IO（stat / sqlite）由调用方完成，本模块只吃 datetime，不碰文件。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

# 阈值（分钟/小时/秒）—— 单一真源，测试直接引用，不写死字面量。
FRESH_MAX_MIN = 45          # 快照年龄 ≤45min 视为新鲜（定时任务 09:00/14:20 实测 ~11min）
BLOCK_AGE_H = 8             # 快照 >8h 未刷新 → 禁催（正常两次同步间隔 <6h）
ARCHIVE_SKEW_SEC = 120      # 归档库比快照新 >120s 才算新批次（同轮写 xlsx→库差 ~10s）

FRESH = "fresh"
STALE = "stale"
BLOCKED = "blocked"


@dataclass
class FreshnessVerdict:
    state: str
    snapshot_at: datetime | None
    archive_at: datetime | None
    age_min: int | None
    banner: str

    @property
    def blocked(self) -> bool:
        return self.state == BLOCKED


def assess_freshness(snapshot_at: datetime | None,
                     archive_at: datetime | None = None,
                     now: datetime | None = None,
                     *,
                     fresh_max_min: int = FRESH_MAX_MIN,
                     block_age_h: int = BLOCK_AGE_H,
                     archive_skew_sec: int = ARCHIVE_SKEW_SEC) -> FreshnessVerdict:
    """判定数据新鲜度并生成横幅文本。纯计算，不读任何文件。

    Args:
        snapshot_at: 快照产出时刻（Part*.xlsx 最大 mtime），None = 取不到。
        archive_at:  归档库最新同步完成时刻（export_runs.MAX(finished_at)），None = 无库/查不到。
        now:         当前时刻，默认 datetime.now()；测试可注入固定值。
    """
    now = now or datetime.now()

    if snapshot_at is None:
        return FreshnessVerdict(
            BLOCKED, None, archive_at, None,
            "⛔ 数据源不可用: 取不到 Part*.xlsx 产出时刻, 本次禁止生成催办行动建议 "
            "(数据可能未同步, 先跑 sync_status 确认)")

    try:
        age_min = int((now - snapshot_at).total_seconds() // 60)
    except (TypeError, ValueError):
        return FreshnessVerdict(BLOCKED, snapshot_at, archive_at, None,
                                "⛔ 数据源不可用: 快照时刻解析失败, 本次禁止催办")

    asof = snapshot_at.strftime("%m-%d %H:%M")
    # 归档库比快照新很多 = 后台已拉到新数据但没导出 → 快照被超越，催办必错。
    superseded = (archive_at is not None
                  and (archive_at - snapshot_at).total_seconds() > archive_skew_sec)

    if superseded:
        return FreshnessVerdict(
            BLOCKED, snapshot_at, archive_at, age_min,
            f"⛔ 快照已被超越: 数据截至 {asof}, 但后台已同步到 "
            f"{archive_at.strftime('%m-%d %H:%M')} —— 新数据还没导出到 Part 表, "
            f"本次禁止催办(催了就是催已完成的事)")

    if age_min > block_age_h * 60:
        return FreshnessVerdict(
            BLOCKED, snapshot_at, archive_at, age_min,
            f"⛔ 数据已 {age_min // 60} 小时未刷新(截至 {asof}), 本次禁止催办 —— "
            f"先确认同步任务是否正常, 再采信任何停滞/超期数字")

    if age_min > fresh_max_min:
        return FreshnessVerdict(
            STALE, snapshot_at, archive_at, age_min,
            f"⚠️ 数据截至 {asof}(距今 {age_min} 分钟), 对方可能已推进 —— "
            f"催办前先在钉钉核对当前状态, 别按旧快照直接催")

    return FreshnessVerdict(FRESH, snapshot_at, archive_at, age_min, "")
