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

设置 `DATAAGENT_DB_PATH`（SQLite 文件路径）、`DATAAGENT_ENCRYPTION_KEY`（Fernet 密钥，使用 `python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'` 生成）和可选的 `DATAAGENT_LISTEN`（默认 `127.0.0.1:8000`）。`uv run --directory backend semantica-dataagent init` 建库并仅输出一次管理员 token，妥善保存；再运行 `uv run --directory backend semantica-dataagent serve`。前端开发使用 `pnpm --dir web dev`。管理员通过 `/api/v1/settings` 的 PUT 写入 `values`；键为 `neo4j_uri`、`neo4j_user`、`neo4j_password`、`milvus_uri`、`embedding_url`、`embedding_model`、`embedding_concurrency`、`embedding_timeout_seconds`、`embedding_timeout_per_input_seconds`、`embedding_retries`、`embedding_backoff_seconds`、`source_directory`、`knowledge_generation`，空字符串或 null 删除。GET 不回显密码；连接地址不得包含用户信息、查询参数或片段，Neo4j 密码使用专用字段。配置修改对下一次健康检查生效；embedding 并发、超时、重试与退避参数在下一次构建开始时读取，检索请求则每次读取当前值。

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

## 共享测试存储隔离

日常集成测试默认使用小模型：`TEST_EMBEDDING_URL=http://127.0.0.1:17998`、`TEST_EMBEDDING_MODEL=model/bge-small-zh-v1.5`（512 维）。只有真实样例构建、验收评测，或专门复验 bge-m3 兼容性时，才改用 `http://127.0.0.1:17997` 与 `model/bge-m3`（1024 维）；不要用 bge-m3 跑日常主接缝测试。连接 `-test` Neo4j / Milvus / embedding 的集成测试，每次运行必须生成唯一知识代与集合前缀，只读取和清理本次创建的数据；在 `finally` 中删除这些节点和集合。不能清理或修改其他测试的代，缺少连接环境变量时必须明确跳过并说明原因。

## 端到端测试工具

测试 agent 不再为每张票手写起服务、配存储、构建和清理的脚本。工具在 `backend/tests/e2e/harness.py`，一条命令完成临时 SQLite + Fernet 密钥、`init` 管理员 token、随机空闲端口起 `serve`、配置测试存储与 embedding、启用源适配器与解析器、构建、执行检查，并在成功、失败、超时和中断时清理本代数据、停服务、删除临时库。

从仓库根目录运行：

```sh
export TMPDIR="$HOME/wt/sda/scratch/tmp"
uv run --directory backend python tests/e2e/harness.py \
  --scratch "$HOME/wt/sda/scratch/issue-47" \
  --source-dir ../samples/demo \
  --source canonical --source markdown \
  --neo4j-password-command "ssh baize@172.16.242.124 kubectl -n semantica-dataagent-test get secret neo4j-auth -o jsonpath='{.data.auth}' | base64 -d | cut -d/ -f2-" \
  --checks tests/e2e/example_checks.py \
  --log "$HOME/wt/sda/scratch/issue-47/e2e.log"
```

`--neo4j-password-command` 的 stdout 只进入进程内存（去掉末尾换行和可选的 `neo4j/` 前缀），不打印、不落盘。也可以改用环境变量 `NEO4J_PASSWORD` 或 `--neo4j-password-env <名称>`。默认存储是测试实例 `bolt://172.16.242.124:31611`、`http://172.16.242.124:31612` 和本机 embedding `http://127.0.0.1:17998`（`model/bge-small-zh-v1.5`）；需要 bge-m3 时传入 `--embedding-url http://127.0.0.1:17997 --embedding-model model/bge-m3`；工具拒绝连接开发实例的 31601 / 31602 / 31603。

常用参数：

| 参数 | 作用 |
| --- | --- |
| `--scratch` | 必填；本次临时目录和 `e2e-<代名>-serve.log` 的父目录 |
| `--source-dir` | 源目录，默认仓库内 `samples/demo` |
| `--source ID[:JSON]` | 启用的源适配器及参数，可重复；默认 `canonical` + `markdown` |
| `--parser ID` | 启用的任务解析器，可重复；默认全部解析器 |
| `--embedding-url` / `--embedding-model` | embedding 服务地址和模型名；默认使用本机小模型，可同时指定以切换 bge-m3 |
| `--checks <文件>` | 票专属检查文件，可重复 |
| `--generation` / `--generation-prefix` | 指定知识代名，或使用前缀加随机后缀（默认 `e2e_`） |
| `--build-timeout` | 等待构建结束的秒数，默认 900 |
| `--expect-build succeeded\|failed\|any` | 期望的构建结果，默认 `succeeded` |
| `--no-builtin-checks` | 不跑内置检查（测试预期失败的构建时使用） |
| `--expect-table` / `--search-query` / `--validate-sql` | 内置检查使用的表、检索词和 SQL |
| `--log <文件>` | 同时把带时间戳的进度和最终结果写入该文件 |

dtb 源的参数示例：

```sh
--source 'dtb:{"snapshot_path":"dtb","dimensions_path":"dimensions","latest":true,"dimension_row_limit":50000}' \
--source 'markdown:{"path":"knowledge"}'
```

内置检查：`describe_tables`、`search_knowledge`、`get_lineage`、`validate_sql`、`get_capabilities`、`build_counts`（构建记录与计数）。内置检查在 `--expect-build failed` 时通常也会失败，测试失败构建的票应加 `--no-builtin-checks`，只保留票专属检查。

票专属检查放在一个 Python 文件里，函数名以 `check_` 开头；不需要改工具本身。每个函数收到一个上下文，可同步或异步：

```python
def check_join_evidence(ctx):
    edges = ctx.mcp_call(
        "get_lineage", {"table": "shop.orders", "direction": "up", "depth": 1}
    )
    ctx.require(edges, "expected upstream lineage")
    response = ctx.rest.get("/api/v1/tables", params={"database": "shop"})
    ctx.require(response.status_code == 200, response.text)
    return {"edges": len(edges)}  # 见下面的返回值约定
```

`ctx` 提供 `mcp_call(name, arguments, raw=False)`（工具失败时抛异常；`raw=True` 返回完整结果以便处理游标）、`rest`（已带 admin token 的同步 `httpx.Client`）、`token`、`build`、`build_id`、`generation`、`source_dir`、`health`、`work_dir` 和 `require(condition, message)`。检查函数抛异常、返回 `False`、或返回带 `ok: false` 的 dict 记为失败；返回 `None` / `True` / 其他 dict 都记为通过，dict 内容作为 `info` 写入结果。一个检查失败不会跳过其余检查。

输出约定：进度行带本地时间戳；最后两行是不带时间戳的 `E2E_RESULT {json}` 和 `E2E_DONE exit=<码>`。只有构建结果符合 `--expect-build`、所有检查通过、清理全部成功（服务进程已退出、本代 Neo4j 节点和 Milvus 集合清空、临时库删除）时才返回 0。清理异常或无法确认无残留都返回非零。

推荐的后台跑法（每 60 秒 tail 一次，直到看到 `E2E_DONE`）：

```sh
LOG="$HOME/wt/sda/scratch/issue-47/e2e.log"
timeout 1800 nohup uv run --directory backend python tests/e2e/harness.py \
  --scratch "$HOME/wt/sda/scratch/issue-47" \
  --source-dir ../samples/demo --source canonical --source markdown \
  --neo4j-password-command "…" --log "$LOG" >/dev/null 2>&1 &
while ! grep -q '^E2E_DONE ' "$LOG" 2>/dev/null; do
  sleep 60
  tail -n 20 "$LOG"
done
grep -E '^E2E_(RESULT|DONE) ' "$LOG"
```

工具自身测试不访问网络，覆盖参数解析、检查加载、结果归一化、真实本地进程停止，以及“检查失败仍然执行清理”；守护测试与 lint 见上面的命令表。示例票专属检查在 `backend/tests/e2e/example_checks.py`。

## 构建和知识浏览

`source_directory` 指向项目仓库以外、包含规范数据模型文件的目录。`embedding_url` 是 OpenAI 兼容 embedding 服务的 base URL，应用向其 `/embeddings` 发送请求；应用配置的 `embedding_model` 默认 `model/bge-m3`（infinity 部署的模型名），本地日常集成测试则显式配置小模型。构建集合维度取自 embedding 响应，不在构建或检索代码里写死 1024；512 / 1024 两种维度都必须可用。embedding 客户端（`embedding_client.py`）在进程内共享一个并发上限，构建写入向量和 MCP 检索都要先取得槽位；每次请求的超时为 `embedding_timeout_seconds + embedding_timeout_per_input_seconds × 批内文本数`。默认并发 2、基础超时 60 秒、每条文本 20 秒、重试 2 次、退避基数 1 秒。只重试连接断开、超时和 5xx；4xx 及响应格式错误直接失败。构建记录在 `counts` 里写入 `embedding_requests`、`embedding_successful_batches`、`embedding_retries`、`embedding_elapsed_ms`。`knowledge_generation` 默认 `default`，只允许英文字母开头、后接字母数字下划线；构建和检索只操作这一代。管理员调用 `POST /api/v1/builds`，用 `GET /api/v1/builds` 或 `/api/v1/builds/{id}` 查看结果；冲突构建返回 409。已认证用户可从 `/api/v1/tables` 查询知识浏览数据，MCP streamable HTTP 地址为 `/mcp/`。当前代原地覆盖，知识代切换及旧代保留由后续工单实现。

## 源目录上传

管理员在源文件页上传文件、文件夹或 zip。目标目录相对于 `source_directory`；替换整个知识类时目标格式为 `<Markdown 适配器路径>/<知识类>`（默认 `knowledge/<知识类>`），未包含的文件会删除。zip 可以直接包含文档，也可以包含同名知识类顶层文件夹。单次上传最多 1000 个文件（zip 中包括目录条目），解压后总大小不超过 20 MiB；源文件浏览支持 UTF-8 文本，二进制文件可以下载。源目录必须在项目仓库以外；首次上传将已有源文件一并纳入首个提交。上传以令牌名称为 Git 作者，Git 无法表示的名称（首尾空白、尖括号或换行）需更换令牌名称。进程间上传使用目录锁串行化；仓库已有暂存内容时拒绝上传，避免把其他人的修改收入提交。

## 增量构建与来源

`POST /api/v1/builds` 不传请求体时保持原全量构建；传 `{"build_type":"incremental"}` 在当前知识代增量构建，返回记录包含构建类型、令牌名称和源目录 commit。源目录未处于干净 Git 提交时 commit 为 null。增量以同一知识代上一次成功构建的源文件哈希为基线；首次构建、源目录/启用插件/过滤规则/维度上限/embedding 配置变化或最近一次全量构建失败后需先做全量构建。文件统计区分新增、变化、删除；自动生成索引的派生哈希不计入源文件统计。上传和构建读取共享目录锁，构建在读取完来源快照后释放锁。

每个节点和向量记录来源路径及写入它的 build id。自动生成索引记录其派生来源列表；空目录不产生知识类节点。修改或删除源文件时删除其节点、相关关系和向量，再写新记录；字段、维度值、文档与表的关系以及依赖边根据当前记录重新合并。维度值去重和别名可能跨文件依赖同一张表的行：该表涉及的维度值作为一个受影响单元重算；其他表和文件不重写。失败的增量构建记录待修复来源和向量 id，下一次增量即使源文件已恢复也会重试清理。Neo4j 与 Milvus 之间不做事务；运行中仍可能看到部分更新，失败后需重试。
