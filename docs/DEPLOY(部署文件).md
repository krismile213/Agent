# 部署指南（服务器跑通手册）

> 目标：把这套 agent（CLI 定时简报 / 可选 Web 端）部署到一台新服务器上，从零到"钉钉群每天定时收到简报"全链路可验证。
> 所有字段名、路径、行为均以当前代码为准（2026-09-29 核对），不是通用模板。

## 0. 运行形态（先搞清楚要部署什么）

```
定时器(cron/计划任务) ──► run_brief.bat|.sh ──► daily_brief.py --push ──► 钉钉群机器人
                                                │ 只读工具扫描 work-main 数据目录
                                                └─► LLM API (OpenAI 兼容, 默认 z.ai)
```

三个外部依赖，缺一不可：
1. **LLM API**（OpenAI 兼容，默认 `api.z.ai`，免费 Flash 模型可用）
2. **钉钉群自定义机器人**（webhook + 加签密钥）
3. **数据目录** `work-main/`（简报的业务数据源；没有它 CLI 能跑，简报里工具会报 `[错误] 同步目录不存在`）

## 1. 前置条件（硬性）

| 项 | 要求 | 为什么 |
|---|---|---|
| Python | **≥ 3.10**（本机实测 3.14） | 代码用了 `str \| None` 等 3.10 语法 |
| 时区 | **Asia/Shanghai** | `今日动态`/`本周新增`/停滞天数全按本地日期算，时区错 = 数字全错 |
| 磁盘 | Agent 目录整体**可写** | `sessions/`、`briefs/`、`agent.db`、`kb_cache/` 都写在代码同目录 |
| 出网 | LLM API 域名 + `oapi.dingtalk.com` | 推送走 HTTPS 直连；有代理的服务器见 §7 |

时区验证（部署第一步就做）：

```bash
timedatectl                      # Linux: Time zone 应为 Asia/Shanghai
python -c "from datetime import datetime; print(datetime.now())"
```

## 2. 安装步骤

```bash
git clone https://github.com/krismile213/Agent.git agent && cd agent
python -m venv .venv
source .venv/bin/activate                # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 最小验证(不联网不花钱, 应 7/7 PASS):
python mini_agent.py --selftest
```

依赖分层（装不上时按需裁剪）：`requests` 必装；`pandas/openpyxl/numpy` 简报必需；`fastapi/uvicorn` 仅 Web 端；`mcp` 仅 MCP 接入；`rank_bm25/jieba` 仅知识库检索。

## 3. 配置 config.json

```bash
cp config.example.json config.json && chmod 600 config.json
```

逐字段核对（键名以真实 config 为准，example 里有注释）：

| 字段 | 必填 | 说明 |
|---|---|---|
| `base_url` | ✅ | OpenAI 兼容端点。**免费路线**：`https://api.z.ai/api/paas/v4` + Flash 模型（标准端点，不是 `/coding/paas/v4` 订阅端点——那个过期就全线 429） |
| `api_key` | ✅ | 也可用环境变量 `AGENT_API_KEY`（优先级更高，适合 CI/容器） |
| `model` | ✅ | 免费档推荐 `glm-4.7-flash` |
| `model_fallbacks` | 强烈推荐 | `["glm-4.5-flash"]` —— 免费档限流时重试自动换模型，无人值守的保命链 |
| `dingtalk_webhook` | 推送必填 | 群里"添加自定义机器人"→ 加签模式 → 得到的完整 webhook URL（含 `access_token=`） |
| `dingtalk_webhook_secret` | 推送必填 | 加签模式的 `SEC` 开头密钥（67 字符）。**泄漏 = 任何人可往群里发消息**，所以 config 权限 600 且永不入库 |
| `workmain_root` | 简报必填 | work-main 数据目录的**绝对路径**（见 §4） |
| `temperature/max_turns/context_budget_tokens/request_timeout` | 否 | 默认 0.2 / 30 / 48000 / 120 即可 |
| `rag_sources/rag_embed/rag_rerank` | 否 | 知识库检索；不用可删 |
| `storage/limits` | 否 | SQLite 与限流默认值即可 |

## 4. 数据目录（work-main）

`daily_brief.py` 启动时会 `set_root(workmain_root)`，之后所有工具相对该目录取数。最小可用结构：

```
work-main/
├── data/手机膜/手机膜/          # 9 个 Part*.xlsx(每环节一份, 同步时覆盖)
├── 流程复盘/
│   ├── 时效确认规则表.xlsx      # 停滞目标口径(缺失时用内置兜底值)
│   └── output/预警_加急SKU.md   # 可缺(提示词已注明"不存在就跳过")
└── dingtalk/logs/sync_*.log     # 上游同步日志(缺失时 sync_status 报目录不存在)
```

- 没有这些数据也能部署跑通：`load_plugins` 照常注册 8 个 workmain 工具，调用时返回 `[错误] 同步目录不存在`，简报会如实带出错误——适合先验证链路、后补数据。
- 数据怎么来：原环境靠 `DingTalkApprovalSync` 计划任务（`work-main/dingtalk/run_sync.bat`）从钉钉拉取。**服务器上要么迁移这个同步任务，要么定期手工放最新 xlsx**——同步断了简报会显式告警"数据已 N 天未更新"（2026-09-29 加的防线）。

## 5. 定时任务

### Linux（cron）

新建 `run_brief.sh`（与 run_brief.bat 等价）：

```bash
#!/bin/sh
# AgentDailyBrief — 每日简报(ASCII 日志路径, UTF-8 输出)
cd "$(dirname "$0")"
PYTHONUTF8=1 .venv/bin/python daily_brief.py --push >> briefs/task.log 2>&1
```

```bash
chmod +x run_brief.sh
crontab -e
# 晨检 09:30 + 午后 14:45(脚本按 now.hour>=12 自动切 tag, 不需两条命令不同)
30 9 * * *   /opt/agent/run_brief.sh
45 14 * * *  /opt/agent/run_brief.sh
```

**排序约束**：如果有数据同步任务，简报必须排在同步**之后**（本机是同步 09:00/14:30 → 简报 09:30/14:45），否则读到旧数据。

### Windows Server

沿用现成方案：`run_brief.bat` + 计划任务（本机 `schtasks.exe` 被安全策略拦时用 PowerShell）：

```powershell
$action = New-ScheduledTaskAction -Execute "C:\agent\run_brief.bat"
$trigger = New-ScheduledTaskTrigger -Daily -At "09:30"
Register-ScheduledTask -TaskName "AgentDailyBrief" -Action $action -Trigger $trigger
```

## 6. 端到端验证清单（按序执行，全过才算部署完成）

```bash
# 1. 依赖与语法
python -c "import agentcore, daily_brief, dingtalk_push, plugins.workmain; print('imports OK')"

# 2. 引擎自检(7/7 PASS)
python mini_agent.py --selftest

# 3. 数据面(不联网不花钱): scan 应输出 状态分布/数据截至/口径注
python -c "
import sys; sys.path.insert(0,'.')
import agentcore as core, json
cfg=core.load_config(); core.set_root(cfg['workmain_root'])
reg=core.build_registry(); core.load_plugins(reg,cfg)
print(reg._tools['scan_sync_data'].func(line='手机膜')[:300])"

# 4. 手动跑一次简报(不带 --push, 不惊动钉钉): 看 briefs/<今天>.md 是否生成、六节是否齐全
python daily_brief.py

# 5. 带推送跑一次: 钉钉群应收到消息
python daily_brief.py --push

# 6. 挂 cron, 次日核对
tail briefs/task.log        # 应见 "[晨检简报] 推送成功: 已发送"
```

第 4 步的常见失败：`briefs/` 没产物 + task.log 出现 `HTTP 429` → 看错误码：`1309` 套餐过期（换标准端点/充值）、`1302/1305` 限流（配 `model_fallbacks`，重试链会自动扛）。

## 7. 常见坑（全部真实踩过）

| 坑 | 症状 | 解法 |
|---|---|---|
| **时区不是东八区** | "今日动态"恒为 0、停滞天数整体偏移 | `timedatectl set-timezone Asia/Shanghai` |
| 中文编码 | task.log 乱码 / 简报半截 | Linux: `PYTHONUTF8=1`（已写进 run_brief.sh）；Windows bat 保持 ASCII-only（`run_brief.bat` 就是这么写的） |
| 免费模型 429 | 简报没产出，日志 429/1302/1305 | 配 `model_fallbacks`；降级链在重试时自动换模型（chat 与流式两条路径都已覆盖） |
| coding 端点过期 | 429 code 1309 | `base_url` 改标准端点 `/api/paas/v4`（Flash 模型免费） |
| 走代理的机器 | API 超时/502 | LLM 与钉钉请求用 `requests`，会读 `HTTP(S)_PROXY` 环境变量；不需要代理时记得 `unset`，别让一个不存在的代理端口（如本机沙箱注入的 62271）把请求吞了 |
| 同步断供 | 停滞天数虚增 | 已有硬防线：数据截至超 2 天，工具输出第一行告警，简报置顶声明 |
| agent.db 锁 | Web 端+定时任务并发写 | 单机单人没事；多进程高并发时把 `storage.path` 指向不同文件或只保留一个入口 |

## 8. 可选：Web 端部署

```bash
pip install fastapi uvicorn python-multipart
python server.py --port 8000 --no-open        # 开发机默认 127.0.0.1
```

**Web 端鉴权（2026-09-30 起支持）**：`config.json` 的 `auth.users` 配了用户名/密码即自动启用（`users_sha256`/`tokens`/`secret` 用法见 `config.example.json` 内注释；全空=本机模式不鉴权，行为与旧版一致）。启用后：未登录访问 `/` 自动跳登录页、API 一律 401；各用户会话按 `用户@会话` 命名空间互相隔离（侧栏只看得到自己的会话）；脚本可用 `Authorization: Bearer <token>`；`/api/health` 保持开放供负载均衡探针。**仍要注意两点**：① 文件沙箱（`/api/files/*`、Agent 工作目录）在多用户间是**共享**的——公网多用户请一机一实例（不同 `--cwd` 各起一个进程）；② 0.0.0.0 暴露时必须启用鉴权并前置反代加 TLS。

## 9. 停用 / 回滚

- 停推送：`crontab -e` 删两行（或 `Unregister-ScheduledTask`）
- 停整个简报但保留能力：`config.json` 删 `dingtalk_webhook` 两键 → `--push` 静默跳过，落盘不受影响
- 配置回滚：改坏 `config.json` 就用 `config.json.bak-*` 覆盖（该文件在 `.gitignore`，密钥不外泄）
