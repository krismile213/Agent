# -*- coding: utf-8 -*-
"""口径漂移体检单测 —— 三查 + 已知豁免 + 误报防护 + 接入守卫 + 探针降级。"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (str(ROOT), str(ROOT / "plugins")):
    if p not in sys.path:
        sys.path.insert(0, p)

from rules import drift_check as dc

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


# 基线（裁决真值）—— resolve_targets 正常输出应与之一致
RESOLVED_OK = dict(dc.CANONICAL_TARGETS["手机膜"])
SOURCES_OK = {p: "snapshot" for p in dc.SNAPSHOT_BACKED_PARTS["手机膜"]}
SOURCES_OK.update({"Part8": "default", "Part9": "default"})


def test_values():
    print("\n[查一 值漂移]")
    check("一致时无漂移", dc.check_values(RESOLVED_OK, "手机膜") == [])
    # 偷改：Part3 从 8 被改成 6
    bad = dict(RESOLVED_OK, Part3=6)
    f = dc.check_values(bad, "手机膜")
    check("值被偷改能报出", len(f) == 1 and "Part3" in f[0] and "8→6" in f[0], str(f))
    # 已知豁免核心：xlsx=75 不影响（我们比 resolve 输出, 输出=13 → 一致）
    check("Part1 13vs75 已知差异不误报", dc.check_values(RESOLVED_OK, "手机膜") == [])
    # 壳基线独立
    shell = dict(dc.CANONICAL_TARGETS["手机壳"])
    check("壳基线 Part1=75 不误报", dc.check_values(shell, "手机壳") == [])
    check("壳用膜值会被判漂移", len(dc.check_values(dict(shell, Part1=13), "手机壳")) == 1)
    # 非数字 / 缺 Part
    check("非数字值报可疑", len(dc.check_values(dict(RESOLVED_OK, Part2="??"), "手机膜")) == 1)
    check("缺 Part 不算漂移", dc.check_values({"Part1": 13}, "手机膜") == [])


def test_sources():
    print("\n[查二 来源静默回退]")
    check("全 snapshot 来源一致", dc.check_sources(SOURCES_OK, "手机膜", True) == [])
    # 上次漏导出 bug 的形态：Part1 来源从 snapshot 退成 default, 值仍是 13
    bad = dict(SOURCES_OK, Part1="default")
    f = dc.check_sources(bad, "手机膜", True)
    check("静默回退能抓(光比数值抓不到)", len(f) == 1 and "Part1" in f[0] and "default" in f[0], str(f))
    check("snapshot 不可用时跳过来源查", dc.check_sources(bad, "手机膜", False) == [])
    # 壳 Part1 不折算：来源 default 也不该报
    shell_src = {p: "snapshot" for p in dc.SNAPSHOT_BACKED_PARTS["手机壳"]}
    shell_src["Part1"] = "default"
    check("壳 Part1 来源豁免", dc.check_sources(shell_src, "手机壳", True) == [])
    check("xlsx 作为合法次级来源也报(真源可用)", len(dc.check_sources(
        dict(SOURCES_OK, Part3="xlsx"), "手机膜", True)) == 1)


def test_hashes():
    print("\n[查三 hash 漂移]")
    cur = {"snapshot": "abc123456789", "rule_xlsx": "def987654321"}
    check("首次无基线不报", dc.check_hashes(cur, {}) == [])
    check("hash 一致不报", dc.check_hashes(cur, cur) == [])
    f = dc.check_hashes(cur, dict(cur, rule_xlsx="OLD______"))
    check("文件被改能报出", len(f) == 1 and "rule_xlsx" in f[0] and "变更" in f[0], str(f))
    check("新增文件也提示", len(dc.check_hashes(dict(cur, new="x"), cur)) == 1)


def test_assess():
    print("\n[汇总判定]")
    v = dc.assess(RESOLVED_OK, SOURCES_OK, "手机膜",
                  current_hashes={"snapshot": "a"}, baseline_hashes={"snapshot": "a"})
    check("全一致 → OK", v.state == dc.OK and not v.blocked and v.banner == "")
    v = dc.assess(dict(RESOLVED_OK, Part1=75), SOURCES_OK, "手机膜")
    check("有漂移 → DRIFT", v.state == dc.DRIFT and v.blocked and "⛔" in v.banner)
    check("横幅含旧值→新值", "13→75" in v.banner, v.banner)


def test_guard():
    print("\n[接入守卫 enforce_drift_guard]")
    import daily_brief
    md = "# 日报\n## ① 同步\n内容\n## ⑥ 行动建议\n• 催A\n• 催B"
    new, hit = daily_brief.enforce_drift_guard(md, "⛔ 口径漂移: 测试漂移")
    check("无横幅不动", daily_brief.enforce_drift_guard(md, "") == (md, False))
    check("非⛔不动", daily_brief.enforce_drift_guard(md, "⚠️ 提示") == (md, False))
    check("有横幅替换⑥", hit and "暂不催办" in new and "催A" not in new, new)
    check("保留①及之前", "## ① 同步" in new and "内容" in new)
    check("保留⑥标题", "## ⑥ 行动建议" in new)


def test_probe_degrade():
    print("\n[探针降级]")
    import drift_probe
    # 指向不存在的文件 → 不能抛异常, 降级为 OK
    junk = ROOT / "__no_base__.json"
    if junk.exists():
        junk.unlink()
    v, note = drift_probe.probe(
        snapshot=ROOT / "__no__.json", xlsx=ROOT / "__no__.xlsx",
        baseline_path=junk, line="手机膜")
    check("真源缺失不抛异常", v.state == dc.OK)
    check("降级留痕", "降级" in note or "ok" in note, note)
    check("真源缺失不写空基线", not junk.exists())
    if junk.exists():
        junk.unlink()


if __name__ == "__main__":
    test_values()
    test_sources()
    test_hashes()
    test_assess()
    test_guard()
    test_probe_degrade()
    print(f"\n[test_drift_check] 通过 {PASS} 项, 失败 {FAIL} 项")
    sys.exit(1 if FAIL else 0)
