# 规范数据模型 v1

`v1/*.schema.json` 是六类记录的 JSON Schema（Draft 2020-12）。源目录中的 `tables`、`fields`、`tasks`、`lineage`、`dimension_values`、`documents` 文件以扩展名选择 JSON、CSV 或 XLSX；JSON 是记录数组，CSV 和 XLSX 的首行是英文属性名。字段的 `partition` 在表格中写 `true` / `false`。文件名决定实体类型；每条记录都经过对应 schema 校验。适配器返回每个源文件的 SHA-256。知识文档的 `path` 是相对知识类目录的路径。`samples/demo` 是虚构示例。
