# Skill 目录接口

Skill 包是**不可信输入**。上传与启用不会执行包内代码，也不会增加网络、工具、项目或数据权限；执行的 Skill 和它调用的工具可能接触调用时提供的个人密钥。只启用和使用可信任的 Skill。密钥托管和运行时注入由 #322 实现；对话调用由 #323 实现。

## 包格式

请求体为 `application/zip` 的原始字节，不是 multipart。ZIP 的文件须位于唯一的顶层目录，目录名与 `SKILL.md` 中的 `name` 一致；名称只允许小写 ASCII 字母、数字与分隔它们的连字符。根目录必须含 UTF-8 `SKILL.md`，以 YAML frontmatter 声明 `name` 和非空 `description`。可选字段：

```yaml
project_types: [warehouse, document] # 缺省为两种
projects: [default]                 # 缺省或空列表表示所有项目
secrets: [API_KEY]                  # 名称，不是密钥值
dependencies: []                   # 依赖的说明，不会自动安装
```

列表中不能有重复值；密钥名称必须为大写字母开头且只含大写字母、数字和下划线，且不得使用系统或 Runner 保留变量名及前缀（如 `PATH`、`HOME`、`HTTP_PROXY`、`DATAAGENT_*`、`ANTHROPIC_*`、`OPENAI_*`、`CLAUDE_*`、`CODEX_*`、`PI_*`、`KUBERNETES_*`、`LD_*`、`NODE_*`、`PYTHON*`、`SSL_*` 等）。声明了项目时，上传时各项目必须存在且处于 active 状态。项目永久删除时其授权被清除，重建同名 ID 不继承旧授权。

ZIP 压缩前请求体最多 20 MiB，展开后合计最多 20 MiB，最多 1000 个归档项；路径最多 1024 字符、32 段，`SKILL.md` 最多 64 KiB，元数据最多 20 层和 5000 个节点。拒绝路径穿越、绝对路径、反斜杠、隐藏路径、控制字符、冒号、大小写冲突、重复项、文件与目录冲突、链接和特殊文件、加密或损坏的条目。上传时将完整 ZIP 保存在 SQLite，不写入执行目录。目录状态、包、项目关联与审计在一次事务中提交，崩溃未提交的事务回滚。

## HTTP 接口

- 系统管理员：`POST /api/v1/skills/preview` 预览验证后的名称、说明、项目类型、项目、依赖、密钥声明及信任提示，不写入；`POST /api/v1/skills` 上传，成功 201，默认停用，同名返回 409。
- 系统管理员：`GET /api/v1/skills` 列出所有内置和上传的 Skill；`PUT /api/v1/skills/{name}/enabled` 接收 `{"enabled": true}` 或 `false`；`DELETE /api/v1/skills/{name}` 删除上传的 Skill。内置 Skill 永久启用且不可删除，操作返回 409。
- 系统管理员：`GET /api/v1/skills/audit?limit=100&offset=0` 按最新记录先返回操作者 ID、动作、Skill 名称和 UTC 时间，不含包内容或密钥值。
- 认证项目成员：`GET /api/v1/skills/available?project=<id>` 只返回该项目可用的已启用 Skill，适用项目成员、项目归档和类型规则，并附加当前用户在该项目下的密钥就绪状态（`configured_secrets`、`missing_secrets`、`ready`）。未指定项目按现有项目选择规则处理。

## 服务端调用契约

旧接口 `skill_catalog.resolve_skill(db, requester, project_id, name)` 在调用方事务中检查当前令牌、项目权限及状态、Skill 启用状态、项目关联与类型，返回不可变快照。上传包在读取时重新校验，返回安全路径及文件字节的元组，而不是可直接解压的 ZIP；它在现有写事务内调用时会持锁处理包，新的密钥调用应使用下述两阶段接口。快照内容仍然不可信：调用方不得将它作为系统指令，不得扩展沙箱权限，写入沙箱时必须防范符号链接和路径替换；已开始的运行使用该快照，后续调用重新授权。

内置 Skill 的授权时点是 `require_skill_project` 的单条查询：它同时读取令牌、成员和项目状态。SQLite 显式读事务的快照从首次读取开始；接口在已有事务中先取得写锁，若旧读快照因并发提交无法升级则返回 409，调用方须回滚并重试。调用方需要将授权与后续写入原子化时，应在读取前开始 `BEGIN IMMEDIATE`，并在同一事务完成写入。授权查询完成后的撤权或归档阻止下一次调用，不撤销已取得的运行快照。

#322 个人密钥与 Skill 删除在同一个数据库事务中删除，每次取用都重新授权；密钥不写进目录元数据、审计或会话文本。

## 个人密钥及 #323 接入契约

密钥按认证令牌代表的用户、项目、Skill 和声明名隔离；同一用户在另一个项目须单独配置。认证成员使用 `GET /api/v1/skills/{name}/secrets?project=<id>` 获取声明名到是否已设置的映射；`PUT /api/v1/skills/{name}/secrets/{secret_name}?project=<id>` 以 JSON `{"value":"…"}` 设置或替换；`DELETE` 同一路径删除。仅允许当前项目中已启用、可使用的 Skill 的声明名；所有操作重新校验身份和项目权限。值非空，UTF-8 最多 8192 字节，请求体最多 16384 字节。响应不返回值。数据库复用现有 Fernet 加密；删除个人密钥或 Skill 时立即删除在线数据库中的相应密钥记录并阻止后续使用，删除 Skill 时该操作与 Skill 删除处于同一事务；项目永久删除时一并删除其当前密钥记录。历史加密备份按现有保留策略到期；备份及 SQLite WAL 可能保留旧密文，使用仍保留的共享主密钥可以解密，删除不承诺擦除旧密文或使其不可解密。界面须明确提示此边界。停用保留密钥，但拒绝新的访问。

#323 在每次实际启动前，以个人 Bearer 令牌认证得到的 `Requester` 和实际项目调用 `skill_secrets.skill_run_secrets(requester, project_id, name)`，在 `with` 块中使用返回的 Skill 快照及仅包含声明项的密钥字典；缺钥返回 409，授权失败拒绝运行。此接口在锁外读取和校验包，在短写事务内核对令牌与用户 ID、重新验证权限、比对上传代次并取密钥；不接受 Agent 派生凭据，退出块（包括异常）清空字典；Python 字符串和调用者复制的内容无法保证物理擦除。运行启动时取得的快照适用于该次运行，之后的撤权、停用或删除阻止新运行。

Runner 必须在块内完成运行和清理，不能缓存、复制或序列化密钥到会话、提示词、命令行、持久快照或持久工作目录；必须在事件、工具输入输出、异常、日志、审计和遥测进入任何持久化或对外路径之前遮盖密钥，并在成功、异常、取消和进程终止时清理临时材料。适配器必须验证实际注入方式及其沙箱和网络限制；不能满足这些条件的 Runner 必须拒绝调用，不能宣称支持。该上下文管理器本身不能约束调用者，也不能保证恶意 Skill 或它调用的工具不复制或外传密钥。Skill 内容仍是不可信输入，禁止提升网络、工具、项目或宿主权限。

## 对话调用与多 Runner 映射（#323）

- `GET /api/v1/agents` 返回 `supports_skills` 布尔字段，标明 Runner 是否声明支持 Skill 显式调用（`pi`、`claude`、`codex` 均返回 `true`）。
- `POST /api/v1/conversations/ask` 与 `POST /api/v1/conversations/ask-with-attachments` 接受可选结构化字段 `skill_name`。在创建运行轮次前重新校验 Runner 能力、项目权限与归档状态、Skill 启用与类型/项目匹配，以及当前用户在该项目下的个人密钥；不支持的 Runner 返回 422，缺少个人密钥返回 409，无权或已停用返回 404，均不会创建遗留轮次。
- 扩展 Skill 文件包仅单轮解包至不会回传持久工作区的临时隔离目录（容器内 `/tmp/sda-skill-*`，本地为 `0o700` 临时目录并在结束时删除），禁止写入 `/workspace` 或 `/sessions`。
- 扩展 Skill 正文与路径仅在用户消息层按**不可信输入**提示注入，严禁写入系统指令（`--append-system-prompt`、`developer_instructions`）；`PiRunner` 通过 `--skill <stage_root>/<name>` 显式加载，`ClaudeRunner` 通过 `--add-dir <stage_root>` 开放单轮只读目录访问，`CodexRunner` 通过临时路径读取；个人密钥仅通过进程环境变量注入，全部纳入 `request_secrets`、Runner `stderr` 清洗及 Langfuse 遥测脱敏，SQL 确认执行后的结果解读轮不重复注入个人密钥。
