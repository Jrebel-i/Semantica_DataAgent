# 存储用 Neo4j + Milvus，两者之间不做跨库发布

Neo4j 存结构、关系和全文索引，Milvus 只存向量，两者只用 id 对应。参考项目 DataAgent 用 Neo4j + Fuseki + Milvus 三套存储并做跨库原子发布与回滚，带来很重的维护和延迟负担，所以这里不用 RDF/SPARQL，也不做跨库事务：构建先写 Neo4j 再写 Milvus，任一边失败就把这次构建标为失败，下次重跑覆盖。

## Consequences

- 检索有硬约束：每次 `search_knowledge` 最多 5 次存储往返（集群往返约 61ms）。
- 测试必须连真实的 Neo4j 和 Milvus，不用假存储。
