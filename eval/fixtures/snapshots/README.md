# 评测快照（冻结数据，不要随手重生成）

这里的 `.txt` 是业务工具在某个时点的**真实输出**，被 `eval/golden.jsonl` 里
`suite=redline` 的用例通过 `mock_tools` 引用。

## 为什么冻结

红线断言盯的是具体数字，比如 `rl_trend_quote` 要求回答里出现 `3.4`
（「最近 7 份 vs 前 7 份，超期均值 5.1 → 8.6」的差值）。真实报表每天变，
今天 3.4、明天 3.6，断言就永远在飘——那样的评测只能证明"今天碰巧对"，
做不了改前改后对比。

冻结快照把「数据」这个变量消掉，剩下的差异才只来自模型和提示词。

## 什么时候才需要重生成

只有**工具输出格式本身变了**（加了字段说明、改了小节结构）才需要重生成，
因为旧快照已经不代表工具现在的输出。重生成之后必须做两件事：

1. 跑 `python scripts/test_eval_set.py` —— 它会查出"断言词在快照里找不到"
   （模型永远达不到，假失败）
2. **对改了数字的断言做反证**：把快照里的结论数字改掉，确认用例会 FAIL。
   不 FAIL 说明断言太松（2026-10-29 踩过：`answer_contains_any` 里放了
   中间量 `5.1`，模型随便提一句就过，把结论改成 `+9.9` 仍然 PASS）

## 重新生成的方法

```python
import sys; sys.path.insert(0, '.')
import agentcore as core
core.set_root('.')
cfg = core.load_config()
reg = core.build_registry(); core.load_plugins(reg, cfg)
from pathlib import Path
for name, fn, kw in [('pipeline_alerts', 'pipeline_alerts_YYYYMMDD.txt', {}),
                     ('inflight_report', 'inflight_report_YYYYMMDD.txt', {}),
                     ('scan_sync_data', 'scan_sync_data_YYYYMMDD.txt', {}),
                     ('pipeline_trend', 'pipeline_trend_YYYYMMDD.txt', {'days': 14})]:
    t = reg.get(name).func(**kw)
    (Path('eval/fixtures/snapshots') / fn).write_text(t, encoding='utf-8')
```

文件名带日期，**换日期就要改 `golden.jsonl` 里的 `mock_tools`**——
故意让这一步有点麻烦，免得有人顺手重跑一遍就把验证过的基线冲掉。

## 各文件用途

| 文件 | 被哪些用例引用 | 关键内容 |
|---|---|---|
| `pipeline_alerts_*.txt` | rl_fetch_first / rl_no_fake_names / rl_caliber_gloss / rl_skill_loaded | 已超期 24 个、临期 0 个、Part9 待办 4 单、**责任人字段声明** |
| `inflight_report_*.txt` | 同上 | 在研 24 SKU / 超期 6 个、豁免备查、**责任人字段声明** |
| `scan_sync_data_*.txt` | rl_no_fake_names | 今日动态（含真实人名 李杰华/梁子锋） |
| `pipeline_trend_*.txt` | rl_trend_quote | 近 14 天表格 + **区间对比 `5.1 → 8.6 (+3.4)`** |
