"""Codex CLI adapter for the online agent protocol."""

import json
import os
import shutil
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
    is_sandboxed,
    mcp_headers,
    passthrough_environment,
    query_plan_event,
    read_marker,
    redact_event,
    request_secrets,
    result_summary,
    write_json,
)
from .mcp_context import CONVERSATION_HEADER, TURN_HEADER
from .skill import read_skill


@dataclass
class CodexEventParser(EventParser):
    thread_id: str | None = None

    def feed(self, record):
        kind = record.get("type")
        if kind == "thread.started":
            self.thread_id = record.get("thread_id")
        if kind in ("turn.failed", "error"):
            error = record.get("error") or record.get("message") or "Codex failed"
            self.error = (
                error.get("message", str(error))
                if isinstance(error, dict)
                else str(error)
            )
        item = record.get("item") or {}
        typ = item.get("type")
        if kind == "item.completed" and typ == "agent_message":
            return self.emit_text(item.get("text", ""), emit_empty=True)
        if typ in ("mcp_tool_call", "command_execution", "file_change"):
            ident = item.get("id")
            name = item.get("tool") or typ
            if kind == "item.started":
                args = item.get("arguments") or {"command": item.get("command")}
                return [self.start_tool(ident, name, args)]
            if kind == "item.completed":
                events = [
                    self.end_tool(
                        ident,
                        name,
                        is_error=item.get("status") == "failed"
                        or bool(item.get("error")),
                        summary=result_summary(
                            item.get("result")
                            or item.get("aggregated_output")
                            or item.get("error"),
                            name,
                        ),
                    )
                ]
                clarification = clarification_event(name, item.get("result"))
                if clarification is not None:
                    events.append(clarification)
                if item.get("status") != "failed" and not item.get("error"):
                    plan = query_plan_event(name, item.get("result"))
                    if plan is not None:
                        events.append(plan)
                return events
        return []


class CodexRunner:
    agent_id = "codex"
    effort_levels = ()

    def __init__(self, binary=None):
        self.binary = binary

    def _sandboxed(self):
        return is_sandboxed(self.binary)

    def available(self):
        return available_in_runtime("codex", self.binary)

    def write_config(self, request, model_url=None, mcp_url=None) -> Path:
        config = request.workspace / "sessions" / "codex"
        config.mkdir(parents=True, exist_ok=True)
        auth = (
            f'env_key = "{KEY_ENV}"\n'
            if request.provider.api_key
            else "requires_openai_auth = false\n"
        )
        headers = mcp_headers(request)
        cfg = (
            'model_provider = "dataagent"\n'
            + "developer_instructions = "
            + json.dumps(
                "Follow this retrieval skill exactly for warehouse questions.\n\n"
                + read_skill(),
                ensure_ascii=False,
            )
            + '\n[model_providers.dataagent]\nname = "dataagent"\nbase_url = '
            + json.dumps(model_url or request.provider.base_url)
            + '\nwire_api = "responses"\n'
            + auth
            + f"[mcp_servers.{MCP_SERVER}]\nurl = "
            + json.dumps(mcp_url or request.mcp_url)
            + f'\nbearer_token_env_var = "{TOKEN_ENV}"\n'
            + "http_headers = { "
            + json.dumps(CONVERSATION_HEADER)
            + " = "
            + json.dumps(headers[CONVERSATION_HEADER])
            + ", "
            + json.dumps(TURN_HEADER)
            + " = "
            + json.dumps(headers[TURN_HEADER])
            + " }\n"
        )
        (config / "config.toml").write_text(cfg, encoding="utf-8")
        os.chmod(config / "config.toml", 0o600)
        return config

    async def run(self, request):
        sandboxed = self._sandboxed()
        work = request.workspace / "work"
        work.mkdir(parents=True, exist_ok=True)
        turn = SandboxTurn(
            request,
            sandbox_class=default_sandbox_class() if sandboxed else None,
        )
        config = self.write_config(request, turn.model_url, turn.mcp_url)
        marker = config / "thread-id.json"
        thread = read_marker(marker, "thread_id") if marker.exists() else None
        env = passthrough_environment("PATH", "LANG", "LC_ALL", "SYSTEMROOT")
        env.update(
            {
                "HOME": "/tmp" if sandboxed else str(work),
                "CODEX_HOME": "/sessions/codex" if sandboxed else str(config),
                TOKEN_ENV: request.mcp_token,
            }
        )
        if request.provider.api_key:
            env[KEY_ENV] = request.provider.api_key
        command = [
            "codex" if sandboxed else self.binary or shutil.which("codex"),
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-m",
            request.model,
        ]
        if thread:
            command += ["resume", thread]
        command.append(request.prompt)
        async with turn:
            if sandboxed:
                command, exec_header = turn.wrap_command(
                    command,
                    env,
                    ("HOME", "CODEX_HOME", TOKEN_ENV, KEY_ENV),
                )
            else:
                exec_header = None
            async with agent_process(
                command,
                cwd=str(work),
                env=env,
                header=exec_header,
            ) as process:
                parser = CodexEventParser()
                secrets = request_secrets(request)
                async for line in process.lines():
                    try:
                        record = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    for event in parser.feed(record):
                        yield redact_event(event, secrets)
                    if parser.thread_id and not thread:
                        write_json(marker, {"thread_id": parser.thread_id})
                        thread = parser.thread_id
                code = await process.wait()
                await process.wait_for_stderr()
                if code and not parser.error:
                    parser.error = (
                        process.stderr_text()[-4000:]
                        or f"codex exited with status {code}"
                    )
                yield redact_event(parser.finish(), secrets)
