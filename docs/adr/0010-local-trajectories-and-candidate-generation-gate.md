# 网关同源采集本地问答轨迹，通过隔离候选知识代与防泄漏门禁完成自主演化

Status: accepted

自主演化需要完整可观测的检索与 SQL 校验过程，但不能把业务查询数据行或未经验证的改动直接写入线上知识代。参照 `ruc-datalab/EvoOntology` 的工程原则并结合 [0003](0003-knowledge-generations.md) 的多知识代架构与安全数据边界，第八期确立以下两项边界决策：

1. **MCP 与对话网关同源采集本地问答轨迹**：在 MCP 工具调用网关与在线对话服务端点同源持久化按 `project_id` 严格隔离的结构化**问答轨迹**（在线对话归属到会话与轮次，纯本地 MCP 调用按项目、令牌与 30 分钟空闲窗口串联），保留完整搜索词、完整 SQL、命中 `ref` 列表、澄清选项与 `validate_sql` 完整问题明细，但强制剥离用户附件正文、抽样与查询结果数据行、原始外部错误详情及各类密钥凭据，并随对话删除及 `session_retention_days` 到期同步物理清理。外部 Langfuse 遥测仅同步补齐 `project_id` 与本地 MCP 会话 `traceId` 用于可观测看盘，不作为演化唯一数据源，避免受默认关闭 `langfuse_capture_content`、有损内存队列或离线隔离部署的制约。
2. **隔离候选知识代与防泄漏双模门禁**：扩展 [0003](0003-knowledge-generations.md)，**演化运行**中的候选改动必须在基于源目录工作副本叠加构建的临时**候选知识代**上验证（复用既有 `embedding-cache.sqlite` 避免未改内容重复向量化，不切换线上 `generation_pointer`，不占用正式保留代配额，验证结束或服务启动恢复时自动清理）。各项目的**评测题集**物理隔离存放在源目录之外（禁止被源适配器索引或通过源浏览接口读取），普通成员点赞以 `answer_confirmed=false` 录入，管理员确认或已审批纠错产出以 `answer_confirmed=true` 并按 70/30 分层纳入 `dev` / `holdout`（`holdout` 对演化诊断器不可见）。当已确认回归题 $\ge 5$ 道时，门禁执行检索召回 + Agent 生成 SQL + `validate_sql` 静态口径零回退比对；冷启动不足 5 题时，冻结本批轨迹去重后最近 30% 留出验证集执行 `validate_sql` 无新增错误 + 独立空白上下文匿名随机交换 A/B 盲评（零硬伤且严格净胜出）。

## Considered Options

- **直接从外部 Langfuse API 拉取历史 trace 作为演化输入**：会强迫生产环境开启高敏感全文上报、受制于本地 MCP 碎片化与异步丢包，且离线隔离环境不可用，故放弃。
- **使用真实业务查询执行结果作为默认演化门禁**：每次演化运行需要项目级 DTB 个人令牌且消耗数仓执行配额；默认采用检索召回 + Agent SQL + `validate_sql` 静态口径比对（与 [0005](0005-agent-isolation-instead-of-budgets.md) 交互解耦），保留人工按需线下跑真实取数评测的能力。

## Consequences

- 本地问答轨迹与 Langfuse 必须共享相同的敏感情报剥离与不可信文本隔离规则；`Conversation` 删除与项目永久删除流程须同步清理对应**问答轨迹**与隔离**评测题集**。
- **演化运行**在门禁未通过或超出单次预算（默认 `max_rounds=3`、`max_eval_cases=20`）时保持现状（`incomplete`），绝不污染当前知识代。
