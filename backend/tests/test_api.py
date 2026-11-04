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
