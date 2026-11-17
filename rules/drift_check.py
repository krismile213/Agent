# -*- coding: utf-8 -*-
"""口径漂移判定（纯函数，零 IO，不含人名）。

为什么需要它
------------
代码有单测防回归，但业务规则的数字散在 规则快照.json / 时效确认规则表.xlsx /
内置常量 三处，改错了没人知道，更阴的是**静默回退**：真源文件都在、来源却悄悄
变成兜底（2026-11-16 `rules/__init__.py` 漏导出 describe_sources → workmain
ImportError 被 except 吞 → Part1 回退 75，而直调单测全绿）。这类缺陷靠人肉对表
永远发现不了。本模块是"给数字口径装的 CI"：在每次发简报前体检，发现漂移就
禁催 + 告警，把"悄悄用错数字"变成"当场暴露"。

三查
----
1. check_values  —— 权威值(resolve_targets 输出) 是否偏离裁决基线 CANONICAL_TARGETS。
                    抓"某个真源被偷改"（快照/xlsx/常量任一被改到 resolve 输出上）。
2. check_sources —— snapshot 可用时, Part 来源是否仍是 snapshot。
                    抓"静默回退"（明明有真源, 却用了 xlsx/default）。
                    ⚠️ 光比数值抓不到: 兜底值也写成了 13, 回退后数字不变, 只有来源会变。
3. check_hashes  —— 源文件 hash 相对上次基线是否变化 → 提示"源被动过, 请复核"。

设计纪律
--------
- 零 IO：不读文件、不算 hash、不碰 xlsx；所有输入由调用方（探针）传进来。
  与 rules/freshness.py 同构，便于脱离真实文件系统单测。
- 已知豁免：Part1 膜 13(快照) vs 75(xlsx) 是用户 2026-11-16 拍板认可的旧值差异,
  不算漂移 —— 我们比的是 resolve 输出 vs 裁决基线, 天然避开了这个坑。
- CANONICAL_TARGETS 是独立于真源链的"裁决基线"(类似测试 golden)。合法改口径时
  必须同步改这里 + targets.py, 否则会被判漂移 —— 这正是强制评审的意图。
"""

from dataclasses import dataclass, field

OK = "ok"
DRIFT = "drift"

#: 裁决基线（2026-11-16 用户拍板）—— 独立于真源链的校验锚点。
#: 膜 Part1=13(提案→取数1 + 取数→签样12); 壳 Part1=75(待业务校准, 不折算)。
#: ⚠️ 业务合法调整口径时, 必须同步更新本表 + rules/targets.py, 否则会被判漂移。
_CANON_COMMON = {
    "Part2": 1, "Part3": 8, "Part4": 7, "Part5": 22,
    "Part6": 13, "Part7": 7, "Part8": 15, "Part9": 1,
}
CANONICAL_TARGETS = {
    "手机膜": {"Part1": 13, **_CANON_COMMON},
    "手机壳": {"Part1": 75, **_CANON_COMMON},
}

#: snapshot 可用时, 这些 Part 的来源应当是 snapshot。
#: 壳无「取数」段, Part1 不折算（来源可能是 xlsx/default），故不纳入来源检查。
SNAPSHOT_BACKED_PARTS = {
    "手机膜": {"Part1", "Part2", "Part3", "Part4", "Part5", "Part6", "Part7"},
    "手机壳": {"Part2", "Part3", "Part4", "Part5", "Part6", "Part7"},
}


@dataclass
class DriftVerdict:
    state: str
    findings: list = field(default_factory=list)   # 每条漂移的可读说明
    banner: str = ""

    @property
    def blocked(self) -> bool:
        return self.state == DRIFT


def check_values(resolved: dict, line: str) -> list:
    """权威值 vs 裁决基线。返回漂移说明列表（空=一致）。"""
    canon = CANONICAL_TARGETS.get(line)
    if not canon or not resolved:
        return []
    out = []
    for part, expect in canon.items():
        got = resolved.get(part)
        if got is None:
            continue    # 某 Part 本次没产出（如壳缺段），不算漂移
        try:
            got_i = int(got)
        except (TypeError, ValueError):
            out.append(f"{line} {part}: 权威值不可解析({got!r}), 基线={expect}")
            continue
        if got_i != expect:
            out.append(f"{line} {part}: 权威值={got_i}, 裁决基线={expect} "
                       f"(旧→新 {expect}→{got_i})")
    return out


def check_sources(sources: dict, line: str, snapshot_available: bool) -> list:
    """snapshot 可用时, 检查来源是否仍是 snapshot。抓"静默回退"。"""
    if not snapshot_available or not sources:
        return []
    expect_parts = SNAPSHOT_BACKED_PARTS.get(line, set())
    out = []
    for part in sorted(expect_parts):
        src = sources.get(part)
        if src and src != "snapshot":
            out.append(f"{line} {part}: 来源={src}, 期望=snapshot "
                       f"(真源可用却回退, 检查 rules 导出链)")
    return out


def check_hashes(current: dict, baseline: dict) -> list:
    """源 hash 相对基线变化 → 提示复核。baseline 空=首次体检, 不报。"""
    if not baseline:
        return []
    out = []
    for name in sorted(set(current) | set(baseline)):
        old, new = baseline.get(name), current.get(name)
        if old != new:
            out.append(f"源文件[{name}] 已变更 "
                       f"({(old or '无')[:8]}→{(new or '无')[:8]}), 请复核口径")
    return out


def assess(resolved: dict, sources: dict, line: str, *,
           snapshot_available: bool = True,
           current_hashes: dict | None = None,
           baseline_hashes: dict | None = None) -> DriftVerdict:
    """汇总三查。任一命中即判漂移（禁催 + 告警）。纯计算，不读文件。"""
    findings = []
    findings += check_values(resolved, line)
    findings += check_sources(sources, line, snapshot_available)
    findings += check_hashes(current_hashes or {}, baseline_hashes or {})
    if not findings:
        return DriftVerdict(OK, [], "")
    banner = ("⛔ 口径漂移: " + "; ".join(findings)
              + "。本轮暂缓催办, 先复核口径真源与 rules 导出链")
    return DriftVerdict(DRIFT, findings, banner)
