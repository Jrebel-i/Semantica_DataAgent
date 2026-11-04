# 本项目代码规范

通用规则见 `~/.claude/dev-standards/rules.md`。本文件只写本项目特有的规则，以及从 LESSONS.md 升级来的规则；与通用规则冲突时，以本文件为准。review 时逐条检查。

## 命令

后端使用 Python 3.12 和 uv；前端使用 Node 24 和 pnpm。

| 用途 | 命令（仓库根目录） |
| --- | --- |
| 安装后端依赖 | `uv sync --directory backend --locked` |
| 后端单测 | `uv run --directory backend pytest tests/test_api.py` |
| 守护测试 | `uv run --directory backend pytest ../tests/test_agents_size.py ../tests/test_data_ignore.py` |
| lint / 格式检查 | `uv run --directory backend ruff check src tests`；`uv run --directory backend ruff format --check src tests` |
| 前端安装 / 测试 | `pnpm --dir web install --frozen-lockfile`；`pnpm --dir web test` |
| 前端格式检查 | `pnpm --dir web format:check`（生成的 `src/api.ts` 由类型漂移检查管理） |
| 类型检查 / 构建 | `pnpm --dir web build` |
| OpenAPI 与类型漂移 | `pnpm --dir web check:types`；更新时先 `uv run --directory backend python -m semantica_dataagent.openapi > web/contract/openapi.json`，再 `pnpm --dir web types` |
| 全量套件 | 仅发布级验收时执行项目所有测试；本票按任务书只运行相关测试。 |

## 平台与环境矩阵

Python 3.12 / uv；Node 24 / pnpm 10；开发机 WSL2 Linux；存储运行在 k8s，见 [infrastructure.md](infrastructure.md)。

## 守护测试

`tests/test_agents_size.py` 检查根目录 AGENTS.md 不超过 16,000 字节；每次改动都运行。`tests/test_data_ignore.py` 使用 `git check-ignore` 检查 `data/` 下的业务数据不会进入 git；每次改动都运行。`web check:types` 同时比较后端 OpenAPI、快照与生成类型。

## Red check 复验做法

后端通过 uv 可编辑安装；复制 worktree 时复制的 `backend/.venv` 仍指向原 worktree 源码，直接运行会测到原代码并假通过。在 Linux / WSL 的副本中删除复制的虚拟环境并重建，再应用回退补丁和运行测试：

```sh
python3 -c "import shutil; shutil.rmtree('backend/.venv')"
uv sync --directory backend --locked
patch -R -p1 < /path/to/rollback.patch
PYTHONPYCACHEPREFIX="$(mktemp -d)" uv run --directory backend pytest -p no:cacheprovider tests/test_api.py::test_authentication -q
```

使用临时 HOME；清理新建的 pycache 和副本。

## 运行应用

设置 `DATAAGENT_DB_PATH`（SQLite 文件路径）、`DATAAGENT_ENCRYPTION_KEY`（Fernet 密钥，使用 `python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'` 生成）和可选的 `DATAAGENT_LISTEN`（默认 `127.0.0.1:8000`）。`uv run --directory backend semantica-dataagent init` 建库并仅输出一次管理员 token，妥善保存；再运行 `uv run --directory backend semantica-dataagent serve`。前端开发使用 `pnpm --dir web dev`。管理员通过 `/api/v1/settings` 的 PUT 写入 `values`；键为 `neo4j_uri`、`neo4j_user`、`neo4j_password`、`milvus_uri`、`embedding_url`，空字符串或 null 删除。GET 不回显密码；连接地址不得包含用户信息、查询参数或片段，Neo4j 密码使用专用字段。配置修改对下一次健康检查生效。

## 真实使用

真实集群连接使用 [infrastructure.md](infrastructure.md) 的测试命名空间与凭据。健康检查只读；其他基础设施检查脚本可能创建临时集合。只在 `semantica-dataagent` 和 `semantica-dataagent-test` 命名空间操作；不得改动其他命名空间或集群级资源。

## 已拍板的决定

架构决策见 [ADR 目录](adr/)。尤其注意 [存储边界](adr/0002-neo4j-plus-milvus-without-cross-store-release.md) 和 [插件边界](adr/0006-pluggable-ingestion-for-open-source.md)。

## 存量失败基线

| 测试 | 失败摘要 | issue |
| --- | --- | --- |

## 从 LESSONS 升级的规则

<!-- 每条规则后面附 2–3 条证据（spec / PR / commit）。 -->

## 语言与框架规则

## 发布

<!-- 版本号规则、产物清单、发布渠道，以及如何确认发布成功。 -->
