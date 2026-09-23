# mini_agent 测试用例清单

> 配套命令：`python scripts/run_all.py`（快速层，秒级）｜`--tier full`（全量，约 15 分钟 + ~10 万 token）。
> 用例状态：**auto** = 已由自动化脚本覆盖（标注脚本名）；**manual** = 手动执行（给出命令与预期）。
> 约定：任何代码/prompt/工具描述改动合入前，`--tier fast` 必须全绿；涉及引擎行为改动加跑 `full`。

## M1 内核与沙箱

| ID | 用例 | 状态 | 命令 / 覆盖 | 预期 |
|---|---|---|---|---|
| TC-K01 | 路径越界拦截（`../` 逃逸被拒） | auto | `mini_agent.py --selftest` | PASS 且含"路径越界拦截" |
| TC-K02 | 五个内置工具读写检索 | auto | 同上 | 7 项全 PASS |
| TC-K03 | 未知工具报错回灌（模型自愈） | auto | 同上 | 错误文本含"[工具错误]" |
| TC-K04 | run_python 中文编码（PYTHONUTF8） | auto | 同上 | 输出含"中文输出: 42" |
| TC-K05 | 任意目录为沙箱 + AGENT.md 注入 | manual | `python mini_agent.py --cwd "C:\Users\dell\Desktop\work-main" "全流程红线是多少天?"`（先把 AGENT.md.example 复制到 work-main 根部改名 AGENT.md） | 回答含"60"，来源引用 AGENT.md/工具 |
| TC-K06 | 工具输出 8000 字符截断 | manual | 让它读一个大日志并转述 | 工具结果尾部出现"已截断"，任务仍完成 |

## M2 权限与人工干预

| ID | 用例 | 状态 | 命令 / 覆盖 | 预期 |
|---|---|---|---|---|
| TC-P01 | 写操作确认 + diff 预览 | manual | 交互模式让它"把xx写入 t.md"，看确认块 | 显示新旧行数与 unified diff，y 后落盘 |
| TC-P02 | `a` 本会话总允许 | manual | 上例确认时输 `a`，再触发同工具 | 第二次不再询问 |
| TC-P03 | --yolo 全放行 | auto | smoke_web / 各 E2E | 无确认直接执行 |
| TC-P04 | 拒绝后模型换方案 | manual | TC-P01 时输 `n` | 模型回应拒绝原因或换只读方案，不崩溃 |
| TC-P05 | 任务停止 → cancelled + 会话保留 | auto | test_upgrade E2E；manual：Web 点停止 | cancelled 事件/回答含"中断"；`--resume` 可继续 |
| TC-P06 | 停止连带拒绝未决审批 | manual | Web 触发写任务，出审批卡片后点"停止" | 卡片消失，引擎退出而非永久阻塞 |

## M3 会话与记忆

| ID | 用例 | 状态 | 命令 / 覆盖 | 预期 |
|---|---|---|---|---|
| TC-M01 | MEMORY.md 注入系统提示 | auto | test_upgrade 单测 | 提示含"跨会话记忆"与条目内容 |
| TC-M02 | save_memory 写级确认 + 落盘 | auto | eval 用例 memory_save；manual 交互确认 | MEMORY.md 出现该行 |
| TC-M03 | --resume 跨进程恢复上下文 | manual | `--session t1 "记住X"` → 新进程 `--session t1 --resume "X是什么"` | 能答上，说明历史已恢复 |
| TC-M04 | 中断会话悬空 tool_calls 修复 | auto | test_upgrade 单测 | 补"[中断]"合成结果，resume 不报协议错 |
| TC-M05 | 上下文压缩 | manual | config 调 `context_budget_tokens: 3000` 跑长任务后还原 | 出现"[压缩]"，任务仍正确完成 |
| TC-M06 | 会话命令 /new /usage /tools | manual | 交互模式输入三个命令 | 分别清历史/显示token/列工具 |

## M4 反思闭环

| ID | 用例 | 状态 | 命令 / 覆盖 | 预期 |
|---|---|---|---|---|
| TC-R01 | 反思通过 → 不修正 | auto | test_reflect_fix 单测 | 无 fix_round 事件，原答案返回 |
| TC-R02 | 发现问题 → 自动修正一轮 | auto | test_reflect_fix 单测（FakeClient）；manual：`--reflect` 跑一个含易错数字的任务 | 出现"[修正]"，回答修订 |
| TC-R03 | 反思结论入历史（跨轮参考） | auto | 同上 | 历史含"[反思结论]" |

## M5 子 agent

| ID | 用例 | 状态 | 命令 / 覆盖 | 预期 |
|---|---|---|---|---|
| TC-S01 | 只读隔离 | auto | test_advanced 单测 | 子agent调 write_file 收到"未注册的工具" |
| TC-S02 | 专属角色注入 | auto | unit_roles_tools | 子agent系统提示含角色文本 |
| TC-S03 | 工具白名单 / 无交集报错 | auto | 同上 | 仅白名单工具可见；无交集时错误回灌 |
| TC-S04 | 扇出硬上限 4 任务 | auto | unit_research_fanout | 第 5 个任务被丢弃 |
| TC-S05 | verify 核查员 | auto | unit_verify + E2E adv_roles | 结果含"[核查]"节 |
| TC-S06 | 真实并行调查 | auto | E2E adv_research / eval 用例 research_fanout | 两问题一指令得出，回答含 5050 |
| TC-S07 | 上下文隔离效果 | manual | 跑一次扇出后看 `sessions/sub_*.jsonl` 数量与主会话 token | 子会话独立落盘；主会话 prompt tokens 不随子agent过程膨胀 |

## M6 计划模式

| ID | 用例 | 状态 | 命令 / 覆盖 | 预期 |
|---|---|---|---|---|
| TC-PL01 | 计划生成 + 批准 + 严格执行 | auto | eval 用例 plan_mode；E2E adv_plan | events 含 plan/plan_approved；按计划落盘 |
| TC-PL02 | 否决 → 完全不执行 | auto | unit_plan_mode | 返回"[计划被用户否决]"，0 次执行调用 |
| TC-PL03 | 计划注入任务指令 | auto | 同上 | 历史含"已批准的执行计划" |
| TC-PL04 | stepwise 全步完成 | auto | unit_stepwise + E2E adv_steps | 3 个 step_done，最终答案=末步 |
| TC-PL05 | stepwise 中途停止 | auto | unit_stepwise | "第1步后按用户要求停止" |
| TC-PL06 | stepwise 修改指令转向 | auto（CLI 真输入为 manual） | unit_stepwise；manual：`--plan --stepwise` 第1步后输入"跳过X" | 指令入历史，后续步骤遵循 |
| TC-PL07 | Web 计划审批卡片 | manual | Web 勾"先出计划"发任务 | 卡片显示计划全文；允许执行/拒绝不动 |
| TC-PL08 | Web 分步审批 | manual | Web 勾"分步执行" | 每步弹出步骤卡片，允许继续/拒绝停止 |

## M7 Web 前端

| ID | 用例 | 状态 | 命令 / 覆盖 | 预期 |
|---|---|---|---|---|
| TC-W01 | 页面加载/会话侧栏/切换 | manual | `python server.py` 后浏览 | 会话列表可点开恢复历史 |
| TC-W02 | SSE 流式工具卡片 | auto | smoke_web | tool_call/tool_result 事件实时渲染 |
| TC-W03 | 审批卡片 diff + 批准落盘 | auto | smoke_web | 文件在批准后写入 |
| TC-W04 | 停止→立即转向 | auto | test_upgrade E2E | stop 后新任务非 409 |
| TC-W05 | 断线重放（Last-Event-ID） | auto | test_upgrade E2E | 重连按原 seq 补发 |
| TC-W06 | 反思/计划/分步三开关 | manual | 勾选组合发任务 | 对应事件/卡片出现 |
| TC-W07 | 运行中再发消息 → 409 提示 | manual | 任务执行中再点发送 | 界面提示"任务正在运行" |

## M8 MCP 双向

| ID | 用例 | 状态 | 命令 / 覆盖 | 预期 |
|---|---|---|---|---|
| TC-MC01 | 手写 server 握手/列表/调用 | auto | test_mcp | initialize/tools/list/call 全过 |
| TC-MC02 | 插件域工具经 MCP 可用 | auto | 同上 | query_sku_route 经 MCP 返回 G531 数据 |
| TC-MC03 | bridge 接入外部 server | auto | 同上 | self_* 前缀工具可经 Registry 跨进程调用 |
| TC-MC04 | stdout 协议通道纯净 | auto | 同上（无 JSON 解析告警即过） | 启动日志全走 stderr |
| TC-MC05 | 第三方客户端接入 | manual | 客户端 mcp 配置加 `{"mini-agent": {"command": "python", "args": ["...\\mcp_server.py"]}}` | 客户端内可直接调用全部工具 |

## M9 workmain 插件（8 工具）

| ID | 用例 | 状态 | 命令 / 覆盖 | 预期 |
|---|---|---|---|---|
| TC-WM01 | query_batches 批次清单 | manual | `python mini_agent.py --yolo "用 query_batches 列出所有数据批次"` | 5 个批次含手机膜/手机壳及最新时间 |
| TC-WM02 | get_product_info | auto | eval wm_product_g570 | 回答含 B0H8Q8719H |
| TC-WM03 | query_sku_route（双口径） | auto | eval wm_route_g531；manual 换 caliber=system | action 口径含 Part8/71；system 口径可查 |
| TC-WM04 | search_kb 知识库 | auto | eval wm_kb_60d | 回答含"60"，来源为知识库命中 |
| TC-WM05 | sync_status 同步日志 | auto | daily_brief 真跑链路 | 返回最近日志尾部 |
| TC-WM06 | scan_sync_data 僵尸过滤 | manual | `--yolo "用 scan_sync_data 扫描并总结"` | >90 天遗留单独计数不进 TOP |
| TC-WM07 | pipeline_alerts 四层信号 | auto | eval wm_alerts；直测见 09-23 记录 | 空60/海69 标注、临期/超期/段级分层 |
| TC-WM08 | run_route_check 隔离输出 | manual | 交互模式（会弹确认） | 输出进 agent_tmp，不覆盖正式汇总 |

## M10 定时晨检 + 钉钉推送

| ID | 用例 | 状态 | 命令 / 覆盖 | 预期 |
|---|---|---|---|---|
| TC-D01 | 晨检落盘（只读注册表） | auto | `python daily_brief.py`（已多次真跑） | briefs/<日期>.md 六节结构，摘除写工具日志出现 |
| TC-D02 | 推送（加签） | auto | `--push`（已真跑） | 群里收到简报 |
| TC-D03 | 推送 dry 模式 | auto | `python dingtalk_push.py --title t --text x --dry` | 只打印不发送 |
| TC-D04 | 计划任务存在 | manual | `schtasks /Query /TN AgentDailyBrief` | 下次运行时间 09:45 |
| TC-D05 | 次日自动推送 | manual | 次日上午观察群与 `briefs/task.log` | 09:45 自动产出并推送 |

## M11 评测系统本身

| ID | 用例 | 状态 | 命令 / 覆盖 | 预期 |
|---|---|---|---|---|
| TC-E01 | 金标准全量（14 用例） | auto | `python eval/run_eval.py` | 14/14，报告落 eval/reports/ |
| TC-E02 | 单用例 / 换模型对比 | manual | `--case arith_tool --model glm-5.3` | 报告标注模型名，可比对 |
| TC-E03 | 网络异常单用例重试不崩 | auto | （2026-09-22 真实触发过） | 该用例重试一次，仍败记 FAIL 继续跑 |
| TC-E04 | plan/research 纳入回归 | auto | 用例 plan_mode / research_fanout | events_contains 断言生效 |

## M12 文件导入/下载（Web）

| ID | 用例 | 状态 | 命令 / 覆盖 | 预期 |
|---|---|---|---|---|
| TC-F01 | 上传(中文文件名)到 uploads/ | auto | test_files | 返回相对路径，文件落盘 |
| TC-F02 | 重名自动加序号 | auto | 同上 | 导入测试(1).txt |
| TC-F03 | 目录浏览可见/隐藏敏感文件 | auto | 同上 | config.json 不出现在列表 |
| TC-F04 | 下载内容与上传一致 | auto | 同上 | 字节一致，带 filename 头 |
| TC-F05 | config.json 拒绝下载(403) | auto | 同上 | 敏感文件保护 |
| TC-F06 | 下载/上传路径穿越拦截(400) | auto | 同上 | ../ 与文件名路径成分均被防 |
| TC-F07 | UI 导入→引用→生成→下载 全链路 | manual | Web 导入一个 xlsx，消息"用 run_python 读 uploads/xx.xlsx 前几行"，让它生成报告 md，文件面板点击下载 | 全链路闭环 |
