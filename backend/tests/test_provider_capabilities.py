import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from semantica_dataagent import cli
from semantica_dataagent.db import digest

DEFAULT_CAPABILITY = {"description": "", "vision": False, "effort_levels": []}


def _migration_config(path: Path) -> Config:
    config = Config()
    config.set_main_option(
        "script_location", str(Path(cli.__file__).parent / "migrations")
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    token = initialize()
    with TestClient(app) as test_client:
        yield test_client, token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _payload(**overrides):
    payload = {
        "name": "Company gateway",
        "type": "anthropic-messages",
        "base_url": "https://gateway.example.test",
        "models": ["model-a", "model-b"],
    }
    payload.update(overrides)
    return payload


def test_create_without_capabilities_returns_defaults(client):
    c, token = client
    created = c.post("/api/v1/providers", headers=_headers(token), json=_payload())

    assert created.status_code == 201
    body = created.json()
    assert body["models"] == ["model-a", "model-b"]
    assert body["model_capabilities"] == {
        "model-a": DEFAULT_CAPABILITY,
        "model-b": DEFAULT_CAPABILITY,
    }

    listed = c.get("/api/v1/providers", headers=_headers(token)).json()
    assert listed[0]["model_capabilities"] == body["model_capabilities"]


def test_capabilities_round_trip_and_fill_defaults_for_unlisted_models(client):
    c, token = client
    created = c.post(
        "/api/v1/providers",
        headers=_headers(token),
        json=_payload(
            model_capabilities={
                "model-b": {
                    "description": "  带截图问答  ",
                    "vision": True,
                    "effort_levels": ["high", "low", "high"],
                }
            }
        ),
    )

    assert created.status_code == 201
    capabilities = created.json()["model_capabilities"]
    assert capabilities["model-a"] == DEFAULT_CAPABILITY
    assert capabilities["model-b"] == {
        "description": "带截图问答",
        "vision": True,
        "effort_levels": ["low", "high"],
    }

    listed = c.get("/api/v1/providers", headers=_headers(token)).json()[0]
    assert listed["model_capabilities"] == capabilities


@pytest.mark.parametrize(
    "capabilities",
    [
        {"model-a": {"effort_levels": ["ultra"]}},
        {"model-a": {"description": "x" * 201}},
        {"missing-model": {"description": "not in models"}},
    ],
)
def test_invalid_capabilities_return_422(client, capabilities):
    c, token = client
    response = c.post(
        "/api/v1/providers",
        headers=_headers(token),
        json=_payload(model_capabilities=capabilities),
    )

    assert response.status_code == 422


def test_update_prunes_capabilities_when_models_are_removed(client):
    c, token = client
    created = c.post(
        "/api/v1/providers",
        headers=_headers(token),
        json=_payload(
            model_capabilities={
                "model-a": {"description": "a", "vision": True, "effort_levels": []},
                "model-b": {
                    "description": "b",
                    "vision": False,
                    "effort_levels": ["max"],
                },
            }
        ),
    ).json()

    updated = c.put(
        f"/api/v1/providers/{created['id']}",
        headers=_headers(token),
        json={"models": ["model-b", "model-c"]},
    )

    assert updated.status_code == 200
    assert updated.json()["models"] == ["model-b", "model-c"]
    assert updated.json()["model_capabilities"] == {
        "model-b": {
            "description": "b",
            "vision": False,
            "effort_levels": ["max"],
        },
        "model-c": DEFAULT_CAPABILITY,
    }


def test_update_capabilities_replaces_explicit_mapping(client):
    c, token = client
    created = c.post(
        "/api/v1/providers",
        headers=_headers(token),
        json=_payload(
            model_capabilities={
                "model-a": {"description": "a", "vision": True, "effort_levels": []},
                "model-b": {"description": "b", "vision": True, "effort_levels": []},
            }
        ),
    ).json()

    updated = c.put(
        f"/api/v1/providers/{created['id']}",
        headers=_headers(token),
        json={
            "model_capabilities": {
                "model-b": {
                    "description": "only b",
                    "vision": True,
                    "effort_levels": ["medium"],
                }
            }
        },
    )

    assert updated.status_code == 200
    assert updated.json()["model_capabilities"] == {
        "model-a": DEFAULT_CAPABILITY,
        "model-b": {
            "description": "only b",
            "vision": True,
            "effort_levels": ["medium"],
        },
    }


def test_update_capabilities_cannot_reference_removed_model(client):
    c, token = client
    created = c.post(
        "/api/v1/providers", headers=_headers(token), json=_payload()
    ).json()

    updated = c.put(
        f"/api/v1/providers/{created['id']}",
        headers=_headers(token),
        json={
            "models": ["model-a"],
            "model_capabilities": {
                "model-b": {"description": "stale", "effort_levels": []}
            },
        },
    )

    assert updated.status_code == 422


def test_old_provider_without_capability_column_reads_as_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "old.db"
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(path))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    config = _migration_config(path)
    command.upgrade(config, "0025")
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO providers "
            "(id, name, type, base_url, api_key, models, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-provider",
                "Legacy gateway",
                "anthropic-messages",
                "https://legacy.example.test",
                None,
                json.dumps(["legacy-model"]),
                "2026-01-01",
                "2026-01-01",
            ),
        )
        db.execute(
            "INSERT INTO tokens "
            "(digest, name, role, id, created_at, last_used_at, revoked) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                digest("old-admin-token"),
                "old admin",
                "admin",
                "old-id",
                "2026-01-01",
                None,
                0,
            ),
        )
    command.upgrade(config, "head")

    from semantica_dataagent.app import app

    with TestClient(app) as c:
        response = c.get(
            "/api/v1/providers",
            headers={"Authorization": "Bearer old-admin-token"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body[0]["models"] == ["legacy-model"]
    assert body[0]["model_capabilities"] == {"legacy-model": DEFAULT_CAPABILITY}
