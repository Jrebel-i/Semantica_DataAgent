import asyncio
import os
import time
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from neo4j import AsyncGraphDatabase
from pydantic import BaseModel
from pymilvus import MilvusClient

from .db import Setting, Token, digest, sessions

app = FastAPI(
    title="Semantica DataAgent", docs_url=None, redoc_url=None, openapi_url=None
)
bearer = HTTPBearer(auto_error=False)
KEYS = {"neo4j_uri", "neo4j_user", "neo4j_password", "milvus_uri", "embedding_url"}
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
    with sessions()() as db:
        token = db.get(Token, digest(credentials.credentials))
        if token is None:
            raise HTTPException(
                status_code=401,
                detail="Invalid token",
                headers={"WWW-Authenticate": "Bearer"},
            )
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
