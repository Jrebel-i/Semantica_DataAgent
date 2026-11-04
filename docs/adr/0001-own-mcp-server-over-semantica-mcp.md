# 自建 HTTP MCP，不使用 semantica-mcp

Semantica 自带的两个 MCP server 只走 stdio、没有鉴权，背后只有内存图（JSON 文件）和内存/SQLite 向量，连不上 Neo4j 和 Milvus，搜索是子串匹配，也没有知识类、索引地图、表结构、SQL 校验等工具。因此我们只把 Semantica 当作库使用（摄入、存储适配器、检索），在 FastAPI 上自建 streamable HTTP MCP，在线 agent 和本地用户连同一个 MCP。上游 #1663（Neo4j session）、#1722（自定义工具）落地后可以再评估。
