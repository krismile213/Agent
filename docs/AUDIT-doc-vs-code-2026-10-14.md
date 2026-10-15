# 文档 × 代码 一致性核查（2026-10-14）

> 方法：所有结论均用**真实注册表/文件实测**验证，不靠数行数或推测。
> 环境：Python 3.13 隔离环境，加载真实 `config.json`（`workmain_root` / `rag_sources` 均已配置）。
> 复核命令见文末「复现」。

## 结论速览

发现 **5 类 / 13 处**不一致。按影响排序：

| 级别 | 问题 | 影响 |
|---|---|---|
| 🔴 高 | A. 工具数量口径全线对不上 | 对外讲解/接入会报错数字 |
| 🔴 高 | B. README 完全没有 platform 层 | 新增的 10 个平台工具无文档 |
| 🟡 中 | D. 跨会话记忆脏数据 | **每轮注入系统提示**，污染上下文 |
| 🟡 中 | C. config 默认值文档与部署文档冲突 | 照 README/example 配会踩 429 |
| ⚪ 低 | E. 编号乱序 / 报告口径歧义 | 观感 + 分析误读 |

---

## A. 工具数量口径（全部实测）

实测（`core.build_registry()` + `load_plugins()`）：

```
[插件] rag: 注册 3 个工具
[插件] workmain: 注册 18 个工具     # = 8 基础 + 10 平台
工具总数: 31
```

| 项 | 文档写的 | 实测 | 位置 |
|---|---|---|---|
| 内置工具 | **9** | **10** | `README.md:124` |
| RAG | 3 | 3 ✅ | — |
| workmain 注册数 | **6** | **18** | `README.md:91` |
| workmain 注册数 | **8** | **18** | `README.md:124`、`docs/DEPLOY(部署文件).md:83` |
| 工具总数 | **20** | **31** | `README.md:124` |
| MCP 暴露 | "全部 20 个" | 动态 = 注册表全量（当前 31） | `README.md:124` |

- 内置 10 = `read_file / list_dir / grep / write_file / undo_write / run_python / save_memory / recall_memory / dingtalk_send / research`。
  README 少算的正是后加的 **`recall_memory`**。
- workmain 基础 8 = `query_batches / get_product_info / query_sku_route / search_kb / sync_status / scan_sync_data / pipeline_alerts / run_route_check`（README 表格 8 行 ✅，但正文写 6）。
- `mcp_server.py` 暴露的是 `reg._tools` **全量**（`mcp_server.py:48-50`），并非固定 20，数字随插件配置变化。

## B. platform 层在 README 完全缺失

- `grep -i platform README.md` → **空**。README 主功能章节只字未提。
- 实际存在 **10 个** `platform_*` 只读工具（`plugins/workmain.py:794` `_register_platform`，`return 10`）：
  `platform_batches / platform_overview / platform_sku_route / platform_alerts / platform_transport / platform_records / platform_inflight_overview / platform_inflight_alerts / platform_daily_report / platform_deltas`
- 配套的 `X-Plat-Context` 动态上下文注入、`platform_root` 配置、脱敏（`_plat_sanitize`）也只散落在 `AGENT.md`，README 无对应章节。
- README:124 的 "20 个工具（内置 9 + RAG 3 + workmain 8）" 完全没算平台 10 个。

## C. 其它文档数字过时

1. **`scripts/run_all.py` 文件头 docstring**：写 "金标准评测(14用例)"，实际 `eval/golden.jsonl` = **23** 用例（core 12 + workmain 4 + ability 7）。
2. **`README.md:120` 测试套件清单不完整**：只列 10 个（selftest/test_reflect_fix/test_upgrade/test_advanced/test_rag/test_files/test_safety/test_mcp/smoke_web/run_eval），而 `run_all.py` 的 FAST 层实际有 **19** 个套件。未列出的 14 个：
   test_security、test_human_intervention、test_research_guard、test_freshness、test_session_analysis、test_brief_guard、test_web_features、test_web_ui.js、test_memory_ckpt、test_auth、test_plat_context、test_platform_tools、test_web_md.js、test_enterprise。
3. **`README.md:29` 与实际配置不符**：README 说 "默认配置指向 z.ai coding plan，`base_url=…/coding/paas/v4`，`model=glm-5.3-flash`"；实际 `config.json` 是 `base_url=https://api.z.ai/api/paas/v4`（**标准端点**）+ `model=glm-4.5-flash` + `model_fallbacks=["glm-4.7-flash"]`。
4. **`config.example.json` 与 `DEPLOY.md` 自相矛盾**：example 里给的是 `coding/paas/v4` + `glm-5.3`，而 `docs/DEPLOY(部署文件).md:59` 明确警告「**标准端点，不是 `/coding/paas/v4` 订阅端点——那个过期就全线 429**」。照 example 配会踩坑。

## D. 跨会话记忆脏数据（会被注入系统提示）

`agentcore.py:543` 明确：`memory/MEMORY.md` **每次启动注入系统提示**。当前内容全是测试残留：

`memory/MEMORY.md`：
```
- [2026-10-06] 测试记忆条目-可删除
- [2026-10-06] 测试记忆条目-可删除     ← 重复
- [2026-10-09] AI 助手嵌入联调已通过
- [2026-10-09] AI 助手嵌入联调已通过   ← 重复
```

流水日志同样有残留：
- `memory/log/2026-10-06.md`：`测试记忆条目-可删除` ×2
- `memory/log/2026-10-09.md`：`AI 助手嵌入联调已通过` ×2
- `memory/log/2026-10-12.md`：`测试记忆条目-可删除` ×1

影响：模型每轮都能看到「测试记忆条目-可删除」，属噪声，且重复项让 MEMORY.md 失去可信度。

## E. 低优先

1. **README 路线图编号乱序**：`README.md:187` 是 `7.`，`:188` 是 `6.`。
2. **`analysis/session_analysis_2026-10-12.md` 的"数据窗口"口径歧义**：表头写 `数据窗口: 2026-09-22 ~ 2026-09-24`，但正文列出的 task30/33（09-28）、task249（10-10）远超该窗口。
   根因（已定位）：`scripts/analyze_sessions.py:136-139` 的 `window` **只统计 `sessions/*.jsonl` 的 ts**，而任务/审批/LLM 面来自 `agent.db`（范围更宽）。属"窗口标签名不副实"，非数据错误——建议改名或改为取两源并集。

---

## 建议处理顺序

1. **D（清记忆脏数据）** — 零风险，立刻见效：删 `memory/MEMORY.md` 的 4 行 + 三个 log 文件里的测试行。
2. **A + B（工具数量口径 + 补 platform 章节）** — README 一次性对齐：内置 10 / RAG 3 / workmain 18（8+10）/ 总数 31，并补 platform 表。
3. **C.4（config.example 端点）** — 与 DEPLOY 统一为标准端点，避免新环境踩 429。
4. **C.1 / C.2 / E** — 顺手修。

## 处理状态（2026-10-14 已执行）

| 项 | 处理 | 落点 |
|---|---|---|
| D 记忆脏数据 | ✅ 清理 | `memory/MEMORY.md` 4→1 行；删 `log/2026-10-06.md`、`log/2026-10-12.md`；`log/2026-10-09.md` 去重。备份：`/tmp/memory_backup_20261014` |
| D 根因 | ✅ 无需改 | 实测当前 `test_upgrade.py` 已有 backup/restore，不再污染；残留系历史遗留 |
| A 工具数量 | ✅ 已对齐 | `README.md:91`、`:143`（内置 10 / RAG 3 / workmain 18 / 总数 31） |
| B platform 章节 | ✅ 已补 | `README.md` 新增「platform 工具清单（10 个）」表 + X-Plat-Context/脱敏/口径纪律说明 |
| C.1 run_all docstring | ✅ 已改 | `scripts/run_all.py` 14→23 用例 |
| C.2 测试套件清单 | ✅ 已补 | `README.md:139` 列全 fast 层 19 套件 + full 追加项 |
| C.3 README 默认配置 | ✅ 已改 | `README.md:29` 改为标准端点 + `glm-4.5-flash` + fallback |
| C.4 config.example 端点 | ✅ 已改 | `config.example.json` 标准端点 + `glm-4.7-flash` + `model_fallbacks`；常用对照补 coding 端点告警 |
| E.1 路线图编号 | ✅ 已修 | `README.md:206-207` 7/6 → 6/7 |
| E.2 报告窗口歧义 | ✅ 已改 | `scripts/analyze_sessions.py:251` 标签改为「会话窗口(仅 sessions/*.jsonl, 不含 agent.db)」 |

**验证**：`scripts/test_session_analysis.py` 41/41 通过；`config.example.json` JSON 合法。本次改动均为文档/注释/报告文案，未触及引擎行为。

## 复现

```bash
# 工具数量实测
python -c "import sys;sys.path.insert(0,'.');import agentcore as core;cfg=core.load_config();core.set_root(cfg.get('workmain_root','.'));r=core.build_registry();core.load_plugins(r,cfg);print(len(r._tools))"

# 评测用例数
wc -l < eval/golden.jsonl

# README 是否提及 platform
grep -i platform README.md
```
