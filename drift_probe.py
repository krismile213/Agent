# -*- coding: utf-8 -*-
"""口径漂移 IO 探针 —— 给 rules.drift_check 纯函数喂数据, 并把判定接入简报。

职责边界
--------
- rules/drift_check.py 是纯函数（零 IO）；本模块负责它不想碰的脏活：
  读规则快照/xlsx、算文件 hash、读写 hash 基线文件。
- **失败静默降级**：任何异常都返回 OK（不阻断简报生成），只在 return 的 note 里
  留下痕迹供日志打印。口径漂移是"加一层防护", 绝不能反过来让主流程挂掉。
- hash 基线存在 rules/ 同级的 drift_baseline.json（gitignore, 可重建）。
  首次运行只建立基线、不报漂移（避免部署第一天全量告警）。

被谁调用
--------
daily_brief.finish() 收尾处, 在 enforce_freshness_guard 之后调 check_and_guard()。
也可独立跑 `python drift_probe.py` 看当前体检结果。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys_path_added = False
import sys
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
    sys_path_added = True

from rules import resolve_targets
from rules import drift_check as dc
from rules.targets import DEFAULT_SNAPSHOT, DEFAULT_RULE_XLSX

BASELINE = HERE / "drift_baseline.json"


def _sha256(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:                       # noqa: BLE001
        return None


def _load_baseline(path: Path, line: str) -> dict:
    """按 line 取基线 —— 膜/壳的真源不同, 共用一个文件必须分键, 否则互相覆盖。"""
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(doc, dict):
            return {}
        lines = doc.get("lines", {})
        if isinstance(lines, dict):         # 新版: 按品类分键
            return (lines.get(line) or {}).get("hashes", {})
        return doc.get("hashes", {})        # 兼容旧版单键格式
    except Exception:                       # noqa: BLE001
        return {}


def _save_baseline(path: Path, hashes: dict, state: str, line: str) -> None:
    """只在无漂移(OK)时滚动该品类基线 —— 漂移时保留旧基线, 持续提醒直到人工处置。"""
    if state != dc.OK:
        return
    try:
        doc = {}
        try:
            doc = json.loads(path.read_text(encoding="utf-8-sig")) or {}
        except Exception:                   # noqa: BLE001
            pass
        lines = doc.get("lines", {}) if isinstance(doc.get("lines"), dict) else {}
        lines[line] = {"hashes": hashes, "state": state,
                       "updated": __import__("datetime").datetime.now().isoformat()}
        path.write_text(json.dumps(
            {"lines": lines}, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:                       # noqa: BLE001
        pass


def probe(snapshot: Path | None = None, xlsx: Path | None = None,
          baseline_path: Path | None = None, line: str = "手机膜"):
    """跑一次体检, 返回 (DriftVerdict, 日志说明)。任何异常降级为 OK。"""
    snap = Path(snapshot or DEFAULT_SNAPSHOT)
    rule = Path(xlsx or DEFAULT_RULE_XLSX)
    base = Path(baseline_path or BASELINE)

    try:
        resolved, sources = resolve_targets(snapshot_path=snap, rule_xlsx=rule, line=line)
        # 来源检查只对膜有意义（壳 Part1 不折算, dc 内部已按 line 收窄检查范围）
        snap_ok = _sha256(snap) is not None
        current = {"snapshot": _sha256(snap), "rule_xlsx": _sha256(rule)}
        current = {k: v for k, v in current.items() if v is not None}
        baseline = _load_baseline(base, line)
        verdict = dc.assess(resolved, sources, line,
                            snapshot_available=snap_ok,
                            current_hashes=current, baseline_hashes=baseline)
        fresh = not bool(baseline)          # 首次无基线: check_hashes 不报, 但标注一下
        # 真源一个都读到时才写基线 —— 否则会把 {} 空基线写进去(测试降级用例暴露)。
        if current:
            _save_baseline(base, current, verdict.state, line)
        note = (f"漂移体检 {line}: {verdict.state}, {len(verdict.findings)} 项"
                + ("（首次建立基线）" if fresh else ""))
        return verdict, note
    except Exception as e:                  # noqa: BLE001
        return dc.DriftVerdict(dc.OK, [], ""), f"漂移体检跳过(降级): {type(e).__name__}: {e}"


def guard_banner(line: str = "手机膜") -> str:
    """供 daily_brief 调用的便捷入口: 有漂移返回 ⛔ 横幅, 否则空串。"""
    verdict, note = probe(line=line)
    print(f"[drift] {note}")
    return verdict.banner if verdict.blocked else ""


if __name__ == "__main__":
    for ln in ("手机膜", "手机壳"):
        v, n = probe(line=ln)
        print(f"[{ln}] {n}")
        print(f"  state={v.state} findings={v.findings}")
        if v.banner:
            print(f"  banner={v.banner}")
