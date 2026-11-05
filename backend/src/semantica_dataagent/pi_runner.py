"""pi CLI adapter using RPC mode with a per-conversation config directory."""

import asyncio
import contextlib
import json
import os
import re
import shutil
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

from .agent_sandbox import SessionSandbox, register_session, unregister_session
from .agent_types import (
    CLARIFICATION,
    DONE,
    ERROR,
    TEXT_DELTA,
    TOOL_END,
    TOOL_START,
    AgentEvent,
    AgentTurnRequest,
)
from .k8s_sandbox import KubernetesSessionSandbox
from .mcp_context import CONVERSATION_HEADER, TURN_HEADER
from .skill import read_skill

PROVIDER_KEY = "dataagent"
TOKEN_ENV = "DATAAGENT_TOKEN"
KEY_ENV = "DATAAGENT_PROVIDER_KEY"
MCP_SERVER = "dataagent"
# pi emits one JSON record per line; message_end can contain the whole
# conversation and tool results, well past asyncio.StreamReader's 64 KiB default.
STREAM_LIMIT = 64 * 1024 * 1024
_REF_RE = re.compile(
    r"(?:document|table|field|task|dimension_value|lineage|category):"
    r"[^\s\"'<>()\[\]{}`]+"
)
_FENCE_RE = re.compile(r"```([A-Za-z0-9_+.-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)
_SQL_LANGS = {"sql", "hive", "hiveql", "spark", "sparksql", "doris", "mysql"}


def _truncate(value, limit: int = 400) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tool_args_summary(name: str, args) -> str:
    if not isinstance(args, dict):
        return _truncate(args, 200)
    short = name.split("__")[-1]
    if short == "search_knowledge":
        return _truncate(
            f"query={args.get('query', '')} scope={args.get('scope', '')}", 200
        )
    if short == "describe_tables":
        return _truncate(f"tables={args.get('names', [])}", 200)
    if short == "validate_sql":
        return _truncate(f"dialect={args.get('dialect', '')}", 200)
    if short == "read_document":
        return _truncate(f"doc_id={args.get('doc_id', '')}", 200)
    if short == "read_task_sql":
        return _truncate(f"task_id={args.get('task_id', '')}", 200)
    if short == "get_lineage":
        return _truncate(
            f"table={args.get('table', '')} direction={args.get('direction', '')}", 200
        )
    if short == "lookup_dim_values":
        return _truncate(
            f"table={args.get('table', '')} field={args.get('field') or ''} "
            f"keyword={args.get('keyword', '')}",
            200,
        )
    if short == "find_join_path":
        return _truncate(f"tables={args.get('tables', [])}", 200)
    for key in ("command", "path", "file_path", "pattern", "query"):
        if key in args:
            return _truncate(f"{key}={args[key]}", 200)
    return _truncate(args, 200)


def _result_text(result) -> str:
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            if parts:
                return "\n".join(parts)
        if "structuredContent" in result:
            return _truncate(result["structuredContent"], 600)
    return _truncate(result, 600)


def _result_summary(result) -> str:
    text = _result_text(result)
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return _truncate(text, 500)
    if isinstance(parsed, dict) and "status" in parsed:
        return _truncate(f"status={parsed['status']}", 200)
    return _truncate(text, 500)


def extract_refs(text: str) -> list[str]:
    refs: list[str] = []
    for match in _REF_RE.finditer(text):
        ref = match.group(0).rstrip(".,;:)]}")
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def extract_sql(text: str) -> list[dict]:
    blocks = []
    for match in _FENCE_RE.finditer(text):
        language = match.group(1).lower()
        sql = match.group(2).strip()
        if not sql:
            continue
        if language not in _SQL_LANGS and (
            language or not re.match(r"(?is)^(select|with)\b", sql)
        ):
            continue
        aliases = {
            "spark": "spark",
            "sparksql": "spark",
            "sql": "hive",
            "hiveql": "hive",
        }
        dialect = aliases.get(language, language or "hive")
        blocks.append({"sql": sql, "dialect": dialect})
    return blocks


def _redact(value, secrets: tuple[str, ...]):
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, secrets) for key, item in value.items()}
    return value


def _redact_event(event: AgentEvent, secrets: tuple[str, ...]) -> AgentEvent:
    return AgentEvent(event.type, _redact(event.data, secrets))


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
class PiEventParser:
    """Pure translation of pi JSONL records into backend events."""

    text_parts: list[str] = field(default_factory=list)
    final_text: str | None = None
    refs: list[str] = field(default_factory=list)
    error_message: str | None = None
    stop_reason: str | None = None
    settled: bool = False

    def _add_refs(self, text: str) -> None:
        for ref in extract_refs(text):
            if ref not in self.refs:
                self.refs.append(ref)

    def feed(self, record) -> list[AgentEvent]:
        if not isinstance(record, dict):
            return []
        kind = record.get("type")
        if kind == "message_update":
            event = record.get("assistantMessageEvent") or {}
            event_type = event.get("type")
            if event_type == "text_delta":
                delta = event.get("delta", "")
                if delta:
                    self.text_parts.append(delta)
                    return [AgentEvent(TEXT_DELTA, {"text": delta})]
            elif event_type == "error":
                self.error_message = _truncate(event.get("error") or "provider error")
            return []
        if kind == "message_end":
            message = record.get("message") or {}
            if message.get("role") == "assistant":
                text = _message_text(message)
                if text:
                    self.final_text = text
                    self.stop_reason = message.get("stopReason") or self.stop_reason
                    self._add_refs(text)
                if message.get("stopReason") == "error":
                    self.error_message = _truncate(
                        message.get("errorMessage") or "provider error"
                    )
            return []
        if kind == "tool_execution_start":
            name = record.get("toolName") or "tool"
            return [
                AgentEvent(
                    TOOL_START,
                    {
                        "id": record.get("toolCallId"),
                        "name": name,
                        "summary": _tool_args_summary(name, record.get("args")),
                        "args": record.get("args"),
                    },
                )
            ]
        if kind == "tool_execution_end":
            name = record.get("toolName") or "tool"
            result = record.get("result")
            events = [
                AgentEvent(
                    TOOL_END,
                    {
                        "id": record.get("toolCallId"),
                        "name": name,
                        "is_error": bool(record.get("isError")),
                        "summary": _result_summary(result),
                        "duration_ms": record.get("durationMs"),
                    },
                )
            ]
            if name.split("__")[-1] == "request_clarification":
                questions = _clarification_questions(result)
                if questions:
                    events.append(AgentEvent(CLARIFICATION, {"questions": questions}))
            return events
        if kind == "auto_retry_end" and record.get("success") is False:
            self.error_message = _truncate(
                record.get("finalError") or "agent retry failed"
            )
            return []
        if kind == "extension_error":
            self.error_message = _truncate(record.get("error") or "extension failed")
            return []
        if kind == "response":
            if record.get("command") == "prompt" and record.get("success") is False:
                self.error_message = _truncate(record.get("error") or "prompt failed")
            return []
        if kind == "agent_settled":
            self.settled = True
            return []
        return []

    def finish(self) -> AgentEvent:
        if self.error_message and not (self.final_text or self.text_parts):
            return AgentEvent(ERROR, {"message": self.error_message})
        text = (
            self.final_text if self.final_text is not None else "".join(self.text_parts)
        )
        self._add_refs(text)
        citations = [{"ref": ref, "type": ref.split(":", 1)[0]} for ref in self.refs]
        return AgentEvent(
            DONE,
            {
                "text": text,
                "stop_reason": self.stop_reason,
                "sql": extract_sql(text),
                "citations": citations,
            },
        )


def _clarification_questions(result) -> list[dict]:
    text = _result_text(result)
    try:
        body = json.loads(text)
    except (TypeError, ValueError):
        return []
    if isinstance(body, dict) and "structuredContent" in body:
        body = body["structuredContent"]
    questions = body.get("questions") if isinstance(body, dict) else None
    return questions if isinstance(questions, list) else []


class PiRunner:
    agent_id = "pi"

    def __init__(self, binary: str | None = None):
        self.binary = binary

    def _sandboxed(self) -> bool:
        return (
            not self.binary
            and os.environ.get("DATAAGENT_AGENT_MODE", "sandbox") != "local"
        )

    def available(self) -> bool:
        return bool(
            shutil.which(
                "kubectl"
                if os.environ.get("DATAAGENT_AGENT_MODE") == "k8s"
                else "docker"
            )
            if self._sandboxed()
            else self.binary or shutil.which("pi")
        )

    def _binary(self) -> str:
        binary = self.binary or shutil.which("pi")
        if not binary:
            raise FileNotFoundError("pi executable not found")
        return binary

    def write_config(self, request: AgentTurnRequest) -> tuple[Path, Path, str]:
        config = request.workspace / ("work/config" if self._sandboxed() else "config")
        sessions = request.workspace / "sessions"
        work = request.workspace / "work"
        for directory in (config, sessions, work):
            directory.mkdir(parents=True, exist_ok=True)
        provider = {
            "name": request.provider.name,
            "baseUrl": request.provider.base_url,
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
                    "url": request.mcp_url,
                    "headers": {
                        "Authorization": f"Bearer ${{{TOKEN_ENV}}}",
                        CONVERSATION_HEADER: request.conversation_id,
                        TURN_HEADER: str(request.turn_id or ""),
                    },
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
        _write_json(config / "models.json", models)
        _write_json(config / "mcp.json", mcp)
        _write_json(config / "settings.json", settings)
        skill_path = config / "skills" / "dataagent-retrieval" / "SKILL.md"
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(read_skill(), encoding="utf-8")
        os.chmod(skill_path, 0o600)
        return config, sessions, str(work)

    async def run(self, request: AgentTurnRequest) -> AsyncIterator[AgentEvent]:
        config, sessions, work = self.write_config(request)
        env = self._environment(request, config, sessions)
        model = f"{PROVIDER_KEY}/{request.model}"
        skill = read_skill()
        sandboxed = self._sandboxed()
        sandbox = None
        exec_header = None
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
        if sandboxed:
            sandbox_class = (
                KubernetesSessionSandbox
                if os.environ.get("DATAAGENT_AGENT_MODE") == "k8s"
                else SessionSandbox
            )
            sandbox = sandbox_class(
                request.workspace, request.provider.base_url, request.mcp_url
            )
            register_session(request.conversation_id, sandbox)
            try:
                await asyncio.to_thread(sandbox.start)
                # Configuration contains only environment references for secrets.
                models = json.loads((config / "models.json").read_text())
                models["providers"][PROVIDER_KEY]["baseUrl"] = sandbox.model_url
                _write_json(config / "models.json", models)
                mcp = json.loads((config / "mcp.json").read_text())
                mcp["mcpServers"][MCP_SERVER]["url"] = sandbox.mcp_url
                _write_json(config / "mcp.json", mcp)
                command[command.index(str(sessions))] = "/sessions"
                env["PI_CODING_AGENT_DIR"] = "/workspace/config"
                env["PI_CODING_AGENT_SESSION_DIR"] = "/sessions"
                env["HOME"] = "/tmp"
                docker = ["docker", "exec", "-i"]
                for key in (
                    "PI_CODING_AGENT_DIR",
                    "PI_CODING_AGENT_SESSION_DIR",
                    "PI_OFFLINE",
                    TOKEN_ENV,
                    KEY_ENV,
                    "HOME",
                ):
                    if key in env:
                        docker.extend(["--env", key])
                if isinstance(sandbox, KubernetesSessionSandbox):
                    command, exec_header = sandbox.wrap_command(
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
                    command = docker + [sandbox.name, *command]
            except BaseException:
                unregister_session(request.conversation_id, sandbox)
                await asyncio.to_thread(sandbox.close)
                raise
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=work,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
                limit=STREAM_LIMIT,
            )
        except BaseException:
            if sandbox is not None:
                unregister_session(request.conversation_id, sandbox)
                await asyncio.to_thread(sandbox.close)
            raise
        stderr: deque[str] = deque(maxlen=80)
        drain = asyncio.create_task(_drain(process.stderr, stderr))
        parser = PiEventParser()
        secrets = tuple(
            secret for secret in (request.mcp_token, request.provider.api_key) if secret
        )
        finished = False
        try:
            assert process.stdin is not None
            if exec_header is not None:
                process.stdin.write(exec_header)
            process.stdin.write(
                (
                    json.dumps(
                        {"id": "turn", "type": "prompt", "message": request.prompt},
                        ensure_ascii=False,
                    )
                    + "\n"
                ).encode()
            )
            await process.stdin.drain()
            assert process.stdout is not None
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                for event in parser.feed(record):
                    yield _redact_event(event, secrets)
                if parser.settled:
                    break
            if parser.settled:
                await _stop(process)
                finished = True
                yield _redact_event(parser.finish(), secrets)
                return
            returncode = await process.wait()
            finished = True
            if returncode != 0 or parser.error_message:
                message = parser.error_message or (
                    self._sanitize("\n".join(stderr), request)
                    or f"pi exited with status {returncode}"
                )
                yield AgentEvent(ERROR, {"message": message})
                return
            yield _redact_event(parser.finish(), secrets)
        finally:
            if sandbox is not None:
                unregister_session(request.conversation_id, sandbox)
                await asyncio.to_thread(sandbox.close)
            if not finished:
                await _stop(process)
            drain.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await drain

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


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


async def _drain(stream, lines: deque[str]) -> None:
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            return
        lines.append(line.decode("utf-8", "replace").rstrip("\n"))


async def _stop(process) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        process.kill()
        await process.wait()
