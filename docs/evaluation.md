# 取数评测

评测命令面向数仓型项目，沿用 `semantica-dataagent eval-exec`。题集文件只放在本地或部署机（默认在 `data/eval/`，不入仓库），测试和文档不能引用真实题集的业务内容。

## 题集格式

题集是 YAML 列表，也可以写成 `{questions: [...]}`。每道题原有字段保持不变：

| 字段 | 含义 |
| --- | --- |
| `id` | 题号，题集内唯一 |
| `question` | 用户原话 |
| `expected_tables` / `expected_fields` | 期望命中的表和字段 |
| `caliber` | 业务口径 |
| `reference_sql` | 业务确认过的参考 SQL |
| `notes` / `dialect` / `clarify` | 可选；`dialect` 默认 `hive` |

#235 新增三个字段：

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `category` | `未分类` | 覆盖统计用的题型分类 |
| `split` | `dev` | `dev` 或 `holdout`，由拆分命令写入 |
| `answer_confirmed` | `true` | 参考答案是否经业务确认；`false` 的题不参与评测 |

旧题集缺少 `category`、`split`、`answer_confirmed` 时按“未分类、dev、已确认”运行，不需要改文件。新题如果答案还没经过业务确认，必须显式写 `answer_confirmed: false`。

示例：

```yaml
- id: q1
  question: 最近一年有多少订单？
  expected_tables: [shop.orders]
  expected_fields: [shop.orders.id]
  caliber: 订单数按订单编号去重，按 dt 过滤
  reference_sql: SELECT COUNT(DISTINCT id) FROM shop.orders WHERE dt >= '2025-01-01'
  category: 指标口径
  answer_confirmed: true
  split: dev
```

## 拆分开发集与留出集

```sh
semantica-dataagent eval-split --questions data/eval/questions.yaml
```

默认按 `category` 分层，用固定种子 `235` 和默认留出比例 30% 拆分（40 题时是 28 / 12），把 `split` 写回原文件。写回先写临时文件再原子替换，重复执行同一份题集得到相同结果。

常用参数：

- `--seed 235`：固定随机种子。
- `--holdout-ratio 0.3`：按比例计算留出题数。
- `--holdout-size 12`：直接指定留出题数，其余为 dev。
- `--dev-size 28`：直接指定开发集题数，其余为 holdout。

`--dev-size` 与 `--holdout-size` 同时给出时必须正好等于题数。固定种子和题集内容不变时，拆分结果可复现；调整题集后应重新检查每个 `category` 在两侧都有代表题。

## 运行评测

默认只跑开发集，并且每道题只跑一遍：

```sh
semantica-dataagent eval-exec \
  --questions data/eval/questions.yaml \
  --report "$HOME/wt/sda/scratch/eval/baseline" \
  --base-url http://127.0.0.1:8000 \
  --token DATAAGENT_TOKEN \
  --agent pi --provider-id <provider> --model <model> \
  --max-queries 200
```

- `--split dev`：默认；只跑开发集。
- `--split holdout`：显式跑留出集，只在发版验收时使用。
- `--split all`：调试用；会同时跑两套题。
- `--repeat 3`：同一道题跑 3 遍，每遍新开一个对话；多数一致算对，平局算错，出现过不一致的题标为波动题。
- `--only q1,q2`：只跑指定题号，保留部署后的快速冒烟用法；题号必须在当前 `split` 且未被排除。
- `--max-queries`：按“题数 ×（repeat + 参考 SQL 一遍）”估算，默认 40。默认 28 道 dev 题跑 1 遍需要 56，跑 3 遍需要 112；跑完整开发集时要显式提高，例如 `--max-queries 200`。`--only` 冒烟保持默认即可。
- `--skip-reference`：不执行参考 SQL，此时所有题都是 `uncomparable`。

未确认答案的题先被排除，不计入任何指标，并在报告的 `excluded_unconfirmed` 和 Markdown“未确认答案”一节列出。

报告同时写 `<report>.json` 和 `<report>.md`。JSON 顶层包含 `split`、`repeat`、`metrics`、`excluded_unconfirmed`、`excluded`、`failure_reasons`、`failure_categories`、`coverage`、`coverage_gaps` 和 `questions`。每道题包含实际 SQL、参考 SQL、逐遍的 `runs`（对话 id、工具调用序列、实际 SQL、一致性结果），以及 `equal_runs`、`result`（`correct` / `wrong` / `tie`）、`correct`、`volatile` 和 `failure_category`。

`coverage` 按题目的 `category` 统计题数、答对、答错、平局和波动；某类题全部未答对时列进 `coverage_gaps`。

## 失败归因与合并

失败分类固定为：

- 选错表
- 选错字段
- 口径错
- 取值错
- 时间语义错
- Join / 粒度错
- 该澄清没澄清
- 不该澄清却澄清
- 题目本身有问题

分类由人看过报告后填写。可以在一个 YAML 映射里写题号到分类：

```yaml
q1: 选错表
q2: 口径错
```

也可以写成对象列表：`[{id: q1, failure_category: 选错表}]`。然后合并回报告并重算汇总：

```sh
semantica-dataagent eval-report \
  --report "$HOME/wt/sda/scratch/eval/baseline" \
  --categories "$HOME/wt/sda/scratch/eval/categories.yaml"
```

命令会更新 JSON 里的每题 `failure_category`，重算 `failure_categories`、`coverage` 和 `metrics.classified/unclassified`，并重写 Markdown。未知题号或不在枚举里的分类会直接拒绝。修正分类时重复执行同一命令即可，后一次给出的值覆盖旧值；把某题写成空值可清空该题分类。

检查点时按 `coverage` 和 `failure_categories` 查看“每类错误占几题”。这是人工归因流程，不依赖 Langfuse 里的内容，也不在 CI 中自动运行。
