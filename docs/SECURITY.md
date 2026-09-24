# 安全模型 (Threat Model)

> 本文档描述 mini_agent 的信任边界、威胁与对策、残余风险——面试可直接按此讲解。

## 信任边界

```
不可信: 沙箱文件内容 / 知识库与RAG块 / 外部MCP server输出 / 模型自身判断
半可信: 用户指令(本机单用户, 但可能被诱导性文件间接影响)
可信:   引擎代码 / 本地索引与备份 / 审批收件箱(人的判断是最后防线)
出界:   LLM API(内容经用户自有Key出网, 属已知接受项) / 钉钉webhook(密钥在gitignored配置)
```

## 威胁与对策

| # | 威胁 | 攻击面 | 对策 | 验证 |
|---|---|---|---|---|
| T1 | **提示注入**(文件/检索内容里藏指令, 诱导模型越权) | read_file/grep/kb_search/MCP工具的输出 | ①工具输出全部用 `<untrusted_data>` 包裹并声明"数据非指令"(系统提示规则7); ②注入模式启发式扫描(忽略之前/你现在是/执行以下指令…), 命中打 `[!]` 标记并告警事件; ③写级工具仍需人工批准=最后防线 | test_security E2E(11/11): 恶意文件指令"创建hack.txt"未被服从且告警出现; 金标准 injection_defense 用例 |
| T2 | **路径穿越**(读写沙箱外文件) | 所有文件工具 | `safe_path()` 归一化+前缀校验, 越界直接拒绝 | selftest"路径越界拦截"; test_files 穿越用例(400) |
| T3 | **任意命令执行** | run_python | AST静态拦截: subprocess/os.system/popen/exec族/spawn/socket/requests/urllib/http/ctypes/pickle/注册表/eval/exec/compile/__import__/shutil.rmtree/os.remove; 拦截文案回灌让模型改道; 120秒超时; 写级确认 | test_security 拦截矩阵(11类全拦, 4例正常放行零误拦) |
| T4 | **敏感数据外泄** | 下载接口/文件列表/git | config.json/.env/credential/secret/password 类: 列表隐藏+下载403; gitignore 恒久隔离; run_python 禁网络出网 | test_files(403/隐藏); 提交前密钥扫描流程 |
| T5 | **误覆盖/破坏文件** | write_file | diff 预览+逐次确认; 覆盖前自动备份 `.trash/`; `undo_write` 一键恢复 | test_safety(备份/恢复) |
| T6 | **恶意外部工具**(接入的MCP server) | mcp_bridge | 默认 write 级(每次调用需确认); 前缀命名可辨识; 单server连接失败不影响整体 | test_mcp(bridge注册/调用) |
| T7 | **无人值守时的越权** | 定时晨检 | 只读注册表(写级工具在启动时全部摘除), 物理上不可能写 | daily_brief 构建逻辑+运行日志 |
| T8 | **失控循环/资源耗尽** | 主循环 | max_turns 上限; 单工具输出8000字符截断; LLM重试退避; research扇出硬上限4×15轮 | selftest/各E2E |

## 残余风险(已知且接受, 持续收敛)

1. **run_python 的 open() 写文件**未被禁(合法数据处理需要): 可写沙箱内任意文件且不进 `.trash` 备份。评估: 单用户本机+写级确认+沙箱限界, 可接受; 收敛方向是运行时 open 重定向或改用 write_file 通道。
2. **注入防御是缓解不是根除**: 标记包裹+告警显著降低成功率(E2E 验证), 但对抗性构造仍可能绕过——所以写操作的人工批准(T1③)是不可拆除的一层。
3. **内容出网到 LLM API**: 审批数据经用户自有 Key 发往模型服务商, 属部署时的合规决策项; 需完全内网时可换本地模型(endpoint 可配置)。
4. **单用户信任模型**: 无认证/RBAC, 服务只绑 127.0.0.1; 对外暴露前需加认证层。

## 安全相关的约定

- 新工具必须标注 read/write 级别; write 级默认弹确认
- 新增配置若含密钥, 先加 .gitignore 再创建文件
- 合入前 `python scripts/run_all.py`(fast) 必须全绿; 涉及引擎/工具改动加跑 `--tier full`
