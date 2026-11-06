# 规范数据模型 v2

`v2/*.schema.json` 是七类记录的 JSON Schema（Draft 2020-12）。源目录中的 `tables`、`fields`、`tasks`、`lineage`、`column_lineage`、`dimension_values`、`documents` 文件以扩展名选择 JSON、CSV 或 XLSX；JSON 是记录数组，CSV 和 XLSX 的首行是英文属性名。字段的 `partition` 在表格中写 `true` / `false`。文件名决定实体类型；每条记录都经过对应 schema 校验。适配器返回每个源文件的 SHA-256。知识文档的 `path` 是相对知识类目录的路径。`samples/demo` 是虚构示例。

`column_lineage` 描述字段到字段的依赖，包含任务标识、证据及解析状态；v1 文件仍保持兼容。字段元数据变化后增量构建重新计算字段依赖；旧知识代需重新构建才能产生字段级关系。
