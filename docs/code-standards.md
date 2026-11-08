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

## API 时间字段

所有 REST 响应里的时间字段一律是 ISO 8601、带显式 UTC 偏移（统一以 Pydantic 输出的 `Z` 结尾）。数据库里已有的无时区值按 UTC 解释，在序列化时补上时区，不能把无时区字符串直接返回给客户端；秒级精度不丢。

- 响应模型的时间字段使用 `semantica_dataagent.api_time.UtcDatetime`，不要直接写 `datetime` 或裸 `str`。
- 新增或修改返回时间的接口时，把该接口加进 `backend/tests/test_api_time.py` 的接口清单；该测试同时检查 OpenAPI 把时间字段声明为 `date-time`。
- 前端直接用服务端返回值构造 `Date`，不再为缺时区的值补 `Z`。

## 项目接口

项目是独立知识库实例的元数据；成员以令牌的 `id`（用户身份）关联项目，角色为 `admin` 或 `member`。系统管理员（全局角色 `admin`）隐含所有项目的管理员，不需要成员行。

- `GET /api/v1/projects` 需要认证。系统管理员返回全部项目，其它用户只返回自己是成员的项目；每项包含 `id`、`name`、`type`、`status`、`created_at`、`my_role`。系统管理员的 `my_role` 恒为 `admin`。
- `PATCH /api/v1/projects/{id}` 仅系统管理员可用，只接受 `name`；请求里出现 `type`、`status` 等其它字段返回 422。名称两端空白会被去掉，长度为 1–200。
- 默认项目 id 固定为 `default`，类型 `warehouse`，显示名默认“业务数据”，可改名但不可删除；删除与归档留给 #190。
- 新建普通用户令牌时自动写入默认项目的 `member` 成员行；管理员令牌不写成员行。`project_context` 的解析规则仍恒为默认项目（#175 再按项目解析），但名称、类型、状态来自 `projects` 表。

## 数据库迁移

- 迁移号接在合并时 `origin/master` 的最大迁移号之后；并行票可能先合并，提交前重新 `git fetch` 并核对，空库 `alembic heads` 只能有一个头。
- 迁移必须幂等：用 `INSERT ... WHERE NOT EXISTS` 或等价的“先查再插”处理种子数据，重复运行不能产生重复项目或成员。
- 升级即生效，不重建、不停服、不移动检索存储数据。`serve` / `migrate` 在版本不是 head 时先自动备份 SQLite，再执行迁移；已是 head 时不备份。
- 迁移测试用上一版本（合并时的 master）的数据库升级，逐行比较会话、令牌、纠错审批、构建记录、设置和知识代指针，并检查默认项目、成员回填、自动备份与空库单头。

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

设置 `DATAAGENT_DB_PATH`（SQLite 文件路径）、`DATAAGENT_ENCRYPTION_KEY`（Fernet 密钥，使用 `python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'` 生成）和可选的 `DATAAGENT_LISTEN`（默认 `127.0.0.1:8000`）。在线问答的 agent 工作目录由 `DATAAGENT_AGENT_ROOT` 指定（默认是数据库文件同目录下的 `agent-workspaces`）；MCP 地址由 `DATAAGENT_PUBLIC_URL` 指定（默认从监听地址推导）。`uv run --directory backend semantica-dataagent init` 建库并仅输出一次管理员 token，妥善保存；再运行 `uv run --directory backend semantica-dataagent serve`。前端开发使用 `pnpm --dir web dev`。管理员通过 `/api/v1/settings` 的 PUT 写入 `values`；键为 `neo4j_uri`、`neo4j_user`、`neo4j_password`、`milvus_uri`、`embedding_url`、`embedding_model`、`embedding_concurrency`、`embedding_timeout_seconds`、`embedding_timeout_per_input_seconds`、`embedding_retries`、`embedding_backoff_seconds`、`embedding_cache_max_mb`、`source_directory`、`knowledge_generation`、`generation_retention`、`schedule_interval_seconds`、`assistant_agent`、`assistant_provider_id`、`assistant_model`、`assistant_timeout_seconds`，空字符串或 null 删除。GET 不回显密码；连接地址不得包含用户信息、查询参数或片段，Neo4j 密码使用专用字段。配置修改对下一次健康检查生效；embedding 并发、超时、重试与退避参数在下一次构建开始时读取，检索请求则每次读取当前值。
## 真实使用

真实集群连接使用 [infrastructure.md](infrastructure.md) 的测试命名空间与凭据。健康检查只读；其他基础设施检查脚本可能创建临时集合。只在 `semantica-dataagent` 和 `semantica-dataagent-test` 命名空间操作；不得改动其他命名空间或集群级资源。

## 已拍板的决定

架构决策见 [ADR 目录](adr/)。尤其注意 [存储边界](adr/0002-neo4j-plus-milvus-without-cross-store-release.md) 和 [插件边界](adr/0006-pluggable-ingestion-for-open-source.md)。

## 存量失败基线

| 测试 | 失败摘要 | issue |
| --- | --- | --- |

## 从 LESSONS 升级的规则

<!-- 每条规则后面附 2–3 条证据（spec / PR / commit）。 -->

### 测试要经过真实入口、真实形态和真实量级

规则：

1. 测试要从真实入口（构建流程、MCP 客户端、REST、真实 runner）进入；只测内部函数会漏掉“没接上”的缺陷。Red check 必须真的能变红：写明撤掉什么、哪个测试失败、失败信息是什么。
2. 外部系统、runner、协议的测试夹具用**真实返回形态**（抓取或官方样例），不自己编；假服务只复现“我们以为的行为”。
3. 持久化的数据要做“写库 → 重读”的往返测试；通用的摘要 / 截断函数不得用于需要原样重放的数据。
4. 界面的 mock 数据要包含真实量级的极端值（长标题、长字段名、长文档、上百字的选项）；合并部署后在真实数据上过一遍。
5. 新增校验或限制时，用验收题集的参考 SQL 和知识库示例 SQL 跑一遍通过率，写进验收项。

证据：

- spec #1：#22 元数据修正覆盖层的单测在撤掉构建里的 `apply_overrides` 后仍通过；#20 PR #62 澄清工具 schema 单测没问题，但 pi 重建 schema 时丢了 `$defs`，网关 400，在线问答全部失败，只有真实 e2e 暴露。
- spec #73：#76 的 DTB 拉取器靠假 DTB 通过全部单测与 e2e，真实联调连续 5 轮才暴露差异（分页字段、token 失效的 HTTP 200 + body 503、`defaultLimit` 静默截断、字段接口不返回分区列，PR #94）；#75 的分区校验只接受字面量，14 道审定参考 SQL 里 13 道被执行器拒（PR #123）。
- 二期后续：#165 事件摘要用合成 payload 测通过，真实 pi 结果形态下线上 35 条 `validate_sql` 摘要全是 `status=unknown`；#148 通用摘要函数深度限制 4，把澄清选项的 `id/label/ref` 存成 `"…"`，直播正常、重读才暴露，三条真实会话的数据永久损坏（PR #148）；#140 mock 文档太短，真实长文档下抽屉正文可视区只有 318px（抽屉 874px）。

## 语言与框架规则

- commit、PR 标题与正文、交接文档一律用中文。类型前缀（`agent:`、`build:`、`mcp:`、`test:` 等）、代码标识符、命令和日志原文保持原样。例如 `agent: 在会话沙箱中运行 Claude 和 Codex (#19)`。

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

`--neo4j-password-command` 的 stdout 只进入进程内存（去掉末尾换行和可选的 `neo4j/` 前缀），不打印、不落盘。也可以改用环境变量 `NEO4J_PASSWORD` 或 `--neo4j-password-env <名称>`。默认存储是测试实例 `bolt://172.16.242.124:31611`、`http://172.16.242.124:31612` 和本机 embedding `http://127.0.0.1:17998`（`model/bge-small-zh-v1.5`）；需要 bge-m3 时传入 `--embedding-url http://127.0.0.1:17997 --embedding-model model/bge-m3`；工具拒绝连接开发实例的 31601 / 31602 / 31603。第二套测试存储 `bolt://172.16.242.124:31621`、`http://172.16.242.124:31622`（Secret 在 `semantica-dataagent-test2`）用 `--neo4j-uri`、`--milvus-uri` 和对应的 `--neo4j-password-command` 指定；并行测试按任务书各用一套。

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

`source_directory` 指向项目仓库以外、包含规范数据模型文件的目录。`embedding_url` 是 OpenAI 兼容 embedding 服务的 base URL，应用向其 `/embeddings` 发送请求；应用配置的 `embedding_model` 默认 `model/bge-m3`（infinity 部署的模型名），本地日常集成测试则显式配置小模型。构建集合维度取自 embedding 响应，不在构建或检索代码里写死 1024；512 / 1024 两种维度都必须可用。embedding 客户端（`embedding_client.py`）在进程内共享一个并发上限，构建写入向量和 MCP 检索都要先取得槽位；每次请求的超时为 `embedding_timeout_seconds + embedding_timeout_per_input_seconds × 批内文本数`。默认并发 2、基础超时 60 秒、每条文本 20 秒、重试 2 次、退避基数 1 秒。只重试连接断开、超时和 5xx；4xx 及响应格式错误直接失败。构建记录在 `counts` 里写入 `embedding_requests`、`embedding_successful_batches`、`embedding_retries`、`embedding_elapsed_ms`。`knowledge_generation` 默认 `default`，只允许英文字母开头、后接字母数字下划线；构建和检索只操作这一代。管理员调用 `POST /api/v1/builds`，请求以 `queued` 记录持久化并按提交顺序逐个执行；前一个结束后自动开始下一个，不再返回冲突。用 `GET /api/v1/builds` 或 `/api/v1/builds/{id}` 查看状态、逐阶段日志和统计；统计中的实体计数是本次构建实际写入或变更的实体数，所有时间字段为带时区的 UTC。进程重启时，运行中的构建标为 `failed`（中断），排队中的构建标为 `cancelled` 并记录原因。已认证用户可从 `/api/v1/tables` 查询知识浏览数据；列表默认 `view=full`，保持字段与文档明细，`view=summary` 只返回表名、库名、说明、`ref`、字段数、文档数和是否有分区字段。`GET /api/v1/tables/{database}/{table}` 返回单表完整信息，不存在时返回 404。MCP streamable HTTP 地址为 `/mcp/`。全量重建写入新知识代，成功后切换当前指针；默认保留两代，管理员可配置保留数和切换已保留的代。

## 源目录上传

管理员在源文件页上传文件、文件夹或 zip。目标目录相对于 `source_directory`；替换整个知识类时目标格式为 `<Markdown 适配器路径>/<知识类>`（默认 `knowledge/<知识类>`），未包含的文件会删除。zip 可以直接包含文档，也可以包含同名知识类顶层文件夹。单次上传最多 1000 个文件（zip 中包括目录条目），解压后总大小不超过 20 MiB；源文件浏览支持 UTF-8 文本，二进制文件可以下载。源目录必须在项目仓库以外；首次上传将已有源文件一并纳入首个提交。上传以令牌名称为 Git 作者，Git 无法表示的名称（首尾空白、尖括号或换行）需更换令牌名称。进程间上传使用目录锁串行化；仓库已有暂存内容时拒绝上传，避免把其他人的修改收入提交。

## 表检索与文档关联

表检索同时检索表和知识文档，将命中文档关联的表纳入融合排序；返回结果仍只包含表。构建关系时，文档正文中形如 `数据库.表名` 且与本代已知表匹配的名称会建立关联，字典文件名关联也保留。表检索优先返回名称精确命中或全文直接命中的表，其余表按融合分数排序；图关联权重为 1.5，排序版本为 3。返回的 `score` 是融合分，跨直接命中优先组时不保证递减，应按返回顺序使用结果。已有知识代需重新构建以建立正文关联；文档提及表不代表它一定适用于查询，应结合文档口径核验。

## Embedding 缓存

构建时按 `(embedding_model, 向量维度, SHA-256(文本))` 复用向量，缓存文件默认与 `DATAAGENT_DB_PATH` 同目录的 `embedding-cache.sqlite`（SQLite WAL），也可用 `DATAAGENT_EMBEDDING_CACHE_PATH` 指定路径。先去重再查缓存；一次 `add_vectors` 最多查询一次、写入一个事务。`embedding_cache_max_mb` 默认 500，设为 0 关闭。缓存损坏、目录只读或写入失败时降级为不使用缓存并记录告警，构建继续。换模型名或维度时使用新的缓存键，不会命中旧向量。

## 增量构建与来源

`POST /api/v1/builds` 不传请求体时保持原全量构建；传 `{"build_type":"incremental"}` 在当前知识代增量构建，返回记录包含构建类型、令牌名称和源目录 commit。源目录未处于干净 Git 提交时 commit 为 null。增量以同一知识代上一次成功构建的源文件哈希为基线；首次构建、源目录/启用插件/过滤规则/维度上限/embedding 配置变化或最近一次全量构建失败后需先做全量构建。文件统计区分新增、变化、删除；自动生成索引的派生哈希不计入源文件统计。上传和构建读取共享目录锁，构建在读取完来源快照后释放锁。定时增量构建使用 `schedule_interval_seconds`（0 或空值关闭，1–604800 秒启用），配置保存后立即重新计时；重启后按保存的间隔恢复，并与手动构建共用队列。

每个节点和向量记录来源路径及写入它的 build id。自动生成索引记录其派生来源列表；空目录不产生知识类节点。修改或删除源文件时删除其节点、相关关系和向量，再写新记录；字段、维度值、文档与表的关系以及依赖边根据当前记录重新合并。维度值去重和别名可能跨文件依赖同一张表的行：该表涉及的维度值作为一个受影响单元重算；其他表和文件不重写。失败的增量构建记录待修复来源和向量 id，下一次增量即使源文件已恢复也会重试清理。Neo4j 与 Milvus 之间不做事务；运行中仍可能看到部分更新：单文件删除至重写期间检索可能暂时缺失，也可能因存储可见性延迟返回旧内容；完成后读取更新后的内容。失败后需重试。

## 本地 MCP 委托问答

MCP 工具的参数 schema 必须自包含，不含任何层级的 `$ref` 或 `$defs`；部分客户端重建根 schema 时会丢掉定义，网关会拒绝悬空引用（见 #20）。

`ask_dataagent` 让本地 MCP 用户把完整问题交给在线 agent，并在完成后返回回答、SQL 和引用。默认 agent 是 `pi`，provider 和 model 默认取第一个已配置 provider 及其第一个模型；管理员可用 `assistant_agent`、`assistant_provider_id`、`assistant_model` 设置覆盖，也可用 `DATAAGENT_ASK_AGENT`、`DATAAGENT_ASK_PROVIDER_ID`、`DATAAGENT_ASK_MODEL` 提供部署默认值。`assistant_timeout_seconds`（默认 300，范围 0.1–3600）控制等待时间；超时返回 `unavailable` 和 `conversation_id`，调用者可用该 id 继续同一对话。对话记在调用者 token 名下。

## 在线 agent 容器

构建镜像：`docker build -t semantica-dataagent-agent-runner:1.2.0 deploy/agent-runner`。默认使用 Docker 沙箱；仅在可信的开发环境排障时设置 `DATAAGENT_AGENT_MODE=local`，此模式没有文件和网络隔离。容器的 CPU 和内存默认分别为 2 核和 2g，可用 `DATAAGENT_AGENT_CPUS`、`DATAAGENT_AGENT_MEMORY` 配置。

Docker Desktop / WSL 下，agent 只连接 internal 网络；同一网络中的专用中继仅转发到当前 provider 和 MCP 的两个目标地址。中继还连接 Docker bridge，并把配置中的宿主机 loopback 地址转换为 `host.docker.internal`；所以 WSL mirrored 网络中的 `127.0.0.1:3425` 和后端监听端口可由中继访问。两端 URL 的路径保留，HTTPS 保留原主机名以验证证书。不要把容器连接到普通 bridge 或挂载其他宿主机路径。

provider 的 base_url 与 api_key 每轮按会话当前设置重新下发给 runner，不写进持久化的会话记录；切换 provider 时旧 key 不会被新 provider 的轮次复用。pi 使用固定 session id 和会话目录延续历史，Claude 用会话文件判断 `--resume`，Codex 用持久化的 thread id 执行 `resume`；三种实现都允许在同一会话内切换 model / provider。

## Provider 模型能力

`GET /api/v1/providers` 的每个 provider 仍以 `models: list[str]` 表示模型列表，并新增 `model_capabilities` 对象，按模型名映射到 `{description, vision, effort_levels}`。`description` 是单行说明，最长 200 字；`vision` 表示模型是否接受图片；`effort_levels` 是思考强度档位的子集，固定取值 `low`、`medium`、`high`、`extra`、`max`（界面依次为低、中（推荐）、高、超高、最高），`medium` 是默认推荐档。响应会为 `models` 中的每个模型补一个能力对象；没填的模型返回默认值：说明为空、不支持图片、`effort_levels` 为空。空档位表示只有 agent 默认强度，不暴露档位选择。

`POST` / `PUT /api/v1/providers` 可带可选的 `model_capabilities`，缺省表示不修改。请求里的能力模型名必须出现在 `models` 中，档位必须在固定取值内，说明超过 200 字返回 422。`PUT` 省略能力但修改 `models` 时，已删除模型的能力一并清理，新增模型按默认值读取；显式提供能力时整份映射替换，未列出的模型回到默认值。数据库列 `providers.model_capabilities` 保存归一化后的 JSON（档位按固定顺序去重），迁移 0026 只加可空列，旧 provider 升级后仍按默认值读取。

管理员可在 provider 编辑页对已保存 provider 的某个模型发起探测：`POST /api/v1/providers/{id}/probe`，请求 `{model}`，返回 `{model, vision, effort: {low, medium, high, extra, max}, notes}`，每个结果取 `yes` / `no` / `unknown`。仅 `admin` 可调用（普通用户 403）；provider 不存在返回 404，模型不在该 provider 的 `models` 中返回 422。同一 provider 同时只允许一个探测，重复请求返回 409；探测总超时默认 60 秒，单请求默认 15 秒，可用 `DATAAGENT_PROVIDER_PROBE_TIMEOUT_SECONDS`、`DATAAGENT_PROVIDER_PROBE_REQUEST_TIMEOUT_SECONDS` 调整。探测不会写能力、会话、轮次或事件，也不会把 API key 放进响应、日志或探测请求体；API key 由现有解密函数读出后只放在对应 provider 的认证头里。

探测固定发送极小的无害输入：一张内联生成的 1×1 PNG 加一句文字用于图片探测，`max_tokens` / `max_completion_tokens` / `max_output_tokens` / `maxOutputTokens` 为 32 的短文本用于五档强度。2xx 且响应符合该 provider 的成功形态记 `yes`；400、413、415、422 记 `no`；5xx、超时、网络错误、无法解析或异常成功形态记 `unknown`，原因写进 `notes`。硬总时限由定时器关闭进行中的请求来保证，httpx 的单请求超时只作为第一道限制。各 provider 类型使用真实请求形态：`anthropic-messages` 请求 `/v1/messages`，图片用 base64 `image` block，强度用 `thinking: {type: "adaptive", display: "summarized"}` 与 `output_config.effort`；`openai-completions` 请求 `/chat/completions`，图片用 `image_url` data URL，强度用 `reasoning_effort`；`openai-responses` 请求 `/responses`，图片用 `input_image`，强度用 `reasoning: {effort, summary: "auto"}` 并带 `include: ["reasoning.encrypted_content"]`，成功响应必须包含 `output` 列表，只有 `status: "completed"` 或带 `error` 的 2xx 响应记 `unknown`；`google-generative-ai` 请求 `/models/{model}:generateContent`，图片用 `inlineData`，Gemini 3 等用 `thinkingLevel`，其余模型用 `thinkingBudget`。统一档位映射为 `low`→`low`、`medium`→`medium`、`high`→`high`、`extra`→`xhigh`、`max`→`max`；Google 的 `extra` / `max` 使用最高离散档位或更大预算。

探测结果只用于界面展示，不会自动写入能力。管理员在页面上点“应用探测结果”后，前端把 `vision=yes` 与 `effort=yes` 的档位填入该模型的复选框；管理员可继续修改，再用原来的 `PUT /api/v1/providers/{id}` 保存 `model_capabilities`。

## 观测数据边界

启用 Langfuse 时，用户问题、agent 回答及 SQL 是观测内容，可能包含用户自行输入的信息；界面必须告知用户问题和回答会上报。应用持有的 MCP token、provider API key、Langfuse 密钥、Neo4j 密码、认证与 Cookie 头和会话文件内容不得上报。上报字段采用集中允许列表，并遮盖已知秘密及 Bearer、sk-、password=、api_key=、token= 等凭据形式；不得把会话文件或请求头作为遥测载荷。

## 会话管理接口

`GET /api/v1/conversations` 默认只列出未归档会话；`?archived=true` 列出已归档会话。响应包含 `pinned`、`archived`，置顶会话按置顶时间倒序，其余按更新时间倒序。`PATCH /api/v1/conversations/{id}` 接收 `title`、`pinned`、`archived` 的可选字段；标题去除首尾空白后长度须为 1–200，归档运行中的会话返回 409。归档不会取消置顶，恢复后仍保持原置顶顺序。详情与删除接口同样按所有者授权，删除已归档会话与普通会话一致。

`POST /api/v1/conversations/ask` 带 `conversation_id` 时可以改用同一 agent 允许范围内的其他 provider / model；改 agent 仍返回 409，要求新建对话。provider 不在该 agent 的 `assistant_agent_profiles.providers` 允许列表时返回 422（列表为空或未配置表示不限制），model 不在 provider 的 `models` 中同样返回 422。切换后本轮及之后的轮次使用新值，会话行的 `provider_id` / `model` 更新为最近一次使用；每轮在 `turns.provider_id` / `turns.model` 记录自己使用的值，旧数据的空值按会话值展示。切换 provider 时按该轮 provider 重新下发凭据，旧 provider 的 key 不沿用。

## Kubernetes 会话沙箱

`DATAAGENT_AGENT_MODE=k8s` 使用独立会话 Pod；本地默认继续使用 Docker。Pod 规格、命名空间 RBAC 和 NetworkPolicy 在 `tests/test_k8s_sandbox_spec.py` 检查，真实双栈默认拒绝及允许目标还须在集群运行 `tests/e2e/checks/issue_26.py` 验证。会话出站实际由 init 容器的 iptables 限制；当前 flannel 不执行 NetworkPolicy。会话 Pod 在独立命名空间使用临时卷，轮次开始和结束时传递该对话的工作目录及会话文件；不得向 agent 容器传入 Kubernetes ServiceAccount token 或能力。运行时 Neo4j Secret 经 `DATAAGENT_NEO4J_AUTH` 注入时只用于匹配用户名的默认凭据，管理员显式保存的设置优先。

## 查询结果下载链接

查询所有者可通过 `POST /api/v1/queries/{id}/download-links` 获取 CSV、XLSX、JSON 的短期相对路径，浏览器以当前 origin 补全；MCP 的 `execute_query` 返回面向集群内调用方的绝对地址。链接是敏感凭据，持有人在 30 分钟内无需登录即可下载绑定结果，切勿公开或记录完整链接；过期、密钥轮换或结果清理后失效。部署时代理和网关的访问日志必须对 `/download-link/` 后的路径脱敏或禁用记录。
