import asyncio
import base64
import json
import logging
import math
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

from . import knowledge, project_context
from .api_time import UtcDatetime
from .auth import admin, authenticate_mcp, bearer, current_user
from .build_queue import build_queue, recover_interrupted_builds
from .db import (
    Build,
    GenerationPointer,
    KnowledgeGeneration,
    Project,
    ProjectMember,
    Setting,
    Token,
    digest,
    generation_pointer,
    sessions,
)
from .embedding_client import validate_settings
from .project_api import (
    admin_project_scope,
    ensure_default_project_member,
    project_scope,
)
from .project_context import ProjectContext
from .scheduler import pull_scheduler, scheduler


@asynccontextmanager
async def lifespan(application):
    from .agent_sandbox import cleanup_orphans
    from .conversation_api import cleanup_expired_sessions
    from .db import QueryAudit, QueryExecution, Turn

    await asyncio.to_thread(project_context.recover_projects)

    if os.environ.get("DATAAGENT_AGENT_MODE", "sandbox") != "local":
        await asyncio.to_thread(cleanup_orphans)
    with sessions().begin() as db:
        db.execute(
            update(QueryExecution)
            .where(QueryExecution.status == "running")
            .values(status="failed")
        )
        db.execute(
            update(QueryAudit)
            .where(QueryAudit.status == "running")
            .values(status="interrupted")
        )
        db.execute(
            update(Turn)
            .where(Turn.status == "running")
            .values(
                status="failed",
                error="Application restarted",
                finished_at=datetime.now(UTC),
            )
        )
    await asyncio.to_thread(cleanup_expired_sessions)

    async def cleanup_loop():
        while True:
            await asyncio.sleep(3600)
            try:
                await asyncio.to_thread(cleanup_expired_sessions)
            except Exception:
                import logging

                logging.getLogger(__name__).exception("Session cleanup failed")

    cleanup_task = asyncio.create_task(cleanup_loop())

    await asyncio.to_thread(recover_interrupted_builds)
    await asyncio.to_thread(knowledge.cleanup_interrupted_generations)
    from .corrections import recover_applying

    await asyncio.to_thread(recover_applying)
    from .pull_service import recover_pulls

    try:
        await asyncio.to_thread(recover_pulls)
    except Exception:  # retry recovery on the next startup
        import logging

        logging.getLogger(__name__).exception("Pull recovery failed")
    build_queue.start()
    project_context.start_schedule()
    from .pull_service import interval as pull_interval

    pull_scheduler.start(pull_interval())
    server = create_mcp()
    application.state.mcp_app = server.streamable_http_app()
    async with server.session_manager.run():
        try:
            yield
        finally:
            cleanup_task.cancel()
            await asyncio.gather(cleanup_task, return_exceptions=True)
            pull_scheduler.stop()
            scheduler.stop()
            build_queue.stop()
            knowledge.close_readers()


class _RedactDownloadAccess(logging.Filter):
    """Keep bearer link paths out of uvicorn access logs."""

    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) == 5:
            client, method, path, version, status = record.args
            if "/download-link/" in path:
                path = path.split("/download-link/", 1)[0] + "/download-link/[redacted]"
                record.args = (client, method, path, version, status)
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactDownloadAccess())


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
    "session_retention_days",
    "embedding_concurrency",
    "embedding_timeout_seconds",
    "embedding_timeout_per_input_seconds",
    "embedding_retries",
    "embedding_backoff_seconds",
    "embedding_cache_max_mb",
    "build_concurrency",
    "assistant_agent",
    "assistant_provider_id",
    "assistant_model",
    "assistant_timeout_seconds",
    "assistant_agent_profiles",
    "langfuse_enabled",
    "langfuse_url",
    "langfuse_public_key",
    "langfuse_secret_key",
} | set(project_context.PROJECT_SETTING_KEYS)
SECRET = {"langfuse_secret_key", "neo4j_password"}


class SettingsUpdate(BaseModel):
    values: dict[str, str | None]


class Status(BaseModel):
    status: Literal["up", "down", "not_configured"]
    elapsed_ms: float


class TokenProject(BaseModel):
    model_config = {"extra": "forbid"}

    project_id: str
    role: Literal["admin", "member"]


class TokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200, pattern=r"\S")
    role: Literal["user", "admin"]
    projects: list[TokenProject] | None = None


class TokenProjectView(BaseModel):
    project_id: str
    role: Literal["admin", "member"]


class TokenInfo(BaseModel):
    id: str
    name: str
    role: Literal["user", "admin"]
    created_at: UtcDatetime
    last_used_at: UtcDatetime | None
    revoked: bool
    projects: list[TokenProjectView]

    model_config = {"from_attributes": True}


class TokenIssued(TokenInfo):
    token: str


def token_view(db, row: Token) -> TokenInfo:
    projects = [
        TokenProjectView(project_id=member.project_id, role=member.role)
        for member in db.query(ProjectMember)
        .filter(ProjectMember.token_id == row.id)
        .order_by(ProjectMember.project_id)
        .all()
    ]
    return TokenInfo(
        id=row.id,
        name=row.name,
        role=row.role,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        revoked=row.revoked,
        projects=projects,
    )


@app.post("/api/v1/tokens", response_model=TokenIssued, status_code=201)
def create_token(body: TokenCreate, _: Annotated[str, Depends(admin)]):
    plaintext = secrets.token_urlsafe(32)
    with sessions().begin() as db:
        row = Token(digest=digest(plaintext), name=body.name, role=body.role)
        db.add(row)
        db.flush()
        if body.role == "user":
            if body.projects is None:
                ensure_default_project_member(db, row)
            else:
                requested: set[str] = set()
                for item in body.projects:
                    if item.project_id in requested:
                        raise HTTPException(
                            status_code=422,
                            detail="Duplicate project in token membership",
                        )
                    requested.add(item.project_id)
                    if db.get(Project, item.project_id) is None:
                        raise HTTPException(
                            status_code=422,
                            detail=f"Project not found: {item.project_id}",
                        )
                    db.add(
                        ProjectMember(
                            project_id=item.project_id,
                            token_id=row.id,
                            role=item.role,
                        )
                    )
        return TokenIssued(**token_view(db, row).model_dump(), token=plaintext)


@app.get("/api/v1/tokens", response_model=list[TokenInfo])
def list_tokens(_: Annotated[str, Depends(admin)]):
    with sessions()() as db:
        return [
            token_view(db, row)
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


@app.get("/api/v1/assistant-defaults")
def assistant_defaults(
    _: Annotated[str, Depends(current_user)],
) -> dict[str, str | None]:
    from .ask_api import _configured

    result = {
        key: _configured(key)
        for key in ("assistant_agent", "assistant_provider_id", "assistant_model")
    }
    with sessions()() as db:
        row = db.get(Setting, "assistant_agent_profiles")
        result["assistant_agent_profiles"] = row.value if row else None
    return result


@app.get("/api/v1/settings")
def get_settings(_: Annotated[str, Depends(admin)]) -> dict[str, str | None]:
    with sessions()() as db:
        values = {row.key: row.value for row in db.query(Setting).all()}
    if not values.get("neo4j_password") and project_context.neo4j_password(values):
        values["neo4j_password"] = "configured"
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
    profiles = body.values.get("assistant_agent_profiles")
    if profiles:
        try:
            parsed = json.loads(profiles)
            if not isinstance(parsed, dict) or any(
                not isinstance(name, str)
                or not isinstance(profile, dict)
                or set(profile) != {"providers", "default_provider", "default_model"}
                or not isinstance(profile["providers"], list)
                or any(not isinstance(value, str) for value in profile["providers"])
                or not isinstance(profile["default_provider"], str)
                or not isinstance(profile["default_model"], str)
                or (
                    profile["default_provider"]
                    and profile["default_provider"] not in profile["providers"]
                )
                for name, profile in parsed.items()
            ):
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=422, detail="Invalid agent profiles"
            ) from None
    limit = body.values.get(project_context.DIMENSION_VALUE_LIMIT_KEY)
    if limit and (
        not limit.isascii() or not limit.isdigit() or not 1 <= int(limit) <= 1000000
    ):
        raise HTTPException(status_code=422, detail="Invalid dimension value limit")
    interval = body.values.get(project_context.SCHEDULE_INTERVAL_KEY)
    if interval and (
        not interval.isascii()
        or not interval.isdigit()
        or not 0 <= int(interval) <= 604800
    ):
        raise HTTPException(status_code=422, detail="Invalid schedule interval")
    concurrency = body.values.get("build_concurrency")
    if concurrency not in (None, "") and (
        not concurrency.isascii()
        or not concurrency.isdigit()
        or not 1 <= int(concurrency) <= 16
    ):
        raise HTTPException(status_code=422, detail="Invalid build concurrency")
    cache_mb = body.values.get("embedding_cache_max_mb")
    if cache_mb not in (None, ""):
        try:
            cache_size = float(cache_mb)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=422, detail="Invalid embedding_cache_max_mb"
            ) from None
        if not math.isfinite(cache_size) or not 0 <= cache_size <= 1000000:
            raise HTTPException(
                status_code=422, detail="Invalid embedding_cache_max_mb"
            )
    retention = body.values.get(project_context.GENERATION_RETENTION_KEY)
    if retention and (
        not retention.isascii()
        or not retention.isdigit()
        or not 1 <= int(retention) <= 100
    ):
        raise HTTPException(status_code=422, detail="Invalid generation retention")
    session_retention = body.values.get("session_retention_days")
    if session_retention and (
        not session_retention.isascii()
        or not session_retention.isdigit()
        or not 1 <= int(session_retention) <= 36500
    ):
        raise HTTPException(status_code=422, detail="Invalid session retention")
    generation = body.values.get(project_context.GENERATION_KEY)
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
    if body.values.get("langfuse_enabled") not in (None, "", "true", "false"):
        raise HTTPException(status_code=422, detail="Invalid Langfuse switch")
    for key in ("neo4j_uri", "milvus_uri", "embedding_url", "langfuse_url"):
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
        if project_context.GENERATION_KEY in body.values and generation_pointer(
            db, project_context.DEFAULT_PROJECT_ID
        ):
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
    if project_context.SCHEDULE_INTERVAL_KEY in body.values:
        project_context.configure_schedule(int(interval) if interval else 0)
    return get_settings("admin")


async def check_neo4j(values: dict[str, str]):
    password = project_context.neo4j_password(values)
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
        "neo4j": ("neo4j_uri", "neo4j_user"),
        "milvus": ("milvus_uri",),
        "embedding": ("embedding_url",),
    }[name]
    if not all(values.get(key) for key in required) or (
        name == "neo4j"
        and not values.get("neo4j_password")
        and not project_context.neo4j_password(values)
    ):
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
    timestamp: UtcDatetime
    stage: str
    level: Literal["info", "error"]
    message: str


class BuildView(BaseModel):
    id: int
    project_id: str
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    started_at: UtcDatetime
    finished_at: UtcDatetime | None
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
    return BuildView(
        id=row.id,
        project_id=row.project_id or "default",
        status=row.status,
        started_at=row.started_at,
        finished_at=row.finished_at,
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
    created_at: UtcDatetime | None
    source_commit: str | None
    counts: dict[str, int]


class GenerationsView(BaseModel):
    generations: list[GenerationView]
    source_head: str | None
    behind: bool


@app.get("/api/v1/generations", response_model=GenerationsView)
def list_generations(
    _: Annotated[str, Depends(current_user)],
    scope: Annotated[ProjectContext, Depends(project_scope)],
):

    from .source_directory import git

    values = knowledge.settings(scope.project_id)
    project = project_context.from_values(values)
    head = None
    try:
        head = (
            git(project.source_directory, "rev-parse", "--verify", "HEAD")
            .decode()
            .strip()
        )
    except (KeyError, OSError, ValueError, RuntimeError):
        pass
    with sessions()() as db:
        pointer = generation_pointer(db, project.project_id)
        rows = (
            db.query(KnowledgeGeneration)
            .filter(KnowledgeGeneration.project_id == project.project_id)
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
def switch_generation(
    name: str,
    scope: Annotated[ProjectContext, Depends(admin_project_scope)],
):
    with sessions().begin() as db:
        row = db.get(KnowledgeGeneration, name)
        if row is None or row.project_id != scope.project_id:
            raise HTTPException(status_code=404, detail="Generation not found")
        if row.deleting:
            raise HTTPException(status_code=409, detail="Generation is being deleted")
        pointer = generation_pointer(db, scope.project_id)
        if pointer is None:
            db.add(GenerationPointer(project_id=scope.project_id, generation=name))
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
    scope: Annotated[ProjectContext, Depends(admin_project_scope)],
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)],
    body: BuildRequest | None = None,
):
    build_type = body.build_type if body else "full"
    with sessions()() as db:
        token_name = db.get(Token, digest(credentials.credentials)).name
    build_id = project_context.enqueue_build(
        build_type, token_name, project_id=scope.project_id, notify=False
    )
    with sessions()() as db:
        result = build_view(db.get(Build, build_id))
    project_context.wake_build_queue()
    return result


@app.get("/api/v1/builds", response_model=list[BuildView])
def builds(
    _: Annotated[str, Depends(current_user)],
    scope: Annotated[ProjectContext, Depends(project_scope)],
):
    with sessions()() as db:
        return [
            build_view(row)
            for row in db.query(Build)
            .filter(Build.project_id == scope.project_id)
            .order_by(Build.id.desc())
            .all()
        ]


@app.get("/api/v1/builds/{build_id}", response_model=BuildView)
def build_detail(
    build_id: int,
    _: Annotated[str, Depends(current_user)],
    scope: Annotated[ProjectContext, Depends(project_scope)],
):
    with sessions()() as db:
        row = db.get(Build, build_id)
        if row is None or (row.project_id or "default") != scope.project_id:
            raise HTTPException(status_code=404, detail="Build not found")
        return build_view(row)


from .dimension_api import lookup_dim_values as _lookup_dim_values
from .personal_settings import router as personal_settings_router
from .query_api import execute_query as _execute_query
from .query_api import propose_query as _propose_query
from .query_api import router as query_router
from .sample_api import DEFAULT_LIMIT as _SAMPLE_DEFAULT_LIMIT
from .sample_api import MAX_LIMIT as _SAMPLE_MAX_LIMIT
from .sample_api import sample_table_data as _sample_table_data
from .sql_validation import find_join_path as _find_join_path
from .sql_validation import validate_sql as _validate_sql
from .table_api import describe_tables as _describe_tables
from .table_api import router as table_router
from .table_api import search_knowledge as _search_knowledge
from .task_api import get_lineage as _get_lineage
from .task_api import read_task_sql as _read_task_sql
from .task_api import router as task_router

app.include_router(query_router)
app.include_router(personal_settings_router)
app.include_router(table_router)
app.include_router(task_router)

from .document_api import list_categories as _list_categories
from .document_api import read_category_index as _read_category_index
from .document_api import read_document as _read_document
from .document_api import router as document_router

app.include_router(document_router)

from .skill_api import router as skill_router

app.include_router(skill_router)

from .provider_api import router as provider_router

app.include_router(provider_router)

from .conversation_api import router as conversation_router

app.include_router(conversation_router)

from .ask_api import ask_dataagent as _ask_dataagent
from .clarification import Questions
from .clarification import request_clarification as _request_clarification
from .corrections import Text as CorrectionText
from .corrections import propose_correction as _propose_correction
from .corrections import router as corrections_router

app.include_router(corrections_router)
from .gaps import Text as GapText
from .gaps import report_knowledge_gap as _report_knowledge_gap
from .gaps import router as gaps_router

app.include_router(gaps_router)

from .project_api import router as projects_router

app.include_router(projects_router)


def _mcp_project_id() -> str:
    """Project bound to this MCP request by the transport middleware."""
    from .mcp_context import current_project_id

    return current_project_id()


def _mcp_project_arg() -> str | None:
    """Project argument that preserves the historical default-project calls."""
    project_id = _mcp_project_id()
    return None if project_id == project_context.DEFAULT_PROJECT_ID else project_id


def describe_tables(names: list[str]) -> list[dict]:
    """Describe tables in the caller's project, including fields and documents."""
    return _describe_tables(names, project_id=_mcp_project_arg())


def search_knowledge(
    query: str,
    scope: Literal["tables", "fields", "docs", "tasks", "all"],
    top_k: int = 5,
    category: str | None = None,
) -> list[dict]:
    """Search one project's tables, fields, tasks and knowledge documents."""
    return _search_knowledge(
        query,
        scope,
        top_k,
        category,
        project_id=_mcp_project_arg(),
    )


def lookup_dim_values(table: str, keyword: str, field: str | None = None) -> list[dict]:
    """Look up indexed dimension values in the caller's project."""
    return _lookup_dim_values(table, keyword, field, project_id=_mcp_project_arg())


def list_categories() -> list[dict]:
    """List knowledge categories in the caller's project."""
    return _list_categories(project_id=_mcp_project_arg())


def read_category_index(category: str) -> dict:
    """Read one knowledge category's index document."""
    return _read_category_index(category, project_id=_mcp_project_arg())


def read_document(doc_id: str, section: str | None = None) -> dict:
    """Read a knowledge document from the caller's project."""
    return _read_document(doc_id, section, project_id=_mcp_project_arg())


def get_lineage(
    table: str,
    direction: Literal["up", "down"] = "up",
    depth: int = 1,
    column: str | None = None,
) -> list[dict]:
    """Find table or field lineage in the caller's project."""
    return _get_lineage(
        table,
        direction,
        depth,
        column,
        project_id=_mcp_project_arg(),
    )


def read_task_sql(task_id: str) -> dict:
    """Read a task's SQL from the caller's project."""
    return _read_task_sql(task_id, project_id=_mcp_project_arg())


def validate_sql(sql: str, dialect: Literal["hive", "spark", "doris"] = "hive") -> dict:
    """Validate SQL against the caller's project metadata."""
    return _validate_sql(sql, dialect, project_id=_mcp_project_arg())


def find_join_path(tables: list[str]) -> dict:
    """Find a join path between tables in the caller's project."""
    return _find_join_path(tables, project_id=_mcp_project_arg())


def request_clarification(questions: Questions) -> dict:
    """Ask the questioner to pick an evidence-backed interpretation."""
    return _request_clarification(questions, project_id=_mcp_project_arg())


def report_knowledge_gap(question: GapText, missing: GapText) -> dict:
    """Record missing knowledge in the caller's project."""
    return _report_knowledge_gap(question, missing, project_id=_mcp_project_id())


def propose_correction(
    target: CorrectionText, patch: CorrectionText, reason: CorrectionText
) -> dict:
    """Propose a correction in the caller's project."""
    return _propose_correction(target, patch, reason, project_id=_mcp_project_id())


async def ask_dataagent(question: str, conversation_id: str | None = None) -> dict:
    """Ask the configured online agent; only the default project is supported."""
    return await _ask_dataagent(
        question, conversation_id, project_id=_mcp_project_arg()
    )


async def sample_table_data(
    table: Annotated[str, Field(min_length=1)],
    columns: Annotated[list[str] | None, Field(default=None)] = None,
    limit: Annotated[
        int, Field(default=_SAMPLE_DEFAULT_LIMIT, ge=1, le=_SAMPLE_MAX_LIMIT)
    ] = _SAMPLE_DEFAULT_LIMIT,
) -> dict:
    """Return a few real rows from one table in the caller's project."""
    return await _sample_table_data(
        table, columns, limit, project_id=_mcp_project_arg()
    )


async def propose_query(sql: str) -> dict:
    """Propose a validated Doris SQL query for the questioner to confirm."""
    return await _propose_query(sql, project_id=_mcp_project_arg())


async def execute_query(
    sql: str | None = None, confirmation_id: str | None = None
) -> dict:
    """Plan SQL, then execute only after the user explicitly confirms the plan."""
    return await _execute_query(sql, confirmation_id, project_id=_mcp_project_arg())


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
    server.tool()(report_knowledge_gap)
    server.tool()(propose_correction)
    server.tool()(ask_dataagent)
    server.tool()(sample_table_data)
    server.tool()(propose_query)
    server.tool()(execute_query)

    from .skill import register_prompt

    register_prompt(server)

    def get_capabilities() -> dict:
        from importlib.metadata import version

        from .mcp_context import current_requester
        from .project_api import accessible_projects

        project_id = _mcp_project_id()
        values = (
            knowledge.settings()
            if project_id == project_context.DEFAULT_PROJECT_ID
            else knowledge.settings(project_id)
        )
        project = project_context.from_values(values, project_id)
        name, project_type = project.name, project.project_type
        try:
            with sessions()() as db:
                row = db.get(Project, project_id)
                if row is not None:
                    name, project_type = row.name, row.type
        except Exception:  # noqa: BLE001, S110 - metadata is advisory
            pass
        try:
            projects = accessible_projects(current_requester())
        except Exception:  # noqa: BLE001 - metadata must not break capabilities
            projects = []
        return {
            "application_version": version("semantica-dataagent"),
            "schema_version": "v1",
            "ranking_version": knowledge.RANKING_VERSION,
            "tools": sorted(t.name for t in server._tool_manager.list_tools()),
            "prompts": sorted(
                prompt.name for prompt in server._prompt_manager.list_prompts()
            ),
            "current_generation": project.generation,
            "project": {
                "id": project.project_id,
                "name": name,
                "type": project_type,
            },
            "projects": [
                {"id": row.id, "name": row.name, "type": row.type} for row in projects
            ],
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
                requester = authenticate_mcp(
                    credentials,
                    request.headers.get("x-dataagent-conversation-id"),
                    request.headers.get("x-dataagent-turn-id"),
                )
            except HTTPException as exc:
                await JSONResponse(
                    {"detail": exc.detail},
                    status_code=exc.status_code,
                    headers=exc.headers,
                )(scope, receive, send)
                return
            from .mcp_context import (
                CONVERSATION_HEADER,
                PROJECT_HEADER,
                PROJECT_QUERY,
                TURN_HEADER,
                bind_mcp_context,
            )
            from .mcp_contract import ToolInvalidInput
            from .mcp_project import resolve_mcp_project

            query_project = request.query_params.get(PROJECT_QUERY)
            header_project = request.headers.get(PROJECT_HEADER)
            if (
                query_project is not None
                and header_project is not None
                and query_project != header_project
            ):
                project_id, project_error = None, "请只提供一种项目选择"
            else:
                requested = (
                    query_project if query_project is not None else header_project
                )
                try:
                    project_id = resolve_mcp_project(
                        requester, requester.project_id, requested
                    )
                    project_error = None
                except ToolInvalidInput as exc:
                    project_id, project_error = None, str(exc)

            with bind_mcp_context(
                requester,
                request.headers.get(CONVERSATION_HEADER),
                request.headers.get(TURN_HEADER),
                project_id=project_id,
                project_error=project_error,
            ):
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
    parameters = dict(body.parameters)
    token = (
        parameters.pop("token", None)
        if (category, plugin_id) == ("puller", "dtb")
        else None
    )
    with sessions().begin() as db:
        if token:
            from .crypto import encrypt

            secret = db.get(Setting, "puller.dtb.token")
            if secret is None:
                db.add(Setting(key="puller.dtb.token", value=encrypt(token)))
            else:
                secret.value = encrypt(token)
        project_context.upsert_plugin_config(
            db, category, plugin_id, body.enabled, parameters
        )
    if (category, plugin_id) == ("puller", "dtb"):
        pull_scheduler.configure(
            parameters.get("interval_seconds", 0) if body.enabled else 0
        )
    return PluginView(
        category=category,
        id=plugin.id,
        version=plugin.version,
        display_name=plugin.display_name,
        parameters_schema=plugin.parameters_schema,
        enabled=body.enabled,
        parameters=parameters,
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
    target: str | None = None
    category: str | None = None
    subfolder: str | None = None
    files: list[SourceFileUpload] = []
    archive_base64: str | None = None
    replace: bool = False


class SourceCommit(BaseModel):
    commit: str


class SourcePreview(BaseModel):
    category: str
    target: str
    added: list[str]
    overwritten: list[str]
    unchanged: list[str]
    deleted: list[str]
    counts: dict[str, int]
    truncated: bool


class SourceContent(BaseModel):
    path: str
    content: str
    encoding: Literal["utf-8", "base64"]


class SourceHistory(BaseModel):
    commit: str
    author: str
    date: UtcDatetime
    message: str


class SourceDiff(BaseModel):
    diff: str


def source_path(project_id=None):
    value = project_context.source_directory_value(project_id)
    if value is None:
        raise HTTPException(422, "Source directory is not configured")
    return value


@contextmanager
def source_repository(project_id=None):
    from . import source_directory

    try:
        with source_directory.repository(source_path(project_id)) as root:
            yield root
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc


def source_category_root(scope):
    config = (
        project_context.plugin_config("source", "markdown")
        if scope.project_id == project_context.DEFAULT_PROJECT_ID
        else None
    )
    return config[1].get("path", "knowledge") if config else "knowledge"


def source_upload_target(body, category_root):
    from . import source_directory as source

    if body.target is not None and (
        body.category is not None or body.subfolder is not None
    ):
        raise HTTPException(422, "Choose target or category, not both")
    if body.subfolder is not None and body.category is None:
        raise HTTPException(422, "Knowledge category is required")
    if body.target is not None:
        try:
            return source.relative(body.target), None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    if body.category is None:
        raise HTTPException(422, "Knowledge category is required")
    try:
        root = source.relative(category_root)
        category = source.category_path(body.category)
        target = root / category
        if body.subfolder is not None:
            target = target / source.subfolder_path(body.subfolder)
        return target, root / category
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def source_upload_files(body, target):
    from . import source_directory as source

    if body.archive_base64 is not None and body.files:
        raise HTTPException(422, "Choose files or an archive")
    try:
        if body.archive_base64 is not None:
            files = source.unpack_archive(source.decode(body.archive_base64))
            prefix = (body.category or target.name) + "/"
            if (
                files
                and all(name.startswith(prefix) for name in files)
                and (body.replace or body.category is not None)
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
    return files


@app.post("/api/v1/source/upload", response_model=SourceCommit)
def upload_sources(
    body: SourceUpload,
    scope: Annotated[ProjectContext, Depends(admin_project_scope)],
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)],
):
    from . import source_directory as source

    category_root = source_category_root(scope)
    target, delete_root = source_upload_target(body, category_root)
    files = source_upload_files(body, target)
    with sessions()() as db:
        user = db.get(Token, digest(credentials.credentials))
        author = user.name
    with source_repository(scope.project_id) as root:
        return SourceCommit(
            commit=source.upload(
                root,
                target.as_posix(),
                files,
                body.replace,
                author,
                category_root,
                delete_root.as_posix() if delete_root else None,
            )
        )


@app.post("/api/v1/source/upload/preview", response_model=SourcePreview)
def preview_sources(
    body: SourceUpload,
    scope: Annotated[ProjectContext, Depends(admin_project_scope)],
):
    from . import source_directory as source

    category_root = source_category_root(scope)
    target, delete_root = source_upload_target(body, category_root)
    files = source_upload_files(body, target)
    try:
        with source.preview_repository(source_path(scope.project_id)) as root:
            result = source.preview(
                root,
                target.as_posix(),
                files,
                body.replace,
                category_root,
                delete_root.as_posix() if delete_root else None,
            )
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return SourcePreview(
        category=body.category if body.category is not None else target.name,
        target=target.as_posix(),
        **result,
    )


@app.get("/api/v1/source/files", response_model=list[str])
def source_files(
    scope: Annotated[ProjectContext, Depends(admin_project_scope)],
):
    from . import source_directory as source

    with source_repository(scope.project_id) as root:
        return source.list_files(root)


@app.get("/api/v1/source/file", response_model=SourceContent)
def source_file(
    path: str,
    scope: Annotated[ProjectContext, Depends(admin_project_scope)],
):
    from . import source_directory as source

    with source_repository(scope.project_id) as root:
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
def source_history(
    path: str,
    scope: Annotated[ProjectContext, Depends(admin_project_scope)],
):
    from . import source_directory as source

    with source_repository(scope.project_id) as root:
        return source.history(root, path)


@app.get("/api/v1/source/diff", response_model=SourceDiff)
def source_diff(
    path: str,
    before: str,
    after: str,
    scope: Annotated[ProjectContext, Depends(admin_project_scope)],
):
    from . import source_directory as source

    with source_repository(scope.project_id) as root:
        return SourceDiff(diff=source.diff(root, path, before, after))


def mount_frontend(application: FastAPI, directory):
    """Serve the built UI after all API and MCP routes have been registered."""
    from starlette.exceptions import HTTPException as StarletteHTTPException
    from starlette.staticfiles import StaticFiles

    class FrontendFiles(StaticFiles):
        async def get_response(self, path, scope):
            try:
                return await super().get_response(path, scope)
            except StarletteHTTPException as exc:
                headers = dict(scope.get("headers", []))
                accepts_html = b"text/html" in headers.get(b"accept", b"")
                if (
                    exc.status_code != 404
                    or not accepts_html
                    or path.startswith(("api/", "mcp/"))
                    or "." in path.rsplit("/", 1)[-1]
                ):
                    raise
                return await super().get_response("index.html", scope)

    application.mount(
        "/", FrontendFiles(directory=directory, html=True), name="frontend"
    )


class PullView(BaseModel):
    id: int
    status: str
    started_at: UtcDatetime
    finished_at: UtcDatetime | None
    tables: int
    tasks: int
    error: str | None
    commit: str | None
    build_id: int | None

    model_config = {"from_attributes": True}


@app.post("/api/v1/pulls/dtb", response_model=PullView)
def trigger_pull(_: Annotated[str, Depends(admin)]):
    from .pull_service import run_pull

    try:
        return run_pull()
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/v1/pulls", response_model=list[PullView])
def list_pulls(_: Annotated[str, Depends(current_user)]):
    from .db import PullRecord

    with sessions()() as db:
        return [
            PullView.model_validate(row)
            for row in db.query(PullRecord).order_by(PullRecord.id.desc()).all()
        ]


if static_dir := os.environ.get("DATAAGENT_STATIC_DIR"):
    mount_frontend(app, static_dir)
