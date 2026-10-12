# 失败模式复盘 2026-10-12

- 数据窗口: 2026-09-22T14:34:09 ~ 2026-09-24T09:45:50
- 会话文件: 55 个 | 任务: 323 | LLM 调用: 165 | 工具调用: 259

## 1. LLM 调用面

| 模型 | 调用 | 错误 | 错误率 | 触发重试 | 重试率 | 平均延迟ms | prompt tokens | completion tokens |
|---|---|---|---|---|---|---|---|---|
| glm-4.5-flash | 70 | 0 | 0% | 70 | 100% | 24345 | 313007 | 23671 |
| glm-5.3-flash | 51 | 1 | 2% | 1 | 2% | 10958 | 135298 | 12014 |
| glm-4.7-flash | 44 | 1 | 2% | 1 | 2% | 35707 | 178155 | 15405 |

彻底失败明细:

- `2026-09-28T09:45:10.776` task30 glm-5.3-flash 重试3次后仍失败, 耗时7524ms
- `2026-09-28T10:32:28.485` task33 glm-4.7-flash 重试3次后仍失败, 耗时64680ms

## 2. 工具调用面

| 工具 | 调用 | 失败 | 失败率 | 平均耗时ms |
|---|---|---|---|---|
| probe | 54 | 0 | 0% | 0 |
| read_file | 45 | 2 | 4% | 9.6 |
| list_dir | 38 | 2 | 5% | 1.6 |
| pipeline_alerts | 28 | 0 | 0% | 2978.6 |
| run_python | 28 | 0 | 0% | 188.8 |
| scan_sync_data | 26 | 0 | 0% | 3804.7 |
| sync_status | 26 | 0 | 0% | 32.8 |
| kb_search | 3 | 0 | 0% | 19441.3 |
| save_memory | 3 | 0 | 0% | 9.7 |
| write_file | 2 | 0 | 0% | 5.5 |
| get_product_info | 1 | 0 | 0% | 172.0 |
| grep | 1 | 0 | 0% | 530.0 |
| platform_batches | 1 | 0 | 0% | 5.0 |
| query_sku_route | 1 | 0 | 0% | 1302.0 |
| research | 1 | 0 | 0% | 53131.0 |
| search_kb | 1 | 0 | 0% | 210.0 |

失败明细(耗时≤5ms 的多为参数/校验类秒失败, 非超时):

- `2026-09-24T13:54:49.349` task14 read_file (read) 2ms
- `2026-09-24T13:55:38.522` task15 list_dir (read) 2ms
- `2026-09-24T13:56:24.152` task17 list_dir (read) 1ms
- `2026-09-28T14:10:46.280` task64 read_file (read) 2ms

## 3. 任务面

状态分布: empty=1, ok=318, running=4

轮数分布: 1轮×158, 2轮×147, 3轮×4, 4轮×2, 5轮×8

异常任务(empty=空结果 / running=未收尾):

- task18 `eval_research_fanout` running (None轮, 起于 2026-09-24T13:56:41)
- task30 `brief_20260928` running (None轮, 起于 2026-09-28T09:45:03)
- task33 `brief_20260928` running (None轮, 起于 2026-09-28T10:31:23)
- task60 `smoke_web` running (None轮, 起于 2026-09-28T12:14:09)
- task249 `brief_20261010_pm` empty (5轮, 起于 2026-10-10T14:10:03)

## 4. 审批面

| 类型 | 决策 | 次数 |
|---|---|---|
| plan | denied | 15 |
| step | stop | 15 |
| tool | allowed | 161 |
| tool | denied | 59 |

## 5. 会话 jsonl 面

- 事件类型: end×57, message×436, reflect×1
- 结束原因: brief×6, exit×14, subagent×37
- reflect 反思事件: 1 次
- 工具错误归类:
  - `未注册的工具` × 7

## 6. 行动建议(按数据自动推导)

1. **幻觉工具名 7 次**(如 未注册的工具): 模型调用了不存在的工具。建议: ① 子 agent 的系统提示里显式列出「可用工具名白名单」; ② 在总系统提示加一句「只能调用本次会话已注册的工具，不要臆造工具名」。
2. **路径误用 4 次**(NotADirectory/FileNotFound): list_dir/read_file 参数把文件当目录或路径不存在。建议: 两个工具的 description 各补一条「参数示例+易错点」(如 list_dir 必须指向目录、read_file 先确认存在), 并在 description 里注明可配合 list_dir 探查。
3. **LLM 重试率偏高 72/165(44%)**, 集中在 glm-4.5-flash: 免费档限流是主因。建议: 简报类定时任务错峰; 或在 agentcore payload 传 thinking:disabled 降低单次耗时。
4. **LLM 彻底失败 2 次**: 2026-09-28T09:45 glm-5.3-flash(重试3次仍失败); 2026-09-28T10:32 glm-4.7-flash(重试3次仍失败)。 建议核对当时网络/额度; 若持续出现需加告警。
5. **空结果任务 1 个**: task249(brief_20261010_pm, 5轮)。已知防线已修(ab2caba 空简报防线), 此处仅存档核对。
6. **崩溃/未收尾任务 4 个**: task18(eval_research_fanout); task30(brief_20260928); task33(brief_20260928); task60(smoke_web)。多为评测/冒烟会话进程被杀; 建议 sessions 清理或收尾时标记。
7. **审批拒绝率 89/250(36%)**: 工具颗粒度或描述不清导致人工频繁叫停。建议把高频被拒工具合并或细化描述。
