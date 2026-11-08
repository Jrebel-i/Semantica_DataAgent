"""Claude Code adapter for the online agent protocol."""

import json
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .agent_plugin import (
    MCP_SERVER,
    TOKEN_ENV,
    EventParser,
    SandboxTurn,
    agent_process,
    available_in_runtime,
    clarification_event,
    default_sandbox_class,
    has_session_file,
    is_sandboxed,
    mcp_server_config,
    passthrough_environment,
    query_plan_event,
    redact_event,
    request_secrets,
    result_summary,
    write_json,
)
from .skill import read_skill

EFFORT_BY_LEVEL = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "extra": "xhigh",
    "max": "max",
}


@dataclass
class ClaudeEventParser(EventParser):
    streamed: bool = False
    tools: dict = field(default_factory=dict)

    def feed(self, record):
        events = []
        kind = record.get("type")
        if kind == "stream_event":
            event = record.get("event") or {}
            if event.get("type") == "message_start":
                self.streamed = False
            delta = event.get("delta") or {}
            if (
                event.get("type") == "content_block_delta"
                and delta.get("type") == "text_delta"
            ):
                self.streamed = True
                return self.emit_text(delta.get("text", ""), emit_empty=True)
        if kind == "assistant":
            for block in record.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    text = block.get("text", "")
                    if not self.streamed:
                        events.extend(self.emit_text(text, emit_empty=True))
                elif block.get("type") == "tool_use":
                    ident, name = block.get("id"), block.get("name", "tool")
                    args = block.get("input")
                    self.tools[ident] = name
                    events.append(self.start_tool(ident, name, args))
        elif kind == "user":
            for block in record.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    ident = block.get("tool_use_id")
                    result = block.get("content")
                    if isinstance(result, list):
                        result = {"content": result}
                    name = self.tools.get(ident, "tool")
                    events.append(
                        self.end_tool(
                            ident,
                            name,
                            is_error=bool(block.get("is_error")),
                            summary=result_summary(result, name),
                        )
                    )
                    clarification = clarification_event(name, result)
                    if clarification is not None:
                        events.append(clarification)
                    if not block.get("is_error"):
                        plan = query_plan_event(name, result)
                        if plan is not None:
                            events.append(plan)
        elif kind == "result":
            if record.get("is_error"):
                self.error = record.get("result") or "Claude failed"
            else:
                self.final_text = record.get("result") or "".join(self.text_parts)
        return events


class ClaudeRunner:
    agent_id = "claude"
    effort_levels = tuple(EFFORT_BY_LEVEL)

    def __init__(self, binary=None):
        self.binary = binary

    def _sandboxed(self):
        return is_sandboxed(self.binary)

    def available(self):
        return available_in_runtime("claude", self.binary)

    def write_config(self, request, mcp_url=None) -> Path:
        config = request.workspace / "sessions" / "claude"
        config.mkdir(parents=True, exist_ok=True)
        server = {"type": "http", **mcp_server_config(request)}
        server["url"] = mcp_url or request.mcp_url
        write_json(config / "mcp.json", {"mcpServers": {MCP_SERVER: server}})
        return config

    async def run(self, request):
        sandboxed = self._sandboxed()
        work = request.workspace / "work"
        work.mkdir(parents=True, exist_ok=True)
        turn = SandboxTurn(
            request,
            sandbox_class=default_sandbox_class() if sandboxed else None,
        )
        config = self.write_config(request, turn.mcp_url)
        mcp = config / "mcp.json"
        session = str(uuid.UUID(request.session_id))
        existing = has_session_file(config, session)
        env = passthrough_environment("PATH", "LANG", "LC_ALL", "SYSTEMROOT")
        env.update(
            {
                "HOME": "/tmp" if sandboxed else str(work),
                "CLAUDE_CONFIG_DIR": "/sessions/claude" if sandboxed else str(config),
                "ANTHROPIC_BASE_URL": turn.model_url,
                "ANTHROPIC_API_KEY": request.provider.api_key or "unused",
                TOKEN_ENV: request.mcp_token,
            }
        )
        command = [
            "claude" if sandboxed else self.binary or shutil.which("claude"),
            "-p",
            "--bare",
            "--verbose",
            "--include-partial-messages",
            "--output-format",
            "stream-json",
            "--model",
            request.model,
            "--dangerously-skip-permissions",
            "--strict-mcp-config",
            "--mcp-config",
            "/sessions/claude/mcp.json" if sandboxed else str(mcp),
            "--append-system-prompt",
            "Follow this retrieval skill exactly for warehouse questions.\n\n"
            + read_skill(),
            "--resume" if existing else "--session-id",
            session,
        ]
        effort = EFFORT_BY_LEVEL.get(request.effort or "")
        if effort:
            command += ["--effort", effort]
        command.append(request.prompt)
        async with turn:
            if sandboxed:
                command, exec_header = turn.wrap_command(
                    command,
                    env,
                    (
                        "HOME",
                        "CLAUDE_CONFIG_DIR",
                        "ANTHROPIC_BASE_URL",
                        "ANTHROPIC_API_KEY",
                        TOKEN_ENV,
                    ),
                )
            else:
                exec_header = None
            async with agent_process(
                command,
                cwd=str(work),
                env=env,
                header=exec_header,
            ) as process:
                parser = ClaudeEventParser()
                secrets = request_secrets(request)
                async for line in process.lines():
                    try:
                        record = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    for event in parser.feed(record):
                        yield redact_event(event, secrets)
                code = await process.wait()
                await process.wait_for_stderr()
                if code and not parser.error:
                    parser.error = (
                        process.stderr_text()[-4000:]
                        or f"claude exited with status {code}"
                    )
                yield redact_event(parser.finish(), secrets)
