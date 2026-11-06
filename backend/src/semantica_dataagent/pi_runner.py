"""pi CLI adapter using RPC mode with a per-conversation config directory."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .agent_plugin import (
    KEY_ENV,
    MCP_SERVER,
    TOKEN_ENV,
    EventParser,
    SandboxTurn,
    agent_process,
    available_in_runtime,
    clarification_event,
    default_sandbox_class,
    error_event,
    extract_refs,
    extract_sql,
    is_sandboxed,
    mcp_headers,
    redact_event,
    request_secrets,
    result_summary,
    tool_end_event,
    tool_start_event,
    truncate,
    write_json,
)
from .agent_sandbox import SessionSandbox
from .agent_types import AgentEvent, AgentTurnRequest
from .skill import read_skill

PROVIDER_KEY = "dataagent"

__all__ = [
    "PiEventParser",
    "PiRunner",
    "SessionSandbox",
    "extract_refs",
    "extract_sql",
]


def _message_text(message) -> str:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


@dataclass
class PiEventParser(EventParser):
    """Pure translation of pi JSONL records into backend events."""

    settled: bool = False
    error_overrides_text: bool = False

    def feed(self, record) -> list[AgentEvent]:
        if not isinstance(record, dict):
            return []
        kind = record.get("type")
        if kind == "message_update":
            event = record.get("assistantMessageEvent") or {}
            event_type = event.get("type")
            if event_type == "text_delta":
                return self.emit_text(event.get("delta", ""))
            if event_type == "error":
                self.error = truncate(event.get("error") or "provider error")
            return []
        if kind == "message_end":
            message = record.get("message") or {}
            if message.get("role") == "assistant":
                text = _message_text(message)
                if text:
                    self.final_text = text
                    self.stop_reason = message.get("stopReason") or self.stop_reason
                    self.add_refs(text)
                if message.get("stopReason") == "error":
                    self.error = truncate(
                        message.get("errorMessage") or "provider error"
                    )
            return []
        if kind == "tool_execution_start":
            name = record.get("toolName") or "tool"
            return [
                tool_start_event(record.get("toolCallId"), name, record.get("args"))
            ]
        if kind == "tool_execution_end":
            name = record.get("toolName") or "tool"
            result = record.get("result")
            events = [
                tool_end_event(
                    record.get("toolCallId"),
                    name,
                    is_error=bool(record.get("isError")),
                    summary=result_summary(result),
                    duration_ms=record.get("durationMs"),
                )
            ]
            clarification = clarification_event(name, result)
            if clarification is not None:
                events.append(clarification)
            return events
        if kind == "auto_retry_end" and record.get("success") is False:
            self.error = truncate(record.get("finalError") or "agent retry failed")
            return []
        if kind == "extension_error":
            self.error = truncate(record.get("error") or "extension failed")
            return []
        if kind == "response":
            if record.get("command") == "prompt" and record.get("success") is False:
                self.error = truncate(record.get("error") or "prompt failed")
            return []
        if kind == "agent_settled":
            self.settled = True
            return []
        return []


class PiRunner:
    agent_id = "pi"

    def __init__(self, binary: str | None = None):
        self.binary = binary

    def _sandboxed(self) -> bool:
        return is_sandboxed(self.binary)

    def available(self) -> bool:
        return available_in_runtime("pi", self.binary)

    def _binary(self) -> str:
        import shutil

        binary = self.binary or shutil.which("pi")
        if not binary:
            raise FileNotFoundError("pi executable not found")
        return binary

    def write_config(
        self,
        request: AgentTurnRequest,
        model_url: str | None = None,
        mcp_url: str | None = None,
    ) -> tuple[Path, Path, str]:
        config = request.workspace / ("work/config" if self._sandboxed() else "config")
        sessions = request.workspace / "sessions"
        work = request.workspace / "work"
        for directory in (config, sessions, work):
            directory.mkdir(parents=True, exist_ok=True)
        provider = {
            "name": request.provider.name,
            "baseUrl": model_url or request.provider.base_url,
            "api": request.provider.type,
            "apiKey": f"${KEY_ENV}" if request.provider.api_key else "unused",
            "models": [
                {
                    "id": request.model,
                    "name": request.model,
                    "contextWindow": 128000,
                    "maxTokens": 8192,
                }
            ],
        }
        models = {"providers": {PROVIDER_KEY: provider}}
        mcp = {
            "mcpServers": {
                MCP_SERVER: {
                    "url": mcp_url or request.mcp_url,
                    "headers": mcp_headers(request),
                    "exposure": "direct",
                    "description": (
                        "Semantica DataAgent warehouse knowledge: search tables, "
                        "documents, lineage, dimension values and validate SQL"
                    ),
                }
            }
        }
        settings = {
            "compaction": {
                "enabled": True,
                "reserveTokens": 16384,
                "keepRecentTokens": 20000,
            },
            "enableInstallTelemetry": False,
            "enableAnalytics": False,
        }
        write_json(config / "models.json", models)
        write_json(config / "mcp.json", mcp)
        write_json(config / "settings.json", settings)
        skill_path = config / "skills" / "dataagent-retrieval" / "SKILL.md"
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(read_skill(), encoding="utf-8")
        os.chmod(skill_path, 0o600)
        return config, sessions, str(work)

    async def run(self, request: AgentTurnRequest):
        sandboxed = self._sandboxed()
        turn = SandboxTurn(
            request,
            sandbox_class=default_sandbox_class() if sandboxed else None,
        )
        config, sessions, work = self.write_config(
            request, turn.model_url, turn.mcp_url
        )
        env = self._environment(request, config, sessions)
        model = f"{PROVIDER_KEY}/{request.model}"
        skill = read_skill()
        command = [
            "pi" if sandboxed else self._binary(),
            "--mode",
            "rpc",
            "--session-id",
            request.session_id,
            "--session-dir",
            str(sessions),
            "--model",
            model,
            "--no-context-files",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--append-system-prompt",
            "Follow this retrieval skill exactly for warehouse questions.\n\n" + skill,
        ]
        async with turn:
            if sandboxed:
                command[command.index(str(sessions))] = "/sessions"
                env["PI_CODING_AGENT_DIR"] = "/workspace/config"
                env["PI_CODING_AGENT_SESSION_DIR"] = "/sessions"
                env["HOME"] = "/tmp"
                command, exec_header = turn.wrap_command(
                    command,
                    env,
                    (
                        "PI_CODING_AGENT_DIR",
                        "PI_CODING_AGENT_SESSION_DIR",
                        "PI_OFFLINE",
                        TOKEN_ENV,
                        KEY_ENV,
                        "HOME",
                    ),
                )
            else:
                exec_header = None
            async with agent_process(
                command,
                cwd=work,
                env=env,
                header=exec_header,
                stdin_open=True,
                start_new_session=True,
            ) as process:
                parser = PiEventParser()
                secrets = request_secrets(request)
                process.write(
                    (
                        json.dumps(
                            {"id": "turn", "type": "prompt", "message": request.prompt},
                            ensure_ascii=False,
                        )
                        + "\n"
                    ).encode()
                )
                await process.drain()
                while True:
                    line = await process.readline()
                    if not line:
                        break
                    try:
                        record = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    for event in parser.feed(record):
                        yield redact_event(event, secrets)
                    if parser.settled:
                        break
                if parser.settled:
                    yield redact_event(parser.finish(), secrets)
                    return
                returncode = await process.wait()
                if returncode != 0 or parser.error:
                    message = parser.error or (
                        self._sanitize(process.stderr_text(), request)
                        or f"pi exited with status {returncode}"
                    )
                    yield error_event(message)
                    return
                yield redact_event(parser.finish(), secrets)

    def _environment(
        self, request: AgentTurnRequest, config: Path, sessions: Path
    ) -> dict[str, str]:
        allowed = (
            "PATH",
            "HOME",
            "USER",
            "SHELL",
            "LANG",
            "LC_ALL",
            "TMPDIR",
            "SYSTEMROOT",
            "NO_PROXY",
            "no_proxy",
        )
        env = {key: os.environ[key] for key in allowed if key in os.environ}
        env.update(
            {
                "PI_CODING_AGENT_DIR": str(config),
                "PI_CODING_AGENT_SESSION_DIR": str(sessions),
                "PI_OFFLINE": "1",
                TOKEN_ENV: request.mcp_token,
            }
        )
        if request.provider.api_key:
            env[KEY_ENV] = request.provider.api_key
        return env

    def _sanitize(self, text: str, request: AgentTurnRequest) -> str:
        for secret in (request.mcp_token, request.provider.api_key):
            if secret:
                text = text.replace(secret, "[redacted]")
        return text[-4000:]
