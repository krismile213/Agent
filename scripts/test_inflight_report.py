#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/test_inflight_report.py — inflight_report(事中日报本地只读) 离线测试(零 LLM)

覆盖: 注册契约(level=read → 无人值守晨/午报注册表自动保留) / 表头第3行解析 /
      脱敏红线(xlsx「责任人」列真名与豁免「原因」原文不得透出) / 口径注必现 /
      date 归一化与人话报错 / line 路径逃逸拒绝 / 降级不抛异常 / 真实日报(存在才跑)。

红线背景: Agent 是数据上云的最后一跳, 而事中日报 xlsx 的「责任人」列是真实姓名、
「豁免备查.原因」含登记人姓名 —— 平台 outbound_guard 词典没有"责任人"这一条、
平台 API 也从不返回该字段, 所以本工具只读所需列, 这里用硬断言钉死。
"""
import os
import re
import sys
from pathlib import Path

# ⚠️ 摘掉沙箱注入的代理(同 test_auth.py / test_platform_tools.py)
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
           "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "plugins"))   # load_plugins 同款: 插件按目录导入
os.chdir(HERE)

import json  # noqa: E402

import agentcore as core          # noqa: E402
import workmain as wm             # noqa: E402  (plugins/workmain.py)

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name
          + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


class _Reg:
    """最小 registry 桩: 记录注册的 Tool。"""
    def __init__(self):
        self.tools = {}

    def register(self, tool):
        self.tools[tool.name] = tool


def _mk_tools(cfg=None):
    r = _Reg()
    wm.register(r, cfg or {})
    return r


def _cfg():
    return json.loads((HERE / "config.json").read_text(encoding="utf-8"))


# ---------- 注册契约 ----------

def test_contract():
    reg = _mk_tools(_cfg())
    t = reg.tools.get("inflight_report")
    check("契约: inflight_report 已注册", t is not None)
    if t is None:
        return
    check("契约: level=read(自动放行, 无人值守链路可用)", t.level == "read")
    # 2026-10-29: 9 基础 + 10 平台 + pipeline_trend(跨日期趋势) = 20
    check("契约: workmain 工具数 9 基础 + 10 平台 + 1 趋势 = 20",
          len(reg.tools) == 20, extra=str(len(reg.tools)))
    props = t.parameters.get("properties", {})
    check("契约: line 枚举仅手机膜/手机壳(路径逃逸防线)",
          props.get("line", {}).get("enum") == ["手机膜", "手机壳"])
    check("契约: date 可选(required 为空)", t.parameters.get("required") == [])
    check("契约: 描述含口径注(segment vs Part 双口径提醒)",
          "Part" in t.description and "segment" in t.description)


def test_unattended_registry():
    """无人值守晨/午报: build_readonly_registry 留 read 剔 write, 本工具必须保留。"""
    reg = core.build_registry()
    core.load_plugins(reg, _cfg())
    ro = [n for n, t in reg._tools.items() if t.level == "read"]
    wo = [n for n, t in reg._tools.items() if t.level != "read"]
    check("无人值守: inflight_report 在只读名单",
          "inflight_report" in ro)
    check("无人值守: run_route_check 仍在写级名单(对照)",
          "run_route_check" in wo)


# ---------- 真实日报(文件存在才跑) ----------

def _report(line="手机膜", date="20261015"):
    root = Path(_cfg().get("workmain_root") or wm.DEFAULT_ROOT)
    return root / "data" / line / line / "reports" / f"事中日报_{date}.xlsx"


def test_real():
    reg = _mk_tools(_cfg())
    f = _report()
    if not f.exists():
        print(f"SKIP 真实日报: {f} 不存在(仅离线用例已覆盖)")
        return
    out = reg.tools["inflight_report"].func(line="手机膜")
    check("真实: 首行是标题而非表头(header=2 生效)",
          out.splitlines()[0].startswith("流程事中日报")
          and "SKU" not in out.splitlines()[0])
    for sec in ("在研状态总表", "超期明细", "风险预警", "异常上报", "豁免备查"):
        check(f"真实: 含「{sec}」节", f"— {sec}" in out or sec in out)
    check("真实: 口径注必现(引用须注明来源)", "口径注" in out and "Part" in out)
    # 工具默认取"最新一份"日报, 断言不能写死某天的文件名(2026-10-19 修:
    # 原来断言 事中日报_20261015.xlsx, 日报推进到 1019 后必挂)
    check("真实: 来源行带文件名", re.search(r"事中日报_\d{8}\.xlsx", out) is not None)
    check("真实: 长度受 clip 控制", len(out) <= 3800, extra=str(len(out)))

    # 人工登记（2026-10-15）：跨日期延续的在研备注，真源在生成器之外的
    # 资产文件\事中日报人工登记.xlsx。工具必须把它带给模型（催办前必读的业务决策）。
    try:
        import pandas as pd
        _ov = pd.read_excel(f, sheet_name="在研SKU状态总表", header=2, dtype=str)
    except ImportError:
        _ov = None
    if _ov is not None and "人工登记" in _ov.columns:
        note_map = {str(r["SKU"]).strip(): str(r["人工登记"]).strip()
                    for _, r in _ov.iterrows()
                    if str(r.get("人工登记") or "").strip()
                    not in ("", "nan", "None")}
        placed = [f"人工登记:{v}" for v in note_map.values() if f"人工登记:{v}" in out]
        check("人工登记: 备注按SKU带入输出", len(placed) == len(note_map),
              extra=f"notes={note_map} placed={placed}")

    # —— 脱敏硬校验: 拿 xlsx 里的真名/原文回来比对, 一个都不许出现 ——
    try:
        import pandas as pd
    except ImportError:
        print("SKIP 脱敏硬校验: 无 pandas")
        return
    names, reasons = set(), []
    for sn in ("在研SKU状态总表", "超期明细", "风险预警", "异常上报清单"):
        df = pd.read_excel(f, sheet_name=sn, header=2, dtype=str)
        if "责任人" in df.columns:
            names |= {str(x).strip() for x in df["责任人"].dropna()
                      if str(x).strip() and str(x).strip() != "—"}
    ex = pd.read_excel(f, sheet_name="豁免备查", header=2, dtype=str)
    if "原因" in ex.columns:
        reasons = [str(x).strip() for x in ex["原因"].dropna()
                   if len(str(x).strip()) > 12]
    check("脱敏红线: 表内至少取到1个责任人真名(测试前提)",
          bool(names) or not reasons, extra=f"names={names}")
    leaked = sorted(n for n in names if n in out)
    check("脱敏红线: 输出不含任何责任人真名", not leaked, extra=str(leaked))
    leaked_txt = [r[:18] for r in reasons if r[:18] in out]
    check("脱敏红线: 输出不含豁免原因原文", not leaked_txt,
          extra=str(leaked_txt[:3]))
    check("脱敏红线: 输出不含完整11位手机号",
          not __import__("re").search(r"(?<!\d)1[3-9]\d{9}(?!\d)", out))
    check("脱敏红线: 豁免节明确不返回原因", "不返回「原因」原文" in out)


def test_date_param():
    reg = _mk_tools(_cfg())
    f14 = _report(date="20261014")
    if f14.exists():
        out = reg.tools["inflight_report"].func(line="手机膜", date="2026-10-14")
        check("date: 连字符形式命中 20261014",
              "事中日报_20261014.xlsx" in out and "2026-10-14" in out)
        out2 = reg.tools["inflight_report"].func(line="手机膜", date="20261014")
        check("date: 紧凑形式命中同一文件", "事中日报_20261014.xlsx" in out2)
    out3 = reg.tools["inflight_report"].func(line="手机膜", date="20260101")
    # 可用清单只列"最近 8 份", 不能硬编码某个日期(2026-10-27 实测: 数据涨到 10-27 后
    # 原来钉死的 20261015 已被挤出前 8, 测试假失败)。只验证"确实列出了 8 位日期"。
    check("date: 不存在的日期 → 人话错误+可用清单",
          out3.startswith("[错误]") and "已有" in out3
          and len(re.findall(r"20\d{6}", out3)) >= 1)
    out4 = reg.tools["inflight_report"].func(line="手机膜", date="abc")
    check("date: 非法形式 → 人话错误", out4.startswith("[错误]")
          and "YYYYMMDD" in out4)


def test_guard_and_degrade():
    reg = _mk_tools(_cfg())
    out = reg.tools["inflight_report"].func(line="../../etc")
    check("安全: line 路径逃逸被拒", out.startswith("[错误]") and "非法品类名" in out)
    out2 = reg.tools["inflight_report"].func(line="手表")
    check("降级: 品类无日报目录 → 人话错误不抛异常",
          out2.startswith("[错误]") and "事中日报" in out2)
    out3 = reg.tools["inflight_report"].func(line="手机壳")
    check("品类: 手机壳可读(默认最新)",
          out3.startswith("流程事中日报") and "— 在研状态总表" in out3)


def test_fallback_name_selection():
    """撞锁改名(平台侧 2026-10-20 约定): 同日并存 正式名(旧)+ _HHMMSS后缀(新) 时,
    必须取新那份。旧选择逻辑按文件名末段比大小, "20261020" > "153000" 恒成立,
    被占用写不进的旧正式名会赢 → 晨/午简报静默读到 09:00 旧数据。
    """
    import shutil
    import tempfile

    import pandas as pd
    tmp = Path(tempfile.mkdtemp(prefix="inflight_fb_"))
    try:
        rpt = tmp / "data" / "手机膜" / "手机膜" / "reports"
        rpt.mkdir(parents=True)

        def mk(name, gen, mtime):
            p = rpt / name
            pd.DataFrame(
                [[f"流程事中日报 · 手机膜 · 2026-10-20（生成于 {gen}）"]]
            ).to_excel(p, index=False, header=False)
            os.utime(p, (mtime, mtime))
            return p

        # 正式名 = 09:00 旧数据(正是被 Excel 占用、写不进去的那份); 后缀名 = 15:30 新数据
        mk("事中日报_20261020.xlsx", "2026-10-20 09:00", 1_700_000_000)
        mk("事中日报_20261020_153000.xlsx", "2026-10-20 15:30", 1_700_000_600)
        prev = mk("事中日报_20261019.xlsx", "2026-10-19 09:00", 1_699_900_000)

        reg = _mk_tools({"workmain_root": str(tmp)})
        out = reg.tools["inflight_report"].func(line="手机膜")
        # 注: 不能只断言 "09:00" not in out —— 工具来源行自带固定文案
        # "平台每日09:00生成", 旧正式名的标题是 "生成于 2026-10-20 09:00"。
        check("改名: 默认取最新 → _HHMMSS 后缀那份(旧正式名不得赢)",
              "事中日报_20261020_153000.xlsx" in out
              and "15:30" in out and "生成于 2026-10-20 09:00" not in out)
        out2 = reg.tools["inflight_report"].func(line="手机膜", date="2026-10-20")
        check("改名: date=2026-10-20 → 同取后缀新份",
              "事中日报_20261020_153000.xlsx" in out2)
        out3 = reg.tools["inflight_report"].func(line="手机膜", date="20261019")
        check("改名: date=20261019 → 前一天正式名(后缀不串门)",
              "事中日报_20261019.xlsx" in out3 and "15:30" not in out3)
        check("改名: 不同日期不得按 mtime 误取(日期是第一排序键)",
              "20261019" not in out and prev.exists())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    test_contract()
    test_unattended_registry()
    test_real()
    test_date_param()
    test_guard_and_degrade()
    test_fallback_name_selection()
    tag = "全部通过" if not fails else f"失败 {len(fails)} 项"
    print(f"\n== {tag} ==")
    for f in fails:
        print("  FAIL:", f)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
