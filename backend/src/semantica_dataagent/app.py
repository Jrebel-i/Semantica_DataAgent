import asyncio
import base64
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials
from mcp.server.fastmcp import FastMCP
from neo4j import AsyncGraphDatabase
from pydantic import BaseModel, Field
from pymilvus import MilvusClient
from sqlalchemy import text, update

from . import knowledge
from .auth import _authenticated, admin, bearer, current_user
from .build_queue import build_queue, enqueue_build
from .db import (
    Build,
    GenerationPointer,
    KnowledgeGeneration,
    PluginConfig,
    Setting,
    Token,
    digest,
    sessions,
)
from .embedding_client import validate_settings
from .scheduler import scheduler


@asynccontextmanager
async def lifespan(application):
    from .agent_sandbox import cleanup_orphans

    if os.environ.get("DATAAGENT_AGENT_MODE", "sandbox") != "local":
        await asyncio.to_thread(cleanup_orphans)
    with sessions().begin() as db:
        db.execute(
            update(Build)
            .where(Build.status == "running")
            .values(
                status="failed",
                error="Build interrupted by application shutdown",
                finished_at=datetime.now(UTC).isoformat(),
            )
        )
        db.execute(
            update(Build)
            .where(Build.status == "queued")
            .values(
                status="cancelled",
                error="Build cancelled because the application restarted before it started",
                finished_at=datetime.now(UTC).isoformat(),
            )
        )
    await asyncio.to_thread(knowledge.cleanup_interrupted_generations)
    build_queue.start()
    with sessions()() as db:
        interval = db.get(Setting, "schedule_interval_seconds")
        scheduler.start(int(interval.value) if interval else 0)
    server = create_mcp()
    application.state.mcp_app = server.streamable_http_app()
    async with server.session_manager.run():
        try:
            yield
        finally:
            scheduler.stop()
            build_queue.stop()
            knowledge.close_readers()


app = FastAPI(
    title="Semantica DataAgent",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
KEYS = {
    "neo4j_uri",
    "neo4j_user",
    "neo4j_password",
    "milvus_uri",
    "embedding_url",
    "embedding_model",
    "source_directory",
    "knowledge_generation",
    "generation_retention",
    "schedule_interval_seconds",
    "dimension_value_limit",
    "embedding_concurrency",
    "embedding_timeout_seconds",
    "embedding_timeout_per_input_seconds",
    "embedding_retries",
    "embedding_backoff_seconds",
    "assistant_agent",
    "assistant_provider_id",
    "assistant_model",
    "assistant_timeout_seconds",
}
SECRET = {"neo4j_password"}


class SettingsUpdate(BaseModel):
    values: dict[str, str | None]


class Status(BaseModel):
    status: Literal["up", "down", "not_configured"]
    elapsed_ms: float


class TokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200, pattern=r"\S")
    role: Literal["user", "admin"]


class TokenInfo(BaseModel):
    id: str
    name: str
    role: Literal["user", "admin"]
    created_at: datetime
    last_used_at: datetime | None
    revoked: bool

    model_config = {"from_attributes": True}


class TokenIssued(TokenInfo):
    token: str


@app.post("/api/v1/tokens", response_model=TokenIssued, status_code=201)
def create_token(body: TokenCreate, _: Annotated[str, Depends(admin)]):
    plaintext = secrets.token_urlsafe(32)
    with sessions().begin() as db:
        row = Token(digest=digest(plaintext), name=body.name, role=body.role)
        db.add(row)
        db.flush()
        return TokenIssued(
            **TokenInfo.model_validate(row).model_dump(), token=plaintext
        )


@app.get("/api/v1/tokens", response_model=list[TokenInfo])
def list_tokens(_: Annotated[str, Depends(admin)]):
    with sessions()() as db:
        return [
            TokenInfo.model_validate(row)
            for row in db.query(Token).order_by(Token.created_at, Token.id).all()
        ]


@app.delete("/api/v1/tokens/{token_id}", status_code=204)
def revoke_token(token_id: str, _: Annotated[str, Depends(admin)]):
    with sessions()() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.query(Token).filter_by(id=token_id).first()
        if row is None:
            raise HTTPException(status_code=404, detail="Token not found")
        if (
            not row.revoked
            and row.role == "admin"
            and db.query(Token).filter_by(role="admin", revoked=False).count() <= 1
        ):
            raise HTTPException(
                status_code=409, detail="Cannot revoke the last active admin token"
            )
        row.revoked = True
        db.commit()


@app.get("/api/v1/me")
def me(role: Annotated[str, Depends(current_user)]) -> dict[str, str]:
    return {"role": role}


@app.get("/api/v1/settings")
def get_settings(_: Annotated[str, Depends(admin)]) -> dict[str, str | None]:
    with sessions()() as db:
        values = {row.key: row.value for row in db.query(Setting).all()}
    return {
        key: ("configured" if key in values else None)
        if key in SECRET
        else values.get(key)
        for key in sorted(KEYS)
    }


@app.put("/api/v1/settings")
def put_settings(
    body: SettingsUpdate, _: Annotated[str, Depends(admin)]
) -> dict[str, str | None]:
    if set(body.values) - KEYS:
        raise HTTPException(status_code=422, detail="Unknown setting")
    try:
        validate_settings(body.values)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    limit = body.values.get("dimension_value_limit")
    if limit and (
        not limit.isascii() or not limit.isdigit() or not 1 <= int(limit) <= 1000000
    ):
        raise HTTPException(status_code=422, detail="Invalid dimension value limit")
    interval = body.values.get("schedule_interval_seconds")
    if interval and (
        not interval.isascii()
        or not interval.isdigit()
        or not 0 <= int(interval) <= 604800
    ):
        raise HTTPException(status_code=422, detail="Invalid schedule interval")
    retention = body.values.get("generation_retention")
    if retention and (
        not retention.isascii()
        or not retention.isdigit()
        or not 1 <= int(retention) <= 100
    ):
        raise HTTPException(status_code=422, detail="Invalid generation retention")
    generation = body.values.get("knowledge_generation")
    if generation and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", generation):
        raise HTTPException(status_code=422, detail="Invalid generation")
    timeout = body.values.get("assistant_timeout_seconds")
    if timeout:
        try:
            seconds = float(timeout)
        except ValueError:
            seconds = 0
        if not 0.1 <= seconds <= 3600:
            raise HTTPException(status_code=422, detail="Invalid assistant timeout")
    for key in ("neo4j_uri", "milvus_uri", "embedding_url"):
        value = body.values.get(key)
        if value:
            try:
                address = urlsplit(value)
                allowed = (
                    {"bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc"}
                    if key == "neo4j_uri"
                    else {"http", "https"}
                )
                if (
                    address.scheme not in allowed
                    or not address.hostname
                    or address.username is not None
                    or address.password is not None
                    or address.query
                    or address.fragment
                ):
                    raise ValueError("Invalid address")
            except ValueError:
                raise HTTPException(
                    status_code=422, detail="Invalid connection address"
                ) from None
    cipher = Fernet(os.environ["DATAAGENT_ENCRYPTION_KEY"].encode())
    with sessions().begin() as db:
        if "knowledge_generation" in body.values and db.get(GenerationPointer, 1):
            raise HTTPException(
                status_code=409, detail="Use the generation switch endpoint"
            )
        for key, value in body.values.items():
            row = db.get(Setting, key)
            if value is None or value == "":
                if row:
                    db.delete(row)
            else:
                stored = (
                    cipher.encrypt(value.encode()).decode() if key in SECRET else value
                )
                if row:
                    row.value = stored
                else:
                    db.add(Setting(key=key, value=stored))
    if "schedule_interval_seconds" in body.values:
        scheduler.configure(int(interval) if interval else 0)
    return get_settings("admin")


async def check_neo4j(values: dict[str, str]):
    password = (
        Fernet(os.environ["DATAAGENT_ENCRYPTION_KEY"].encode())
        .decrypt(values["neo4j_password"].encode())
        .decode()
    )
    async with (
        AsyncGraphDatabase.driver(
            values["neo4j_uri"],
            auth=(values["neo4j_user"], password),
            connection_timeout=2,
        ) as driver,
        driver.session() as session,
    ):
        result = await session.run("RETURN 1")
        await result.consume()


def check_milvus(uri: str):
    client = MilvusClient(uri=uri, timeout=2)
    try:
        client.list_collections(timeout=2)
    finally:
        client.close()


async def probe(name: str, values: dict[str, str]) -> Status:
    required = {
        "neo4j": ("neo4j_uri", "neo4j_user", "neo4j_password"),
        "milvus": ("milvus_uri",),
        "embedding": ("embedding_url",),
    }[name]
    if not all(values.get(key) for key in required):
        return Status(status="not_configured", elapsed_ms=0)
    start = time.monotonic()
    try:
        async with asyncio.timeout(3):
            if name == "neo4j":
                await check_neo4j(values)
            elif name == "milvus":
                await asyncio.to_thread(check_milvus, values["milvus_uri"])
            else:
                async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
                    response = await client.get(
                        values["embedding_url"].rstrip("/") + "/health"
                    )
                    response.raise_for_status()
        status = "up"
    except Exception:  # noqa: BLE001 - each external probe must fail independently
        status = "down"
    return Status(status=status, elapsed_ms=round((time.monotonic() - start) * 1000, 2))


@app.get("/healthz")
async def health() -> dict[str, Status]:
    with sessions()() as db:
        values = {row.key: row.value for row in db.query(Setting).all()}
    names = ("neo4j", "milvus", "embedding")
    results = await asyncio.gather(*(probe(name, values) for name in names))
    return dict(zip(names, results, strict=True))


class BuildLog(BaseModel):
    timestamp: datetime
    stage: str
    level: Literal["info", "error"]
    message: str


class BuildView(BaseModel):
    id: int
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    started_at: datetime
    finished_at: datetime | None
    counts: dict[str, int]
    error: str | None
    parse_failures: list[dict[str, str]]
    build_type: Literal["full", "incremental"]
    triggered_by: str | None
    source_commit: str | None
    logs: list[BuildLog]


class BuildRequest(BaseModel):
    build_type: Literal["full", "incremental"] = "full"


def build_logs(value: str | None) -> list[BuildLog]:
    try:
        entries = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(entries, list):
        return []
    return [
        BuildLog.model_validate(entry) for entry in entries if isinstance(entry, dict)
    ]


def build_view(row: Build) -> BuildView:
    def utc(value):
        if value is None:
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    return BuildView(
        id=row.id,
        status=row.status,
        started_at=utc(row.started_at),
        finished_at=utc(row.finished_at),
        counts=json.loads(row.counts),
        error=row.error,
        parse_failures=json.loads(row.parse_failures or "[]"),
        build_type=row.build_type or "full",
        triggered_by=row.triggered_by,
        source_commit=row.source_commit,
        logs=build_logs(row.logs),
    )


class GenerationView(BaseModel):
    name: str
    current: bool
    created_at: str | None
    source_commit: str | None
    counts: dict[str, int]


class GenerationsView(BaseModel):
    generations: list[GenerationView]
    source_head: str | None
    behind: bool


@app.get("/api/v1/generations", response_model=GenerationsView)
def list_generations(_: Annotated[str, Depends(current_user)]):
    from pathlib import Path

    from .source_directory import git

    values = knowledge.settings()
    head = None
    try:
        head = (
            git(Path(values["source_directory"]), "rev-parse", "--verify", "HEAD")
            .decode()
            .strip()
        )
    except (KeyError, OSError, ValueError, RuntimeError):
        pass
    with sessions()() as db:
        pointer = db.get(GenerationPointer, 1)
        rows = (
            db.query(KnowledgeGeneration)
            .order_by(KnowledgeGeneration.created_at.desc())
            .all()
        )
        result = []
        for row in rows:
            build = db.get(Build, row.build_id) if row.build_id else None
            result.append(
                GenerationView(
                    name=row.name,
                    current=bool(pointer and row.name == pointer.generation),
                    created_at=row.created_at,
                    source_commit=build.source_commit if build else None,
                    counts=json.loads(build.counts) if build else {},
                )
            )
        current = next((row for row in result if row.current), None)
        return GenerationsView(
            generations=result,
            source_head=head,
            behind=bool(
                head
                and current
                and current.source_commit
                and head != current.source_commit
            ),
        )


@app.post("/api/v1/generations/{name}/switch", response_model=GenerationView)
def switch_generation(name: str, _: Annotated[str, Depends(admin)]):
    with sessions().begin() as db:
        row = db.get(KnowledgeGeneration, name)
        if row is None:
            raise HTTPException(status_code=404, detail="Generation not found")
        if row.deleting:
            raise HTTPException(status_code=409, detail="Generation is being deleted")
        pointer = db.get(GenerationPointer, 1)
        if pointer is None:
            db.add(GenerationPointer(id=1, generation=name))
        else:
            pointer.generation = name
        build = db.get(Build, row.build_id) if row.build_id else None
        return GenerationView(
            name=name,
            current=True,
            created_at=row.created_at,
            source_commit=build.source_commit if build else None,
            counts=json.loads(build.counts) if build else {},
        )


@app.post("/api/v1/builds", status_code=202, response_model=BuildView)
def start_build(
    _: Annotated[str, Depends(admin)],
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)],
    body: BuildRequest | None = None,
):
    build_type = body.build_type if body else "full"
    with sessions()() as db:
        token_name = db.get(Token, digest(credentials.credentials)).name
    build_id = enqueue_build(build_type, token_name, notify=False)
    with sessions()() as db:
        result = build_view(db.get(Build, build_id))
    build_queue.enqueue()
    return result


@app.get("/api/v1/builds", response_model=list[BuildView])
def builds(_: Annotated[str, Depends(current_user)]):
    with sessions()() as db:
        return [
            build_view(row) for row in db.query(Build).order_by(Build.id.desc()).all()
        ]


@app.get("/api/v1/builds/{build_id}", response_model=BuildView)
def build_detail(build_id: int, _: Annotated[str, Depends(current_user)]):
    with sessions()() as db:
        row = db.get(Build, build_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Build not found")
        return build_view(row)


from .dimension_api import lookup_dim_values
from .sql_validation import find_join_path, validate_sql
from .table_api import describe_tables, search_knowledge
from .table_api import router as table_router
from .task_api import get_lineage, read_task_sql
from .task_api import router as task_router

app.include_router(table_router)
app.include_router(task_router)

from .document_api import list_categories, read_category_index, read_document
from .document_api import router as document_router

app.include_router(document_router)

from .skill_api import router as skill_router

app.include_router(skill_router)

from .provider_api import router as provider_router

app.include_router(provider_router)

from .conversation_api import router as conversation_router

app.include_router(conversation_router)

from .ask_api import ask_dataagent
from .clarification import request_clarification


def create_mcp():
    server = FastMCP(
        "Semantica DataAgent",
        host=os.environ.get("DATAAGENT_LISTEN", "127.0.0.1:8000").rsplit(":", 1)[0],
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
    )
    server.tool()(describe_tables)
    server.tool()(search_knowledge)
    server.tool()(lookup_dim_values)
    server.tool()(list_categories)
    server.tool()(read_category_index)
    server.tool()(read_document)
    server.tool()(get_lineage)
    server.tool()(read_task_sql)
    server.tool()(validate_sql)
    server.tool()(find_join_path)
    server.tool()(request_clarification)
    server.tool()(ask_dataagent)

    from .skill import register_prompt

    register_prompt(server)

    def get_capabilities() -> dict:
        from importlib.metadata import version

        return {
            "application_version": version("semantica-dataagent"),
            "schema_version": "v1",
            "ranking_version": knowledge.RANKING_VERSION,
            "tools": sorted(t.name for t in server._tool_manager.list_tools()),
            "prompts": sorted(
                prompt.name for prompt in server._prompt_manager.list_prompts()
            ),
            "current_generation": knowledge.settings()["knowledge_generation"],
        }

    server.tool()(get_capabilities)
    from .mcp_contract import install_contract

    install_contract(server)
    return server


@app.api_route("/mcp", methods=["GET", "POST", "DELETE"], include_in_schema=False)
def mcp_redirect(_: Annotated[str, Depends(current_user)]):
    return RedirectResponse("/mcp/", status_code=307)


class MCPAuthentication:
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            request = Request(scope, receive)
            try:
                credentials = await bearer(request)
                requester = _authenticated(credentials)
            except HTTPException as exc:
                await JSONResponse(
                    {"detail": exc.detail},
                    status_code=exc.status_code,
                    headers=exc.headers,
                )(scope, receive, send)
                return
            from .mcp_context import CONVERSATION_HEADER, bind_mcp_context

            with bind_mcp_context(requester, request.headers.get(CONVERSATION_HEADER)):
                await self.inner.state.mcp_app(scope, receive, send)
            return
        await self.inner.state.mcp_app(scope, receive, send)


app.mount("/mcp", MCPAuthentication(app))


class PluginView(BaseModel):
    category: str
    id: str
    version: str
    display_name: str
    parameters_schema: dict
    enabled: bool
    parameters: dict


class PluginUpdate(BaseModel):
    enabled: bool
    parameters: dict


class FilterRules(BaseModel):
    database_include: list[str] = []
    database_exclude: list[str] = []
    task_include: list[str] = []
    task_exclude: list[str] = []


@app.get("/api/v1/plugins", response_model=list[PluginView])
def list_plugins(_: Annotated[str, Depends(admin)]):
    from .plugins import configured

    return [
        PluginView(
            category=category,
            id=plugin.id,
            version=plugin.version,
            display_name=plugin.display_name,
            parameters_schema=plugin.parameters_schema,
            enabled=enabled,
            parameters=parameters,
        )
        for category, plugin, enabled, parameters in configured()
    ]


@app.put("/api/v1/plugins/{category}/{plugin_id}", response_model=PluginView)
def update_plugin(
    category: str, plugin_id: str, body: PluginUpdate, _: Annotated[str, Depends(admin)]
):
    from jsonschema import Draft202012Validator

    from .db import PluginConfig
    from .plugins import discover

    plugin = discover().get((category, plugin_id))
    if plugin is None:
        raise HTTPException(404, "Plugin not found")
    errors = sorted(
        Draft202012Validator(plugin.parameters_schema).iter_errors(body.parameters),
        key=lambda e: list(map(str, e.absolute_path)),
    )
    if errors:
        raise HTTPException(
            422,
            detail=[
                {"path": list(error.absolute_path), "message": error.message}
                for error in errors
            ],
        )
    with sessions().begin() as db:
        row = db.get(PluginConfig, (category, plugin_id))
        if row is None:
            row = PluginConfig(
                category=category,
                plugin_id=plugin_id,
                enabled=body.enabled,
                parameters=json.dumps(body.parameters),
            )
            db.add(row)
        else:
            row.enabled = body.enabled
            row.parameters = json.dumps(body.parameters)
    return PluginView(
        category=category,
        id=plugin.id,
        version=plugin.version,
        display_name=plugin.display_name,
        parameters_schema=plugin.parameters_schema,
        enabled=body.enabled,
        parameters=body.parameters,
    )


@app.get("/api/v1/filters", response_model=FilterRules)
def read_filters(_: Annotated[str, Depends(admin)]):
    from .filters import get_filters

    return get_filters()


@app.put("/api/v1/filters", response_model=FilterRules)
def write_filters(body: FilterRules, _: Annotated[str, Depends(admin)]):
    with sessions().begin() as db:
        row = db.get(Setting, "ingestion_filters")
        if row is None:
            db.add(Setting(key="ingestion_filters", value=body.model_dump_json()))
        else:
            row.value = body.model_dump_json()
    return body


class SourceFileUpload(BaseModel):
    path: str
    content_base64: str


class SourceUpload(BaseModel):
    target: str
    files: list[SourceFileUpload] = []
    archive_base64: str | None = None
    replace: bool = False


class SourceCommit(BaseModel):
    commit: str


class SourceContent(BaseModel):
    path: str
    content: str
    encoding: Literal["utf-8", "base64"]


class SourceHistory(BaseModel):
    commit: str
    author: str
    date: datetime
    message: str


class SourceDiff(BaseModel):
    diff: str


def source_path():
    with sessions()() as db:
        value = db.get(Setting, "source_directory")
        if value is None or not value.value:
            raise HTTPException(422, "Source directory is not configured")
        return value.value


@contextmanager
def source_repository():
    from . import source_directory

    try:
        with source_directory.repository(source_path()) as root:
            yield root
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/v1/source/upload", response_model=SourceCommit)
def upload_sources(
    body: SourceUpload,
    _: Annotated[str, Depends(admin)],
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)],
):
    from . import source_directory as source

    if body.archive_base64 is not None and body.files:
        raise HTTPException(422, "Choose files or an archive")
    try:
        if body.archive_base64 is not None:
            files = source.unpack_archive(source.decode(body.archive_base64))
            target = source.relative(body.target)
            prefix = target.name + "/"
            if (
                body.replace
                and files
                and all(name.startswith(prefix) for name in files)
            ):
                files = {name[len(prefix) :]: data for name, data in files.items()}
        else:
            if len(body.files) > source.MAX_FILES or sum(
                len(item.content_base64) for item in body.files
            ) > (source.MAX_BYTES + 2) // 3 * 4 + 4 * len(body.files):
                raise ValueError("Upload too large")
            files = {
                item.path: source.decode(item.content_base64) for item in body.files
            }
            if len(files) != len(body.files):
                raise ValueError("Duplicate path")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    with sessions()() as db:
        user = db.get(Token, digest(credentials.credentials))
        author = user.name
        config = db.get(PluginConfig, ("source", "markdown"))
        category_root = (
            json.loads(config.parameters).get("path", "knowledge")
            if config
            else "knowledge"
        )
    with source_repository() as root:
        return SourceCommit(
            commit=source.upload(
                root, body.target, files, body.replace, author, category_root
            )
        )


@app.get("/api/v1/source/files", response_model=list[str])
def source_files(_: Annotated[str, Depends(admin)]):
    from . import source_directory as source

    with source_repository() as root:
        return source.list_files(root)


@app.get("/api/v1/source/file", response_model=SourceContent)
def source_file(path: str, _: Annotated[str, Depends(admin)]):
    from . import source_directory as source

    with source_repository() as root:
        file = source.safe(root, source.relative(path))
        if not file.is_file():
            raise HTTPException(404, "Source file not found")
        raw = file.read_bytes()
        try:
            content = raw.decode("utf-8")
            if "\x00" in content:
                raise UnicodeDecodeError("utf-8", raw, 0, 1, "Binary content")
            encoding = "utf-8"
        except UnicodeDecodeError:
            content = base64.b64encode(raw).decode("ascii")
            encoding = "base64"
        return SourceContent(path=path, content=content, encoding=encoding)


@app.get("/api/v1/source/history", response_model=list[SourceHistory])
def source_history(path: str, _: Annotated[str, Depends(admin)]):
    from . import source_directory as source

    with source_repository() as root:
        return source.history(root, path)


@app.get("/api/v1/source/diff", response_model=SourceDiff)
def source_diff(path: str, before: str, after: str, _: Annotated[str, Depends(admin)]):
    from . import source_directory as source

    with source_repository() as root:
        return SourceDiff(diff=source.diff(root, path, before, after))
