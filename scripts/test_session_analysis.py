#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/test_session_analysis.py — 失败模式复盘分析器的离线单测(零 LLM, 零真实数据)

覆盖:
  1. load_db_stats: 自建 sqlite 夹具(四张表同 storage schema), 聚合数字精确到个位数
  2. load_db_stats 对缺失库/坏库的降级(available=False + note)
  3. load_jsonl_stats: kind/end/reflect/工具错误归一(未注册的工具)/路径误用/时间窗
  4. derive_findings: 六条规则各自触发/不触发(幻觉工具名/路径误用/重试率/空任务/
     running残留/审批拒绝率)
  5. render_markdown: 六节结构与关键行
  6. 只读保证: 跑完 load_db_stats 后夹具库字节不变(mode=ro)
"""
import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "scripts"))
from host_shim import rm_rf  # noqa: E402

import analyze_sessions as az  # noqa: E402

RESULTS = []
TMP = HERE / ".workbuddy" / "tmp_test_session_analysis"


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail and not cond else ""))


def _build_db(path: Path):
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE tasks(id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, mode TEXT,
      parent_id INTEGER, started TEXT, ended TEXT, status TEXT, answer TEXT,
      turns INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER);
    CREATE TABLE llm_calls(ts TEXT, task_id INTEGER, model TEXT, latency_ms INTEGER,
      status TEXT, retries INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER,
      stream INTEGER);
    CREATE TABLE tool_calls(ts TEXT, task_id INTEGER, tool TEXT, level TEXT,
      ok INTEGER, duration_ms INTEGER);
    CREATE TABLE approvals(ts TEXT, session TEXT, kind TEXT, name TEXT,
      decision TEXT, steer TEXT);
    """)
    con.executemany("INSERT INTO tasks(session,mode,status,turns,started) VALUES(?,?,?,?,?)", [
        ("brief_20261012", "chat", "ok", 5, "2026-10-12T09:30:01"),
        ("brief_20261012", "chat", "empty", 5, "2026-10-12T14:10:03"),
        ("eval_fanout", "chat", "running", None, "2026-09-24T13:56:41"),
        ("chat_x", "chat", "ok", 2, "2026-09-25T10:00:00"),
    ])
    con.executemany(
        "INSERT INTO llm_calls(ts,task_id,model,latency_ms,status,retries,"
        "prompt_tokens,completion_tokens,stream) VALUES(?,?,?,?,?,?,?,?,?)",
        [("2026-10-12T09:30:05", 1, "glm-4.7-flash", 8000, "ok", 0, 900, 1400, 1),
         ("2026-10-12T09:30:20", 1, "glm-4.5-flash", 24000, "ok", 1, 900, 1400, 1),
         ("2026-10-12T09:30:40", 1, "glm-4.5-flash", 22000, "ok", 2, 900, 1400, 1),
         ("2026-09-28T09:45:10", 3, "glm-5.3-flash", 7524, "error", 3, 500, 0, 1)])
    con.executemany(
        "INSERT INTO tool_calls(ts,task_id,tool,level,ok,duration_ms) VALUES(?,?,?,?,?,?)",
        [("2026-10-12T09:30:06", 1, "sync_status", "read", 1, 30),
         ("2026-10-12T09:30:06", 1, "scan_sync_data", "read", 1, 3800),
         ("2026-10-12T09:30:06", 1, "list_dir", "read", 0, 2),
         ("2026-10-12T09:30:06", 1, "read_file", "read", 0, 2)])
    con.executemany(
        "INSERT INTO approvals(ts,session,kind,name,decision) VALUES(?,?,?,?,?)",
        [("2026-10-12T09:30:06", "s1", "tool", "write_file", "allowed"),
         ("2026-10-12T09:30:06", "s1", "tool", "run_python", "denied"),
         ("2026-10-12T09:30:06", "s1", "tool", "write_file", "denied"),
         ("2026-10-12T09:30:06", "s1", "plan", "p", "denied")])
    con.commit()
    con.close()


def _build_sessions(d: Path):
    d.mkdir(parents=True, exist_ok=True)
    lines_a = [
        {"ts": "2026-10-12T09:30:01", "kind": "message",
         "msg": {"role": "user", "content": "今日晨检"}},
        {"ts": "2026-10-12T09:30:05", "kind": "message",
         "msg": {"role": "assistant", "content": "", "tool_calls": [
             {"id": "c1", "type": "function",
              "function": {"name": "write_file", "arguments": "{}"}}]}},
        {"ts": "2026-10-12T09:30:06", "kind": "message",
         "msg": {"role": "tool", "tool_call_id": "c1",
                 "content": "<untrusted_data>[工具错误] 未注册的工具: write_file"}},
        {"ts": "2026-10-12T09:30:07", "kind": "message",
         "msg": {"role": "tool", "tool_call_id": "c2",
                 "content": "[工具错误] NotADirectoryError: 不是目录: data/x.txt"}},
        {"ts": "2026-10-12T09:30:08", "kind": "reflect",
         "critique": "核对结论: 缺来源标记"},
        {"ts": "2026-10-12T09:30:09", "kind": "end", "reason": "brief"},
    ]
    lines_b = [
        {"ts": "2026-10-12T14:10:01", "kind": "message",
         "msg": {"role": "user", "content": "午后"}},
        {"ts": "2026-10-12T14:10:30", "kind": "message",
         "msg": {"role": "tool", "tool_call_id": "c3",
                 "content": "[工具错误] 未注册的工具: edit_file"}},
        {"ts": "2026-10-12T14:10:31", "kind": "end", "reason": "exit"},
    ]
    (d / "brief_20261012.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in lines_a), encoding="utf-8")
    (d / "brief_20261012_pm.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in lines_b), encoding="utf-8")


def test_db_stats():
    print("\n== load_db_stats 聚合 ==")
    db = TMP / "t.db"
    if TMP.exists():
        rm_rf(TMP)
    TMP.mkdir(parents=True, exist_ok=True)
    _build_db(db)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    st = az.load_db_stats(db)
    after = hashlib.sha256(db.read_bytes()).hexdigest()
    check("只读保证: 跑完库字节不变", before == after)
    check("available=True", st["available"])
    check("llm_total=4", st["llm_total"] == 4, str(st["llm_total"]))
    m47 = st["llm"]["glm-4.7-flash"]
    check("4.7-flash: 1调用0错0重试", m47["calls"] == 1 and m47["errors"] == 0
          and m47["retried"] == 0)
    m45 = st["llm"]["glm-4.5-flash"]
    check("4.5-flash: 2调用2次触发重试", m45["calls"] == 2 and m45["retried"] == 2)
    check("失败明细2条里的error=1条", len(st["llm_fail"]) == 1
          and st["llm_fail"][0]["model"] == "glm-5.3-flash")
    check("tool_total=4 失败2", st["tool_total"] == 4 and st["tool_fail_total"] == 2)
    check("失败工具: list_dir/read_file 各1",
          sorted(x["tool"] for x in st["tool_fail"]) == ["list_dir", "read_file"])
    check("任务状态: ok=2 empty=1 running=1",
          st["tasks"]["by_status"] == {"ok": 2, "empty": 1, "running": 1})
    check("异常任务2个(empty+running各1)", len(st["bad_tasks"]) == 2)
    appr = {(a["kind"], a["decision"]): a["n"] for a in st["approvals"]}
    check("审批: tool allowed1/denied2 + plan denied1",
          appr == {("tool", "allowed"): 1, ("tool", "denied"): 2,
                   ("plan", "denied"): 1})


def test_db_degraded():
    print("\n== 库缺失/损坏降级 ==")
    st = az.load_db_stats(TMP / "no_such.db")
    check("缺失库: available=False 且带 note",
          not st["available"] and "不存在" in st["note"])
    bad = TMP / "bad.db"
    bad.write_bytes(b"not a sqlite file at all")
    st2 = az.load_db_stats(bad)
    check("坏库: 不抛异常, available=False", not st2["available"])


def test_jsonl_stats():
    print("\n== load_jsonl_stats ==")
    sd = TMP / "sessions"
    _build_sessions(sd)
    js = az.load_jsonl_stats(sd)
    check("2个文件", js["files"] == 2)
    check("kind 分布: message6/reflect1/end2",
          js["kinds"] == {"message": 6, "reflect": 1, "end": 2}, str(js["kinds"]))
    check("end reason: brief1/exit1",
          js["end_reasons"] == {"brief": 1, "exit": 1})
    check("reflect=1", js["reflect"] == 1)
    check("工具错误归一: '未注册的工具'×2(不同工具名归一类)",
          js["tool_errors"].get("未注册的工具") == 2, str(js["tool_errors"]))
    check("路径误用1次", js["path_errors"] == 1)
    check("时间窗起=09:30:01", js["window"][0] == "2026-10-12T09:30:01")
    check("时间窗止=14:10:31", js["window"][1] == "2026-10-12T14:10:31")
    js2 = az.load_jsonl_stats(TMP / "not_a_dir")
    check("目录缺失: files=0 不抛异常", js2["files"] == 0)


def test_findings():
    print("\n== derive_findings 规则触发 ==")
    db = TMP / "t.db"
    st = az.load_db_stats(db)
    js = az.load_jsonl_stats(TMP / "sessions")
    fs = az.derive_findings(st, js)
    joined = "\n".join(fs)
    check("F1 幻觉工具名触发(2次<3? 夹具仅2次→不触发)", "幻觉工具名" not in joined)
    # 构造触发场景: 3次未注册
    js3 = dict(js); js3["tool_errors"] = {"未注册的工具": 3, "NotADirectoryError": 0}
    js3["path_errors"] = 2
    fs3 = az.derive_findings(st, js3)
    j3 = "\n".join(fs3)
    check("F1 3次幻觉→触发", "幻觉工具名 3 次" in j3)
    check("F2 路径误用2次→触发", "路径误用 2 次" in j3)
    check("F4 重试率触发(2/4=50%≥30%)", "重试率偏高" in j3)
    check("F6 空结果任务触发", "空结果任务 1 个" in j6 if (j6 := "\n".join(fs3)) else False)
    check("F7 running残留触发", "崩溃/未收尾任务 1 个" in j6)
    check("F9 审批拒绝率触发(3/4=75%)", "审批拒绝率" in j6)
    # 全绿场景: 不该触发业务类建议
    clean_db = {"available": True, "llm_total": 10, "llm": {"m": {"retried": 0}},
                "llm_fail": [], "tool_total": 10, "tool_fail_total": 0,
                "tool_fail": [], "tools": {}, "tasks": {"by_status": {"ok": 10}},
                "bad_tasks": [], "approvals": [{"kind": "tool", "decision": "allowed", "n": 10}]}
    clean_js = {"files": 1, "tool_errors": {}, "path_errors": 0}
    fs_clean = az.derive_findings(clean_db, clean_js)
    check("全绿场景: 0 条建议", fs_clean == [])


def test_render():
    print("\n== render_markdown ==")
    db = TMP / "t.db"
    st = az.load_db_stats(db)
    js = az.load_jsonl_stats(TMP / "sessions")
    md = az.render_markdown(st, js, az.derive_findings(st, js), "2026-10-12")
    check("标题带日期", md.startswith("# 失败模式复盘 2026-10-12"))
    for sec in ["## 1. LLM 调用面", "## 2. 工具调用面", "## 3. 任务面",
                "## 4. 审批面", "## 5. 会话 jsonl 面", "## 6. 行动建议"]:
        check(f"含 {sec}", sec in md)
    check("LLM 表含模型行", "| glm-4.7-flash | 1 |" in md)
    check("工具失败明细含秒失败提示", "秒失败" in md)
    check("异常任务列出 task 编号", "empty" in md and "running" in md)
    check("建议带编号", "1. **" in md)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    test_db_stats()
    test_db_degraded()
    test_jsonl_stats()
    test_findings()
    test_render()
    rm_rf(TMP)
    n_ok = sum(1 for _, ok in RESULTS if ok)
    print(f"\n[总览] 失败模式复盘单测: {n_ok}/{len(RESULTS)}")
    return 0 if n_ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
