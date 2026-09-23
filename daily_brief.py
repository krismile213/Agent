#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_brief.py — 无人值守的每日晨检简报(定时任务入口)

设计(无人值守三原则):
  1. 只读注册表: 把写级工具全部摘掉, 挂到计划任务跑也不可能有副作用
  2. 固定任务提示: sync_status 查同步 + 读预警清单 → 汇总简报
  3. 落盘 briefs/<日期>.md; 加 --push 且配置了钉钉webhook则顺带推送

配合已有体系: 数据同步本身由 work-main/dingtalk 的计划任务负责,
本脚本只消费其日志与产物, 不重复同步。

Windows 计划任务注册(每天 09:35, 排在同步之后, 复用你现有的任务模式):
  schtasks /Create /TN AgentDailyBrief /TR "python C:\\Users\\dell\\Agent\\daily_brief.py --push" /SC DAILY /ST 09:35
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agentcore as core  # noqa: E402


def build_readonly_registry(cfg: dict) -> core.Registry:
    """只保留只读工具的注册表 —— 无人值守的安全底线."""
    reg = core.build_registry()
    core.load_plugins(reg, cfg)
    dropped = [n for n, t in list(reg._tools.items()) if t.level != "read"]
    for n in dropped:
        del reg._tools[n]
    core.log(f"[晨检] 无人值守模式: 摘除写级工具 {len(dropped)} 个, "
             f"保留只读 {len(reg.names())} 个")
    return reg


TASK_TEMPLATE = """今日晨检({date})。数据口径: data/手机膜/手机膜 自动同步目录(重点手机膜)。请依次完成:
1) 用 sync_status 查看钉钉同步定时任务的运行状态。
2) 用 scan_sync_data 实时扫描: 状态分布 / 数据截至时间 / 今日有动态的SKU。
3) 用 pipeline_alerts 获取四层信号: 本周新增SKU / 全流程临期(空运60·海运69) /
   已超期 / 环节停滞超目标(近似段级)。
4) 用 read_file 读取 流程复盘/output/预警_加急SKU.md 的前40行(不存在就跳过),
   仅作背景补充 —— 必须注明它是旧批次产物, 与实时数据冲突时以实时为准。
输出中文markdown简报(250~450字), 分六节:
① 同步状态与数据截至 ② 今日动态 ③ 本周新增SKU(编号+型号)
④ 临期预警(按剩余天数升序, 带卡点与空运/海运口径) ⑤ 已超期与环节停滞TOP(带超标天数)
⑥ 行动建议(≤2条)。所有数字必须来自工具输出并注明来源; 每条提醒要可行动。"""


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="mini_agent 每日晨检简报")
    ap.add_argument("--push", action="store_true", help="若配置了钉钉webhook则推送")
    args = ap.parse_args()

    cfg = core.load_config()
    if not cfg["api_key"]:
        sys.exit("未配置 API Key")

    workmain = cfg.get("workmain_root") or r"C:\Users\dell\Desktop\work-main"
    core.set_root(workmain)  # 沙箱指向 work-main, 才能读预警文件

    registry = build_readonly_registry(cfg)
    today = datetime.now().strftime("%Y-%m-%d")
    task = TASK_TEMPLATE.format(date=today)

    transcript = core.Transcript(
        HERE / "sessions" / f"brief_{datetime.now().strftime('%Y%m%d')}.jsonl")
    history = [{"role": "system", "content": core.build_system_prompt()}]
    client = core.LLMClient(cfg)
    answer = core.run_task(
        client, registry, core.YoloPolicy(), transcript, history, task, cfg,
        emit=lambda k, d: print(f"  [{k}] {str(d)[:120]}")
        if k in ("tool_call", "tool_result", "fatal") else None)
    transcript.log("end", {"reason": "brief", **client.usage})
    transcript.close()

    brief_dir = HERE / "briefs"
    brief_dir.mkdir(exist_ok=True)
    out = brief_dir / f"{today}.md"
    body = f"# 晨检简报 {today}\n\n{answer}\n\n> 用量: {client.usage_summary()}\n"
    out.write_text(body, encoding="utf-8")
    print(f"[晨检] 简报已写入 {out}")
    print(f"[晨检] {client.usage_summary()}")

    if args.push:
        try:
            import dingtalk_push
            ok, msg = dingtalk_push.send(f"晨检简报 {today}",
                                         answer[:1800] or "(空)")
            print(f"[晨检] 推送{'成功' if ok else '失败'}: {msg}")
        except Exception as e:
            print(f"[晨检] 推送异常(不影响落盘): {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
