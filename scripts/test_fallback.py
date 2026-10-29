#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/test_fallback.py — 失败兜底文本单测(零 LLM, 零网络)

背景(2026-10-29 实测): 关掉平台后端问"今天有哪些加急预警", 模型照抄
HTTPConnectionPool 堆栈, 只说"请稍后重试"就收场 —— 而本地 pipeline_alerts
明明能答。根因不是模型笨, 是**错误文本没告诉它还能做什么**。

结论: 错误文本就是给模型的提示词。只写"失败了"它就只会道歉;
写清"还能用什么、怎么标注"它就会降级。

本文件钉住三件事:
  1. 每个失败分支都必须给出下一步(不能只报错);
  2. 不许把底层堆栈原样丢给模型(它会原样转述给用户);
  3. 不许引导模型拿历史数据冒充当天数据。
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import agentcore as core  # noqa: E402

RESULTS = []

# 每条失败文本都必须带"下一步"的痕迹: 出现任一即认为给了可行动指引
ACTIONABLE = ("处理", "改用", "可用", "建议", "不要")


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not cond else ""))


def _registry():
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    reg = core.build_registry()
    core.load_plugins(reg, cfg)
    return reg, cfg


def _with_patch(mod, attr, value, fn):
    """临时替换模块属性, 无论 fn 是否抛异常都还原 —— 否则一条用例失败会污染后面全部。"""
    orig = getattr(mod, attr)
    setattr(mod, attr, value)
    try:
        return fn()
    finally:
        setattr(mod, attr, orig)


def test_empty_sync_dir():
    """同步目录里没有 Part*.xlsx(最常见: 今天同步还没跑完)。"""
    print("\n== 同步目录为空 ==")
    reg, _ = _registry()
    wm = sys.modules["workmain"]     # 插件以顶层模块名加载, 不是 plugins.workmain
    for name in ("pipeline_alerts", "scan_sync_data"):
        out = _with_patch(wm, "_latest_parts", lambda d: [], lambda: reg.execute(name, {}))
        check(f"{name}: 报错但给出下一步", any(k in out for k in ACTIONABLE), out[:60])
        check(f"{name}: 明确禁止拿历史数据冒充今天",
              "冒充" in out or "不要用历史" in out, out[:60])
        check(f"{name}: 指向 sync_status / inflight_report 之一",
              "sync_status" in out or "inflight_report" in out, out[:60])


def test_missing_product_file():
    """商品资料.xlsx 不存在(workmain_root 配错或资产缺失)。

    这条分支在注册闭包里(用的是闭包里的 full()), 无法从外部注入 —— 直接校验
    源码文案。弱一点, 但足以防止有人把它改回裸 "[错误] 找不到商品资料"。
    """
    print("\n== 商品资料缺失 ==")
    src = (HERE / "plugins" / "workmain.py").read_text(encoding="utf-8")
    check("文案说明是文件不存在", "商品资料文件不存在" in src)
    check("指向 workmain_root 配置", "workmain_root" in src and "商品资料" in src)
    check("不猜 SKU 信息", "不要猜 SKU 信息" in src)


def test_no_raw_stacktrace():
    """平台不通时的文本不许含底层堆栈 —— 模型会原样转述给用户。"""
    print("\n== 不把堆栈丢给用户 ==")
    reg, _ = _registry()
    out = reg.execute("platform_alerts", {})
    if "[平台接口不可用]" not in out:
        print("  (SKIP) 平台后端这次是通的, 跳过")
        return
    for noise in ("HTTPConnectionPool", "Max retries", "Traceback", "urllib3"):
        check(f"不含 {noise}", noise not in out, out[:80])
    check("给了降级指引", "pipeline_alerts" in out, out[:60])


def test_unknown_tool_message():
    """工具名写错时, 错误文本要列出可用工具(模型据此自我纠正)。"""
    print("\n== 工具名写错 ==")
    reg, _ = _registry()
    out = reg.execute("no_such_tool", {})
    check("提示未注册", "未注册" in out, out[:60])
    check("列出可用工具", "可用:" in out, out[:60])


def main() -> int:
    test_empty_sync_dir()
    test_missing_product_file()
    test_no_raw_stacktrace()
    test_unknown_tool_message()
    bad = [n for n, ok in RESULTS if not ok]
    print("\n" + "=" * 46)
    if bad:
        print(f"[兜底文本单测] {len(bad)} 项失败: " + ", ".join(bad))
        return 1
    print(f"[兜底文本单测] {len(RESULTS)}/{len(RESULTS)} 通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
