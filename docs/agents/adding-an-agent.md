# 新增一种在线 agent

在线 agent 的 runner 是插件。REST 层只认统一协议，不认 pi、Claude、Codex
的具体 CLI。新增一种 agent 时，只实现“把它的输出翻译成统一事件”这一层，
沙箱、MCP 头部、进程生命周期、事件字段和密钥遮盖都用共享实现。

接口与共享工具在 `backend/src/semantica_dataagent/agent_plugin.py`，注册表在
`backend/src/semantica_dataagent/agent_runner.py`，入口点定义在
`backend/pyproject.toml`。

## 接口

一个 runner 是一个无参构造的类，必须满足：

```python
class MyAgentRunner:
    agent_id = "my-agent"          # 必须和 entry point 名称一致
    effort_levels = ()             # 本 agent 支持的思考强度；不支持就留空

    def available(self) -> bool: ...

    async def run(self, request: AgentTurnRequest):
        yield AgentEvent(...)      # 统一事件流
```

`AgentTurnRequest` 在 `agent_types.py`，包含对话 id、提示词、会话 id、工作目录、
provider、模型、MCP 地址、token、可选的 `effort` 和 `project_type`（默认
`warehouse`）。写沙箱检索 skill 时用 `read_skill(request.project_type)` 和
`skill_name(request.project_type)`，不要固定成数仓版。`effort` 是后端已按模型与
agent 能力校验过的统一档位（`low` / `medium` / `high` / `extra` / `max`），
runner 只需映射到自己的参数；不支持时返回空 `effort_levels`，收到值也应忽略。
内置三种 runner 的映射和已验证的 CLI 版本见
[`docs/code-standards.md` 的“会话管理接口”一节](../code-standards.md)：取值必须用
CLI 真实接受的值，映射不了的档位不要写进 `effort_levels`，由前端置灰。
`run` 必须以 `done` 或 `error` 结束；
`conversation_api` 和 `ask_api` 只消费这里产出的事件。

统一事件只有 `text_delta`、`tool_start`、`tool_end`、`clarification`、`done`、
`error` 六种。不要直接手写 `done` 的 sql / citations 字段，用
`agent_plugin.done_event()`；工具事件用 `tool_start_event()` /
`tool_end_event()`；澄清用 `clarification_event()`。这样所有 agent 的形状一致。

## 最小示例（只回显，不进入正式注册）

这个假 agent 放在测试里，只用于验证接口；不要把它加到 `pyproject.toml` 的正式
entry points。完整版本见 `backend/tests/test_agent_plugin.py`。

```python
from semantica_dataagent.agent_plugin import EventParser


class EchoRunner:
    agent_id = "echo"

    def available(self):
        return True

    async def run(self, request):
        parser = EventParser()
        for event in parser.emit_text(f"echo: {request.prompt}"):
            yield event
        yield parser.finish()
```

测试通过 `discover_runners([FakeEntryPoint("echo", EchoRunner)])` 注入一个假
entry point，不需要安装、也不需要改包元数据。

## 实现步骤

1. **决定 CLI 调用方式。** 参照 `pi_runner.py`、`claude_runner.py`、
   `codex_runner.py`。若需要每轮一个沙箱，创建 `SandboxTurn(request,
   sandbox_class=default_sandbox_class() if sandboxed else None)`；它负责注册、
   启动、失败回滚和 `close()`。不要自己调 `register_session` / `unregister_session`。
2. **写配置。** 沙箱模式下必须使用 `turn.model_url` / `turn.mcp_url`（由沙箱
   路由得出），不要写原始地址。MCP 的 URL、Authorization、对话头和轮次头统一用
   `mcp_server_config(request)` / `mcp_headers(request)`；token 只以
   `$DATAAGENT_TOKEN` 形式出现在配置里，不落盘。
3. **起进程。** 用 `async with agent_process(command, cwd=work, env=env,
   header=exec_header, ...) as process`。它负责 stdout 流、stderr 收集、超时
   终止和清理。需要向子进程写 JSON 行时传 `stdin_open=True`，用
   `process.write()` / `process.drain()`；k8s 模式把 `turn.wrap_command()`
   返回的 header 传给 `agent_process`。
4. **解析输出。** 每个 agent 一个 `EventParser` 子类，只写 `feed()`；文本累加、
   SQL / 引用提取、错误优先级和最终 `done` 事件由基类处理。pi 的
   `error_overrides_text=False` 是一个已存在的行为差异，新 agent 默认按
   Claude / Codex 的“错误优先”语义即可。
5. **遮盖密钥。** 每个 yield 出去的事件都用
   `redact_event(event, request_secrets(request))`。不要把 token 放进工具参数
   摘要或错误消息。
6. **恢复会话。** 三种已实现的方式可参考：pi 使用固定 session id；
   Claude 用 `has_session_file()` 判断是否 `--resume`；Codex 用
   `read_marker()` / `write_json()` 保存 thread id。选择 CLI 自己支持的机制，
   不要自己发明跨轮格式。
7. **注册入口点。** 在 `backend/pyproject.toml` 增加：

   ```toml
   [project.entry-points."semantica_dataagent.agent_runners"]
   my-agent = "semantica_dataagent.my_agent_runner:MyAgentRunner"
   ```

   entry point 名称必须等于类的 `agent_id`。安装或重装后才能被发现：

   ```sh
   uv sync --directory backend --locked --reinstall-package semantica-dataagent
   uv run --directory backend python -c \
     "from semantica_dataagent.agent_runner import list_agents; print(list_agents())"
   ```

8. **测试。** 至少覆盖：事件解析 fixture、配置中不出现明文密钥、MCP 头部包含
   conversation / turn、沙箱失败会清理、两层问答能恢复会话。端到端检查按
   `docs/code-standards.md` 的“端到端测试工具”一节写一个
   `backend/tests/e2e/checks/issue_<票号>.py`，经 REST 观察两轮问答和工具调用。

## 不要做的事

- 不要在 `agent_runner.py` 里加硬编码的 runner 字典；发现只走 entry points。
- 不要复制沙箱启动 / 清理、MCP 头部、进程 drain、`done` 事件构造或密钥遮盖逻辑。
- 不要把某个 CLI 的原始事件直接透传给 SSE；先翻译成统一协议。
- 不要为测试 agent 增加正式 entry point。测试用可注入的 entry point 对象。
- 不要打印或持久化 MCP token、provider key；错误消息和工具摘要同样要遮盖。
