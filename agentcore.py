#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agentcore.py — 通用 agent 引擎(前端无关)

从 mini_agent.py 抽取的执行内核: LLM客户端/工具注册表/权限策略/上下文压缩/
会话持久化/主循环/反思, 全部通过 **事件回调(emit)** 对外汇报进度, 不依赖任何
前端(CLI/Web/钉钉机器人都是它的薄适配器):

    emit(kind, data) 事件类型:
      task_start   {task}
      assistant    {text}                     一段助手文本
      tool_call    {name, args}               args为原始JSON字符串(已截断)
      tool_result  {name, result}             完整结果(已截断)
      permission_denied {name}
      compact      {tokens, dropped}          触发上下文压缩
      task_end     {answer, usage}            usage=累计token统计
      fatal        {message}                  不可重试错误
      max_turns    {}

权限模型: 引擎只认 PermissionPolicy.allow(tool, kwargs) -> bool,
阻塞等待由策略实现(CLI 用 input(), Web 用事件+HTTP审批)。
"""

from __future__ import annotations

import difflib
import fnmatch
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("缺少依赖 requests: pip install -r requirements.txt")

# ============================================================
# 配置
# ============================================================

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
PLUGINS_DIR = HERE / "plugins"

EXCLUDE_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
                ".idea", "sessions"}
MAX_TOOL_OUTPUT = 8_000        # 单次工具结果回灌模型的字符上限
MAX_FILE_BYTES = 1_000_000     # grep/read 跳过的单文件大小上限

DEFAULTS = {
    "base_url": "https://api.z.ai/api/coding/paas/v4",
    "api_key": "",
    "model": "glm-5.3",
    "temperature": 0.2,
    "max_turns": 30,                 # 单任务最多对话轮数(防失控)
    "context_budget_tokens": 48_000,  # 粗估token超过即触发压缩
    "request_timeout": 120,
}


def load_config() -> dict:
    """config.json 为基础, 环境变量 AGENT_* 可覆盖(密钥优先走环境变量)."""
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text("utf-8")))
        except json.JSONDecodeError as e:
            sys.exit(f"config.json 解析失败: {e}")
    for env, key in (("AGENT_API_KEY", "api_key"),
                     ("AGENT_BASE_URL", "base_url"),
                     ("AGENT_MODEL", "model")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    return cfg


def est_tokens(text: str) -> int:
    """粗估token(中英混合约2字符=1token). 只用于压缩触发判断, 不求精确."""
    return max(1, len(text) // 2)


def clip(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...(超{limit}字符已截断)"


def log(msg: str):
    """引擎侧控制台日志(服务进程的stdout, 与前端事件流互不干扰)."""
    print(msg, flush=True)


# ============================================================
# LLM 客户端 (OpenAI 兼容协议 + 用量统计)
# ============================================================

class LLMClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.endpoint = cfg["base_url"].rstrip("/") + "/chat/completions"
        self.session = requests.Session()
        self.usage = {"calls": 0, "prompt_tokens": 0,
                      "completion_tokens": 0, "total_tokens": 0}

    def _acc(self, u):
        if not isinstance(u, dict):
            return
        self.usage["calls"] += 1
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            self.usage[k] += int(u.get(k) or 0)

    def usage_summary(self) -> str:
        u = self.usage
        return (f"LLM调用{u['calls']}次, prompt {u['prompt_tokens']:,} + "
                f"completion {u['completion_tokens']:,} = {u['total_tokens']:,} tokens")

    def chat(self, messages: list, tools: list | None = None) -> dict:
        """一次模型调用, 返回 assistant 消息 dict. 429/5xx/网络错误自动退避重试."""
        payload = {"model": self.cfg["model"], "messages": messages,
                   "temperature": self.cfg["temperature"]}
        if tools:
            payload["tools"] = tools
        headers = {"Authorization": f"Bearer {self.cfg['api_key']}"}
        last_err = None
        for attempt in range(1, 4):
            try:
                r = self.session.post(self.endpoint, json=payload, headers=headers,
                                      timeout=self.cfg["request_timeout"])
                if r.status_code == 429 or r.status_code >= 500:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                if r.status_code >= 400:
                    raise _FatalError(f"HTTP {r.status_code}: {r.text[:300]}")
                data = r.json()
                self._acc(data.get("usage"))
                return data["choices"][0]["message"]
            except _FatalError:
                raise
            except Exception as e:  # 网络/限流/服务端错误 → 重试
                last_err = e
                if attempt < 3:
                    wait = 2 ** attempt
                    log(f"  [重试] 第{attempt}次调用失败({e}), {wait}s后重试")
                    time.sleep(wait)
        raise RuntimeError(f"LLM调用失败(已重试3次): {last_err}")


class _FatalError(RuntimeError):
    """4xx(除429)等不可重试错误."""


# ============================================================
# 沙箱与内置通用工具 (与任何项目无关; 领域工具走 plugins/)
# ============================================================

ROOT = Path.cwd()  # 沙箱根目录, 由适配器调 set_root() 重设


def set_root(path) -> Path:
    global ROOT
    ROOT = Path(path).resolve()
    return ROOT


def safe_path(rel: str) -> Path:
    """路径安全: 所有文件工具只能访问沙箱 ROOT 内的相对路径."""
    p = (ROOT / rel).resolve()
    root_s = os.path.normcase(str(ROOT))
    ps = os.path.normcase(str(p))
    if ps != root_s and not ps.startswith(root_s + os.sep):
        raise ValueError(f"路径越界(只能访问沙箱内: {ROOT}): {rel}")
    return p


def t_read_file(path: str, offset: int = 0, limit: int = 400) -> str:
    p = safe_path(path)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {path}")
    if p.stat().st_size > MAX_FILE_BYTES * 5:
        raise ValueError(f"文件过大(>5MB), 请先用grep定位再分段读取")
    lines = p.read_text("utf-8", errors="replace").splitlines()
    total = len(lines)
    sel = lines[offset:offset + limit]
    head = f"[{path} 共{total}行, 显示第{offset + 1}~{min(offset + limit, total)}行]\n"
    return head + "\n".join(sel) if sel else head + "(空范围)"


def t_list_dir(path: str = ".") -> str:
    p = safe_path(path)
    if not p.is_dir():
        raise NotADirectoryError(f"不是目录: {path}")
    rows = []
    for ch in sorted(p.iterdir()):
        if ch.name in EXCLUDE_DIRS:
            continue
        if ch.is_dir():
            rows.append(f"{ch.name}/")
        else:
            try:
                rows.append(f"{ch.name}  {ch.stat().st_size:,}B")
            except OSError:
                rows.append(ch.name)
    body = "\n".join(rows) if rows else "(空)"
    return f"[{path}] 共{len(rows)}项:\n{body}"


def t_grep(pattern: str, glob: str = "*", max_results: int = 50) -> str:
    """按正则在沙箱内全文检索, 返回 path:行号:内容."""
    rx = re.compile(pattern)
    hits, scanned = [], 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            rel = os.path.relpath(os.path.join(dirpath, fn), ROOT).replace("\\", "/")
            if not fnmatch.fnmatch(rel, glob):
                continue
            scanned += 1
            if scanned > 3000:
                break
            fp = Path(dirpath) / fn
            try:
                if fp.stat().st_size > MAX_FILE_BYTES:
                    continue
                text = fp.read_text("utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # 二进制(xlsx/zip等)或不可读, 跳过
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel}:{i}:{line.strip()[:200]}")
                    if len(hits) >= max_results:
                        return (f"扫描{scanned}个文件, 命中{len(hits)}行(达上限):\n"
                                + "\n".join(hits))
    if not hits:
        return f"扫描{scanned}个文件, 无命中."
    return f"扫描{scanned}个文件, 命中{len(hits)}行:\n" + "\n".join(hits)


def t_write_file(path: str, content: str) -> str:
    p = safe_path(path)
    existed = p.exists()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"已写入 {path} ({len(content.splitlines())}行, {'覆盖已有文件' if existed else '新建'})"


def t_run_python(code: str) -> str:
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    try:
        r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
    except subprocess.TimeoutExpired:
        return "[工具错误] 执行超过120秒被终止, 请拆小任务"
    parts = [f"exit_code={r.returncode}"]
    if r.stdout.strip():
        parts.append("--- stdout ---\n" + r.stdout.strip())
    if r.stderr.strip():
        parts.append("--- stderr ---\n" + r.stderr.strip())
    if len(parts) == 1:
        parts.append("(无输出)")
    return clip("\n".join(parts))


def t_save_memory(content: str) -> str:
    """跨会话记忆: 追加到 agent 主目录的 MEMORY.md, 每次启动注入系统提示."""
    line = f"- [{datetime.now().strftime('%Y-%m-%d')}] {content.strip()}"
    with open(HERE / "MEMORY.md", "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return f"已记入跨会话记忆 MEMORY.md: {line}"


# ============================================================
# 工具注册表 + 权限策略 (Registry / PermissionPolicy)
# ============================================================

class Tool:
    def __init__(self, name: str, desc: str, params: dict, level: str, func,
                 preview_fn=None):
        self.name = name
        self.description = desc
        self.parameters = params   # JSON Schema
        self.level = level         # "read" 自动放行 / "write" 逐次确认
        self.func = func
        self.preview_fn = preview_fn  # 可选: 确认前的自定义预览(如diff)

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.parameters}}

    def preview(self, kwargs: dict) -> str:
        if self.preview_fn:
            try:
                return self.preview_fn(kwargs)
            except Exception:
                pass
        return f"[写操作] {self.name}({clip(json.dumps(kwargs, ensure_ascii=False), 600)})"


class Registry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, t):
        self._tools[t.name] = t

    def schemas(self) -> list:
        return [t.schema() for t in self._tools.values()]

    def get(self, name: str):
        return self._tools.get(name)

    def names(self) -> list:
        return list(self._tools)

    def execute(self, name: str, kwargs: dict) -> str:
        """执行工具; 任何异常都转成文本回灌模型(自愈), 不中断主循环."""
        t = self._tools.get(name)
        if t is None:
            return f"[工具错误] 未注册的工具: {name} (可用: {', '.join(self._tools)})"
        try:
            out = t.func(**kwargs)
            return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        except TypeError as e:
            return f"[参数错误] {e}\n请对照该工具的参数定义修正后重试."
        except Exception as e:
            return f"[工具错误] {type(e).__name__}: {e}\n请根据错误信息修正后重试."


class YoloPolicy:
    """全放行(--yolo, 风险自负)."""

    def __init__(self):
        self.always: set[str] = set()

    def allow(self, tool, kwargs: dict) -> bool:
        return True


def _preview_write(kwargs: dict) -> str:
    """write_file 的确认预览: 已有文件给 unified diff, 新文件给头10行."""
    path = str(kwargs.get("path", "?"))
    content = str(kwargs.get("content", ""))
    new = content.splitlines()
    try:
        p = safe_path(path)
    except Exception:
        return f"[写操作] write_file({path}) 新建 {len(new)} 行"
    if p.exists():
        try:
            old = p.read_text("utf-8", errors="replace").splitlines()
        except OSError:
            old = ["(旧文件读取失败)"]
        diff = list(difflib.unified_diff(old, new, lineterm="", n=1))[:40]
        body = "\n".join(diff) if diff else "(内容无变化)"
        return f"[写操作] write_file({path}) 覆盖: {len(old)}行 → {len(new)}行, diff:\n{body}"
    shown = "\n".join(new[:10]) if new else "(空文件)"
    return f"[写操作] write_file({path}) 新建 {len(new)} 行, 预览:\n{shown}"


_REG_REF: Registry | None = None  # research 工具运行时引用当前注册表(含插件)


def build_registry() -> Registry:
    global _REG_REF
    r = Registry()
    _REG_REF = r
    r.register(Tool(
        "read_file", "读取沙箱内文本文件(按行, 支持分段). 二进制/xlsx不可读.",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "相对沙箱的路径"},
            "offset": {"type": "integer", "minimum": 0, "description": "起始行偏移(0基)"},
            "limit": {"type": "integer", "minimum": 1, "description": "读取行数, 默认400"}},
         "required": ["path"]}, "read", t_read_file))
    r.register(Tool(
        "list_dir", "列出目录内容(文件带大小, 目录带/后缀).",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "相对沙箱的目录, 默认根"}},
         "required": []}, "read", t_list_dir))
    r.register(Tool(
        "grep", "按正则全文检索沙箱内文本文件, 返回 '相对路径:行号:该行内容'. "
                "pattern为Python re语法; glob匹配相对路径(如 '*.py' / '**/*.md').",
        {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "正则表达式"},
            "glob": {"type": "string", "description": "路径通配过滤, 默认 '*' 全部"},
            "max_results": {"type": "integer", "minimum": 1, "description": "命中上限, 默认50"}},
         "required": ["pattern"]}, "read", t_grep))
    r.register(Tool(
        "write_file", "写入/覆盖文本文件(整体覆盖, 需用户确认, 确认时会看到diff预览).",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "相对沙箱的路径"},
            "content": {"type": "string", "description": "完整文件内容"}},
         "required": ["path", "content"]}, "write", t_write_file,
        preview_fn=_preview_write))
    r.register(Tool(
        "run_python", "在沙箱目录下运行一段Python代码(120秒超时, 需用户确认). "
                      "适合: 数据统计/pandas处理/批量文件操作. stdout和stderr都会返回.",
        {"type": "object", "properties": {
            "code": {"type": "string", "description": "要执行的Python代码"}},
         "required": ["code"]}, "write", t_run_python))
    r.register(Tool(
        "save_memory", "把一条重要结论/用户偏好/踩过的坑写入跨会话记忆(MEMORY.md, "
                       "之后所有会话都可见, 需确认). 只记要点, 一条一行, 不要记敏感数据.",
        {"type": "object", "properties": {
            "content": {"type": "string", "description": "要记住的要点, 一句话"}},
         "required": ["content"]}, "write", t_save_memory))
    r.register(Tool(
        "research",
        "并行派出最多4个只读子agent分头调查(每个独立上下文互不污染), 返回各自结论. "
        "适合: 多文件/多方向的探索、互不依赖的并行查证. tasks为任务数组. "
        "子agent只有只读工具, 需要写操作的任务留给你自己执行.",
        {"type": "object", "properties": {
            "tasks": {"type": "array", "items": {"type": "string"},
                      "description": "子任务列表, 最多4个"},
            "max_turns": {"type": "integer", "minimum": 1,
                          "description": "每个子agent最大轮数, 默认10"}},
         "required": ["tasks"]}, "read", t_research))
    return r


# ============================================================
# 插件系统 (plugins/*.py 定义 register(registry, cfg) 即被加载)
# ============================================================

def load_plugins(registry: Registry, cfg: dict) -> int:
    """加载 plugins/ 下所有插件; 单个插件失败不影响整体. 返回加载成功数."""
    if not PLUGINS_DIR.is_dir():
        return 0
    sys.path.insert(0, str(PLUGINS_DIR))
    loaded = 0
    for f in sorted(PLUGINS_DIR.glob("[!_]*.py")):
        try:
            mod = importlib.import_module(f.stem)
            n = 0
            if hasattr(mod, "register"):
                n = mod.register(registry, cfg) or 0
            loaded += 1
            log(f"[插件] {f.stem}: 注册 {n} 个工具")
        except Exception as e:
            log(f"[插件] {f.stem} 加载失败(跳过): {type(e).__name__}: {e}")
    return loaded


# ============================================================
# 系统提示 + 项目记忆 (对应 CLAUDE.md 机制, 任何目录放 AGENT.md 即生效)
# ============================================================

SYSTEM_TEMPLATE = """你是 mini_agent, 一个运行在本地目录上的通用任务型agent, 通过调用工具完成用户的任务.

规则:
1. 优先用工具获取事实, 不要凭空猜测文件内容/执行结果/业务数字.
2. 回答中的数字必须来自工具输出, 并注明来源(工具名/文件路径); 没有工具依据就说"未验证".
3. 工具报错时读取错误信息, 修正参数重试(自愈), 不要轻易放弃.
4. 被用户拒绝的工具调用, 换一种方案或向用户说明理由.
5. 若加载了领域插件工具, 按其描述的口径使用; 不确定数据含义时先用只读工具查证.
6. 回答用中文, 先结论后细节, 简洁直接.

工作沙箱(工具只能访问其内): {root}
当前日期: {today}
"""


def build_system_prompt() -> str:
    sp = SYSTEM_TEMPLATE.format(root=ROOT, today=datetime.now().strftime("%Y-%m-%d"))
    sp += _memory_block()
    return sp


def _memory_block() -> str:
    """跨会话记忆 + 项目记忆的注入块(主agent与子agent共用)."""
    block = ""
    mem = HERE / "MEMORY.md"
    if mem.exists():
        block += ("\n# 跨会话记忆(MEMORY.md, 历次会话沉淀的要点, 优先级高于一般常识)\n"
                  + clip(mem.read_text("utf-8", errors="replace"), 4_000))
    am = ROOT / "AGENT.md"
    if am.exists():
        block += ("\n# 项目记忆(AGENT.md, 用户维护的业务口径, 优先级高于一般常识)\n"
                  + clip(am.read_text("utf-8", errors="replace"), 6_000))
    return block


# ============================================================
# 子agent (独立上下文的只读调查员, 对应 Claude Code 的 Explore)
# ============================================================

SUBAGENT_SYS = """你是子agent, 由主agent派出独立完成一项调查子任务.

规则:
1. 你只有只读工具, 只调查不修改; 需要写操作的任务留给主agent.
2. 主agent只能看到你的最终结论(看不到你的过程), 结论必须自包含.
3. 按结构输出: 结论(1~2句) / 关键事实(带工具或文件来源) / 如有: 建议.
中文, 500字以内.

工作沙箱: {root}
当前日期: {today}
"""


def run_subagent(client: LLMClient, registry: Registry, task: str, cfg: dict,
                 name: str = "sub") -> str:
    """独立上下文的只读子agent: 自己的会话/系统提示/注册表副本, 结论返回主agent.
    上下文隔离 —— 子agent的中间过程不占用主agent的上下文窗口."""
    ro = Registry()
    for t in registry._tools.values():
        if t.level == "read":
            ro.register(t)
    sp = (SUBAGENT_SYS.format(root=ROOT, today=datetime.now().strftime("%Y-%m-%d"))
          + _memory_block())
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tr = Transcript(HERE / "sessions" / f"sub_{ts}_{name}.jsonl")
    history = [{"role": "system", "content": sp}]
    answer = run_task(client, ro, YoloPolicy(), tr, history, task, cfg, emit=None)
    tr.log("end", {"reason": "subagent", **client.usage})
    tr.close()
    return answer


def t_research(tasks, max_turns: int = 10) -> str:
    """并行扇出: 每个任务一个独立只读子agent(线程), 汇总各自结论.
    硬上限4个任务/每个15轮, 防失控烧token."""
    if isinstance(tasks, str):
        tasks = [tasks]
    if not isinstance(tasks, list) or not tasks:
        return "[参数错误] tasks 需为任务字符串数组"
    tasks = [str(t) for t in tasks[:4]]
    cfg = load_config()
    if not cfg.get("api_key"):
        return "[工具错误] 未配置 API Key"
    sub_cfg = dict(cfg, max_turns=max(1, min(int(max_turns), 15)))
    reg = _REG_REF if _REG_REF is not None else build_registry()
    results, errs = [""] * len(tasks), [""] * len(tasks)

    def _work(i: int, t: str):
        try:
            client = LLMClient(sub_cfg)  # 每个子agent独立client: 线程安全+用量隔离
            results[i] = run_subagent(client, reg, t, sub_cfg, name=f"t{i + 1}")
        except Exception as e:
            errs[i] = f"{type(e).__name__}: {e}"

    threads = [threading.Thread(target=_work, args=(i, t), daemon=True,
                                name=f"subagent-{i + 1}")
               for i, t in enumerate(tasks)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    parts = []
    for i, t in enumerate(tasks):
        body = results[i] if results[i] else f"(失败: {errs[i] or '未知错误'})"
        log(f"[research] 子任务{i + 1} 完成: {t[:50]}")
        parts.append(f"[子任务{i + 1}] {t}\n{clip(body, 2200)}")
    return clip("\n\n".join(parts), 9_000)


# ============================================================
# 上下文压缩 (compaction)
# ============================================================

COMPACT_SYS = ("你是会话压缩器. 把给定对话记录压缩成要点摘要, 必须保留: "
               "未完成的任务目标 / 已确认的结论与数字及其工具来源 / 关键文件路径 / "
               "用户明确的偏好或拒绝. 中文500字以内, 直接输出摘要.")


def maybe_compact(client: LLMClient, history: list, budget: int,
                   emit=None) -> None:
    """粗估token超预算时, 把早期消息压缩成一条摘要(在user边界切分, 不拆散工具对)."""
    if len(history) < 10:
        return
    total = sum(est_tokens(str(m.get("content") or "")) + 60 for m in history)
    if total < budget:
        return
    user_idx = [i for i, m in enumerate(history)
                if m.get("role") == "user"
                and not str(m.get("content", "")).startswith("[会话压缩]")]
    cuts = [i for i in user_idx if 4 <= i <= len(history) - 4]
    if not cuts:
        return
    target = len(history) // 2
    cut = max((i for i in cuts if i <= target), default=min(cuts))
    old = history[1:cut]
    if not old:
        return
    log(f"[压缩] 上下文约{total}tokens超预算{budget}, 压缩前{len(old)}条消息...")
    text = json.dumps([{k: v for k, v in m.items() if k in ("role", "content")}
                       for m in old], ensure_ascii=False)
    summary = client.chat([{"role": "system", "content": COMPACT_SYS},
                           {"role": "user", "content": clip(text, 40_000)}])
    summary = summary.get("content") or "(摘要生成失败, 保留原消息)"
    history[:] = ([history[0],
                   {"role": "user",
                    "content": "[会话压缩]以下是此前对话的摘要, 仅作背景, 不需要回复:\n" + summary}]
                  + history[cut:])
    if emit:
        emit("compact", {"tokens": total, "dropped": len(old)})


# ============================================================
# 会话持久化 (Transcript, JSONL)
# ============================================================

class Transcript:
    """每行一个JSON事件; kind=message 的行用于恢复历史上下文."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._fh = open(path, "a", encoding="utf-8")

    def log(self, kind: str, data: dict):
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "kind": kind}
        rec.update(data)
        self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self):
        self._fh.close()

    @staticmethod
    def load_messages(path: Path) -> list:
        msgs = []
        if not path.exists():
            return msgs
        for line in path.read_text("utf-8").splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("kind") == "message":
                m = obj["msg"]
                if m.get("role") != "system":  # system提示每次重新生成
                    msgs.append(m)
        return Transcript._patch_dangling(msgs)

    @staticmethod
    def _patch_dangling(msgs: list) -> list:
        """修复被中断的会话: assistant 的 tool_calls 若缺对应 tool 结果,
        补一条合成结果 —— 否则恢复会话后消息序列非法, API 会直接拒绝."""
        out, pending = [], {}
        synth = ("[中断] 该工具调用未完成(会话曾被中断), 结果未知. "
                 "如需要请重新执行该工具.")
        for m in msgs:
            r = m.get("role")
            if r == "assistant" and m.get("tool_calls"):
                out.append(m)
                for c in m["tool_calls"]:
                    pending[c.get("id", "")] = True
            elif r == "tool":
                pending.pop(m.get("tool_call_id", ""), None)
                out.append(m)
            else:  # user/下一条assistant: 先补齐此前悬空的调用
                for cid in list(pending):
                    out.append({"role": "tool", "tool_call_id": cid,
                                "content": synth})
                pending.clear()
                out.append(m)
        for cid in list(pending):
            out.append({"role": "tool", "tool_call_id": cid, "content": synth})
        return out


# ============================================================
# 主循环 (agentic loop) + 反思 (reflection) — 事件回调驱动
# ============================================================

def normalize(msg: dict) -> dict:
    m = {"role": "assistant", "content": msg.get("content") or ""}
    if msg.get("tool_calls"):
        m["tool_calls"] = msg["tool_calls"]
    return m


def run_task(client: LLMClient, registry: Registry, policy,
             transcript: Transcript, history: list, task: str, cfg: dict,
             emit=None, cancel=None) -> str:
    """执行一个任务: 模型→工具→结果回灌→再决策, 直到产出最终回答(或达轮数上限).
    cancel: 可选 threading.Event, 置位后在轮/工具边界优雅停止(steering 的基础)."""
    emit = emit or (lambda kind, data: None)
    emit("task_start", {"task": task})
    history.append({"role": "user", "content": task})
    transcript.log("message", {"msg": history[-1]})

    def _cancelled() -> bool:
        return cancel is not None and cancel.is_set()

    for _turn in range(1, cfg["max_turns"] + 1):
        if _cancelled():
            answer = "[用户中断] 任务已按用户要求停止."
            emit("cancelled", {})
            emit("task_end", {"answer": answer, "usage": dict(client.usage)})
            return answer
        maybe_compact(client, history, cfg["context_budget_tokens"], emit)
        try:
            msg = client.chat(history, tools=registry.schemas())
        except _FatalError as e:
            emit("fatal", {"message": f"{e} (请检查config.json的api_key/base_url/model)"})
            return f"[调用失败-不可重试] {e}"

        if (msg.get("content") or "").strip():
            emit("assistant", {"text": (msg["content"] or "").strip()})
        history.append(normalize(msg))
        transcript.log("message", {"msg": history[-1]})

        calls = msg.get("tool_calls") or []
        if not calls:
            answer = (msg.get("content") or "").strip()
            emit("task_end", {"answer": answer, "usage": dict(client.usage)})
            return answer

        for call in calls:
            fn = call.get("function") or {}
            name = fn.get("name", "?")
            raw = fn.get("arguments") or "{}"
            try:
                kwargs = json.loads(raw)
                if not isinstance(kwargs, dict):
                    raise ValueError
            except ValueError:
                result = f"[参数错误] arguments不是合法的JSON对象: {clip(raw, 200)}"
            else:
                emit("tool_call", {"name": name, "args": clip(raw, 300)})
                tool = registry.get(name)
                if _cancelled():
                    result = "[用户中断] 该调用未执行(任务被停止)."
                elif tool is None:
                    result = f"[工具错误] 未注册的工具: {name}"
                elif policy.allow(tool, kwargs):
                    result = clip(registry.execute(name, kwargs))
                else:
                    emit("permission_denied", {"name": name})
                    result = "[用户拒绝] 本次调用被拒绝, 请换方案或询问用户."
            emit("tool_result", {"name": name, "result": str(result)})
            history.append({"role": "tool", "tool_call_id": call.get("id", ""),
                            "content": str(result)})
            transcript.log("message", {"msg": history[-1]})

        if _cancelled():
            answer = "[用户中断] 任务已按用户要求停止."
            emit("cancelled", {})
            emit("task_end", {"answer": answer, "usage": dict(client.usage)})
            return answer

    emit("max_turns", {})
    return "(已达最大轮数上限, 任务未自然结束; 可提高config的max_turns或拆小任务)"


REFLECT_SYS = """你刚完成了一个任务, 请对本轮执行做自我反思(self-check):
1. 回答里的数字/事实是否都有工具输出作为依据? 逐条核对来源.
2. 是否存在未经验证就写出的断言?
3. 文件写入/命令执行是否安全且必要?
4. 任务目标是否有遗漏或偏离?
若发现具体错误, 直接给出修正结论; 若没有问题, 明确输出"反思通过".
不要重复原回答, 只输出检查结论(中文, 200字内)."""


def reflect(client: LLMClient, transcript: Transcript, history: list,
            task_start: int, answer: str, emit=None) -> str:
    """任务后反思: 把本轮工具轨迹+最终回答交给模型自检(纯文本, 不带工具)."""
    emit = emit or (lambda kind, data: None)
    lines = []
    for m in history[task_start:]:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            for c in m["tool_calls"]:
                fn = c.get("function") or {}
                lines.append(f"工具调用: {fn.get('name')} {clip(fn.get('arguments') or '', 120)}")
        elif role == "tool":
            lines.append(f"工具结果: {clip(str(m.get('content')), 600)}")
        elif role == "user" and not str(m.get("content", "")).startswith(("[会话压缩]", "[反思")):
            lines.append(f"用户任务: {clip(str(m.get('content')), 200)}")
    try:
        msg = client.chat([
            {"role": "system", "content": REFLECT_SYS},
            {"role": "user", "content": "任务轨迹:\n" + "\n".join(lines[:80])
             + f"\n\n最终回答:\n{clip(answer, 1500)}"}])
        critique = (msg.get("content") or "").strip()
    except Exception as e:
        critique = f"(反思调用失败: {e})"
    history.append({"role": "user",
                    "content": "[反思结论]上一轮自检结果, 供后续参考, 不需要回复:\n" + critique})
    transcript.log("reflect", {"critique": critique})
    emit("reflect", {"critique": critique})
    return critique


def _reflect_passed(critique: str) -> bool:
    """启发式判断反思结论是否通过.
    顺序: 失败/空 → 通过; 有明确"通过/未发现问题"标记 → 通过;
    再看问题词; 都没有 → 默认通过(模糊时宁可不动, 避免无谓修正轮)."""
    if not critique or critique.startswith("("):
        return True
    if any(n in critique for n in ("未发现", "无明显", "没有问题", "不存在问题",
                                   "未发现问题")):
        return True
    if any(g in critique for g in ("反思通过", "检查通过", "无问题", "无需修正")):
        return True
    if any(b in critique for b in ("需要修正", "需修正", "应修正", "错误结论",
                                   "存在错误", "未经验证", "缺乏依据", "有误",
                                   "遗漏了")):
        return False
    return True


def reflect_and_fix(client: LLMClient, registry: Registry, policy,
                    transcript: Transcript, history: list, task_start: int,
                    answer: str, cfg: dict, emit=None, cancel=None,
                    max_rounds: int = 1) -> str:
    """反思闭环: 反思发现具体问题时, 自动带着工具修正一轮(有界, 不递归反思)."""
    emit = emit or (lambda kind, data: None)
    critique = reflect(client, transcript, history, task_start, answer, emit=emit)
    for _ in range(max(0, int(max_rounds))):
        if _reflect_passed(critique):
            return answer
        if cancel is not None and cancel.is_set():
            return answer
        emit("fix_round", {"critique": clip(critique, 400)})
        transcript.log("fix_round", {"critique": clip(critique, 400)})
        fix_msg = ("[反思修正]上一轮反思指出了具体问题, 请: 修正错误结论 / "
                   "用工具补做缺失的验证, 然后给出修订后的最终回答.")
        answer = run_task(client, registry, policy, transcript, history,
                          fix_msg, cfg, emit=emit, cancel=cancel)
        if not answer or answer.startswith(("[", "(")):
            break
    return answer


# ============================================================
# 计划模式 (plan-then-execute, 对应 Claude Code 的 plan mode)
# ============================================================

PLAN_SYS = """你是任务规划器. 针对用户的任务, 结合可用工具制定一个简洁的执行计划.

要求:
- 3~6步, 每步一行: 序号. 动作(用什么工具/看什么文件) → 该步产出
- 涉及写文件/执行命令的步骤, 行首标注[写]
- 最后一行输出"预计轮次: N"
- 不执行任何工具, 只输出计划本体."""


def propose_plan(client: LLMClient, registry: Registry, task: str,
                 cfg: dict) -> str:
    tools_desc = "\n".join(f"- {t.name}({'写' if t.level == 'write' else '读'}): "
                           f"{t.description}" for t in registry._tools.values())
    msg = client.chat([
        {"role": "system", "content": PLAN_SYS + "\n可用工具:\n" + tools_desc},
        {"role": "user", "content": task}])
    return (msg.get("content") or "").strip()


def plan_and_run(client: LLMClient, registry: Registry, policy,
                 confirm, transcript: Transcript, history: list, task: str,
                 cfg: dict, emit=None, cancel=None) -> str:
    """计划模式: 生成计划 → confirm(plan)人工批准 → 按计划执行.
    confirm(plan_text)->bool 由适配器提供(CLI=input确认, Web=审批收件箱)."""
    emit = emit or (lambda kind, data: None)
    if cancel is not None and cancel.is_set():
        return run_task(client, registry, policy, transcript, history,
                        task, cfg, emit=emit, cancel=cancel)
    plan = propose_plan(client, registry, task, cfg)
    if not plan:
        emit("plan_rejected", {"plan": "(计划生成失败, 直接执行)"})
        return run_task(client, registry, policy, transcript, history,
                        task, cfg, emit=emit, cancel=cancel)
    emit("plan", {"plan": plan})
    if not confirm(plan):
        emit("plan_rejected", {"plan": plan})
        history.append({"role": "user",
                        "content": "[计划已否决]用户否决了该计划, 任务未执行, "
                                   "等待用户进一步指示:\n" + plan})
        transcript.log("message", {"msg": history[-1]})
        return "[计划被用户否决] 任务未执行。"
    emit("plan_approved", {})
    task2 = task + "\n\n[已批准的执行计划, 请严格按计划执行]\n" + plan
    return run_task(client, registry, policy, transcript, history,
                    task2, cfg, emit=emit, cancel=cancel)


# ============================================================
# 离线自检 (不需要API Key)
# ============================================================

def _raises(fn) -> bool:
    try:
        fn()
        return False
    except Exception:
        return True


def selftest(registry: Registry) -> int:
    testdir = ROOT / ".selftest_demo"
    shutil.rmtree(testdir, ignore_errors=True)
    passed, failed = [], []

    def check(name: str, cond: bool):
        (passed if cond else failed).append(name)
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")

    print(f"[自检] 沙箱: {ROOT}  已注册工具: {', '.join(registry.names())}")
    check("路径越界拦截", _raises(lambda: safe_path("../outside.txt")))
    r = registry.execute("write_file", {"path": ".selftest_demo/hello.txt",
                                        "content": "hello mini_agent\n编号123456789012345678\n"})
    check("write_file 写入", "已写入" in r)
    r = registry.execute("read_file", {"path": ".selftest_demo/hello.txt"})
    check("read_file 读取", "123456789012345678" in r)
    r = registry.execute("grep", {"pattern": "12345", "glob": ".selftest_demo/*"})
    check("grep 检索", "hello.txt" in r)
    r = registry.execute("run_python", {"code": "print('中文输出:', 6*7)"})
    check("run_python 执行(含中文编码)", "42" in r and "中文输出" in r)
    r = registry.execute("list_dir", {"path": ".selftest_demo"})
    check("list_dir 列目录", "hello.txt" in r)
    r = registry.execute("no_such_tool", {})
    check("未知工具报错回灌", "[工具错误]" in r)
    shutil.rmtree(testdir, ignore_errors=True)
    print(f"[自检] 通过 {len(passed)} 项, 失败 {len(failed)} 项")
    return 0 if not failed else 1
