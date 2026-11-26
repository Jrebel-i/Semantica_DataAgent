# 评测题集与历史工单分离、上下文大模型归纳沉淀与显式流向标记

Status: accepted

第十一期 [0013](0013-unified-llm-knowledge-evolution-and-trace-source.md) 将人工纠错、对话追踪演化和冷启动发现收口进入同一审批流；但对话点赞（`feedback`）、纠错审批通过（`correction`）以及冷启动可选草案（`discovery`）仍会自动写入项目隔离评测集 `eval/questions.yaml`。在真实使用中这造成了双重错位：一方面日常零散问答与未精炼工单混入了衡量 Agent 准确率的基准评测集，另一方面经过人工认可的优秀案例被物理隔离在知识库之外，Agent 后续问答无法检索复用。

第十四期确立以下边界与架构决策：

1. **评测题集（`eval/questions.yaml`）回归纯准确率基准**：
   - 物理隔离不入知识索引的 `eval/questions.yaml` 仅保留**管理员手动维护与批量导入**路径（`/api/v1/eval-questions`），专用于准确率回归评测（如基线题集、`dev` / `holdout` 拆分）。
   - 移除纠错审批通过后的 `promote_correction_to_eval_question` 自动写入；停止对话点赞和冷启动发现向 `eval/questions.yaml` 写入草稿。
2. **新增可检索知识资产「历史工单」（`<markdown_root>/historical-tickets/*.md`）**：
   - 历史工单作为规范 Markdown 文档存放在 Git 源目录（默认 `knowledge/historical-tickets/`），随增量构建写入 Neo4j 与 Milvus 参与混合检索，辅助 Agent 在后续类似问数场景中复用已验证思路与口径。
   - **对话上下文 + 大模型精炼沉淀**：用户在对话中点击「沉淀工单」（`POST /api/v1/conversations/{id}/turns/{turn_id}/feedback`）时，后端采集该会话截至当前轮次的多轮问答、澄清选项与通过 `validate_sql` 校验的 SQL（剥离查询结果行、附件和密钥），结合已有历史工单目录交由配置的大模型（未配置或调用失败时降级为结构化模板）提炼成独立完整、业务可读的历史工单提案，进入「纠错审批」待人工审核入库。
   - **冷启动发现同源承接**：冷启动发现第 3 阶段选中的高频有效查询模板改为经大模型/结构化归纳后生成历史工单提案（`source="discovery"`，`target="document:<markdown_root>/historical-tickets/<slug>.md"`），同样必须经过人工审批才入库。
   - **防基准泄漏门禁**：向历史工单发起对话沉淀、冷启动提案或人工新增时，均比对项目 `eval/questions.yaml` 中的问题与参考 SQL 指纹，命中使用中的评测题（尤其是 `holdout`）时拒绝进入可检索工单库。
3. **纠错审批显式标记「来源」与「去向」**：
   - 扩展 `ProposalSource` 增加 `"feedback"`（`对话点赞沉淀`），并在 `ProposalView` 派生 `destination_kind`（`historical_ticket` / `knowledge_doc` / `semantic_override` / `ossie_model`）。
   - 审批列表、详情页及筛选项同时显式展示与筛选**来源**和**去向**标签，便于审核者一目了然区分「对话点赞 → 历史工单」「冷启动发现 → 历史工单」「纠错演化 → 知识文档 / 语义覆盖层」。
4. **独立「历史工单」管理页面（`/historical-tickets`）**：
   - 新增 `/api/v1/historical-tickets`（`GET`/`POST`/`PATCH`/`DELETE`）及前端「历史工单」页面，支持浏览已入库工单、涉及数据表与 SQL 预览、直达待审批工单提案以及管理员手动增删改（写入源目录并触发增量构建）。

## Considered Options

- **继续把对话点赞/冷启动案例与手动评测基准混存在 `eval/questions.yaml`**：会导致基准失真，且案例无法被 Agent 检索到，故放弃。
- **直接把对话原始一问一答写入知识库而不经上下文归纳与审批**：多轮澄清背景缺失、表达口语化且易引入低质量碎片污染检索结果，故采用“多轮上下文采集 + LLM 精炼 + 纠错审批把关”。
- **新增 SQLite 独立表存储历史工单**：需要额外双写同步到源目录或检索库；复用 Git 源目录 `<markdown_root>/historical-tickets/*.md` 可零迁移复用现有 Git 版本历史、增量构建、混合检索与 `git revert` 能力，故采用文档目录方案。

## Consequences

- `eval/questions.yaml` 仅剩管理员主动管理的评测题集，不再受日常反馈与演化波动影响。
- `CorrectionProposal`（无需数据库迁移）通过 `source` 与派生字段 `destination_kind` 完整表达“从哪里来、落到哪里去”。
- 所有经审批写入 `<markdown_root>/historical-tickets/*.md` 的工单将在增量构建后进入向量与图谱检索。
