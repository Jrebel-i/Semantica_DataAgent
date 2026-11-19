# 电商交易销售额与净销售额统计口径

## 1. 口径变体说明

- **支付 GMV**：统计 `hive_prod.dwd.dwd_orders` 中 `order_status != 'cancelled'` 的 `SUM(pay_amount_cny)`。
- **净销售额（net_gmv）**：在支付 GMV 基础上扣除 `refund_amount_cny`，即 `SUM(pay_amount_cny - refund_amount_cny)`。

## 2. 分区与城市维表关联约束

- 所有针对 `hive_prod.dwd.dwd_orders` 的查询必须显式带 `dt` 分区过滤条件。
- 按城市或战区汇总时，请通过显式外键 `fk_orders_city`（`dwd_orders.city_code = dim_city.city_code`）关联 `hive_prod.dim.dim_city`。
