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

Windows 计划任务(已注册, 2026-09-28 核对: 任务 AgentDailyBrief 每日 09:30,
AgentDailyBriefPM 每日 14:45, 动作都是 run_brief.bat; 排在上游同步任务
DingTalkApprovalSync 的 09:00 / 14:30 之后). 本机 schtasks.exe 被安全策略拦,
注册/查询一律走 PowerShell(模块名 ScheduledTasks):
  Get-ScheduledTask -TaskName AgentDailyBrief
  Set-ScheduledTask -TaskName AgentDailyBrief -Trigger (New-ScheduledTaskTrigger -Daily -At "09:30")
"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agentcore as core  # noqa: E402


# 简报正文下限(字符): 模板要求 280~480 字, 低于此视为被限流掐空, 不值得推送
BRIEF_MIN_CHARS = 100


def ensure_brief_answer(client, history: list, answer: str,
                        min_len: int = BRIEF_MIN_CHARS,
                        sleeps=(20, 40), sleep_fn=time.sleep) -> str:
    """空简报兜底(2026-10-10 教训: 免费档 429/1305 过载把最终轮掐空,
    run_task 返回空正文照样写盘推送, 群里收到空壳).

    answer 过短时: 弹掉 history 尾部的空 assistant 消息, 追加"基于工具输出直接
    生成简报"的纯生成指令(不带工具), 按退避间隔重试; 仍短则返回原值, 由调用方
    决定不推送+非零退出 —— 宁可缺今天的简报, 不发空壳占位。
    """
    a = (answer or "").strip()
    if len(a) >= min_len:
        return a
    while (history and history[-1].get("role") == "assistant"
           and not (history[-1].get("content") or "").strip()
           and not history[-1].get("tool_calls")):
        history.pop()  # 空流残留, 留着会误导续生成
    hint = ("工具调用已全部完成。请基于以上工具输出直接输出中文markdown简报"
            "(280~480字, 按任务要求的六节结构), 不要再调用任何工具, 不要输出其他内容。")
    for wait_s in sleeps:
        sleep_fn(wait_s)  # 给限流桶留恢复时间, 立刻重试大概率还是 429
        msgs = history + [{"role": "user", "content": hint}]
        try:
            a = (client.chat_stream(msgs).get("content") or "").strip()
        except Exception as e:
            core.log(f"  [空简报兜底] 重生成失败: {e}")
            continue
        if len(a) >= min_len:
            return a
    return (answer or "").strip()


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


TASK_TEMPLATE = """今日{tag}({date})。数据口径: data/手机膜/手机膜 自动同步目录(重点手机膜)。请依次完成:
1) 用 sync_status 查看钉钉同步定时任务的运行状态。工具输出第二行 "[结论]" 即权威判定
   (✅成功/❌失败/⏳进行中): 结论为✅就写"同步正常", 严禁自行推断或编造故障原因
   (如"数据库锁定"等日志里没有的错误); "数据截至"反映的是审批内容最后更新时间,
   周末/节假日无审批时数据截至会自然停在上一场, 这不等于同步故障。
2) 用 scan_sync_data 实时扫描: 状态分布 / 数据截至时间 / 今日有动态明细(含所在Part)。
3) 用 pipeline_alerts 获取五层信号: 最近40天新增SKU(滚动40天, 含签样追踪) /
   全流程临期(空运60·海运69) / 已超期 / 环节停滞超目标(近似段级) / Part9新单待办(创建≤7天且审批中)。
4) 用 read_file 读取 流程复盘/output/预警_加急SKU.md 的前40行(不存在就跳过),
   仅作背景补充 —— 必须注明它是旧批次产物, 与实时数据冲突时以实时为准。
输出中文markdown简报(280~480字, 首行标题写"{tag} {date}"), 分六节:
① 同步状态与数据截至(若任一工具输出带 ⚠️ 数据过旧/同步停摆告警, 必须把
   "数据已N天未同步, ⑤的停滞天数含断供期偏大" 写在简报最顶部第一行, 再写其他内容;
   注意: 数据截至久未变≠同步故障, 故障与否只看 sync_status 的 [结论] 行)
② 今日动态(若 pipeline_alerts 的 Part9新单待办 层有单, 逐条带SKU与当前负责人列出)
③ 最近40天新增SKU(滚动40天, 编号+型号+创建日+签样状态: 已签样给"签样MM-DD + 当前环节/已持续/
   时效"(直接抄工具给的"现PartX 已持续N天/时效M天"), 未签样给"未签样N天/目标M天"并把超目标(⚠️)
   的排在前面说明; 这一节对应 data/report 事中汇报表的签样追踪)
④ 临期预警(按剩余天数升序, 带卡点与空运/海运口径) ⑤ 已超期与环节停滞TOP(带超标天数,
   并注明口径"段级停滞=距最后更新vs环节目标", 不要写成全流程耗时; scan_sync_data 与
   pipeline_alerts 的停滞口径不同, 同一SKU数字可能差1天以上, 引用时注明来源工具)
   注: ④⑤ 条目若带 "[到货: 地区 货件号]" 标注(来源=Part8到货表的"到货渠道仓"+"货件号"),
   必须原样带上——这就是加急催办要的"地区+货件号"(俗称"火箭"标记); 同批多站多货件照工具给的抄。
⑥ 行动建议(≤2条)。所有数字必须来自工具输出并注明来源; 每条提醒要可行动。
② 今日动态必须逐条写清"哪条SKU/型号 · 在哪个Part(环节) · 审批状态 · 最新审批节点+操作人+时间 ·
当前负责人", 直接抄工具给的明细, 不要只写编号; 工具没给节点明细就注明"仅字段更新"。"""


def main():
    # 计划任务经 cmd >> task.log 重定向: 不 reconfigure 时 stdout 走 cp936。
    # 只 reconfigure stdout 会让 traceback(走 stderr)仍是 GBK, 日志混编码成乱码,
    # 故两个流一起改成 UTF-8(2026-10-15 修)。
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="mini_agent 每日晨检简报")
    ap.add_argument("--push", action="store_true", help="若配置了钉钉webhook则推送")
    args = ap.parse_args()

    cfg = core.load_config()
    if not cfg["api_key"]:
        sys.exit("未配置 API Key")

    workmain = cfg.get("workmain_root") or r"C:\Users\dell\Desktop\work-main"
    core.set_root(workmain)  # 沙箱指向 work-main, 才能读预警文件

    registry = build_readonly_registry(cfg)
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    pm = now.hour >= 12          # 午后运行: 午后简报(独立文件/会话, 不覆盖晨检)
    tag = "午后简报" if pm else "晨检简报"
    suffix = "_pm" if pm else ""
    task = TASK_TEMPLATE.format(date=today, tag=tag)

    transcript = core.Transcript(
        f"brief_{now.strftime('%Y%m%d')}{suffix}")
    history = [{"role": "system", "content": core.build_system_prompt()}]
    client = core.LLMClient(cfg)
    answer = core.run_task(
        client, registry, core.YoloPolicy(), transcript, history, task, cfg,
        emit=lambda k, d: print(f"  [{k}] {str(d)[:120]}")
        if k in ("tool_call", "tool_result", "fatal") else None)
    transcript.log("end", {"reason": "brief", **client.usage})
    transcript.close()

    answer = ensure_brief_answer(client, history, answer)

    brief_dir = HERE / "briefs"
    brief_dir.mkdir(exist_ok=True)
    out = brief_dir / f"{today}{suffix}.md"
    body = f"# {tag} {today}\n\n{answer}\n\n> 用量: {client.usage_summary()}\n"
    out.write_text(body, encoding="utf-8")
    print(f"[{tag}] 简报已写入 {out}")
    print(f"[{tag}] {client.usage_summary()}")

    ret = 0
    if args.push:
        if len((answer or "").strip()) < BRIEF_MIN_CHARS:
            # 空壳防线: 数据都在(工具轮已成功), 只是生成被限流掐死 —— 不推送不占群,
            # 退出码非0让计划任务面板可见, 人工/下次运行补上
            print(f"[{tag}] ⚠️ 正文不足{BRIEF_MIN_CHARS}字(限流掐空), 已落盘但"
                  f"不推送钉钉; 请稍后手动重跑 run_brief.bat 补发")
            ret = 1
        else:
            try:
                import dingtalk_push
                ok, msg = dingtalk_push.send(f"{tag} {today}",
                                             answer[:1800] or "(空)")
                print(f"[{tag}] 推送{'成功' if ok else '失败'}: {msg}")
            except Exception as e:
                print(f"[{tag}] 推送异常(不影响落盘): {e}")
    return ret


if __name__ == "__main__":
    sys.exit(main())
