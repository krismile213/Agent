# Agent — 通用工作区 Agent 开发区

> **定位（2026-09-22 修订）**：开发一个**通用的 agent**——像 Claude Code 一样，
> 指向任何目录都能干活；前端、多轮对话、人工干预、reflection 等通用能力是主线。
> work-main 只是它的第一个**可选插件**，不是它的目的。
> 完整背景见 work-main/agent/企业级Agent开发方案.md（已附方向修订记录）。

## 快速开始

```powershell
cd C:\Users\dell\Desktop\Agent
copy config.example.json config.json    # 填入 api_key
python mini_agent.py --selftest         # 离线自检, 不消耗token
python mini_agent.py "统计当前目录下所有 .py 文件的总行数, 写入 report.md"
python mini_agent.py                    # 交互模式
python mini_agent.py --reflect "..."    # 任务完成后自检反思(发现问题自动修正一轮)
python mini_agent.py --plan "..."       # 计划模式: 先出计划人工批准再执行
python mini_agent.py --no-plugins ...   # 纯通用模式(不加载领域插件)
python mini_agent.py --cwd "任何目录" ... # 以任何目录为沙箱
python mini_agent.py --session work --resume  # 恢复历史会话
```

高级能力示例：`research` 工具让模型并行派出只读子agent分头调查（"用 research 并行查：A目录结构 + B文件里的X"）；计划模式把人的批准放在执行之前，适合影响面大的任务。

默认配置指向 **z.ai coding plan**（与 ZCode 同一订阅）：`base_url=https://api.z.ai/api/coding/paas/v4`，当前 `model=glm-5.3-flash`（可换 `glm-5.3` 更强）。密钥可走环境变量 `AGENT_API_KEY` / `AGENT_BASE_URL` / `AGENT_MODEL`。

## Web 前端（多轮对话 + 人工干预）

```powershell
python server.py                # http://127.0.0.1:8765, 自动打开浏览器
python server.py --port 9000 --cwd "任何目录"
python server.py --no-plugins --no-open
python scripts/smoke_web.py     # 端到端冒烟测试(含审批闭环)
```

网页功能：多轮对话、工具调用/结果实时流式展示（SSE）、**写操作审批收件箱**（diff 预览 + 允许/本会话总允许/拒绝）、会话侧栏（历史会话恢复）、反思开关、token 用量统计。

架构：**一个引擎，多个前端** —— `agentcore.py`（事件回调驱动，前端无关）← CLI 适配器 `mini_agent.py`（input 确认） / Web 适配器 `server.py`（引擎跑工作线程，事件经 `call_soon_threadsafe` 推给 SSE；审批时引擎线程阻塞在 `threading.Event` 上等浏览器 POST `/api/approve`）。以后加钉钉机器人就是第三个适配器。

## 核心机制（agentcore.py 引擎 + 两个适配器）

| 机制 | 代码位置 | Claude Code 对应概念 |
|---|---|---|
| 主循环 | `agentcore.run_task()` | agentic loop：模型→工具→结果回灌→再决策 |
| 工具注册表 | `agentcore.Registry` / `Tool` | 小而正交的工具集 + JSON schema |
| 权限策略 | `PermissionPolicy`（CLI=交互确认 / Web=审批收件箱） | permission tiers + 写操作 diff 预览 |
| 上下文管理 | `maybe_compact()` + `AGENT.md` | compaction + CLAUDE.md 项目记忆 |
| 会话持久化 | `Transcript` | JSONL transcript / `--resume` 恢复 |
| **插件系统** | `load_plugins()` → `plugins/` | skills / 扩展生态 |
| **反思** | `reflect()`（CLI `--reflect` / Web 勾选） | self-check / critique |
| **用量统计** | `LLMClient.usage` | cost awareness |
| **跨会话记忆** | `MEMORY.md` + `save_memory` 工具 | 长期记忆（LLM 可沉淀要点，每次启动注入） |
| **子agent扇出** | `run_subagent()` + `research` 工具 | Claude Code 的 Explore：并行调查、只读隔离、独立上下文互不污染（线程并行，硬上限 4 任务×15 轮）；**专属角色**（`role` 注入子agent系统提示）+ **工具白名单**（`tools`）；**verify 核查员**逐条验证结论依据 |
| **计划模式** | `plan_and_run()`（CLI `--plan` / Web 勾选"先出计划"） | plan-then-execute：先出计划 → 审批收件箱批准 → 严格按计划执行；**分步执行**（CLI `--stepwise` / Web 勾选"分步执行"）：每步之间暂停，可继续/停止/输入修改指令（中途转向） |
| **中断加固** | `Transcript._patch_dangling()` | 被中断的会话自动补合成工具结果，`--resume` 不再报错 |
| **停止/转向** | `run_task(cancel=...)` + Web 停止按钮 + `/api/stop` | 任务级 interrupt，停止后立即发新指令即 steering |
| **SSE 断线补发** | 事件 seq 编号 + `Last-Event-ID` 重放（每会话保留最近 500 条） | 刷新/断网不丢事件流 |

任何目录根部放一个 `AGENT.md`，agent 以该目录为 `--cwd` 启动时自动注入——这就是"打开什么项目都能帮忙"的口径机制（模板见 `AGENT.md.example`）。

## 插件系统（领域能力的正确打开方式）

内核完全不知道任何具体项目；`plugins/*.py` 定义 `register(registry, cfg)` 并返回工具数即被自动加载：

```python
# plugins/your_domain.py
from mini_agent import Tool, clip

def register(registry, cfg):
    registry.register(Tool("your_tool", "描述(给模型看的)", 
                           {...json schema...}, "read",  # 或 "write" 需确认
                           your_func))
    return 1  # 注册的工具数
```

- 单个插件失败只打日志不影响内核（`--no-plugins` 可整体禁用）
- 领域配置走 config.json（如 `workmain_root`）
- 现有插件：**workmain**（6 个工具，见下表）

### workmain 插件工具清单

| 工具 | 级别 | 说明 |
|---|---|---|
| `query_batches` | 读 | 列出 data/ 下全部批次（Part 数/最新文件时间） |
| `get_product_info` | 读 | 按SKU/型号查商品主数据（表头第2行已处理） |
| `query_sku_route` | 读 | 查SKU路线复盘现状（action/system 两套口径，带口径标签） |
| `search_kb` | 读 | BM25 检索本地知识库（需先 `python agent/kb/build_kb.py` 建索引） |
| `sync_status` | 读 | 钉钉同步定时任务最近日志 |
| `scan_sync_data` | 读 | **实时扫描自动同步目录**：状态分布/数据截至/今日动态SKU/停滞TOP（僵尸单据>90天单独计数），line 参数默认手机膜、可切手机壳 |
| `pipeline_alerts` | 读 | **四层流程信号**（权威表口径）：本周新增SKU / 全流程临期（**空运60·海运69**，运输模式取自路线总览）/ 已超期 / 环节停滞超目标（近似段级，目标值动态读《时效确认规则表.xlsx》，Part8 海运按 35 天）；每条带SKU/型号/卡点/剩余天数，可直接用于催办 |
| `run_route_check` | **写** | 重跑单SKU路线复盘（输出隔离到 agent_tmp，不覆盖正式汇总） |

## 安全模型

路径锁在 `--cwd` 沙箱（越界拦截有自检）；只读放行/写确认/`--yolo` 三档；工具输出 8000 字符截断；`max_turns` 防失控；工具异常转文本回灌由模型自愈。

## 评测（金标准回归）

```powershell
python eval/run_eval.py                  # 全部11用例(core+workmain), 报告落 eval/reports/
python eval/run_eval.py --suite core     # 不依赖workmain插件的8用例
python eval/run_eval.py --case arith_tool --model glm-5.3   # 单用例/换模型对比
```

11 个金标准用例覆盖：工具选择（该用哪个工具）、自愈（文件不存在后的诚实回答）、工具强制（数字必须来自 run_python）、记忆写入、workmain 域工具（G531 卡点/G570 ASIN/知识库红线）。断言式判定（工具使用/回答包含/自然完成/调用上限），不用 judge 模型，跑一遍约 5 万 token、3 分钟。**约定：换模型、改 prompt、改工具描述、改引擎逻辑后必须先跑评测再合入**——报告和失败用例的完整事件流（`eval/sessions/`）是排查回归的第一现场。

## MCP 双向接入（工具生态）

**方向一：把自己的工具暴露成 MCP server**（`mcp_server.py`，手写 stdio JSON-RPC 协议子集，零新依赖）——ZCode / Claude Code 等任何 MCP 客户端可直接调用全部 12 个工具（含 workmain 插件）。接入示例（客户端侧 mcp 配置）：

```json
{"mini-agent": {"command": "python", "args": ["C:\\Users\\dell\\Agent\\mcp_server.py"]}}
```

注意：文件工具的沙箱根 = server 进程的工作目录（跟随客户端启动时的 cwd）；写级工具经外部客户端调用时，权限确认由客户端侧负责。

**方向二：把外部 MCP server 的工具接入 agent**（`mcp_bridge.py`）——config.json 的 `mcp_servers` 数组里配置 stdio server，工具以 `<name>_<tool>` 前缀注册进 Registry，默认写级（需确认），只读 server 可设 `"level": "read"`。外部 MCP 生态（文件系统/浏览器/数据库 server 等）由此直接变成 agent 的工具。

测试：`python scripts/test_mcp.py`（9 项：SDK 客户端连手写 server + bridge 把自己的 server 当外部工具接入，双向闭环）。

## 定时晨检 + 钉钉推送（复用已有体系，不重复造轮子）

数据同步与计划任务框架在 `work-main/dingtalk/` 已存在，agent 侧只做消费：

```powershell
python daily_brief.py            # 立即跑一次晨检: 查同步日志+预警清单 → briefs/<日期>.md
python daily_brief.py --push     # 配置了webhook则同时推送到钉钉群
python dingtalk_push.py --title "测试" --text "hello" --dry   # 推送工具(dry不发送)
```

- **无人值守安全**：晨检用只读注册表（写级工具全部摘除），挂计划任务也零副作用
- **注册每日计划任务**（排在你现有同步任务之后，同一套模式）：`schtasks /Create /TN AgentDailyBrief /TR "python C:\Users\dell\Desktop\Agent\daily_brief.py --push" /SC DAILY /ST 09:35`
- **推送配置**：钉钉群 → 设置 → 智能群助手 → 添加自定义机器人（一分钟），webhook 填进 config.json 的 `dingtalk_webhook`（加签模式再填 `dingtalk_webhook_secret`；关键词模式建议关键词"简报"）

## 路线图（通用 agent 主线）

1. ~~**前端**：FastAPI + SSE 流式 Web UI（会话/审批收件箱/进度可视化）~~ ✅ 已交付（server.py + static/index.html，冒烟测试含审批闭环全通过）
2. ~~**多轮对话增强**：跨会话记忆、任务级 checkpoint、中断后转向（steering）、SSE 断线补发~~ ✅ 已交付（MEMORY.md + save_memory / 悬空 tool_calls 自动修复 / cancel 检查点 + Web 停止按钮 / 事件 seq + Last-Event-ID 重放；测试 `scripts/test_upgrade.py` 全通过）。更深层"断点自动续跑"留在后续
3. **人工干预增强**：~~任务停止/转向~~ ✅；~~计划审批~~ ✅ 已随计划模式交付；待做：批量审批、外发双确认、任务中途追加指令
4. **reflection 例行化**：~~反思结论落库~~ ✅ 已升级为闭环（`reflect_and_fix`：反思发现具体问题 → 带工具自动修正一轮，有界不递归）；~~金标准评测集回归~~ ✅ 已交付（`eval/run_eval.py` + 12 用例，全绿）；待做：评测集扩容、失败模式统计
5. **工具生态**：~~插件 API → MCP 化（同一函数两种暴露）~~ ✅ 已交付（`mcp_server.py` 手写协议双向暴露 + `mcp_bridge.py` 接入外部 server，测试 9/9）；"工具市场"待做
7. **多agent协作**：~~子agent扇出~~ ✅ + ~~计划模式~~ ✅ + ~~专属角色子agent(角色/工具白名单)~~ ✅ + ~~结果自动核查(verify核查员)~~ ✅ + ~~计划分步执行与中途转向~~ ✅；待做：Web 端步骤级文本修改指令、子agent间协作(结果互引)
6. **治理**：用量/成本预算、审计报表、多用户 RBAC；钉钉**入站机器人**（第三前端，Stream 模式收消息）待企业应用开通消息权限后接入——出站推送与定时晨检已交付（见上节）

## 学习练习建议

1. 写一个新插件（如 `plugins/file_organizer.py`：归档/去重工具包）
2. 把 `reflect()` 升级成"发现问题自动修正一轮"的闭环
3. 给 `run_task()` 加任务级 checkpoint：中断后能从最后一个工具调用续跑
4. 用 FastAPI+SSE 给交互模式套一个最小 Web 前端（路线图第 1 步）
5. 读 `sessions/*.jsonl`，复盘模型在哪些场景多走了弯路，针对性改工具描述
