# Apache Ossie 作为语义模型双向交换标准，不改变 Neo4j + Milvus 存储边界

第五期选择 Apache Ossie（`0.3.0.dev0`）作为开放语义模型的导入与导出交换标准，面向数仓型项目打通双向互操作，同时保持既有存储与检索架构不变：

1. **存储边界不变**：Ossie 仅作为 `ossie` 源适配器的输入格式和 `export-ossie` / 项目导出接口的输出格式。在线知识问答、融合检索、连接路径查找与 SQL 校验继续全部读写 Neo4j 与 Milvus，不新增语义查询引擎或运行时依赖。
2. **三路分流边界**：导入时使用内置官方 JSON Schema（`schemas/ossie/ossie-schema.json`）离线校验并兼容早期 `0.1`/`0.2` 形式。真实物理表与单列物理映射进入 `SDATable` / `SDAField`；逻辑字段解引用后的物理主外键写入 `RELATES_TO`；行级计算字段、指标聚合表达式与 AI 上下文渲染为受保护的 `generated=true` 派生知识文档与索引地图，避免把计算列当作物理列写入 SQL。
3. **确定性导出与非可移植扩展**：导出只从当前知识代图存储读取，不调用大模型猜测或强转自由文本知识文档中的未审定指标为 `metrics`；表关联的非索引知识文档仅将标题与 `ref` 摘要写入数据集 `ai_context.instructions`。物理列的 `partition` 标记与 `raw_type` 通过 `nonportable_extensions`（`vendor_name="SEMANTICA_DATAAGENT"`）承载，并在返回或写盘前强制通过内置官方 Schema 自校验，保证物理表、目录、分区标记、字段说明与外键关系的无损往返。

## Consequences

- 派生知识文档的唯一权威来源仍是源目录中的 Ossie 模型文件，不支持通过文档纠错提案直接改写。
- 存量数仓项目可先确定性导出表结构、外键与文档摘要骨架，再在外部工具补充已审定指标与计算字段后导回。
- 从 DTB 自动加工为 Ossie 模型与混合维度值的第六期管道见 [0008-dtb-to-ossie-pipeline.md](0008-dtb-to-ossie-pipeline.md)。
