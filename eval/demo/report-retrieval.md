# 评测报告

- 生成时间：2026-11-05T09:25:07.607803+00:00
- 模式：retrieval
- 题集：questions.yaml
- 前 N 条：5

## 总体指标

| 指标 | 值 |
| --- | --- |
| 题数 | 30 |
| 表召回率@5 | 98.33% |
| 表全命中题占比@5 | 96.67% |
| validate_sql 通过率 | 100.00% |
| 口径命中率 | N/A |
| 参考 SQL 口径自检率 | 100.00% |
| 检索错误题数 | 0 |
| Agent 错误题数 | 0 |

## 判定方法

- `table_recall_at_5`：Mean over questions of |expected tables intersected with the first 5 table hits| / |expected tables|; duplicate hits count once.
- `validate_sql_pass_rate`：Fraction of questions whose checked SQL returns validate_sql status=pass. Retrieval mode checks reference_sql; agent mode checks the agent answer SQL.
- `reference_sql_caliber_hit_rate`：Retrieval mode self-check: the same caliber rule applied to reference_sql. It is reported separately and is not used as the agent caliber metric.

## 逐题结果

| 题号 | 表召回率@5 | validate_sql | 口径命中 | 失败原因 |
| --- | --- | --- | --- | --- |
| d01 | 1.00 | 通过 | — | — |
| d02 | 1.00 | 通过 | — | — |
| d03 | 1.00 | 通过 | — | — |
| d04 | 1.00 | 通过 | — | — |
| d05 | 1.00 | 通过 | — | — |
| d06 | 1.00 | 通过 | — | — |
| d07 | 1.00 | 通过 | — | — |
| d08 | 1.00 | 通过 | — | — |
| d09 | 1.00 | 通过 | — | — |
| d10 | 1.00 | 通过 | — | — |
| d11 | 1.00 | 通过 | — | — |
| d12 | 1.00 | 通过 | — | — |
| d13 | 1.00 | 通过 | — | — |
| d14 | 1.00 | 通过 | — | — |
| d15 | 1.00 | 通过 | — | — |
| d16 | 1.00 | 通过 | — | — |
| d17 | 1.00 | 通过 | — | — |
| d18 | 1.00 | 通过 | — | — |
| d19 | 1.00 | 通过 | — | — |
| d20 | 1.00 | 通过 | — | — |
| d21 | 1.00 | 通过 | — | — |
| d22 | 1.00 | 通过 | — | — |
| d23 | 1.00 | 通过 | — | — |
| d24 | 1.00 | 通过 | — | — |
| d25 | 1.00 | 通过 | — | — |
| d26 | 1.00 | 通过 | — | — |
| d27 | 1.00 | 通过 | — | — |
| d28 | 0.50 | 通过 | — | table recall@5 0.50 < 1.00: missing shop.payments |
| d29 | 1.00 | 通过 | — | — |
| d30 | 1.00 | 通过 | — | — |

## 失败原因

### d28：销售额要不要扣掉退款？净额怎么算？

- 期望表：shop.payments, shop.refunds
- 实际召回：shop.refunds, shop.daily_sales, shop.orders, shop.returns, shop.inventory
- 期望字段：shop.payments.id, shop.refunds.id
- 口径：全额退款从净支付销售额扣除，部分退款按实际退款金额扣除；问题未说明全额/部分及日期口径时应澄清。
- 出处：出处：samples/demo/knowledge/metrics/definitions/sales.md（退款处理：全额扣除、部分按实际金额、退款日与支付日不同）。
- table recall@5 0.50 < 1.00: missing shop.payments
- 归类：检索没召回
