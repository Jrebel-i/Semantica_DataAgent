"""Claude Code adapter for the online agent protocol."""

import asyncio
import contextlib
import json
import os
import shutil
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .agent_sandbox import SessionSandbox, register_session, unregister_session
from .agent_types import DONE, ERROR, TEXT_DELTA, TOOL_END, TOOL_START, AgentEvent
from .pi_runner import (
    STREAM_LIMIT,
    _drain,
    _redact_event,
    _result_summary,
    _stop,
    _tool_args_summary,
    _write_json,
    extract_refs,
    extract_sql,
)
from .skill import read_skill


@dataclass
class ClaudeEventParser:
    text_parts: list[str] = field(default_factory=list)
    final_text: str = ""
    error: str | None = None
    tools: dict = field(default_factory=dict)

    def feed(self, record):
        events = []
        kind = record.get("type")
        if kind == "assistant":
            for block in record.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    text = block.get("text", "")
                    self.text_parts.append(text)
                    events.append(AgentEvent(TEXT_DELTA, {"text": text}))
                elif block.get("type") == "tool_use":
                    ident, name = block.get("id"), block.get("name", "tool")
                    args = block.get("input")
                    self.tools[ident] = name
                    events.append(
                        AgentEvent(
                            TOOL_START,
                            {
                                "id": ident,
                                "name": name,
                                "args": args,
                                "summary": _tool_args_summary(name, args),
                            },
                        )
                    )
        elif kind == "user":
            for block in record.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    ident = block.get("tool_use_id")
                    events.append(
                        AgentEvent(
                            TOOL_END,
                            {
                                "id": ident,
                                "name": self.tools.get(ident, "tool"),
                                "is_error": bool(block.get("is_error")),
                                "summary": _result_summary(block.get("content")),
                            },
                        )
                    )
        elif kind == "result":
            if record.get("is_error"):
                self.error = record.get("result") or "Claude failed"
            else:
                self.final_text = record.get("result") or "".join(self.text_parts)
        return events

    def finish(self):
        if self.error:
            return AgentEvent(ERROR, {"message": self.error})
        text = self.final_text or "".join(self.text_parts)
        return AgentEvent(
            DONE,
            {
                "text": text,
                "stop_reason": None,
                "sql": extract_sql(text),
                "citations": [
                    {"ref": r, "type": r.split(":", 1)[0]} for r in extract_refs(text)
                ],
            },
        )


class ClaudeRunner:
    agent_id = "claude"

    def __init__(self, binary=None):
        self.binary = binary

    def _sandboxed(self):
        return (
            not self.binary
            and os.environ.get("DATAAGENT_AGENT_MODE", "sandbox") != "local"
        )

    def available(self):
        return bool(
            shutil.which("docker")
            if self._sandboxed()
            else self.binary or shutil.which("claude")
        )

    async def run(self, request):
        sandboxed = self._sandboxed()
        work = request.workspace / "work"
        config = request.workspace / "sessions" / "claude"
        work.mkdir(parents=True, exist_ok=True)
        config.mkdir(parents=True, exist_ok=True)
        sandbox = None
        model_url, mcp_url = request.provider.base_url, request.mcp_url
        if sandboxed:
            sandbox = SessionSandbox(request.workspace, model_url, mcp_url)
            register_session(request.conversation_id, sandbox)
            try:
                await asyncio.to_thread(sandbox.start)
                model_url, mcp_url = sandbox.model_url, sandbox.mcp_url
            except BaseException:
                unregister_session(request.conversation_id, sandbox)
                await asyncio.to_thread(sandbox.close)
                raise
        mcp = config / "mcp.json"
        _write_json(
            mcp,
            {
                "mcpServers": {
                    "dataagent": {
                        "type": "http",
                        "url": mcp_url,
                        "headers": {"Authorization": "Bearer ${DATAAGENT_TOKEN}"},
                    }
                }
            },
        )
        session = str(uuid.UUID(request.session_id))
        existing = any(config.glob(f"projects/*/{session}.jsonl"))
        env = {
            k: os.environ[k]
            for k in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT")
            if k in os.environ
        }
        env.update(
            {
                "HOME": "/tmp" if sandboxed else str(work),
                "CLAUDE_CONFIG_DIR": "/sessions/claude" if sandboxed else str(config),
                "ANTHROPIC_BASE_URL": model_url,
                "ANTHROPIC_API_KEY": request.provider.api_key or "unused",
                "DATAAGENT_TOKEN": request.mcp_token,
            }
        )
        command = [
            "claude" if sandboxed else self.binary or shutil.which("claude"),
            "-p",
            "--bare",
            "--verbose",
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
            request.prompt,
        ]
        if sandboxed:
            docker = ["docker", "exec", "-i"]
            for key in (
                "HOME",
                "CLAUDE_CONFIG_DIR",
                "ANTHROPIC_BASE_URL",
                "ANTHROPIC_API_KEY",
                "DATAAGENT_TOKEN",
            ):
                docker.extend(["--env", key])
            command = docker + [sandbox.name, *command]
        process = None
        drain = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(work),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=STREAM_LIMIT,
            )
            stderr = deque(maxlen=80)
            drain = asyncio.create_task(_drain(process.stderr, stderr))
            parser = ClaudeEventParser()
            secrets = (request.mcp_token, request.provider.api_key or "")
            async for line in process.stdout:
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                for event in parser.feed(record):
                    yield _redact_event(event, secrets)
            code = await process.wait()
            if code and not parser.error:
                parser.error = (
                    "\n".join(stderr)[-4000:] or f"claude exited with status {code}"
                )
            yield _redact_event(parser.finish(), secrets)
        finally:
            if process is not None:
                await _stop(process)
            if drain:
                drain.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await drain
            if sandbox is not None:
                unregister_session(request.conversation_id, sandbox)
                await asyncio.to_thread(sandbox.close)
