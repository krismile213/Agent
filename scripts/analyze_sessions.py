#!/usr/bin/env python3
"""失败模式复盘: 聚合 agent.db(llm_calls/tool_calls/tasks/approvals) 与 sessions/*.jsonl,
输出 markdown 报告 + 规则化行动建议, 指导改工具描述/提示词。全程只读。

用法:
    python scripts/analyze_sessions.py [--db agent.db] [--sessions sessions ...]
                                       [--out analysis/session_analysis_<今天>.md]
产物: markdown 报告(默认落 analysis/ 目录), 同时在 stdout 打印摘要。
"""
import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

TOOL_ERR_RE = re.compile(r"\[工具错误\]\s*(.+)")
PATH_ERR_RE = re.compile(r"(NotADirectoryError|FileNotFoundError|IsADirectoryError|PermissionError)")


# ---------- DB 侧聚合 ----------

def load_db_stats(db_path: Path) -> dict:
    """只读聚合 agent.db 四张表。库不存在/缺表时返回空节并带 note。"""
    stats = {"available": False, "note": "", "llm": [], "tools": [], "tasks": {},
             "approvals": [], "llm_fail": [], "tool_fail": [], "bad_tasks": []}
    if not db_path.exists():
        stats["note"] = f"数据库不存在: {db_path}"
        return stats
    try:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as e:
        stats["note"] = f"数据库打开失败: {e}"
        return stats
    stats["available"] = True
    try:
        # --- llm_calls: 按模型聚合 + 失败明细 ---
        per_model = {}
        for model, n, err, retried, lat, pt, ct in con.execute(
                "SELECT model, COUNT(*), SUM(status!='ok'), SUM(retries>0),"
                " AVG(latency_ms), SUM(prompt_tokens), SUM(completion_tokens)"
                " FROM llm_calls GROUP BY model"):
            per_model[model] = {"calls": n, "errors": err or 0,
                                "retried": retried or 0,
                                "avg_latency_ms": round(lat or 0),
                                "prompt_tokens": pt or 0, "completion_tokens": ct or 0}
        stats["llm"] = per_model
        stats["llm_total"] = con.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
        for row in con.execute(
                "SELECT ts, task_id, model, latency_ms, retries FROM llm_calls"
                " WHERE status!='ok' ORDER BY ts"):
            stats["llm_fail"].append({"ts": row[0], "task_id": row[1],
                                      "model": row[2], "latency_ms": row[3],
                                      "retries": row[4]})
        # --- tool_calls: 按工具聚合 + 失败明细 ---
        per_tool = {}
        for tool, n, bad, dur in con.execute(
                "SELECT tool, COUNT(*), SUM(ok=0), AVG(duration_ms)"
                " FROM tool_calls GROUP BY tool"):
            per_tool[tool] = {"calls": n, "fails": bad or 0,
                              "avg_ms": round(dur or 0, 1)}
        stats["tools"] = per_tool
        stats["tool_total"] = con.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
        stats["tool_fail_total"] = con.execute(
            "SELECT COUNT(*) FROM tool_calls WHERE ok=0").fetchone()[0]
        for row in con.execute(
                "SELECT ts, task_id, tool, level, duration_ms FROM tool_calls"
                " WHERE ok=0 ORDER BY ts"):
            stats["tool_fail"].append({"ts": row[0], "task_id": row[1],
                                       "tool": row[2], "level": row[3],
                                       "duration_ms": row[4]})
        # --- tasks: 状态分布 + turns 分布 + 异常任务 ---
        stats["tasks"] = {"by_status": dict(con.execute(
            "SELECT status, COUNT(*) FROM tasks GROUP BY status").fetchall()),
            "by_mode": dict(con.execute(
            "SELECT mode, COUNT(*) FROM tasks GROUP BY mode").fetchall()),
            "turns": dict(con.execute(
            "SELECT turns, COUNT(*) FROM tasks WHERE turns IS NOT NULL"
            " GROUP BY turns ORDER BY turns").fetchall())}
        for row in con.execute(
                "SELECT id, session, mode, status, turns, started FROM tasks"
                " WHERE status IN ('empty','running') ORDER BY id"):
            stats["bad_tasks"].append({"id": row[0], "session": row[1],
                                       "mode": row[2], "status": row[3],
                                       "turns": row[4], "started": row[5]})
        # --- approvals: 类型×决策 ---
        stats["approvals"] = [
            {"kind": k, "decision": d, "n": n}
            for k, d, n in con.execute(
                "SELECT kind, decision, COUNT(*) FROM approvals"
                " GROUP BY kind, decision ORDER BY kind, decision")]
    except sqlite3.Error as e:
        stats["available"] = False
        stats["note"] = f"查询失败(表结构可能变化或文件损坏): {e}"
    finally:
        con.close()
    return stats


# ---------- jsonl 侧聚合 ----------

def _norm_tool_err(text: str) -> str:
    """把 [工具错误] 后面的文本归一成归类键: 去掉冒号后的具体参数。"""
    key = text.strip().split("\n")[0]
    key = re.split(r"[:：]", key, maxsplit=1)[0]
    return key[:40] or "(空)"


def load_jsonl_stats(dirs) -> dict:
    """扫描 sessions 目录: kind 分布/end reason/reflect/工具错误归类/会话数。"""
    stats = {"files": 0, "kinds": {}, "roles": {}, "end_reasons": {},
             "reflect": 0, "tool_errors": {}, "tool_err_examples": {},
             "path_errors": 0, "window": ["", ""]}
    if isinstance(dirs, (str, Path)):
        dirs = [dirs]
    for d in dirs:
        p = Path(d)
        if not p.is_dir():
            continue
        for f in sorted(p.glob("*.jsonl")):
            stats["files"] += 1
            for line in f.read_text("utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("ts", "")
                if ts:
                    if not stats["window"][0] or ts < stats["window"][0]:
                        stats["window"][0] = ts
                    if ts > stats["window"][1]:
                        stats["window"][1] = ts
                kind = rec.get("kind", "?")
                stats["kinds"][kind] = stats["kinds"].get(kind, 0) + 1
                if kind == "end":
                    r = rec.get("reason", "?")
                    stats["end_reasons"][r] = stats["end_reasons"].get(r, 0) + 1
                elif kind == "reflect":
                    stats["reflect"] += 1
                elif kind == "message":
                    m = rec.get("msg") or {}
                    role = m.get("role", "?")
                    stats["roles"][role] = stats["roles"].get(role, 0) + 1
                    if role == "tool":
                        text = str(m.get("content", ""))
                        mm = TOOL_ERR_RE.search(text)
                        if mm:
                            key = _norm_tool_err(mm.group(1))
                            stats["tool_errors"][key] = \
                                stats["tool_errors"].get(key, 0) + 1
                            stats["tool_err_examples"].setdefault(key, text[:120])
                        if PATH_ERR_RE.search(text):
                            stats["path_errors"] += 1
    return stats


# ---------- 行动建议(规则推导) ----------

def derive_findings(db: dict, js: dict) -> list:
    """基于聚合数据推导可执行建议; 每条都带触发它的数字。"""
    out = []
    # F1 幻觉工具名
    ghost = {k: v for k, v in js.get("tool_errors", {}).items()
             if "未注册的工具" in k}
    ghost_n = sum(ghost.values())
    if ghost_n >= 3:
        out.append(f"**幻觉工具名 {ghost_n} 次**(如 {', '.join(list(ghost)[:3])}): "
                   "模型调用了不存在的工具。建议: ① 子 agent 的系统提示里显式列出"
                   "「可用工具名白名单」; ② 在总系统提示加一句「只能调用本次会话已注册的工具，"
                   "不要臆造工具名」。")
    # F2 路径误用
    if js.get("path_errors", 0) >= 2:
        out.append(f"**路径误用 {js['path_errors']} 次**(NotADirectory/FileNotFound): "
                   "list_dir/read_file 参数把文件当目录或路径不存在。建议: 两个工具的 "
                   "description 各补一条「参数示例+易错点」(如 list_dir 必须指向目录、"
                   "read_file 先确认存在), 并在 description 里注明可配合 list_dir 探查。")
    # F3 参数格式错
    fmt = {k: v for k, v in js.get("tool_errors", {}).items()
           if "未注册" not in k and ("{" in k or "JSON" in k or "参数" in k)}
    if fmt:
        out.append(f"**工具参数格式错 {sum(fmt.values())} 次**: "
                   "arguments 不是合法 JSON。建议: 系统提示强调「tool_calls.arguments "
                   "必须是可解析的 JSON 字符串」。")
    # F4 限流重试率
    total = db.get("llm_total") or 0
    retried = sum(v.get("retried", 0) for v in db.get("llm", {}).values())
    if total and retried / total >= 0.3:
        worst = max(db["llm"].items(), key=lambda kv: kv[1]["retried"])[0] \
            if db.get("llm") else "?"
        out.append(f"**LLM 重试率偏高 {retried}/{total}({retried/total:.0%})**, "
                   f"集中在 {worst}: 免费档限流是主因。建议: 简报类定时任务错峰; "
                   "或在 agentcore payload 传 thinking:disabled 降低单次耗时。")
    # F5 彻底失败的 LLM 调用
    if db.get("llm_fail"):
        detail = "; ".join(f"{x['ts'][:16]} {x['model']}(重试{x['retries']}次仍失败)"
                           for x in db["llm_fail"][:3])
        out.append(f"**LLM 彻底失败 {len(db['llm_fail'])} 次**: {detail}。 "
                   "建议核对当时网络/额度; 若持续出现需加告警。")
    # F6 空结果任务
    empt = [t for t in db.get("bad_tasks", []) if t["status"] == "empty"]
    if empt:
        out.append(f"**空结果任务 {len(empt)} 个**: "
                   + "; ".join(f"task{t['id']}({t['session']}, {t['turns']}轮)"
                               for t in empt[:3])
                   + "。已知防线已修(ab2caba 空简报防线), 此处仅存档核对。")
    # F7 running 残留
    runs = [t for t in db.get("bad_tasks", []) if t["status"] == "running"]
    if runs:
        out.append(f"**崩溃/未收尾任务 {len(runs)} 个**: "
                   + "; ".join(f"task{t['id']}({t['session']})" for t in runs[:4])
                   + "。多为评测/冒烟会话进程被杀; 建议 sessions 清理或收尾时标记。")
    # F8 工具失败率
    tf = db.get("tool_fail_total") or 0
    tt = db.get("tool_total") or 0
    if tt and tf / tt >= 0.05:
        by_tool = Counter(x["tool"] for x in db.get("tool_fail", []))
        out.append(f"**工具失败率 {tf}/{tt}({tf/tt:.0%})**, 集中在 "
                   + ", ".join(f"{t}x{n}" for t, n in by_tool.most_common(3))
                   + "。优先查这些工具的参数校验。")
    # F9 审批拒绝率
    denied = sum(a["n"] for a in db.get("approvals", [])
                 if a["decision"] in ("denied", "stop"))
    allowed = sum(a["n"] for a in db.get("approvals", []))
    if allowed and denied / allowed >= 0.25:
        out.append(f"**审批拒绝率 {denied}/{allowed}({denied/allowed:.0%})**: "
                   "工具颗粒度或描述不清导致人工频繁叫停。建议把高频被拒工具合并或细化描述。")
    return out


# ---------- 渲染 ----------

def _pct(a, b):
    return f"{a / b:.0%}" if b else "-"


def render_markdown(db: dict, js: dict, findings: list, today: str) -> str:
    L = []
    L.append(f"# 失败模式复盘 {today}")
    L.append("")
    if not db.get("available"):
        L.append(f"> ⚠️ {db.get('note', '数据库不可用')} — DB 侧各节为空。")
        L.append("")
    if js.get("files"):
        L.append(f"- 数据窗口: {js['window'][0][:19]} ~ {js['window'][1][:19]}")
    L.append(f"- 会话文件: {js.get('files', 0)} 个 | "
             f"任务: {sum(db.get('tasks', {}).get('by_status', {}).values()) or 0} | "
             f"LLM 调用: {db.get('llm_total', 0)} | 工具调用: {db.get('tool_total', 0)}")
    L.append("")
    # 1 LLM
    L.append("## 1. LLM 调用面")
    L.append("")
    if db.get("llm"):
        L.append("| 模型 | 调用 | 错误 | 错误率 | 触发重试 | 重试率 | 平均延迟ms | "
                 "prompt tokens | completion tokens |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for m, v in sorted(db["llm"].items(), key=lambda kv: -kv[1]["calls"]):
            L.append(f"| {m} | {v['calls']} | {v['errors']} | {_pct(v['errors'], v['calls'])} "
                     f"| {v['retried']} | {_pct(v['retried'], v['calls'])} "
                     f"| {v['avg_latency_ms']} | {v['prompt_tokens']} | {v['completion_tokens']} |")
    else:
        L.append("(无数据)")
    L.append("")
    if db.get("llm_fail"):
        L.append("彻底失败明细:")
        L.append("")
        for x in db["llm_fail"]:
            L.append(f"- `{x['ts']}` task{x['task_id']} {x['model']} "
                     f"重试{x['retries']}次后仍失败, 耗时{x['latency_ms']}ms")
        L.append("")
    # 2 工具
    L.append("## 2. 工具调用面")
    L.append("")
    if db.get("tools"):
        L.append("| 工具 | 调用 | 失败 | 失败率 | 平均耗时ms |")
        L.append("|---|---|---|---|---|")
        for t, v in sorted(db["tools"].items(), key=lambda kv: -kv[1]["calls"]):
            L.append(f"| {t} | {v['calls']} | {v['fails']} | "
                     f"{_pct(v['fails'], v['calls'])} | {v['avg_ms']} |")
        L.append("")
        if db.get("tool_fail"):
            L.append("失败明细(耗时≤5ms 的多为参数/校验类秒失败, 非超时):")
            L.append("")
            for x in db["tool_fail"]:
                L.append(f"- `{x['ts']}` task{x['task_id']} {x['tool']} "
                         f"({x['level']}) {x['duration_ms']}ms")
            L.append("")
    else:
        L.append("(无数据)")
        L.append("")
    # 3 任务
    L.append("## 3. 任务面")
    L.append("")
    tk = db.get("tasks", {})
    if tk.get("by_status"):
        L.append("状态分布: " + ", ".join(f"{k}={v}" for k, v in
                                        sorted(tk["by_status"].items())))
        L.append("")
        L.append("轮数分布: " + ", ".join(f"{k}轮×{v}" for k, v in
                                        sorted(tk["turns"].items(),
                                               key=lambda kv: (kv[0] is None, kv[0]))))
        L.append("")
        if db.get("bad_tasks"):
            L.append("异常任务(empty=空结果 / running=未收尾):")
            L.append("")
            for t in db["bad_tasks"]:
                L.append(f"- task{t['id']} `{t['session']}` {t['status']} "
                         f"({t['turns']}轮, 起于 {str(t['started'])[:19]})")
            L.append("")
    else:
        L.append("(无数据)")
        L.append("")
    # 4 审批
    L.append("## 4. 审批面")
    L.append("")
    if db.get("approvals"):
        L.append("| 类型 | 决策 | 次数 |")
        L.append("|---|---|---|")
        for a in db["approvals"]:
            L.append(f"| {a['kind']} | {a['decision']} | {a['n']} |")
    else:
        L.append("(无数据)")
    L.append("")
    # 5 jsonl
    L.append("## 5. 会话 jsonl 面")
    L.append("")
    L.append(f"- 事件类型: " + ", ".join(f"{k}×{v}" for k, v in
                                       sorted(js.get("kinds", {}).items())))
    L.append(f"- 结束原因: " + (", ".join(f"{k}×{v}" for k, v in
                                       sorted(js.get("end_reasons", {}).items())) or "无"))
    L.append(f"- reflect 反思事件: {js.get('reflect', 0)} 次")
    if js.get("tool_errors"):
        L.append(f"- 工具错误归类:")
        for k, v in sorted(js["tool_errors"].items(), key=lambda kv: -kv[1]):
            L.append(f"  - `{k}` × {v}")
    else:
        L.append("- 工具错误归类: 无")
    L.append("")
    # 6 建议
    L.append("## 6. 行动建议(按数据自动推导)")
    L.append("")
    if findings:
        for i, f in enumerate(findings, 1):
            L.append(f"{i}. {f}")
    else:
        L.append("各面指标均在阈值内, 无自动建议。")
    L.append("")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="失败模式复盘分析器(只读)")
    ap.add_argument("--db", default=str(ROOT / "agent.db"), help="agent.db 路径")
    ap.add_argument("--sessions", action="append", default=None,
                    help="sessions 目录, 可传多次; 默认 sessions/")
    ap.add_argument("--out", default=None,
                    help="报告输出路径; 默认 analysis/session_analysis_<今天>.md")
    args = ap.parse_args(argv)

    sess = args.sessions or [str(ROOT / "sessions")]
    db = load_db_stats(Path(args.db))
    js = load_jsonl_stats(sess)
    findings = derive_findings(db, js)
    today = date.today().isoformat()
    md = render_markdown(db, js, findings, today)

    out = Path(args.out) if args.out else ROOT / "analysis" / f"session_analysis_{today}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"报告已写入 {out}")
    print(f"LLM 调用 {db.get('llm_total', 0)} (失败 {len(db.get('llm_fail', []))}, "
          f"触发重试 {sum(v.get('retried', 0) for v in db.get('llm', {}).values())})")
    print(f"工具调用 {db.get('tool_total', 0)} (失败 {db.get('tool_fail_total', 0)})")
    print(f"工具错误归类 {sum(js.get('tool_errors', {}).values())} 条, "
          f"reflect {js.get('reflect', 0)} 次")
    print(f"行动建议 {len(findings)} 条:")
    for i, f in enumerate(findings, 1):
        print(f"  {i}. {f[:100]}{'...' if len(f) > 100 else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
