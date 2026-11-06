"""REST contract for query execution and caller-owned DTB credentials."""

import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    token = initialize()
    with TestClient(app) as c:
        yield c, {"Authorization": f"Bearer {token}"}


def test_personal_dtb_token_is_encrypted_and_never_echoed(client):
    c, headers = client
    secret = "distinct-personal-dtb-token"
    response = c.put("/api/v1/me/dtb-token", headers=headers, json={"token": secret})
    assert response.status_code == 200, response.text
    assert secret not in response.text
    response = c.get("/api/v1/me/dtb-token", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {"configured": True}
    assert secret not in Path(os.environ["DATAAGENT_DB_PATH"]).read_bytes().decode(
        "latin1"
    )


def test_query_plan_rejects_non_select(client):
    c, headers = client
    response = c.post(
        "/api/v1/queries/plan", headers=headers, json={"sql": "DELETE FROM shop.orders"}
    )
    assert response.status_code == 422, response.text
    assert "SELECT" in response.text


def test_query_endpoints_require_authentication(client):
    c, _ = client
    assert c.post("/api/v1/queries/plan", json={"sql": "SELECT 1"}).status_code == 401
    assert c.get("/api/v1/me/dtb-token").status_code == 401


def test_personal_credentials_are_isolated_between_callers(client):
    c, admin = client
    user = c.post(
        "/api/v1/tokens", headers=admin, json={"name": "other", "role": "user"}
    ).json()["token"]
    other = {"Authorization": f"Bearer {user}"}
    assert (
        c.put(
            "/api/v1/me/dtb-token", headers=admin, json={"token": "admin-secret"}
        ).status_code
        == 200
    )
    assert c.get("/api/v1/me/dtb-token", headers=other).json() == {"configured": False}
    assert (
        c.put(
            "/api/v1/me/dtb-token", headers=other, json={"token": "other-secret"}
        ).status_code
        == 200
    )
    assert c.put("/api/v1/me/dtb-token", headers=admin, json={"token": ""}).json() == {
        "configured": False
    }
    assert c.get("/api/v1/me/dtb-token", headers=other).json() == {"configured": True}
