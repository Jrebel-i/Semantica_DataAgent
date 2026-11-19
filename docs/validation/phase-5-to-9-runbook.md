# 第 5～9 期分期 Review 与测试环境验收手册

本文档面向两阶段收口：
1. 在代码合回 `master` 前，按期次逐段 Review 第 5～9 期全部改动；
2. 部署至独立测试集群后，按标准顺序完成端到端、在线接口、真实 DTB/LLM 闭环及 `#236` 业务评测回归。

---

## 第一部分：第 5～9 期分期 Review 导览

目前各期分支仍保持串联独立，**均未合并进 `master`**，推荐按以下顺序逐期审查 Diff、核心模块与架构决策文档：

| 期数 | 分支与精确 Diff 区间 | 核心模块 | 架构决策与文档 |
| --- | --- | --- | --- |
| **第 5 期** | `git diff master..phase-5-ossie` | `ossie_source.py`、`ossie_export.py`、`schemas/ossie/` | [ADR 0007](../adr/0007-ossie-semantic-interchange.md) |
| **第 6 期** | `git diff phase-5-ossie..phase-6-dtb-ossie` | `dtb_ossie.py`、`dtb_tasks.py`、`cli.py`（`convert-dtb-ossie`） | [ADR 0008](../adr/0008-dtb-to-ossie-pipeline.md) |
| **第 7 期** | `git diff phase-6-dtb-ossie..phase-7-ui-ux` | `project_types.py`、`project_api.py`、`web/src/`（全局切换、工作台导航、+ 输入栏、MCP 接入） | — |
| **第 8 期** | `git diff phase-7-ui-ux..phase-8-knowledge-evolution` | `semantic_overrides.py`、`corrections.py`、`correction_draft.py`、`eval_questions.py`、`trajectories.py`、`trajectory_miner.py`、`evolution_*.py`、`web/src/EvolutionPage.tsx` | [ADR 0009](../adr/0009-git-backed-semantic-overrides-and-correction-policy.md)、[ADR 0010](../adr/0010-local-trajectories-and-candidate-generation-gate.md) |
| **第 9 期** | `git diff phase-8-knowledge-evolution..phase-9-query-history-and-fk-probing` | `discovery_api.py`、`discovery_executor.py`、`query_history_miner.py`、`fk_probing.py`、`discovery_runner.py`、`personal_settings.py`、`migrations/versions/0041_*`、`web/src/DiscoveryPanel.tsx`、`README.md` | [ADR 0011](../adr/0011-query-history-mining-and-data-probed-relationships.md)、[GLOSSARY.md](../../GLOSSARY.md) |
| **收口验证** | `git diff phase-9-query-history-and-fk-probing..phase-5-9-release-readiness` | `.github/workflows/ci.yml`、`scripts/verify_phase_5_to_9.py`、`eval/phase-5-9-acceptance/`、`backend/tests/e2e/checks/phase_5_to_9.py`、`tests/test_deploy_manifests.py` | [docs/infrastructure.md](../infrastructure.md)、本手册 |

快速查看每一期的提交概览与变更统计：

```bash
git log --oneline master..phase-5-ossie
git diff --stat phase-5-ossie..phase-6-dtb-ossie
git diff --stat phase-6-dtb-ossie..phase-7-ui-ux
git diff --stat phase-7-ui-ux..phase-8-knowledge-evolution
git diff --stat phase-8-knowledge-evolution..phase-9-query-history-and-fk-probing
git diff --stat phase-9-query-history-and-fk-probing..phase-5-9-release-readiness
```

---

## 第二部分：上测试环境实测执行顺序

在具备独立 Neo4j / Milvus / Embedding 与测试集群资源的机器上，按以下 6 步依次验收（全部测试数据只写测试环境，严禁指向开发端口 `31601`/`31602`/`31603`）：

### 第 1 步：基础设施连通性自检

```bash
cd /path/to/Semantica_DataAgent
NEO4J_PASSWORD='<test-neo4j-password>' uv run --directory backend python ../scripts/check_infra.py test
```

### 第 2 步：自动化端到端 Harness 验收

使用隔离的临时 SQLite、测试存储与实际 MCP / REST 进程一次跑通第 5～9 期端到端自动化检查（含 Ossie 导入/导出、`convert-dtb-ossie`、项目类型边界、评测/演化/草稿 API 与发现配置防护）：

```bash
uv run --directory backend python tests/e2e/harness.py \
  --scratch "$HOME/wt/sda/scratch/phase-5-9" \
  --source-dir ../samples/demo \
  --source canonical --source markdown \
  --neo4j-password '<test-neo4j-password>' \
  --checks tests/e2e/checks/phase_5_to_9.py \
  --log "$HOME/wt/sda/scratch/phase-5-9/e2e.log"
```

### 第 3 步：已部署测试集群在线契约与构建验收

针对已经启动的测试集群服务（如 `http://172.16.242.124:31630`），通过 `--create-temp-project` 创建临时隔离项目上传 `eval/phase-5-9-acceptance/ossie-sample/` 并构建导出，执行后自动归档删除临时项目：

```bash
DATAAGENT_TOKEN='<admin-bearer-token>' \
uv run --directory backend python ../scripts/verify_phase_5_to_9.py \
  --base-url http://172.16.242.124:31630 \
  --create-temp-project \
  --phases all \
  --report "$HOME/wt/sda/scratch/phase5_9_cluster_report"
```

> 默认跳过依赖真实 DTB 令牌/数据与真实 LLM 调用的项目，适合部署后首轮快速冒烟。

### 第 4 步：真实 DTB 查询历史 + 数据探活与真实 LLM 演化验证

1. 在测试数仓型项目中配置可用的 LLM Provider 与 DTB 服务地址。
2. 提前在管理员 **个人设置**（`/personal-settings`）保存有效个人 DTB 令牌，或通过环境变量 `DATAAGENT_PERSONAL_DTB_TOKEN` 注入：

```bash
DATAAGENT_TOKEN='<admin-bearer-token>' \
DATAAGENT_PERSONAL_DTB_TOKEN='<personal-dtb-token>' \
uv run --directory backend python ../scripts/verify_phase_5_to_9.py \
  --base-url http://172.16.242.124:31630 \
  --project <target-warehouse-project> \
  --phases 8,9 \
  --run-live-dtb \
  --run-live-llm \
  --report "$HOME/wt/sda/scratch/phase5_9_live_report"
```

**实测重点核查与阈值校准建议**：
- 核查 SQLite `query_audit` 记录：确认所有发现拉取与数据探活查询均为 `sqlglot` AST 构造的带 `LIMIT` 只读 `SELECT`。
- 核查 `discovery_candidates` 证据：确认未定位到分区的分区表严格呈现 `skipped_no_partition` 且未发生全表扫描；确认表/字段/证据 JSON 中零业务事实表数据行落盘。
- 核查提案审批：确认全部 `source="discovery"` 提案状态均为 `pending`；通过纠错页审批后验证增量构建，并用一键回滚确认 `git revert` 生效。
- 根据真实方言与历史 SQL 规模，评估是否需要微调项目默认参数：`lookback_days`（默认 `7`）、`min_frequency`（默认 `3`）、`min_inclusion_ratio`（默认 `0.8`）、`probe_timeout_seconds`（默认 `30`）。

### 第 5 步：第四期 `#236` 业务评测基线与回归对比

将 `eval/` 下经业务审定的正式题集通过 `eval-split` 切分 `dev` / `holdout` 后运行基线与重复稳定性评测，并合并人工失败归因：

```bash
DATAAGENT_EVAL_TOKEN='<admin-bearer-token>' \
uv run --directory backend semantica-dataagent eval-exec \
  --questions <path/to/confirmed-questions.yaml> \
  --report "$HOME/wt/sda/scratch/eval_baseline" \
  --base-url http://172.16.242.124:31630 \
  --token DATAAGENT_EVAL_TOKEN \
  --agent pi --provider-id <provider-id> --model <model-name> \
  --split dev --repeat 3

uv run --directory backend semantica-dataagent eval-report \
  --report "$HOME/wt/sda/scratch/eval_baseline" \
  --categories <path/to/failure-categories.yaml>
```

### 第 6 步：人工 UI 交互抽检清单

打开测试环境 Web 界面，逐项执行 [cases.yaml](../../eval/phase-5-9-acceptance/cases.yaml) 中标记为 `execution_mode: manual_ui` 的 3 个用例：
- `p7-03`：全局项目切换、成员与项目管理员角色侧栏菜单边界；
- `p7-04`：类 Claude `+` 菜单无边框输入栏（锁定项目/Agent 状态）、表外键跳转与派生文档只读提示；
- `p7-05`：`/mcp-access` 动态令牌显隐与技能包下载、`/personal-settings` 个人 DTB 令牌保存清空联动；
- 并在 `/evolution`「冷启动发现」与 `/corrections`「历史发现」页面核对发现运行明细与候选证据卡片交互。
