#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/test_brief_guard.py — 空简报防线的离线单测(零 LLM)

背景(2026-10-10 午后简报空壳事故): 免费档 429/1305 过载时, 服务端 HTTP 200 建连
成功但 SSE 流一个字没吐就断 —— 原逻辑当作正常返回, run_task 拿到空 answer 照写照推,
钉钉群里收到空壳简报。

覆盖:
  1. chat_stream 空流检测: 空流→进重试链换模型; 3连空→抛错; tool_calls轮不误杀
  2. chat 非流式空响应: 同样进重试链
  3. ensure_brief_answer: 短正文→弹空assistant+纯生成重试; 仍短→返回原值(不推送)
"""
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "scripts"))
from host_shim import rm_rf  # noqa: E402  绕开宿主删除钩子(见 host_shim 说明)

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail and not cond else ""))


# ---------- 打桩 ----------

def _sse(*chunks, tool_call=False):
    """构造 SSE 行序列: chunks 为 content 分片; 空流 = 只有 finish 帧无内容."""
    lines = []
    if tool_call:
        lines.append('data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
                     '"id":"c1","function":{"name":"sync_status","arguments":"{}"}}]}}]}')
    else:
        for c in chunks:
            lines.append('data: {"choices":[{"delta":{"content":%s}}]}'
                         % ('"' + c + '"'))
    lines.append('data: {"choices":[{"delta":{},"finish_reason":"stop"}]}')
    lines.append("data: [DONE]")
    return lines


class FakeResp:
    def __init__(self, sse_lines=None, json_data=None, status=200, text=""):
        self.status_code = status
        self._lines = sse_lines or []
        self._json = json_data
        self.text = text

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def _cfg(i=0):
    return {"base_url": f"http://stub{i}", "api_key": "k", "model": "m1",
            "model_fallbacks": ["m2", "m3"], "temperature": 0.2,
            "request_timeout": 5}


class StubClient:
    """ensure_brief_answer 的 client 打桩: 按序返回预置正文."""

    def __init__(self, contents):
        self.contents = list(contents)
        self.n = 0
        self.last_msgs = None

    def chat_stream(self, msgs, **kw):
        self.n += 1
        self.last_msgs = msgs
        return {"role": "assistant",
                "content": self.contents.pop(0) if self.contents else ""}


# ---------- 用例 ----------

def _isolated_db(name: str):
    """storage._reset 切到项目内独立 db; sqlite 连接持有文件锁,
    Windows 下 TemporaryDirectory 清不掉 → 用 .workbuddy/tmp_* + 忽略清理失败."""
    import storage
    d = HERE / ".workbuddy" / name
    d.mkdir(parents=True, exist_ok=True)
    storage._reset(str(d / "t.db"))
    return d


def _drop_db(d):
    """切回主库再删临时目录。

    sqlite 连接会一直持有 t.db 的句柄, 不去切走就 shutil.rmtree 不掉(Windows
    WinError 32); 之前用 ignore_errors 静默跳过, 于是 .workbuddy/tmp_* 一路堆积,
    现在还会撞上宿主的批量删除拦截, 把整个套件拖红。
    """
    import storage
    storage._reset(HERE / "agent.db")
    for _ in range(3):                      # Windows 句柄释放有延迟, 重试几次
        if not d.exists():
            return
        rm_rf(d)
        if not d.exists():
            return
        time.sleep(0.2)


def test_stream_empty_retry():
    print("\n== chat_stream 空流检测 ==")
    import agentcore as core
    d = _isolated_db("tmp_test_brief_guard")
    try:
        # 1) 空流 → 重试换模型 → 成功
        c = core.LLMClient(_cfg())
        c.session = FakeSession([
            FakeResp(_sse()),                                   # 空流(无content)
            FakeResp(_sse("今天简报正常了。", "各环节数据齐备。")),   # 正常流
        ])
        msg = c.chat_stream([{"role": "user", "content": "hi"}])
        check("空流触发重试且正文完整",
              "今天简报正常了。各环节数据齐备。" == (msg.get("content") or ""))
        check("重试换到备用模型", len(c.session.calls) == 2
              and c.session.calls[1]["json"]["model"] == "m2")

        # 2) 3 连空流 → 抛错(不静默返回空)
        c2 = core.LLMClient(_cfg(1))
        c2.session = FakeSession([FakeResp(_sse()) for _ in range(3)])
        try:
            c2.chat_stream([{"role": "user", "content": "hi"}])
            check("3连空流抛RuntimeError", False, "未抛错")
        except RuntimeError as e:
            check("3连空流抛RuntimeError", "重试" in str(e) or "失败" in str(e))
        check("3连空流恰好打满3次", len(c2.session.calls) == 3)

        # 3) 工具调用轮 content 为空但有 tool_calls → 不算空流, 不重试
        c3 = core.LLMClient(_cfg(2))
        c3.session = FakeSession([FakeResp(_sse(tool_call=True))])
        msg3 = c3.chat_stream([{"role": "user", "content": "hi"}],
                              tools=[{"type": "function", "function": {"name": "x"}}])
        check("tool_calls轮不误杀", (msg3.get("tool_calls") or [{}])[0]
              .get("function", {}).get("name") == "sync_status")
        check("tool_calls轮无重试", len(c3.session.calls) == 1)
    finally:
        _drop_db(d)


def test_chat_empty_retry():
    print("\n== chat 非流式空响应 ==")
    import agentcore as core
    d = _isolated_db("tmp_test_brief_guard2")
    try:
        c = core.LLMClient(_cfg(3))
        empty = {"choices": [{"message": {"role": "assistant", "content": ""}}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 0}}
        good = {"choices": [{"message": {"role": "assistant",
                                         "content": "正常回答"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4}}
        c.session = FakeSession([FakeResp(json_data=empty), FakeResp(json_data=good)])
        msg = c.chat([{"role": "user", "content": "hi"}])
        check("非流式空响应重试成功", msg.get("content") == "正常回答")
        check("非流式重试换模型", len(c.session.calls) == 2
              and c.session.calls[1]["json"]["model"] == "m2")
    finally:
        _drop_db(d)


def test_ensure_brief_answer():
    print("\n== ensure_brief_answer 兜底 ==")
    from daily_brief import ensure_brief_answer, BRIEF_MIN_CHARS

    long_text = "①同步状态正常。" * 30   # 远超下限
    # 1) 正文达标 → 直通, 不触发重生成
    cl = StubClient([])
    out = ensure_brief_answer(cl, [{"role": "user", "content": "t"}], long_text,
                              sleep_fn=lambda s: None)
    check("达标正文直通", out == long_text and cl.n == 0)

    # 2) 空正文 → 弹尾部空assistant + 追加纯生成指令 → 重生成成功
    hist = [{"role": "user", "content": "task"},
            {"role": "assistant", "content": ""}]
    cl2 = StubClient([long_text])
    sleeps = []
    out2 = ensure_brief_answer(cl2, hist, "", sleep_fn=sleeps.append)
    check("空正文重生成成功", out2 == long_text and cl2.n == 1)
    check("尾部空assistant被弹出", all(not (m.get("role") == "assistant"
          and not (m.get("content") or "").strip() and not m.get("tool_calls"))
          for m in hist))
    check("追加纯生成指令", cl2.last_msgs
          and cl2.last_msgs[-1]["role"] == "user"
          and "不要再调用任何工具" in cl2.last_msgs[-1]["content"])
    check("首次退避20s", sleeps == [20])

    # 3) 始终过短 → 用尽退避后返回原值(调用方不推送), 退避按 (20,40)
    cl3 = StubClient(["短", "还是短"])
    sleeps3 = []
    out3 = ensure_brief_answer(cl3, [{"role": "user", "content": "t"}], "",
                               sleep_fn=sleeps3.append)
    check("重生成仍短返回原值", out3 == "" and cl3.n == 2 and sleeps3 == [20, 40])

    # 4) history 尾部是 tool/非空消息 → 不弹
    h4 = [{"role": "user", "content": "t"},
          {"role": "assistant", "content": "调用工具", "tool_calls": [
              {"id": "c1", "function": {"name": "x", "arguments": "{}"}}]}]
    cl4 = StubClient([long_text])
    ensure_brief_answer(cl4, h4, "", sleep_fn=lambda s: None)
    check("尾部带tool_calls的assistant不弹", len(h4) == 2 and cl4.last_msgs[1] is h4[1])

    # 5) 下限值合理(与简报模板 280~480 字匹配)
    check("下限=100字符", BRIEF_MIN_CHARS == 100)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    test_stream_empty_retry()
    test_chat_empty_retry()
    test_ensure_brief_answer()
    n_ok = sum(1 for _, ok in RESULTS if ok)
    print(f"\n{'=' * 46}\n[空简报防线单测] {n_ok}/{len(RESULTS)} 通过")
    return 0 if n_ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
