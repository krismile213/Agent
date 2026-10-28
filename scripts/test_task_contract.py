#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/test_task_contract.py — 任务契约四件套单测(不耗 token, 用假 client)

覆盖 agentcore 的四个"让框架替模型兜底"的能力:
  1) spec     结构化输出约定只拼进任务文本, 不动 system, 不进 transcript 之外的地方
  2) output_parser  确定性后处理在最后一次调用之后跑; 抛异常不连累结果;
                    parse 后的答案才是 emit/落库/返回的答案
  3) warnings 完成度账本(run_task 收尾 emit "warning", 上层能看到"结果可能不完整")
  4) load_skill + 技能索引  只在系统提示里露索引, 正文按需读
运行: python scripts/test_task_contract.py
"""
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import agentcore as core  # noqa: E402

FAIL = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAIL.append(name)


class FakeClient:
    """按脚本吐出 assistant 消息, 不联网."""
    usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
             "total_tokens": 0}

    def __init__(self, msgs):
        self.msgs = list(msgs)
        self.seen = []
        self.warnings = []

    def _next(self, messages, tools=None):
        self.seen.append(json.loads(json.dumps(messages, ensure_ascii=False)))
        return self.msgs.pop(0) if self.msgs else {"role": "assistant", "content": ""}

    def chat(self, messages, tools=None):
        return self._next(messages, tools)

    def chat_stream(self, messages, tools=None, on_delta=None):
        return self._next(messages, tools)


def run(client, task="测试任务", **kw):
    reg = core.Registry()
    reg.register(core.Tool("echo", "回声", {"type": "object", "properties": {}},
                           "read", lambda: "ok"))
    tmp = Path(tempfile.mkdtemp()) / "t.jsonl"
    tr = core.Transcript(tmp)
    events = []
    cfg = {"max_turns": 4, "context_budget_tokens": 48000, "stream": False}
    answer = core.run_task(client, reg, core.YoloPolicy(), tr, [], task, cfg,
                           emit=lambda k, d: events.append((k, d)), **kw)
    tr.close()
    # 断点文件是尽力而为的副产物(任务收尾会清), 但这里多兜一层:
    # 用例失败中途退出时也不能把 .ckpt.json 留在临时目录外
    core.clear_checkpoint(str(tmp))
    return answer, events, client.seen


def main():
    print("[1] spec 结构化输出约定")
    spec = {"目标": "只输出 JSON", "必给字段": {"sku": "SKU 编号"},
            "禁止": ["解释", "寒暄"]}
    txt = core.format_spec(spec)
    check("format_spec 渲染字段", "只输出 JSON" in txt and "sku: SKU 编号" in txt
          and "· 解释" in txt)
    check("空 spec 不产生文本", core.format_spec(None) == "" and core.format_spec({}) == "")
    c = FakeClient([{"role": "assistant", "content": "答案"}])
    _, _, seen = run(c, spec=spec)
    user_msg = [m for m in seen[0] if m["role"] == "user"][-1]["content"]
    check("spec 拼进本轮任务文本", "测试任务" in user_msg and "只输出 JSON" in user_msg)
    check("spec 不进 system", all("只输出 JSON" not in (m.get("content") or "")
                                  for m in seen[0] if m["role"] == "system"))

    print("[2] output_parser 确定性后处理")
    c = FakeClient([{"role": "assistant", "content": "  草稿  "}])
    ans, ev, _ = run(c, output_parser=lambda s: f"[润色]{s.strip()}")
    check("parser 结果即返回值", ans == "[润色]草稿", ans)
    ends = [d for k, d in ev if k == "task_end"]
    check("task_end 带的是后处理后的答案", ends and ends[-1]["answer"] == "[润色]草稿")

    c = FakeClient([{"role": "assistant", "content": "原文"}])

    def boom(_s):
        raise RuntimeError("故意失败")

    ans, ev, _ = run(c, output_parser=boom)
    check("parser 抛异常时原样返回", ans == "原文", ans)
    check("parser 抛异常仍然正常收尾", any(k == "task_end" for k, _ in ev))

    c = FakeClient([{"role": "assistant", "content": "原文"}])
    ans, _, _ = run(c, output_parser=lambda s: "   ")
    check("parser 返回空白视为无效, 保留原文", ans == "原文", ans)

    print("[3] warnings 完成度账本")
    c = FakeClient([{"role": "assistant", "content": "答案"}])
    c.warnings = ["模型输出触顶(finish_reason=length), 正文可能被截断"]
    _, ev, _ = run(c)
    warns = [d["message"] for k, d in ev if k == "warning"]
    check("warnings 通过 warning 事件上抛", warns and "触顶" in warns[0], warns)
    check("_warn 去重", (lambda: (lambda cl: (cl._warn("x"), cl._warn("x"),
                                              len(cl.warnings) == 1)[-1])(
        core.LLMClient({"base_url": "http://x", "api_key": "", "model": "m"})))())

    print("[4] 技能包: 索引 + 按需加载")
    skills = core.list_skills()
    names = [s["name"] for s in skills]
    block = core._skills_block()
    if skills:
        check(f"索引里有 {len(skills)} 个技能: {names}", all(n in block for n in names))
        first = skills[0]
        body = core.t_load_skill(first["name"])
        check("load_skill 返回正文", not body.startswith("[工具错误]") and len(body) > 50)
        check("load_skill 未知名字给人话错误",
              core.t_load_skill("不存在的技能").startswith("[工具错误]"))
        # 索引只露一行描述, 不能把正文整段塞进系统提示
        check("系统提示只装索引不装正文", len(block) < 4000)
    else:
        check("没有技能时索引为空串", block == "")

    print()
    if FAIL:
        print(f"失败 {len(FAIL)} 条: {FAIL}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
