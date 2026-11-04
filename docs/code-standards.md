# 本项目代码规范

通用规则见 `~/.claude/dev-standards/rules.md`。本文件只写本项目特有的规则；与通用规则冲突时以本文件为准。

## 命令

后端使用 Python 3.12 和 uv；前端使用 Node 24 和 pnpm。

| 用途 | 命令（仓库根目录） |
| --- | --- |
| 安装后端依赖 | `uv sync --directory backend --locked` |
| 后端单测 | `uv run --directory backend pytest tests/test_api.py` |
| 守护测试 | `uv run --directory backend pytest ../tests/test_agents_size.py` |
| lint / 格式检查 | `uv run --directory backend ruff check src tests`；`uv run --directory backend ruff format --check src tests` |
| 前端安装 / 测试 | `pnpm --dir web install --frozen-lockfile`；`pnpm --dir web test` |
| 类型检查 / 构建 | `pnpm --dir web build` |
| OpenAPI 与类型漂移 | `pnpm --dir web check:types`；更新时先 `uv run --directory backend python -m semantica_dataagent.openapi > web/contract/openapi.json`，再 `pnpm --dir web types` |
| 全量套件 | 仅发布级验收时执行项目所有测试；本票按任务书只运行相关测试。 |

## 守护测试

`tests/test_agents_size.py` 检查根目录 AGENTS.md 不超过 16,000 字节；每次改动都运行。`web check:types` 同时比较后端 OpenAPI、快照与生成类型。

## 运行应用

设置 `DATAAGENT_DB_PATH`（SQLite 文件路径）、`DATAAGENT_ENCRYPTION_KEY`（Fernet 密钥，使用 `python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'` 生成）和可选的 `DATAAGENT_LISTEN`（默认 `127.0.0.1:8000`）。`uv run --directory backend semantica-dataagent init` 建库并仅输出一次管理员 token，妥善保存；再运行 `uv run --directory backend semantica-dataagent serve`。前端开发使用 `pnpm --dir web dev`。管理员通过 `/api/v1/settings` 的 PUT 写入 `values`；键为 `neo4j_uri`、`neo4j_user`、`neo4j_password`、`milvus_uri`、`embedding_url`，空字符串或 null 删除。GET 不回显密码；连接地址不得包含用户信息、查询参数或片段，Neo4j 密码使用专用字段。配置修改对下一次健康检查生效。

## 真实使用

真实集群连接使用 [infrastructure.md](infrastructure.md) 的测试命名空间与凭据。健康检查只读；其他基础设施检查脚本可能创建临时集合。只在 `semantica-dataagent` 和 `semantica-dataagent-test` 命名空间操作；不得改动其他命名空间或集群级资源。

## 已拍板的决定

架构决策见 [ADR 目录](adr/)。尤其注意 [存储边界](adr/0002-neo4j-plus-milvus-without-cross-store-release.md) 和 [插件边界](adr/0006-pluggable-ingestion-for-open-source.md)。

## 存量失败基线

| 测试 | 失败摘要 | issue |
| --- | --- | --- |
