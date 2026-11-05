import asyncio
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from mcp.server.fastmcp import FastMCP
from neo4j import AsyncGraphDatabase
from pydantic import BaseModel, Field
from pymilvus import MilvusClient
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError

from . import knowledge
from .db import Build, Setting, Token, digest, sessions
from .embedding_client import validate_settings


@asynccontextmanager
async def lifespan(application):
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
    server = create_mcp()
    application.state.mcp_app = server.streamable_http_app()
    async with server.session_manager.run():
        try:
            yield
        finally:
            knowledge.close_readers()


app = FastAPI(
    title="Semantica DataAgent",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
bearer = HTTPBearer(auto_error=False)
KEYS = {
    "neo4j_uri",
    "neo4j_user",
    "neo4j_password",
    "milvus_uri",
    "embedding_url",
    "embedding_model",
    "source_directory",
    "knowledge_generation",
    "dimension_value_limit",
    "embedding_concurrency",
    "embedding_timeout_seconds",
    "embedding_timeout_per_input_seconds",
    "embedding_retries",
    "embedding_backoff_seconds",
}
SECRET = {"neo4j_password"}


def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    with sessions().begin() as db:
        token = db.get(Token, digest(credentials.credentials))
        if token is None or token.revoked:
            raise HTTPException(
                status_code=401,
                detail="Invalid token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        now = datetime.now(UTC).replace(tzinfo=None)
        if token.last_used_at is None or token.last_used_at < now - timedelta(
            minutes=1
        ):
            token.last_used_at = now
        return token.role


def admin(role: Annotated[str, Depends(current_user)]) -> str:
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin required")
    return role


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
    generation = body.values.get("knowledge_generation")
    if generation and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", generation):
        raise HTTPException(status_code=422, detail="Invalid generation")
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


class BuildView(BaseModel):
    id: int
    status: str
    started_at: str
    finished_at: str | None
    counts: dict[str, int]
    error: str | None
    parse_failures: list[dict[str, str]]


def build_view(row: Build) -> BuildView:
    return BuildView(
        id=row.id,
        status=row.status,
        started_at=row.started_at,
        finished_at=row.finished_at,
        counts=json.loads(row.counts),
        error=row.error,
        parse_failures=json.loads(row.parse_failures or "[]"),
    )


@app.post("/api/v1/builds", status_code=202, response_model=BuildView)
def start_build(background: BackgroundTasks, _: Annotated[str, Depends(admin)]):
    try:
        with sessions().begin() as db:
            row = Build(
                status="running", started_at=datetime.now(UTC).isoformat(), counts="{}"
            )
            db.add(row)
            db.flush()
            result = build_view(row)
    except IntegrityError:
        raise HTTPException(status_code=409, detail="Build already running") from None
    background.add_task(knowledge.run_build, result.id)
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
                current_user(credentials)
            except HTTPException as exc:
                await JSONResponse(
                    {"detail": exc.detail},
                    status_code=exc.status_code,
                    headers=exc.headers,
                )(scope, receive, send)
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
