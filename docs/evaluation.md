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
| `answer_confirmed` | `true` | 参考答案是否经业务确认；`false` 的题默认排除，显式开启只采集时才运行 |

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
  --web-url https://dataagent.example.com \
  --token DATAAGENT_TOKEN \
  --agent pi --provider-id <provider> --model <model> \
  --max-queries 200
```

- `--split dev`：默认；只跑开发集。
- `--split holdout`：显式跑留出集，只在发版验收时使用。
- `--split all`：调试用；会同时跑两套题。
- `--repeat 3`：同一道题跑 3 遍，每遍新开一个对话。判对错只看多数判定：可比较遍数（`equal` 或 `different`）必须超过总遍数的一半；其中 `equal` 超过可比较遍数的一半算对，正好一半算平局（按答错计入 `tie`），否则算错。波动使用三态：所有遍都有可比较结果且一致时为 `stable`；至少两个可比较结果不同（即使其余遍不可比）时为 `volatile`；没有可比较结果，或只有一种可比较结果但其余遍不可比时，为 `unknown`。`unknown` 单独汇总，不能写成“不波动”或“稳定”。参考 SQL 正常执行时，`--repeat 1` 的 `equal`、`comparable` 和 `consistency_rate` 与升级前一致；参考 SQL 失败、跳过或可比较遍数不足一半的题在任何 repeat 下都归为 `uncomparable`。
- `--only q1,q2`：只跑指定题号，保留部署后的快速冒烟用法；题号必须在当前 `split` 且未被排除。
- `--web-url https://dataagent.example.com`：浏览器可访问的外部前端**站点根地址**，用于生成根路径 `/chat/<conversation_id>` 会话深链接。它只接受 `http(s)://host[:port]`（可带一个结尾 `/`），不能包含路径前缀、凭据、查询参数或片段。当前前端只识别根路径 `/chat/<id>`，没有验证过的反向代理子路径部署支持，因此 `https://dataagent.example.com/workbench` 会被明确拒绝，不会静默生成打不开的 `/workbench/chat/<id>`。它是独立参数，不从 `--base-url`、`DATAAGENT_PUBLIC_URL` 或监听地址推导；省略时报告仍写对话 id，但不会猜测链接。`eval-report` 也接受 `--web-url`，可给旧报告补链接；旧报告里的 `web_url` 若带路径前缀，重算时会明确报错，需要传入站点根地址修正。
- `--expected-categories a,b,c`：覆盖默认的期望题型列表（维度值筛选、时间语义、口径歧义、合法空结果、多表 Join）。
- `--max-queries`：按“题数 ×（repeat + 参考 SQL 一遍）”估算，默认 40。默认 28 道 dev 题跑 1 遍需要 56，跑 3 遍需要 112；跑完整开发集时要显式提高，例如 `--max-queries 200`。`--only` 冒烟保持默认即可。
- `--skip-reference`：不执行参考 SQL，此时所有题都是 `uncomparable`，不进一致率分母。与 `--collect-unconfirmed` 一起使用时，参考 SQL 一律不发送到执行接口。
- `--collect-unconfirmed`（别名 `--collect-only`）：显式采集 `answer_confirmed=false` 的题，保留每遍对话 id、工具调用、生成 SQL 和执行状态。必须同时给出 `--skip-reference`，否则在任何网络请求前拒绝。默认关闭，已确认题的默认运行方式不变。

会话深链接使用站点根路径下的 `/chat/<conversation_id>`；当前前端只识别根路径，不支持带路径前缀的反向代理部署。页面用当前登录身份请求 `/api/v1/conversations/{id}`，后端继续校验会话所有权，项目访问沿用现有规则；未登录时先登录，无权限的会话不会显示。打开 `/` 或 `/chat` 仍是新对话，行为不变。

未确认答案的题默认被排除，不计入任何指标，并在报告的 `excluded_unconfirmed` 和 Markdown“未确认答案”一节列出。它们仍会出现在“全部题集”覆盖统计里，用来判断题型是否存在；报告用 `question_set_coverage.includes_unconfirmed=true` 明确标注这一点，同时用 `confirmed_coverage_gaps` 单独给出已确认题缺失的题型。

开启 `--collect-unconfirmed --skip-reference` 后，所选 `split` 内的未确认题会实际运行，但题目保留 `answer_confirmed=false` 并标记 `collect_only=true`、`uncomparable_reason=answer_confirmed=false`。它们计入 `questions`、`runs`、`question_set_coverage`、`coverage` 和运行范围统计，但不进入 `equal`、`comparable`、`consistency_rate`、正确率或已确认题分母，`comparable_runs` 恒为 0。未确认题的参考 SQL 不发送到执行接口，因此不计算 `missing_condition_columns`、`condition_incomplete` 或参考列数。允许在 `eval-report` 中依据执行轨迹、工具调用等独立证据填写人工观察分类，但它写入独立的 `observed_failure_category` / `observed_failure_evidence`，不写入 `failure_category`，不进入已验证失败分类汇总，也不代表答案正确或错误。留出集仍要显式 `--split holdout`，只采集开发集时不会运行或泄露留出题内容。

参考 SQL 本身失败、或可比较遍数没有超过一半的题，`result` 为 `uncomparable`：不算错题，不进 `equal` / `comparable` / `consistency_rate` 的分母，并在 `excluded_uncomparable` 和 Markdown“不可比题目”一节单独列出。

报告同时写 `<report>.json` 和 `<report>.md`。JSON 顶层包含 `split`、`only`、`repeat`、`collect_unconfirmed`、`collected_unconfirmed`、`web_url`、`expected_categories`、`metrics`、`excluded_unconfirmed`、`excluded`、`excluded_uncomparable`、`failure_reasons`、`failure_categories`、`observed_failure_categories`、`coverage`、`evaluation_coverage`、`question_set`、`question_set_coverage`、`coverage_gaps`、`confirmed_coverage_gaps`、`scope_coverage_gaps`、`unconfirmed_only_categories` 和 `questions`。每道题包含 `answer_confirmed`、`collect_only`、实际 SQL、参考 SQL、逐遍的 `runs`（对话 id、外部链接 `conversation_url`、工具调用序列、实际 SQL、agent 与参考执行状态、一致性结果），以及 `equal_runs`、`comparable_runs`、`result`（`correct` / `wrong` / `tie` / `uncomparable`）、`correct`、`volatility`（`stable` / `volatile` / `unknown`）、`volatile`（未知时为 `null`）、`volatility_reason`、`uncomparable_reason`、`failure_category` 和 `observed_failure_category` / `observed_failure_evidence`。题目级的 `comparison` / `consistency` / `summary` 和展示用 SQL 取多数判定对应的第一遍，不再固定取第一遍。

核心指标只有一套多数口径：

- `equal`：多数判定为对的题数，等于 `correct`。
- `comparable`：可比较遍数超过一半的题数，是一致率分母。
- `consistency_rate`：`equal / comparable`；不再提供按第一遍计算的 `majority_consistency_rate`。
- `uncomparable`：参考 SQL 失败、参考 SQL 被跳过、已确认题可比较遍数不足一半，或未确认题仅采集的题数。
- `volatile`：已证明结果波动的题数，只计 `volatility=volatile`；不能把 `unknown` 当作稳定。
- `volatility_unknown`：没有足够可比较证据、无法判断是否波动的题数；与 `volatile` 分开汇总。

执行与澄清类指标的单位写进字段名或在本节固定：

- `execution_success` / `execution_total` / `execution_success_rate`：按遍统计，分母是实际发起了执行的遍数（有 SQL 的遍）。
- `clarified`：至少一遍发生澄清的题数；`clarified_runs`：发生澄清的遍数。
- `via_plan` / `via_plan_runs`：至少一遍走查询计划的题数 / 走查询计划的遍数。
- `no_sql` / `no_sql_runs`：至少一遍没有 SQL 的题数 / 没有 SQL 的遍数。
- `condition_incomplete` / `condition_incomplete_runs`：至少一遍缺参考 SQL 条件列的题数 / 缺条件列的遍数；仅采集的未确认题恒为 0。
- `confirmed_questions` / `collected_only_questions` / `collected_only_runs`：仅在开启 `--collect-unconfirmed` 时出现，分别表示参与计分的已确认题数、仅采集题数和仅采集遍数；`questions` 与 `runs` 包含仅采集部分。

覆盖统计分成三层，避免 `--only` 或只跑一个 split 时把别的题误报成缺失：

- `question_set_coverage`：全部题集（含留出集和未确认题）按 `category`、`split`、`answer_confirmed` 汇总。它只有计数，不包含题目内容或参考 SQL，不会泄露留出题答案。`includes_unconfirmed=true` 表示未确认题只参与“题型是否存在”的判断。
- `coverage` / `evaluation_coverage`：本次 `--split` + `--only` 实际评测范围的逐类结果，用于计算答对、答错、平局、不可比、已证明波动和波动未知；`failure_categories` 只统计已验证题，仅采集题的人工观察单独放在 `observed_failure_categories`，并继续以 `collect_only` 计数出现，但不影响答对与一致率分母。
- `coverage_gaps`：期望题型在**全部题集**中题数为 0 的类别，对应“题集缺少的题型”。某类题存在但全部答错不算覆盖缺口。
- `confirmed_coverage_gaps`：期望题型在**已确认题**中题数为 0 的类别，反映可用题集的缺口。
- `scope_coverage_gaps`：期望题型在**本次评测范围**中题数为 0 的类别。只用于说明这一轮没覆盖，不等于题集缺失。
- `unconfirmed_only_categories`：只有未确认题、尚无可用参考答案的题型。

题集里也不要为凑覆盖而补造题目。旧报告没有 `question_set` 元数据时，`eval-report` 会用报告里的已评测题目回退计算，并把 `question_set_coverage.source` 标为 `report_questions`、`includes_unconfirmed=false`。

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

也可以写成对象列表，并为仅采集题附上独立证据：`[{id: q1, failure_category: 选错表}, {id: u1, failure_category: 口径错, evidence: "第 2 遍 agent status=succeeded，工具调用显示..."}]`。然后合并回报告并重算汇总：

```sh
semantica-dataagent eval-report \
  --report "$HOME/wt/sda/scratch/eval/baseline" \
  --categories "$HOME/wt/sda/scratch/eval/categories.yaml"
```

命令会更新 JSON 里的每题分类，重算 `failure_categories`、`observed_failure_categories`、`coverage` 和 `metrics.classified/unclassified`，并重写 Markdown。未知题号、不在枚举里的分类和非法 `evidence` 类型会直接拒绝。已确认题写入 `failure_category`；`collect_only=true` 的未确认题写入 `observed_failure_category`，有 `evidence` 时同时写入 `observed_failure_evidence`，`failure_category` 保持为空。Markdown 把后者单列为“仅采集题人工观察（答案未验证）”，明确它不是参考 SQL 验证出的结果判定，不进入正确率、结果一致率、可比较题或已确认题分母。修正分类时重复执行同一命令即可，后一次给出的值覆盖旧值；把某题写成空值可清空该题分类及观察证据。

检查点时按 `coverage` 和 `failure_categories` 查看已确认题“每类错误占几题”，`observed_failure_categories` 只用于仅采集题的独立观察，二者不能相加，也不能把观察分类当成已验证错误数。这是人工归因流程，不依赖 Langfuse 里的内容，也不在 CI 中自动运行。
