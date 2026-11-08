#!/usr/bin/env python3
"""Reusable end-to-end run for Semantica DataAgent.

Run one command from the repository root:

    uv run --directory backend python tests/e2e/harness.py \
        --scratch "$HOME/wt/sda/scratch/issue-47" \
        --source-dir ../samples/demo \
        --source canonical --source markdown \
        --neo4j-password-command 'ssh baize@172.16.242.124 ...' \
        --checks tests/e2e/example_checks.py \
        --log "$HOME/wt/sda/scratch/issue-47/e2e.log"

The harness creates a temporary SQLite database, initializes an admin token,
starts ``serve`` on a free port, configures the test storage, enables the
requested source adapters, builds, runs checks, and always removes this run's
Neo4j nodes, Milvus collection, service process, and temporary database.

Ticket-specific checks are plain functions named ``check_*`` in a file passed
with ``--checks``. Each receives a context with a synchronous MCP client, an
HTTP client, the admin token, and the build record. See
``tests/e2e/example_checks.py`` and ``docs/code-standards.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import inspect
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SOURCE = ROOT / "samples" / "demo"
CLEANUP_BATCH_SIZE = 2000
GENERATION_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
DEV_ENDPOINTS = {
    ("172.16.242.124", 31601),
    ("172.16.242.124", 31602),
    ("172.16.242.124", 31603),
}
SECRET_SETTING = "neo4j_password"


class UsageError(ValueError):
    """An invalid combination of arguments or an unsafe target."""


def _stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _port(uri: str) -> int | None:
    parts = urlsplit(uri)
    if parts.port is not None:
        return parts.port
    return 443 if parts.scheme in {"https", "bolt+s", "bolt+ssc", "neo4j+s"} else None


def _reject_dev_endpoint(name: str, uri: str) -> None:
    parts = urlsplit(uri)
    port = _port(uri)
    if parts.hostname and (parts.hostname, port) in DEV_ENDPOINTS:
        raise UsageError(
            f"{name} points at the development instance ({uri}); use the test ports"
        )


def _source_spec(value: str) -> tuple[str, dict[str, Any]]:
    plugin_id, separator, raw = value.partition(":")
    plugin_id = plugin_id.strip()
    if not plugin_id:
        raise argparse.ArgumentTypeError("source plugin id must not be empty")
    parameters: dict[str, Any] = {}
    if separator:
        if not raw.strip():
            raise argparse.ArgumentTypeError("source parameters must be a JSON object")
        try:
            parameters = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise argparse.ArgumentTypeError(
                f"invalid JSON parameters for {plugin_id}: {exc.msg}"
            ) from None
        if not isinstance(parameters, dict):
            raise argparse.ArgumentTypeError(
                f"parameters for {plugin_id} must be a JSON object"
            )
    return plugin_id, parameters


def _generation(args: argparse.Namespace) -> str:
    if args.generation:
        generation = args.generation
    else:
        prefix = args.generation_prefix
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,40}", prefix):
            raise UsageError(
                "generation prefix must start with a letter and contain only "
                "letters, digits, and underscores"
            )
        generation = prefix + uuid.uuid4().hex[:8]
    if not GENERATION_RE.fullmatch(generation):
        raise UsageError(
            "generation must start with a letter, contain only letters, digits, "
            "and underscores, and be at most 64 characters"
        )
    return generation


@dataclass(frozen=True)
class Config:
    scratch: Path
    source_dir: Path
    sources: tuple[tuple[str, dict[str, Any]], ...]
    parsers: tuple[str, ...] | None
    checks: tuple[Path, ...]
    log: Path | None
    generation: str
    neo4j_uri: str
    neo4j_user: str
    milvus_uri: str
    embedding_url: str
    embedding_model: str
    neo4j_password_command: str | None
    neo4j_password_env: str
    build_timeout: float
    start_timeout: float
    poll_interval: float
    expect_build: str
    builtin_checks: bool
    expect_table: str
    search_query: str
    validate_sql: str
    validate_dialect: str
    dimension_value_limit: int
    embedding_concurrency: int
    embedding_timeout_seconds: float
    embedding_timeout_per_input_seconds: float
    embedding_retries: int
    embedding_backoff_seconds: float


def parse_args(argv: list[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(
        description="Reusable end-to-end run against isolated test storage.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument(
        "--source",
        action="append",
        type=_source_spec,
        metavar="ID[:JSON]",
        help="source adapter to enable; repeat for more (default: canonical, markdown)",
    )
    parser.add_argument(
        "--parser",
        action="append",
        metavar="ID",
        help="task parser to enable; repeat to restrict (default: every parser)",
    )
    parser.add_argument("--checks", action="append", type=Path, default=[])
    parser.add_argument("--log", type=Path)
    parser.add_argument("--generation")
    parser.add_argument("--generation-prefix", default="e2e_")
    parser.add_argument("--neo4j-uri", default="bolt://172.16.242.124:31611")
    parser.add_argument("--neo4j-user", default="neo4j")
    parser.add_argument(
        "--neo4j-password-command",
        help="shell command whose stdout is the password; output is never printed",
    )
    parser.add_argument("--neo4j-password-env", default="NEO4J_PASSWORD")
    parser.add_argument("--milvus-uri", default="http://172.16.242.124:31612")
    parser.add_argument("--embedding-url", default="http://127.0.0.1:17998")
    parser.add_argument("--embedding-model", default="model/bge-small-zh-v1.5")
    parser.add_argument("--build-timeout", type=float, default=900.0)
    parser.add_argument("--start-timeout", type=float, default=30.0)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument(
        "--expect-build", choices=("succeeded", "failed", "any"), default="succeeded"
    )
    parser.add_argument("--no-builtin-checks", action="store_true")
    parser.add_argument("--expect-table", default="shop.orders")
    parser.add_argument("--search-query", default="销售额")
    parser.add_argument(
        "--validate-sql",
        default=(
            "SELECT id FROM shop.orders WHERE dt = (SELECT MAX(dt) FROM shop.orders)"
        ),
    )
    parser.add_argument(
        "--validate-dialect", choices=("hive", "spark", "doris"), default="hive"
    )
    parser.add_argument("--dimension-value-limit", type=int, default=1000)
    parser.add_argument("--embedding-concurrency", type=int, default=2)
    parser.add_argument("--embedding-timeout-seconds", type=float, default=60.0)
    parser.add_argument(
        "--embedding-timeout-per-input-seconds", type=float, default=20.0
    )
    parser.add_argument("--embedding-retries", type=int, default=2)
    parser.add_argument("--embedding-backoff-seconds", type=float, default=1.0)
    args = parser.parse_args(argv)

    try:
        generation = _generation(args)
        scratch = args.scratch.expanduser().resolve()
        source_dir = args.source_dir.expanduser().resolve()
        if not source_dir.is_dir():
            raise UsageError(f"source directory does not exist: {source_dir}")
        if scratch.exists() and not scratch.is_dir():
            raise UsageError(f"scratch path is not a directory: {scratch}")
        for uri_name, uri in (
            ("neo4j", args.neo4j_uri),
            ("milvus", args.milvus_uri),
            ("embedding", args.embedding_url),
        ):
            _reject_dev_endpoint(uri_name, uri)
        if args.build_timeout <= 0 or args.start_timeout <= 0:
            raise UsageError("timeouts must be positive")
        if args.poll_interval <= 0:
            raise UsageError("poll interval must be positive")
        if not 1 <= args.dimension_value_limit <= 1000000:
            raise UsageError("dimension value limit must be between 1 and 1000000")
        if args.embedding_concurrency < 1:
            raise UsageError("embedding concurrency must be positive")
        sources = args.source or [("canonical", {}), ("markdown", {})]
        ids = [spec[0] for spec in sources]
        if len(set(ids)) != len(ids):
            raise UsageError("source adapters must not be repeated")
        parsers = tuple(args.parser) if args.parser else None
        if parsers is not None and (not parsers or len(set(parsers)) != len(parsers)):
            raise UsageError("parsers must not be empty or repeated")
        checks = tuple(path.expanduser().resolve() for path in args.checks)
        for path in checks:
            if not path.is_file():
                raise UsageError(f"check file does not exist: {path}")
        log = args.log.expanduser().resolve() if args.log else None
    except UsageError as exc:
        parser.error(str(exc))

    return Config(
        scratch=scratch,
        source_dir=source_dir,
        sources=tuple(sources),
        parsers=parsers,
        checks=checks,
        log=log,
        generation=generation,
        neo4j_uri=args.neo4j_uri,
        neo4j_user=args.neo4j_user,
        milvus_uri=args.milvus_uri,
        embedding_url=args.embedding_url,
        embedding_model=args.embedding_model,
        neo4j_password_command=args.neo4j_password_command,
        neo4j_password_env=args.neo4j_password_env,
        build_timeout=args.build_timeout,
        start_timeout=args.start_timeout,
        poll_interval=args.poll_interval,
        expect_build=args.expect_build,
        builtin_checks=not args.no_builtin_checks,
        expect_table=args.expect_table,
        search_query=args.search_query,
        validate_sql=args.validate_sql,
        validate_dialect=args.validate_dialect,
        dimension_value_limit=args.dimension_value_limit,
        embedding_concurrency=args.embedding_concurrency,
        embedding_timeout_seconds=args.embedding_timeout_seconds,
        embedding_timeout_per_input_seconds=args.embedding_timeout_per_input_seconds,
        embedding_retries=args.embedding_retries,
        embedding_backoff_seconds=args.embedding_backoff_seconds,
    )


class Logger:
    def __init__(self, path: Path | None):
        self._file = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file = path.open("w", encoding="utf-8")

    def line(self, message: str) -> None:
        text = f"[{_stamp()}] {message}"
        self.raw(text)

    def raw(self, text: str) -> None:
        print(text, flush=True)
        if self._file is not None:
            self._file.write(text + "\n")
            self._file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


class McpSession:
    """A synchronous facade over one MCP streamable-HTTP session."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 180.0,
        *,
        query: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ):
        self._url = base_url.rstrip("/") + "/mcp/"
        if query:
            self._url += "?" + urlencode(query)
        self._headers = {"Authorization": f"Bearer {token}"}
        self._headers.update(headers or {})
        self._timeout = timeout
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._thread_main, name="e2e-mcp", daemon=True
        )
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._session: Any = None
        self._error: BaseException | None = None

    def open(self, timeout: float) -> None:
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("timed out opening the MCP session")
        if self._error is not None:
            raise RuntimeError(
                f"MCP session failed: {type(self._error).__name__}: {self._error}"
            ) from self._error

    def _thread_main(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except BaseException as exc:  # noqa: BLE001 - surfaced through open/call
            self._error = exc
        finally:
            self._ready.set()
            self._session = None
            self._loop.close()

    async def _serve(self) -> None:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        self._stop = asyncio.Event()
        async with (
            httpx.AsyncClient(
                headers=self._headers, timeout=self._timeout, trust_env=False
            ) as client,
            streamable_http_client(self._url, http_client=client) as (
                read,
                write,
                _,
            ),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            self._session = session
            self._ready.set()
            await self._stop.wait()

    def call_tool(
        self, name: str, arguments: dict[str, Any], timeout: float | None = None
    ):
        if self._error is not None:
            raise RuntimeError(
                f"MCP session failed: {type(self._error).__name__}: {self._error}"
            ) from self._error
        if self._session is None:
            raise RuntimeError("MCP session is not open")
        future = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(name, arguments), self._loop
        )
        return future.result(timeout=timeout or self._timeout)

    def list_tools(self, timeout: float | None = None):
        if self._error is not None:
            raise RuntimeError(
                f"MCP session failed: {type(self._error).__name__}: {self._error}"
            ) from self._error
        if self._session is None:
            raise RuntimeError("MCP session is not open")
        future = asyncio.run_coroutine_threadsafe(
            self._session.list_tools(), self._loop
        )
        return future.result(timeout=timeout or self._timeout).tools

    def close(self) -> None:
        if self._stop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread.is_alive():
            self._thread.join(timeout=15)


def _tool_value(result: Any) -> Any:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        if "result" in structured:
            return structured["result"]
        if "result_fragment" in structured:
            return structured
        return structured
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return None


class CheckContext:
    def __init__(self, runtime: Runtime, mcp: McpSession | None, mcp_error: str | None):
        self.runtime = runtime
        self.config = runtime.config
        self.mcp = mcp
        self.mcp_error = mcp_error
        self.rest = httpx.Client(
            base_url=runtime.base_url,
            headers={"Authorization": f"Bearer {runtime.token}"},
            timeout=180,
            trust_env=False,
        )
        self.token = runtime.token
        self.build = runtime.build or {}
        self.build_id = self.build.get("id")
        self.generation = runtime.config.generation
        try:
            response = self.rest.get("/api/v1/generations")
            if response.is_success:
                current = next(
                    (row for row in response.json()["generations"] if row["current"]),
                    None,
                )
                if current:
                    self.generation = current["name"]
        except httpx.HTTPError:
            pass
        self.source_dir = runtime.config.source_dir
        self.work_dir = runtime.run_dir
        self.health = runtime.health
        self.logger = runtime.logger
        self._sessions: list[McpSession] = []

    def open_mcp(
        self,
        token: str,
        *,
        project: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> McpSession:
        """Open one extra MCP session for a ticket-specific check."""
        session = McpSession(
            self.runtime.base_url,
            token,
            query={"project": project} if project is not None else None,
            headers=headers,
        )
        try:
            session.open(timeout)
        except Exception:
            session.close()
            raise
        self._sessions.append(session)
        return session

    @property
    def expect_table(self) -> str:
        return self.config.expect_table

    def require(self, condition: Any, message: str) -> None:
        if not condition:
            raise AssertionError(message)

    def mcp_call(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        allow_error: bool = False,
        raw: bool = False,
        timeout: float | None = None,
    ) -> Any:
        if self.mcp is None:
            raise RuntimeError(f"MCP session unavailable: {self.mcp_error}")
        result = self.mcp.call_tool(name, arguments or {}, timeout=timeout)
        if getattr(result, "isError", False):
            if allow_error:
                return result
            structured = getattr(result, "structuredContent", None) or {}
            raise RuntimeError(
                f"{structured.get('code', 'tool_error')}: "
                f"{structured.get('message', 'MCP tool failed')}"
            )
        return result if raw else _tool_value(result)

    def close(self) -> None:
        for session in self._sessions:
            session.close()
        self._sessions.clear()
        self.rest.close()


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return repr(value)


def _normalize_check(result: Any) -> tuple[bool, Any]:
    if result is None:
        return True, None
    if isinstance(result, bool):
        return result, None
    if isinstance(result, dict):
        if "ok" in result:
            return bool(result["ok"]), result.get("info")
        return True, result
    return False, f"unsupported check return type: {type(result).__name__}"


def _load_check_functions(files: tuple[Path, ...]) -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    for index, path in enumerate(files):
        module_name = f"_e2e_checks_{index}_{path.stem.replace('-', '_')}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load check file: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        before = len(found)
        for name, value in module.__dict__.items():
            if (
                name.startswith("check_")
                and inspect.isfunction(value)
                and value.__module__ == module_name
            ):
                found.append((name, value))
        if len(found) == before:
            raise RuntimeError(f"no check_* functions in {path}")
    names = [name for name, _ in found]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise RuntimeError("duplicate check functions: " + ", ".join(duplicates))
    return found


def _run_check(name: str, function: Any, context: CheckContext) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = function(context)
        if inspect.isawaitable(result):
            result = asyncio.run(result)
        ok, info = _normalize_check(result)
    except Exception as exc:  # noqa: BLE001 - one failing check must not stop the rest
        ok, info = False, f"{type(exc).__name__}: {exc}"
    elapsed = round((time.monotonic() - started) * 1000)
    entry = {
        "name": name,
        "ok": bool(ok),
        "info": _json_safe(info),
        "elapsed_ms": elapsed,
    }
    context.logger.line(f"check {name}: {'PASS' if ok else 'FAIL'}")
    if not ok:
        context.logger.line(f"  {name}: {entry['info']}")
    return entry


def _builtin_checks() -> list[tuple[str, Any]]:
    return [
        ("describe_tables", _check_describe_tables),
        ("search_knowledge", _check_search_knowledge),
        ("get_lineage", _check_get_lineage),
        ("validate_sql", _check_validate_sql),
        ("get_capabilities", _check_get_capabilities),
        ("build_counts", _check_build_counts),
    ]


def _check_describe_tables(context: CheckContext) -> dict[str, Any]:
    tables = context.mcp_call("describe_tables", {"names": [context.expect_table]})
    context.require(
        isinstance(tables, list) and tables,
        f"describe_tables returned no table named {context.expect_table}",
    )
    table = tables[0]
    context.require(table.get("fields"), f"{context.expect_table} has no fields")
    return {"ok": True, "info": {"tables": len(tables), "fields": len(table["fields"])}}


def _check_search_knowledge(context: CheckContext) -> dict[str, Any]:
    hits = context.mcp_call(
        "search_knowledge",
        {"query": context.config.search_query, "scope": "docs", "top_k": 10},
    )
    context.require(
        isinstance(hits, list) and hits, "search_knowledge returned no hits"
    )
    return {
        "ok": True,
        "info": {"hits": len(hits), "refs": [hit.get("ref") for hit in hits[:5]]},
    }


def _check_get_lineage(context: CheckContext) -> dict[str, Any]:
    edges = context.mcp_call(
        "get_lineage",
        {"table": context.expect_table, "direction": "up", "depth": 1},
    )
    context.require(isinstance(edges, list) and edges, "get_lineage returned no edges")
    context.require(
        all(edge.get("upstream") and edge.get("task_id") for edge in edges),
        "get_lineage edges are missing upstream or task_id",
    )
    return {
        "ok": True,
        "info": {
            "edges": len(edges),
            "upstreams": sorted({edge["upstream"] for edge in edges}),
        },
    }


def _check_validate_sql(context: CheckContext) -> dict[str, Any]:
    result = context.mcp_call(
        "validate_sql",
        {
            "sql": context.config.validate_sql,
            "dialect": context.config.validate_dialect,
        },
    )
    context.require(
        result.get("status") == "pass", f"validate_sql status is {result.get('status')}"
    )
    return {
        "ok": True,
        "info": {
            "status": result["status"],
            "tables": [item.get("status") for item in result.get("tables", [])],
            "fields": [item.get("status") for item in result.get("fields", [])],
        },
    }


def _check_get_capabilities(context: CheckContext) -> dict[str, Any]:
    capabilities = context.mcp_call("get_capabilities", {})
    context.require(
        capabilities.get("current_generation") == context.generation,
        "get_capabilities reports a different generation",
    )
    required = {
        "describe_tables",
        "search_knowledge",
        "get_lineage",
        "validate_sql",
    }
    context.require(
        required <= set(capabilities.get("tools", [])),
        "get_capabilities is missing an expected tool",
    )
    return {
        "ok": True,
        "info": {
            "current_generation": capabilities["current_generation"],
            "tools": len(capabilities.get("tools", [])),
        },
    }


def _check_build_counts(context: CheckContext) -> dict[str, Any]:
    context.require(context.build_id is not None, "build id is missing")
    response = context.rest.get(f"/api/v1/builds/{context.build_id}")
    context.require(
        response.status_code == 200, f"build detail HTTP {response.status_code}"
    )
    build = response.json()
    context.require(
        build.get("status") == context.build.get("status"),
        "build detail status differs from the polled build",
    )
    counts = build.get("counts") or {}
    context.require(bool(counts), "build detail has no counts")
    context.require(
        any(isinstance(value, int) and value > 0 for value in counts.values()),
        "build detail counts are all zero",
    )
    listing = context.rest.get("/api/v1/builds")
    context.require(
        listing.status_code == 200, f"build list HTTP {listing.status_code}"
    )
    context.require(
        any(item.get("id") == context.build_id for item in listing.json()),
        "build list does not contain this build",
    )
    return {"ok": True, "info": {"status": build["status"], "counts": counts}}


@dataclass
class Runtime:
    config: Config
    logger: Logger
    run_dir: Path
    db_path: Path
    service_log: Path
    password: str = field(default="", repr=False)
    server: subprocess.Popen[str] | None = None
    service_handle: Any = None
    base_url: str = ""
    token: str | None = None
    mcp: McpSession | None = None
    build: dict[str, Any] | None = None
    health: dict[str, Any] | None = None
    settings: dict[str, str] = field(default_factory=dict)
    storage_touched: bool = False


def _service_environment(
    runtime: Runtime, encryption_key: str, listen: str | None = None
) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "DATAAGENT_DB_PATH": str(runtime.db_path),
            "DATAAGENT_ENCRYPTION_KEY": encryption_key,
            "PYTHONUNBUFFERED": "1",
        }
    )
    if listen is not None:
        env["DATAAGENT_LISTEN"] = listen
    return env


def _create_run_dir(config: Config) -> Path:
    config.scratch.mkdir(parents=True, exist_ok=True)
    return Path(
        tempfile.mkdtemp(prefix=f"e2e-{config.generation}-", dir=config.scratch)
    ).resolve()


def _resolve_password(config: Config) -> str:
    if config.neo4j_password_command:
        process = subprocess.run(
            config.neo4j_password_command,
            check=False,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if process.returncode != 0:
            raise RuntimeError(
                f"neo4j password command failed with exit {process.returncode}"
            )
        value = process.stdout.strip()
    else:
        value = os.environ.get(config.neo4j_password_env, "").strip()
    value = value.removeprefix("neo4j/")
    if not value:
        raise RuntimeError(
            f"Neo4j password is empty; set {config.neo4j_password_env} or pass "
            "--neo4j-password-command"
        )
    return value


def _init_database(runtime: Runtime, encryption_key: str) -> None:
    env = _service_environment(runtime, encryption_key)
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "from semantica_dataagent.cli import main; main()",
            "init",
        ],
        check=False,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"init failed: {process.stderr.strip()[-1000:]}")
    lines = [line for line in process.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("init did not print an admin token")
    runtime.token = lines[-1].strip()
    if not runtime.db_path.is_file():
        raise RuntimeError("init did not create the database")
    runtime.logger.line(f"database initialized: {runtime.db_path}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _start_service(runtime: Runtime, encryption_key: str) -> None:
    port = _free_port()
    runtime.base_url = f"http://127.0.0.1:{port}"
    runtime.service_handle = runtime.service_log.open("w", encoding="utf-8")
    env = _service_environment(runtime, encryption_key, f"127.0.0.1:{port}")
    runtime.server = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from semantica_dataagent.cli import main; main()",
            "serve",
        ],
        cwd=ROOT,
        env=env,
        stdout=runtime.service_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    runtime.logger.line(
        f"serve started: pid={runtime.server.pid} url={runtime.base_url} "
        f"log={runtime.service_log}"
    )


def _wait_ready(runtime: Runtime) -> None:
    deadline = time.monotonic() + runtime.config.start_timeout
    headers = {"Authorization": f"Bearer {runtime.token}"}
    while time.monotonic() < deadline:
        if runtime.server is not None and runtime.server.poll() is not None:
            raise RuntimeError(
                "serve exited during startup; see " + str(runtime.service_log)
            )
        try:
            response = httpx.get(
                runtime.base_url + "/api/v1/me",
                headers=headers,
                timeout=2,
                trust_env=False,
            )
            if response.status_code == 200:
                runtime.logger.line("service is ready")
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError("service did not become ready before --start-timeout")


def _client(runtime: Runtime) -> httpx.Client:
    return httpx.Client(
        base_url=runtime.base_url,
        headers={"Authorization": f"Bearer {runtime.token}"},
        timeout=60,
        trust_env=False,
    )


def _configure(runtime: Runtime) -> None:
    with _client(runtime) as client:
        response = client.get("/api/v1/plugins")
        if response.status_code != 200:
            raise RuntimeError(f"plugin list HTTP {response.status_code}")
        plugins = response.json()
        by_key = {(item["category"], item["id"]): item for item in plugins}
        requested = dict(runtime.config.sources)
        for plugin_id in requested:
            if ("source", plugin_id) not in by_key:
                raise RuntimeError(f"unknown source adapter: {plugin_id}")
        for category, plugin_id in sorted(by_key):
            if category != "source":
                continue
            parameters = requested.get(plugin_id, {})
            body = {"enabled": plugin_id in requested, "parameters": parameters}
            response = client.put(f"/api/v1/plugins/source/{plugin_id}", json=body)
            if response.status_code != 200:
                raise RuntimeError(
                    f"enabling source {plugin_id} HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                )
            runtime.logger.line(
                f"source {plugin_id}: {'enabled' if body['enabled'] else 'disabled'}"
            )

        parser_ids = runtime.config.parsers
        if parser_ids is None:
            parser_ids = tuple(
                item["id"] for item in plugins if item["category"] == "parser"
            )
        for plugin_id in parser_ids:
            if ("parser", plugin_id) not in by_key:
                raise RuntimeError(f"unknown task parser: {plugin_id}")
        for category, plugin_id in sorted(by_key):
            if category != "parser":
                continue
            body = {"enabled": plugin_id in parser_ids, "parameters": {}}
            response = client.put(f"/api/v1/plugins/parser/{plugin_id}", json=body)
            if response.status_code != 200:
                raise RuntimeError(
                    f"enabling parser {plugin_id} HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                )
            runtime.logger.line(
                f"parser {plugin_id}: {'enabled' if body['enabled'] else 'disabled'}"
            )

        values = {
            "neo4j_uri": runtime.config.neo4j_uri,
            "neo4j_user": runtime.config.neo4j_user,
            "neo4j_password": runtime.password,
            "milvus_uri": runtime.config.milvus_uri,
            "embedding_url": runtime.config.embedding_url,
            "embedding_model": runtime.config.embedding_model,
            "source_directory": str(runtime.config.source_dir),
            "knowledge_generation": runtime.config.generation,
            "dimension_value_limit": str(runtime.config.dimension_value_limit),
            "embedding_concurrency": str(runtime.config.embedding_concurrency),
            "embedding_timeout_seconds": str(runtime.config.embedding_timeout_seconds),
            "embedding_timeout_per_input_seconds": str(
                runtime.config.embedding_timeout_per_input_seconds
            ),
            "embedding_retries": str(runtime.config.embedding_retries),
            "embedding_backoff_seconds": str(runtime.config.embedding_backoff_seconds),
        }
        response = client.put("/api/v1/settings", json={"values": values})
        if response.status_code != 200:
            raise RuntimeError(
                f"settings HTTP {response.status_code}: {response.text[:500]}"
            )
        runtime.storage_touched = True
        runtime.settings = {
            key: value for key, value in values.items() if key != SECRET_SETTING
        }
        if response.json().get(SECRET_SETTING) != "configured":
            raise RuntimeError("settings response did not hide the Neo4j password")
        runtime.logger.line(
            f"storage configured: generation={runtime.config.generation} "
            f"neo4j={runtime.config.neo4j_uri} milvus={runtime.config.milvus_uri} "
            f"embedding={runtime.config.embedding_url} "
            f"embedding_model={runtime.config.embedding_model}"
        )
        health = client.get("/healthz")
        if health.status_code == 200:
            runtime.health = health.json()
            runtime.logger.line(f"health: {runtime.health}")


def _run_build(runtime: Runtime) -> dict[str, Any]:
    with _client(runtime) as client:
        response = client.post("/api/v1/builds")
        if response.status_code != 202:
            raise RuntimeError(
                f"starting build HTTP {response.status_code}: {response.text[:500]}"
            )
        build = response.json()
        build_id = build["id"]
        runtime.logger.line(f"build {build_id} started")
        started = time.monotonic()
        deadline = started + runtime.config.build_timeout
        last_status = build.get("status")
        while time.monotonic() < deadline:
            response = client.get(f"/api/v1/builds/{build_id}")
            if response.status_code != 200:
                raise RuntimeError(
                    f"build detail HTTP {response.status_code}: {response.text[:500]}"
                )
            build = response.json()
            if build.get("status") != last_status:
                runtime.logger.line(f"build {build_id} status={build['status']}")
                last_status = build.get("status")
            if build.get("status") != "running":
                break
            time.sleep(runtime.config.poll_interval)
        else:
            build = {**build, "timed_out": True}
            runtime.logger.line(
                f"build {build_id} still running after {runtime.config.build_timeout}s"
            )
        if not build.get("timed_out"):
            runtime.logger.line(
                f"build {build_id} {build.get('status')} in "
                f"{round(time.monotonic() - started, 1)}s"
            )
        return {
            "id": build.get("id"),
            "status": build.get("status"),
            "started_at": build.get("started_at"),
            "finished_at": build.get("finished_at"),
            "counts": build.get("counts") or {},
            "error": build.get("error"),
            "parse_failures": build.get("parse_failures") or [],
            "timed_out": bool(build.get("timed_out")),
        }


def _open_mcp(runtime: Runtime) -> McpSession:
    session = McpSession(runtime.base_url, runtime.token or "")
    try:
        session.open(timeout=30)
    except Exception:
        session.close()
        raise
    runtime.mcp = session
    return session


def _run_checks(runtime: Runtime) -> list[dict[str, Any]]:
    checks: list[tuple[str, Any]] = []
    failures: list[dict[str, Any]] = []
    if runtime.config.builtin_checks:
        checks.extend(_builtin_checks())
    if runtime.config.checks:
        try:
            checks.extend(_load_check_functions(runtime.config.checks))
        except Exception as exc:  # noqa: BLE001 - report as a failed check
            failures.append(
                {
                    "name": "load_checks",
                    "ok": False,
                    "info": f"{type(exc).__name__}: {exc}",
                    "elapsed_ms": 0,
                }
            )
    if not checks and not failures:
        runtime.logger.line("checks: none requested")
        return []

    mcp = None
    mcp_error = None
    if checks:
        try:
            mcp = _open_mcp(runtime)
            runtime.logger.line("MCP session open")
        except Exception as exc:  # noqa: BLE001 - REST-only checks may still run
            mcp_error = f"{type(exc).__name__}: {exc}"
            runtime.logger.line(f"MCP session unavailable: {mcp_error}")
    context = CheckContext(runtime, mcp, mcp_error)
    results = list(failures)
    try:
        for name, function in checks:
            results.append(_run_check(name, function, context))
    finally:
        context.close()
        if mcp is not None:
            mcp.close()
            runtime.mcp = None
    return results


def _stop_service(server: subprocess.Popen[str] | None) -> dict[str, Any]:
    if server is None:
        return {"status": "not_started", "process_gone": True}
    if server.poll() is not None:
        return {
            "status": "exited",
            "returncode": server.returncode,
            "process_gone": True,
        }
    server.terminate()
    try:
        server.wait(timeout=15)
    except subprocess.TimeoutExpired:
        server.kill()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            return {"status": "failed", "process_gone": False}
    return {
        "status": "stopped",
        "returncode": server.returncode,
        "process_gone": server.poll() is not None,
    }


def _owned_generations(runtime: Runtime) -> set[str]:
    import sqlite3

    names = {runtime.config.generation}
    if runtime.db_path.exists():
        with sqlite3.connect(runtime.db_path) as db:
            names.update(
                row[0]
                for row in db.execute(
                    "SELECT generation FROM builds WHERE generation IS NOT NULL"
                )
            )
    return names


def _cleanup_neo4j(runtime: Runtime) -> dict[str, Any]:
    try:
        from neo4j import GraphDatabase

        query = "MATCH (n:SDAEntity) WHERE n.generation IN $generations"
        with GraphDatabase.driver(
            runtime.config.neo4j_uri,
            auth=(runtime.config.neo4j_user, runtime.password),
            connection_timeout=10,
        ) as driver:
            before, _, _ = driver.execute_query(
                query + " RETURN count(n) AS n",
                generations=list(_owned_generations(runtime)),
            )
            count = before[0]["n"] if before else 0
            for deletion in (
                " MATCH (n)-[batch_relationship]-() WITH DISTINCT batch_relationship LIMIT $batch_size DELETE batch_relationship RETURN count(*) AS n",
                " WITH n LIMIT $batch_size DETACH DELETE n RETURN count(*) AS n",
            ):
                while True:
                    deleted, _, _ = driver.execute_query(
                        query + deletion,
                        generations=list(_owned_generations(runtime)),
                        batch_size=CLEANUP_BATCH_SIZE,
                    )
                    if not deleted[0]["n"]:
                        break
            after, _, _ = driver.execute_query(
                query + " RETURN count(n) AS n",
                generations=list(_owned_generations(runtime)),
            )
            remaining = after[0]["n"] if after else 0
        status = "deleted" if count else "absent"
        if remaining:
            status = "failed"
        return {"status": status, "deleted": count, "remaining": remaining}
    except Exception as exc:  # noqa: BLE001 - cleanup failure is reported, not raised
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}


def _cleanup_milvus(runtime: Runtime) -> dict[str, Any]:
    collections = {"sda_" + name for name in _owned_generations(runtime)}
    client = None
    try:
        from pymilvus import MilvusClient

        client = MilvusClient(uri=runtime.config.milvus_uri, timeout=10)
        names = set(client.list_collections())
        for collection in collections & names:
            client.drop_collection(collection)
        remaining = collections & set(client.list_collections())
        return {
            "status": "failed"
            if remaining
            else ("dropped" if collections & names else "absent"),
            "remaining": sorted(remaining),
        }
    except Exception as exc:  # noqa: BLE001 - cleanup failure is reported, not raised
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if client is not None:
            client.close()


def _remove_run_dir(config: Config, run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    scratch = config.scratch.resolve()
    if run_dir.parent != scratch or not run_dir.name.startswith("e2e-"):
        return {"status": "refused", "path": str(run_dir)}
    try:
        shutil.rmtree(run_dir)
    except OSError as exc:
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    return {"status": "deleted" if not run_dir.exists() else "failed"}


def _cleanup(runtime: Runtime) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if runtime.mcp is not None:
        try:
            runtime.mcp.close()
            result["mcp"] = "closed"
        except Exception as exc:  # noqa: BLE001
            result["mcp"] = f"failed: {type(exc).__name__}: {exc}"
        runtime.mcp = None
    result["service"] = _stop_service(runtime.server)
    if runtime.service_handle is not None:
        runtime.service_handle.close()
        runtime.service_handle = None
    if runtime.password:
        try:
            result["neo4j"] = _cleanup_neo4j(runtime)
        except Exception as exc:  # noqa: BLE001
            result["neo4j"] = {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        try:
            result["milvus"] = _cleanup_milvus(runtime)
        except Exception as exc:  # noqa: BLE001
            result["milvus"] = {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
    else:
        result["neo4j"] = {"status": "skipped", "reason": "no password"}
        result["milvus"] = {"status": "skipped", "reason": "no password"}
    result["database"] = _remove_run_dir(runtime.config, runtime.run_dir)
    runtime.logger.line(
        f"cleanup: {json.dumps(_json_safe(result), ensure_ascii=False)}"
    )
    return result


def _cleanup_ok(cleanup: dict[str, Any]) -> bool:
    service = cleanup.get("service")
    if isinstance(service, dict):
        if service.get("status") not in {"not_started", "exited", "stopped"}:
            return False
        if not service.get("process_gone", False):
            return False
    for name in ("neo4j", "milvus", "database"):
        entry = cleanup.get(name)
        if isinstance(entry, dict) and entry.get("status") in {"failed", "refused"}:
            return False
    return True


def _build_matches(expected: str, build: dict[str, Any] | None) -> bool:
    if build is None or build.get("timed_out"):
        return False
    if expected == "any":
        return True
    return build.get("status") == expected


def run(config: Config) -> int:
    logger = Logger(config.log)
    run_dir: Path | None = None
    runtime: Runtime | None = None
    report: dict[str, Any] = {
        "generation": config.generation,
        "checks": [],
        "ok": False,
    }
    error: str | None = None
    try:
        run_dir = _create_run_dir(config)
        runtime = Runtime(
            config=config,
            logger=logger,
            run_dir=run_dir,
            db_path=run_dir / "app.db",
            service_log=config.scratch / f"e2e-{config.generation}-serve.log",
        )
        report["run_dir"] = str(run_dir)
        report["service_log"] = str(runtime.service_log)
        logger.line(f"run directory: {run_dir}")
        logger.line(f"generation: {config.generation}")
        runtime.password = _resolve_password(config)
        encryption_key = _new_encryption_key()
        _init_database(runtime, encryption_key)
        _start_service(runtime, encryption_key)
        _wait_ready(runtime)
        _configure(runtime)
        report["health"] = runtime.health
        runtime.build = _run_build(runtime)
        report["build"] = runtime.build
        report["checks"] = _run_checks(runtime)
    except KeyboardInterrupt:
        error = "KeyboardInterrupt"
        logger.line("error: interrupted")
        report["error"] = error
    except Exception as exc:  # noqa: BLE001 - report and clean up every failure
        error = f"{type(exc).__name__}: {exc}"
        logger.line(f"error: {error}")
        report["error"] = error
    finally:
        try:
            if runtime is not None:
                report["cleanup"] = _cleanup(runtime)
            else:
                report["cleanup"] = {
                    "service": {"status": "not_started", "process_gone": True}
                }
                if run_dir is not None:
                    report["cleanup"]["database"] = _remove_run_dir(config, run_dir)
        except Exception as exc:  # noqa: BLE001 - still emit the final result
            report["cleanup"] = {
                "error": f"{type(exc).__name__}: {exc}",
                "service": {"status": "failed", "process_gone": False},
            }
        checks_ok = all(check.get("ok") for check in report["checks"])
        build_ok = _build_matches(
            config.expect_build, runtime.build if runtime else None
        )
        cleanup_ok = _cleanup_ok(report["cleanup"])
        report["ok"] = bool(error is None and build_ok and checks_ok and cleanup_ok)
        report["build_matches"] = build_ok
        report["checks_ok"] = checks_ok
        report["cleanup_ok"] = cleanup_ok
        exit_code = 0 if report["ok"] else 1
        logger.raw(
            "E2E_RESULT "
            + json.dumps(_json_safe(report), ensure_ascii=False, sort_keys=True)
        )
        logger.raw(f"E2E_DONE exit={exit_code}")
    logger.close()
    return exit_code


def _new_encryption_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
