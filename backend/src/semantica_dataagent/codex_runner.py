"""Codex CLI adapter for the online agent protocol."""

import asyncio
import contextlib
import json
import os
import shutil
from collections import deque
from dataclasses import dataclass, field

from .agent_sandbox import SessionSandbox, register_session, unregister_session
from .agent_types import DONE, ERROR, TEXT_DELTA, TOOL_END, TOOL_START, AgentEvent
from .pi_runner import (
    STREAM_LIMIT,
    _drain,
    _redact_event,
    _result_summary,
    _stop,
    _tool_args_summary,
    extract_refs,
    extract_sql,
)
from .skill import read_skill


@dataclass
class CodexEventParser:
    text_parts: list[str] = field(default_factory=list)
    error: str | None = None
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
            text = item.get("text", "")
            self.text_parts.append(text)
            return [AgentEvent(TEXT_DELTA, {"text": text})]
        if typ in ("mcp_tool_call", "command_execution", "file_change"):
            ident = item.get("id")
            name = item.get("tool") or typ
            if kind == "item.started":
                args = item.get("arguments") or {"command": item.get("command")}
                return [
                    AgentEvent(
                        TOOL_START,
                        {
                            "id": ident,
                            "name": name,
                            "args": args,
                            "summary": _tool_args_summary(name, args),
                        },
                    )
                ]
            if kind == "item.completed":
                return [
                    AgentEvent(
                        TOOL_END,
                        {
                            "id": ident,
                            "name": name,
                            "is_error": item.get("status") == "failed"
                            or bool(item.get("error")),
                            "summary": _result_summary(
                                item.get("result")
                                or item.get("aggregated_output")
                                or item.get("error")
                            ),
                        },
                    )
                ]
        return []

    def finish(self):
        if self.error:
            return AgentEvent(ERROR, {"message": self.error})
        text = "".join(self.text_parts)
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


class CodexRunner:
    agent_id = "codex"

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
            else self.binary or shutil.which("codex")
        )

    async def run(self, request):
        sandboxed = self._sandboxed()
        work = request.workspace / "work"
        config = request.workspace / "sessions" / "codex"
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
        auth = (
            'env_key = "DATAAGENT_PROVIDER_KEY"\n'
            if request.provider.api_key
            else "requires_openai_auth = false\n"
        )
        cfg = (
            'model_provider = "dataagent"\n'
            + "developer_instructions = "
            + json.dumps(
                "Follow this retrieval skill exactly for warehouse questions.\n\n"
                + read_skill(),
                ensure_ascii=False,
            )
            + '\n[model_providers.dataagent]\nname = "dataagent"\nbase_url = '
            + json.dumps(model_url)
            + '\nwire_api = "responses"\n'
            + auth
            + "[mcp_servers.dataagent]\nurl = "
            + json.dumps(mcp_url)
            + '\nbearer_token_env_var = "DATAAGENT_TOKEN"\n'
        )
        (config / "config.toml").write_text(cfg, encoding="utf-8")
        os.chmod(config / "config.toml", 0o600)
        marker = config / "thread-id"
        thread = marker.read_text().strip() if marker.exists() else None
        env = {
            k: os.environ[k]
            for k in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT")
            if k in os.environ
        }
        env.update(
            {
                "HOME": "/tmp" if sandboxed else str(work),
                "CODEX_HOME": "/sessions/codex" if sandboxed else str(config),
                "DATAAGENT_TOKEN": request.mcp_token,
            }
        )
        if request.provider.api_key:
            env["DATAAGENT_PROVIDER_KEY"] = request.provider.api_key
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
        if sandboxed:
            docker = ["docker", "exec", "-i"]
            for key in (
                "HOME",
                "CODEX_HOME",
                "DATAAGENT_TOKEN",
                "DATAAGENT_PROVIDER_KEY",
            ):
                if key in env:
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
            parser = CodexEventParser()
            secrets = (request.mcp_token, request.provider.api_key or "")
            async for line in process.stdout:
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                for event in parser.feed(record):
                    yield _redact_event(event, secrets)
                if parser.thread_id and not thread:
                    marker.write_text(parser.thread_id, encoding="utf-8")
                    thread = parser.thread_id
            code = await process.wait()
            await drain
            if code and not parser.error:
                parser.error = (
                    "\n".join(stderr)[-4000:] or f"codex exited with status {code}"
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
