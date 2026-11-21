# DTB 元数据与任务 SQL 自动转换为 Apache Ossie 与混合维度落盘

第六期面向主题域项目打通从 DTB 到 Apache Ossie（`0.3.0.dev0`）语义模型的全自动转换与定时拉取闭环，在保持无需人工审批即可端到端流通的同时兼顾构建确定性：

1. **双形态统一核心（`dtb_ossie.py`）**：离线 CLI 子命令 `semantica-dataagent convert-dtb-ossie` 与内置在线拉取器 `DtbOssiePuller`（注册于 `semantica_dataagent.pullers`，`id="ossie"`，展示名 `DTB 拉取为 Ossie`）复用同一套确定性转换管线 `convert_dtb_to_ossie`。
2. **保守确定性 SQL 解析（无大模型、无人工审批）**：
   - `relationships`（外键关系）：仅提取多表显式物理等值连接（`exp.EQ`，双侧均为物理表真实列且不包含 `partition=true` 分区配对），拒绝跨连接函数的表达式、字面量、`CROSS JOIN`、`NATURAL JOIN` 与 `OR` 歧义条件。
   - `primary_key`：仅在目标表全部产数 SQL（支持单查询 `GROUP BY`（不含 `ROLLUP`/`CUBE`/`GROUPING SETS`）或 `QUALIFY ROW_NUMBER() OVER (...) = 1` / 等价子查询 `rn = 1`）能 1 对 1 映射到输出物理列且多任务键组完全一致时写入数据集 `primary_key`。
   - 分区策略与上游依赖：将 `merge_onself`、`INSERT INTO` 增量、`INSERT OVERWRITE` 全表或按分区覆盖策略及直接上游物理表摘要写入 `dataset.ai_context.instructions`。
3. **混合维度值落盘**：
   - 按列统计非空去重取值数：不高于 `low_cardinality_limit`（默认 `20`）时将全部排序取值原样内联写入字段 `ai_context.instructions`；超过阈值时在字段说明中保留前 `sample_limit`（默认 `5`）个示例，并由写出层把含高基数字段的表全部有效物理行（带 `row_id` 以维持 `dimension_stage` 同行编码↔名称别名映射）导出至 `dimension_values.json`（受 `schemas/v2/dimension_value.schema.json` 约束）。
4. **安全发布与项目级拉取互斥**：
   - `publish_ossie_pull` 与 `"mode": "ossie"` 启动恢复沿用日志回放与 `.git/dataagent-pull-backup/` 备份机制，覆盖人工同路径外不删除用户提交的 Ossie 模型及 `dimension_values.json`，仅清理历史 `"定时拉取"` 残留产物，并对内容未变的定时拉取安全空提交。
   - 同一数仓型项目内 `dtb` 与 `ossie` 拉取配置严格互斥（返回 422），文档型项目禁止启用 `ossie` 来源或拉取（返回 422）。项目尚无成功全量构建知识代时，首次 `ossie` 拉取自动入队全量构建（`full`），后续拉取自动入队增量构建（`incremental`）。

## Consequences

- 即使主题域项目未导入完整任务级 `DEPENDS_ON` 上游依赖图，Neo4j 仍承载 `SDATable` / `SDAField` / `RELATES_TO` / `HAS_DOCUMENT` / `SDADimensionValue` 图查询、`find_join_path` 强外键跳数计算、`validate_sql` 联表校对与多知识代隔离，与 Milvus 共同维持现有存储架构（见 [0002](0002-neo4j-plus-milvus-without-cross-store-release.md) 与 [0007](0007-ossie-semantic-interchange.md)）。
- 对需要筛选特定主题表或离线审阅模型的场景，管理员可直接执行 `convert-dtb-ossie` 生成标准化 Ossie 目录并交由 `ossie` 源适配器读取。
