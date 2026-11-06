"""Shared building blocks for agent runner plugins.

A runner plugin only has to translate its own wire protocol.  Session
sandboxing, MCP configuration headers, process lifecycle, event shapes,
session markers and secret redaction live here so every plugin behaves the
same way.
"""

import asyncio
import contextlib
import json
import os
import re
from collections import deque
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

from .agent_sandbox import register_session, unregister_session
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
from .mcp_context import CONVERSATION_HEADER, TURN_HEADER
from .session_runtime import LocalSessionRuntime, runtime_available, runtime_class

TOKEN_ENV = "DATAAGENT_TOKEN"
KEY_ENV = "DATAAGENT_PROVIDER_KEY"
MCP_SERVER = "dataagent"
# Agent CLIs emit one JSON record per line; some records contain the whole
# conversation and tool results, well past asyncio.StreamReader's 64 KiB default.
STREAM_LIMIT = 64 * 1024 * 1024
_REF_RE = re.compile(
    r"(?:document|table|field|task|dimension_value|lineage|category):"
    r"[^\s\"'<>()\[\]{}`]+"
)
_FENCE_RE = re.compile(r"```([A-Za-z0-9_+.-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)
_SQL_LANGS = {"sql", "hive", "hiveql", "spark", "sparksql", "doris", "mysql"}
_MISSING = object()


def truncate(value, limit: int = 400) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def tool_args_summary(name: str, args) -> str:
    if not isinstance(args, dict):
        return truncate(args, 200)
    short = name.split("__")[-1]
    if short == "search_knowledge":
        return truncate(
            f"query={args.get('query', '')} scope={args.get('scope', '')}", 200
        )
    if short == "describe_tables":
        return truncate(f"tables={args.get('names', [])}", 200)
    if short == "validate_sql":
        return truncate(f"dialect={args.get('dialect', '')}", 200)
    if short == "read_document":
        return truncate(f"doc_id={args.get('doc_id', '')}", 200)
    if short == "read_task_sql":
        return truncate(f"task_id={args.get('task_id', '')}", 200)
    if short == "get_lineage":
        return truncate(
            f"table={args.get('table', '')} direction={args.get('direction', '')}", 200
        )
    if short == "lookup_dim_values":
        return truncate(
            f"table={args.get('table', '')} field={args.get('field') or ''} "
            f"keyword={args.get('keyword', '')}",
            200,
        )
    if short == "find_join_path":
        return truncate(f"tables={args.get('tables', [])}", 200)
    for key in ("command", "path", "file_path", "pattern", "query"):
        if key in args:
            return truncate(f"{key}={args[key]}", 200)
    return truncate(args, 200)


def result_text(result) -> str:
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
            return truncate(result["structuredContent"], 600)
    return truncate(result, 600)


def result_summary(result) -> str:
    text = result_text(result)
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return truncate(text, 500)
    if isinstance(parsed, dict) and "status" in parsed:
        return truncate(f"status={parsed['status']}", 200)
    return truncate(text, 500)


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


def redact(value, secrets: tuple[str, ...]):
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: redact(item, secrets) for key, item in value.items()}
    return value


def redact_event(event: AgentEvent, secrets: tuple[str, ...]) -> AgentEvent:
    return AgentEvent(event.type, redact(event.data, secrets))


def request_secrets(request: AgentTurnRequest) -> tuple[str, ...]:
    return tuple(
        secret for secret in (request.mcp_token, request.provider.api_key) if secret
    )


def clarification_questions(result) -> list[dict]:
    text = result_text(result)
    try:
        body = json.loads(text)
    except (TypeError, ValueError):
        return []
    if isinstance(body, dict) and "structuredContent" in body:
        body = body["structuredContent"]
    questions = body.get("questions") if isinstance(body, dict) else None
    return questions if isinstance(questions, list) else []


def text_event(text: str) -> AgentEvent:
    return AgentEvent(TEXT_DELTA, {"text": text})


def tool_start_event(ident, name: str, args) -> AgentEvent:
    return AgentEvent(
        TOOL_START,
        {
            "id": ident,
            "name": name,
            "summary": tool_args_summary(name, args),
            "args": args,
        },
    )


def tool_end_event(
    ident,
    name: str,
    *,
    is_error: bool,
    summary: str,
    duration_ms=_MISSING,
) -> AgentEvent:
    data = {
        "id": ident,
        "name": name,
        "is_error": is_error,
        "summary": summary,
    }
    if duration_ms is not _MISSING:
        data["duration_ms"] = duration_ms
    return AgentEvent(TOOL_END, data)


def clarification_event(name: str, result) -> AgentEvent | None:
    if name.split("__")[-1] != "request_clarification":
        return None
    questions = clarification_questions(result)
    if not questions:
        return None
    return AgentEvent(CLARIFICATION, {"questions": questions})


def done_event(text: str, stop_reason: str | None = None) -> AgentEvent:
    return AgentEvent(
        DONE,
        {
            "text": text,
            "stop_reason": stop_reason,
            "sql": extract_sql(text),
            "citations": [
                {"ref": ref, "type": ref.split(":", 1)[0]} for ref in extract_refs(text)
            ],
        },
    )


def error_event(message: str) -> AgentEvent:
    return AgentEvent(ERROR, {"message": message})


@dataclass
class EventParser:
    """Common text/error accumulation for a runner's wire-protocol parser.

    Subclasses only translate their protocol records into events and call
    ``emit_text``/``finish``.  ``error_overrides_text`` keeps the existing
    per-agent behavior: pi prefers a partial answer, Claude and Codex report
    the error instead.
    """

    text_parts: list[str] = field(default_factory=list)
    final_text: str | None = None
    refs: list[str] = field(default_factory=list)
    error: str | None = None
    stop_reason: str | None = None
    error_overrides_text: bool = True

    def emit_text(self, text: str, *, emit_empty: bool = False) -> list[AgentEvent]:
        if not text and not emit_empty:
            return []
        self.text_parts.append(text)
        return [text_event(text)]

    def add_refs(self, text: str) -> None:
        for ref in extract_refs(text):
            if ref not in self.refs:
                self.refs.append(ref)

    def finish(self) -> AgentEvent:
        if self.error and (
            self.error_overrides_text or not (self.final_text or self.text_parts)
        ):
            return error_event(self.error)
        text = (
            self.final_text if self.final_text is not None else "".join(self.text_parts)
        )
        self.add_refs(text)
        return done_event(text, self.stop_reason)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def read_marker(path: Path, key: str):
    return json.loads(path.read_text(encoding="utf-8"))[key]


def has_session_file(config: Path, session_id: str) -> bool:
    return any(config.glob(f"projects/*/{session_id}.jsonl"))


def passthrough_environment(*names: str) -> dict[str, str]:
    return {name: os.environ[name] for name in names if name in os.environ}


def is_sandboxed(binary: str | None = None) -> bool:
    return not binary and os.environ.get("DATAAGENT_AGENT_MODE", "sandbox") != "local"


def available_in_runtime(name: str, binary: str | None = None) -> bool:
    return runtime_available(name, binary)


def default_sandbox_class():
    return runtime_class()


def mcp_headers(request: AgentTurnRequest) -> dict[str, str]:
    return {
        "Authorization": f"Bearer ${{{TOKEN_ENV}}}",
        CONVERSATION_HEADER: request.conversation_id,
        TURN_HEADER: str(request.turn_id or ""),
    }


def mcp_server_config(request: AgentTurnRequest) -> dict:
    return {"url": request.mcp_url, "headers": mcp_headers(request)}


class SandboxTurn:
    """Start and always tear down one conversation runtime for one turn."""

    def __init__(
        self,
        request: AgentTurnRequest,
        *,
        sandbox_class=None,
    ):
        self.request = request
        runtime = sandbox_class or LocalSessionRuntime
        self.sandbox = runtime(
            request.workspace, request.provider.base_url, request.mcp_url
        )
        self.model_url = self.sandbox.model_url
        self.mcp_url = self.sandbox.mcp_url

    async def __aenter__(self) -> Self:
        if self.sandbox is not None:
            register_session(self.request.conversation_id, self.sandbox)
            try:
                await asyncio.to_thread(self.sandbox.start)
            except BaseException:
                unregister_session(self.request.conversation_id, self.sandbox)
                await asyncio.to_thread(self.sandbox.close)
                raise
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if self.sandbox is not None:
            unregister_session(self.request.conversation_id, self.sandbox)
            await asyncio.to_thread(self.sandbox.close)

    def wrap_command(self, command, env: dict[str, str], keys: Iterable[str]):
        if self.sandbox is None:
            return command, None
        return self.sandbox.wrap_command(command, env, tuple(keys))


class AgentProcess:
    """A running agent CLI with drained stderr and safe teardown."""

    def __init__(self, process, stderr: deque[str], drain):
        self.process = process
        self._stderr = stderr
        self._drain = drain

    def write(self, data: bytes) -> None:
        if self.process.stdin is None:
            raise RuntimeError("agent stdin is not available")
        self.process.stdin.write(data)

    async def drain(self) -> None:
        if self.process.stdin is None:
            raise RuntimeError("agent stdin is not available")
        await self.process.stdin.drain()

    async def readline(self) -> bytes:
        if self.process.stdout is None:
            return b""
        return await self.process.stdout.readline()

    async def lines(self) -> AsyncIterator[bytes]:
        if self.process.stdout is None:
            return
        async for line in self.process.stdout:
            yield line

    async def wait(self) -> int:
        return await self.process.wait()

    async def wait_for_stderr(self) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await self._drain

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    def stderr_text(self) -> str:
        return "\n".join(self._stderr)


async def drain_stream(stream, lines: deque[str]) -> None:
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            return
        lines.append(line.decode("utf-8", "replace").rstrip("\n"))


async def stop_process(process) -> None:
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


@asynccontextmanager
async def agent_process(
    command,
    *,
    cwd,
    env,
    header: bytes | None = None,
    stdin_open: bool = False,
    start_new_session: bool = False,
) -> AsyncIterator[AgentProcess]:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE
        if header is not None or stdin_open
        else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        start_new_session=start_new_session,
        limit=STREAM_LIMIT,
    )
    stderr: deque[str] = deque(maxlen=80)
    drain = asyncio.create_task(drain_stream(process.stderr, stderr))
    agent = AgentProcess(process, stderr, drain)
    try:
        if header is not None:
            agent.write(header)
            assert process.stdin is not None
            await process.stdin.drain()
            if not stdin_open:
                process.stdin.close()
        yield agent
    finally:
        if process.returncode is None:
            await stop_process(process)
        drain.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await drain
