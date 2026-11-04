import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    token = initialize()
    with TestClient(app) as test_client:
        yield test_client, token


def test_authentication(client):
    c, token = client
    for path in ("/api/v1/me", "/api/v1/settings"):
        assert c.get(path).status_code == 401
        assert c.get(path, headers={"Authorization": "Basic wrong"}).status_code == 401
        assert c.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.put("/api/v1/settings", json={"values": {}}).status_code == 401
    assert (
        c.get("/api/v1/me", headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )
    assert (
        c.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"}).json()["role"]
        == "admin"
    )


def test_token_lifecycle(client):
    c, admin_token = client
    assert c.get("/api/v1/tokens").status_code == 401
    assert (
        c.post("/api/v1/tokens", json={"name": "x", "role": "user"}).status_code == 401
    )
    assert c.delete("/api/v1/tokens/unknown").status_code == 401
    headers = {"Authorization": f"Bearer {admin_token}"}
    created = c.post(
        "/api/v1/tokens", headers=headers, json={"name": "Alice", "role": "user"}
    )
    assert created.status_code == 201
    issued = created.json()
    token = issued["token"]
    assert token not in Path(os.environ["DATAAGENT_DB_PATH"]).read_bytes().decode(
        "latin1"
    )
    listing = c.get("/api/v1/tokens", headers=headers)
    assert token not in listing.text
    row = next(item for item in listing.json() if item["id"] == issued["id"])
    assert set(row) == {"id", "name", "role", "created_at", "last_used_at", "revoked"}
    assert row["name"] == "Alice" and row["role"] == "user"
    assert row["created_at"] and row["last_used_at"] is None
    user_headers = {"Authorization": f"Bearer {token}"}
    assert c.get("/api/v1/me", headers=user_headers).status_code == 200
    updated = c.get("/api/v1/tokens", headers=headers).json()
    assert (
        next(item for item in updated if item["id"] == issued["id"])["last_used_at"]
        is not None
    )
    first_used = next(item for item in updated if item["id"] == issued["id"])[
        "last_used_at"
    ]
    assert c.get("/api/v1/me", headers=user_headers).status_code == 200
    updated_again = c.get("/api/v1/tokens", headers=headers).json()
    assert (
        next(item for item in updated_again if item["id"] == issued["id"])[
            "last_used_at"
        ]
        == first_used
    )
    assert c.get("/api/v1/tokens", headers=user_headers).status_code == 403
    assert (
        c.post(
            "/api/v1/tokens", headers=user_headers, json={"name": "x", "role": "admin"}
        ).status_code
        == 403
    )
    assert (
        c.delete(f"/api/v1/tokens/{issued['id']}", headers=user_headers).status_code
        == 403
    )
    assert (
        c.delete(f"/api/v1/tokens/{issued['id']}", headers=headers).status_code == 204
    )
    assert c.get("/api/v1/me", headers=user_headers).status_code == 401
    admin_row = next(
        item
        for item in c.get("/api/v1/tokens", headers=headers).json()
        if item["role"] == "admin"
    )
    assert (
        c.delete(f"/api/v1/tokens/{admin_row['id']}", headers=headers).status_code
        == 409
    )
    second = c.post(
        "/api/v1/tokens",
        headers=headers,
        json={"name": "second admin", "role": "admin"},
    ).json()
    assert (
        c.delete(f"/api/v1/tokens/{admin_row['id']}", headers=headers).status_code
        == 204
    )
    assert c.get("/api/v1/me", headers=headers).status_code == 401
    assert (
        c.get(
            "/api/v1/me", headers={"Authorization": f"Bearer {second['token']}"}
        ).json()["role"]
        == "admin"
    )


def test_existing_tokens_survive_upgrade(tmp_path, monkeypatch):
    import sqlite3

    from alembic import command
    from alembic.config import Config

    from semantica_dataagent import cli
    from semantica_dataagent.app import app
    from semantica_dataagent.db import digest

    path = tmp_path / "old.db"
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(path))
    config = Config()
    config.set_main_option(
        "script_location", str(Path(cli.__file__).parent / "migrations")
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, "0001")
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO tokens VALUES (?, ?, ?)",
            (digest("old-token"), "existing", "admin"),
        )
    command.upgrade(config, "head")
    with TestClient(app) as c:
        headers = {"Authorization": "Bearer old-token"}
        assert c.get("/api/v1/me", headers=headers).json() == {"role": "admin"}
        row = c.get("/api/v1/tokens", headers=headers).json()[0]
        assert row["name"] == "existing" and row["created_at"] and not row["revoked"]


def test_user_cannot_read_or_write_settings(client):
    from semantica_dataagent.db import Token, digest, sessions

    c, _ = client
    with sessions().begin() as db:
        db.add(Token(digest=digest("user-token"), name="user", role="user"))
    headers = {"Authorization": "Bearer user-token"}
    assert c.get("/api/v1/me", headers=headers).json()["role"] == "user"
    assert c.get("/api/v1/settings", headers=headers).status_code == 403
    assert (
        c.put("/api/v1/settings", headers=headers, json={"values": {}}).status_code
        == 403
    )


def test_settings_are_secret_and_health_uses_new_values(client, monkeypatch):
    import time

    import httpx

    import semantica_dataagent.app as app_module

    async def unavailable_neo4j(_values):
        raise OSError("unreachable")

    def unavailable_milvus(_uri):
        raise OSError("unreachable")

    def unavailable_http(_request):
        raise httpx.ConnectError("unreachable")

    original_client = httpx.AsyncClient
    monkeypatch.setattr(app_module, "check_neo4j", unavailable_neo4j)
    monkeypatch.setattr(app_module, "check_milvus", unavailable_milvus)
    monkeypatch.setattr(
        app_module.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(unavailable_http), **kwargs
        ),
    )

    c, token = client
    secret = "test" + "-password"
    headers = {"Authorization": f"Bearer {token}"}
    assert c.get("/healthz").json()["embedding"]["status"] == "not_configured"
    response = c.put(
        "/api/v1/settings",
        headers=headers,
        json={
            "values": {
                "neo4j_uri": "bolt://127.0.0.1:1",
                "neo4j_user": "neo4j",
                "neo4j_password": secret,
                "milvus_uri": "http://127.0.0.1:1",
                "embedding_url": "http://127.0.0.1:1",
            }
        },
    )
    assert response.status_code == 200
    assert secret not in response.text
    assert secret not in c.get("/api/v1/settings", headers=headers).text
    assert secret.encode() not in Path(os.environ["DATAAGENT_DB_PATH"]).read_bytes()
    start = time.monotonic()
    health = c.get("/healthz").json()
    assert time.monotonic() - start < 5
    assert all(item["status"] == "down" for item in health.values())
    c.put("/api/v1/settings", headers=headers, json={"values": {"embedding_url": None}})
    assert c.get("/healthz").json()["embedding"]["status"] == "not_configured"


def test_health_times_out_a_stalled_probe(client, monkeypatch):
    import asyncio
    import time

    import semantica_dataagent.app as app_module

    async def stalled(_values):
        await asyncio.sleep(30)

    monkeypatch.setattr(app_module, "check_neo4j", stalled)
    c, token = client
    c.put(
        "/api/v1/settings",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "values": {
                "neo4j_uri": "bolt://localhost:1",
                "neo4j_user": "neo4j",
                "neo4j_password": "test",
            }
        },
    )
    start = time.monotonic()
    result = c.get("/healthz").json()
    assert result["neo4j"]["status"] == "down"
    assert time.monotonic() - start < 5


def test_connection_addresses_reject_embedded_credentials(client):
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    credential = "user:" + "pass@"
    for key, value in [
        ("neo4j_uri", f"bolt://{credential}localhost:7687"),
        ("milvus_uri", f"http://{credential}localhost:19530"),
        ("embedding_url", f"http://{credential}localhost:8000"),
    ]:
        response = c.put(
            "/api/v1/settings", headers=headers, json={"values": {key: value}}
        )
        assert response.status_code == 422
        assert credential not in response.text
        assert c.get("/api/v1/settings", headers=headers).json()[key] is None


def test_sessions_reuse_engine_and_follow_database_path(tmp_path, monkeypatch):
    from semantica_dataagent.db import Base, Token, sessions

    first = str(tmp_path / "first.db")
    second = str(tmp_path / "second.db")
    monkeypatch.setenv("DATAAGENT_DB_PATH", first)
    first_factory = sessions()
    Base.metadata.create_all(first_factory.kw["bind"])
    with first_factory.begin() as db:
        db.add(Token(digest="first", name="first", role="admin"))
    assert sessions().kw["bind"] is first_factory.kw["bind"]

    monkeypatch.setenv("DATAAGENT_DB_PATH", second)
    second_factory = sessions()
    Base.metadata.create_all(second_factory.kw["bind"])
    with second_factory() as db:
        assert db.get(Token, "first") is None
    monkeypatch.setenv("DATAAGENT_DB_PATH", first)
    with sessions()() as db:
        assert db.get(Token, "first") is not None
    assert sessions().kw["bind"] is first_factory.kw["bind"]
