"""Per-project source adapter configuration (#187)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

DTB_SECRET = "dtb-secret-must-not-leak"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    token = initialize()
    with TestClient(app) as test_client:
        yield test_client, token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_project(c: TestClient, admin: str, name: str, project_type: str) -> dict:
    response = c.post(
        "/api/v1/projects",
        headers=_headers(admin),
        json={"name": name, "type": project_type},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_token(c: TestClient, admin: str, name: str) -> dict:
    response = c.post(
        "/api/v1/tokens",
        headers=_headers(admin),
        json={"name": name, "role": "user"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _add_member(
    c: TestClient, admin: str, project_id: str, token_id: str, role: str
) -> None:
    response = c.put(
        f"/api/v1/projects/{project_id}/members/{token_id}",
        headers=_headers(admin),
        json={"role": role},
    )
    assert response.status_code == 200, response.text


def _adapter(c: TestClient, token: str, project_id: str, adapter_id: str) -> dict:
    response = c.get(f"/api/v1/projects/{project_id}/adapters", headers=_headers(token))
    assert response.status_code == 200, response.text
    return next(row for row in response.json() if row["id"] == adapter_id)


def test_list_adapters_reports_project_type_compatibility(client):
    c, admin = client
    default = c.get("/api/v1/projects/default/adapters", headers=_headers(admin))
    assert default.status_code == 200, default.text
    rows = {row["id"]: row for row in default.json()}
    assert {"canonical", "markdown", "dtb"} <= set(rows)
    assert rows["markdown"]["enabled"] is True
    assert rows["canonical"]["enabled"] is True
    assert rows["dtb"]["enabled"] is False
    assert rows["dtb"]["project_types"] == ["warehouse"]
    assert rows["dtb"]["compatible"] is True
    assert rows["dtb"]["reason"] is None

    document = _create_project(c, admin, "Handbook", "document")
    rows = {
        row["id"]: row
        for row in c.get(
            f"/api/v1/projects/{document['id']}/adapters",
            headers=_headers(admin),
        ).json()
    }
    assert rows["markdown"]["compatible"] is True
    assert rows["markdown"]["enabled"] is True
    assert rows["dtb"]["compatible"] is False
    assert rows["dtb"]["enabled"] is False
    assert "数仓" in rows["dtb"]["reason"]
    assert rows["canonical"]["compatible"] is False
    assert rows["canonical"]["enabled"] is False


def test_document_project_rejects_enabling_dtb_and_its_pull_config(client):
    c, admin = client
    document = _create_project(c, admin, "Handbook", "document")
    url = f"/api/v1/projects/{document['id']}/adapters"

    response = c.put(
        url,
        headers=_headers(admin),
        json={
            "adapters": [
                {"id": "dtb", "enabled": True, "parameters": {}},
            ]
        },
    )
    assert response.status_code == 422, response.text
    assert "数仓" in response.text

    response = c.put(
        url,
        headers=_headers(admin),
        json={
            "adapters": [
                {
                    "id": "dtb",
                    "enabled": False,
                    "parameters": {},
                    "pull": {
                        "enabled": True,
                        "parameters": {"url": "https://dtb.example"},
                    },
                },
            ]
        },
    )
    assert response.status_code == 422, response.text
    assert "数仓" in response.text

    response = c.put(
        url,
        headers=_headers(admin),
        json={"adapters": [{"id": "markdown", "enabled": True, "parameters": {}}]},
    )
    assert response.status_code == 200, response.text


def test_warehouse_project_accepts_dtb_with_pull_config_and_hides_token(client):
    c, admin = client
    project = _create_project(c, admin, "Warehouse", "warehouse")
    url = f"/api/v1/projects/{project['id']}/adapters"
    response = c.put(
        url,
        headers=_headers(admin),
        json={
            "adapters": [
                {
                    "id": "dtb",
                    "enabled": True,
                    "parameters": {"snapshot_path": "dtb", "latest": True},
                    "pull": {
                        "enabled": True,
                        "parameters": {
                            "url": "https://dtb.example",
                            "interval_seconds": 3600,
                            "layer": "dimensions",
                            "token": DTB_SECRET,
                        },
                    },
                }
            ]
        },
    )
    assert response.status_code == 200, response.text

    row = _adapter(c, admin, project["id"], "dtb")
    assert row["enabled"] is True
    assert row["parameters"] == {"snapshot_path": "dtb", "latest": True}
    assert row["pull"]["enabled"] is True
    assert row["pull"]["parameters"] == {
        "url": "https://dtb.example",
        "interval_seconds": 3600,
        "layer": "dimensions",
    }
    assert row["pull"]["token_configured"] is True
    assert DTB_SECRET not in c.get(url, headers=_headers(admin)).text

    response = c.put(
        url,
        headers=_headers(admin),
        json={
            "adapters": [
                {
                    "id": "dtb",
                    "enabled": True,
                    "parameters": {"snapshot_path": "dtb", "latest": True},
                    "pull": {
                        "enabled": True,
                        "parameters": {
                            "url": "https://dtb.example",
                            "interval_seconds": 60,
                        },
                    },
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    assert _adapter(c, admin, project["id"], "dtb")["pull"]["token_configured"] is True
    assert DTB_SECRET.encode() not in Path(os.environ["DATAAGENT_DB_PATH"]).read_bytes()


def test_default_project_pull_token_stays_in_sync_with_legacy_endpoint(client):
    from semantica_dataagent.crypto import decrypt

    c, admin = client
    headers = _headers(admin)
    url = "/api/v1/projects/default/adapters"

    def secret():
        from semantica_dataagent import project_context

        value = project_context.plugin_secret("default", "puller.dtb.token")
        return decrypt(value) if value else None

    response = c.put(
        url,
        headers=headers,
        json={
            "adapters": [
                {
                    "id": "dtb",
                    "enabled": True,
                    "parameters": {},
                    "pull": {
                        "enabled": True,
                        "parameters": {
                            "url": "https://dtb.example",
                            "token": "first-secret",
                        },
                    },
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    assert secret() == "first-secret"

    response = c.put(
        "/api/v1/plugins/puller/dtb",
        headers=headers,
        json={
            "enabled": True,
            "parameters": {"url": "https://dtb.example", "token": "second-secret"},
        },
    )
    assert response.status_code == 200, response.text
    assert secret() == "second-secret"

    response = c.put(
        url,
        headers=headers,
        json={
            "adapters": [
                {
                    "id": "dtb",
                    "enabled": True,
                    "parameters": {},
                    "pull": {
                        "enabled": True,
                        "parameters": {"url": "https://dtb.example", "token": ""},
                    },
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    assert secret() is None


def test_project_adapter_configs_are_isolated(client):
    c, admin = client
    first = _create_project(c, admin, "First", "warehouse")
    second = _create_project(c, admin, "Second", "warehouse")

    response = c.put(
        f"/api/v1/projects/{first['id']}/adapters",
        headers=_headers(admin),
        json={
            "adapters": [
                {
                    "id": "dtb",
                    "enabled": True,
                    "parameters": {"snapshot_path": "first"},
                },
                {
                    "id": "markdown",
                    "enabled": True,
                    "parameters": {"path": "first-knowledge"},
                },
            ]
        },
    )
    assert response.status_code == 200, response.text

    assert _adapter(c, admin, first["id"], "dtb")["enabled"] is True
    assert _adapter(c, admin, second["id"], "dtb")["enabled"] is False
    assert _adapter(c, admin, first["id"], "markdown")["parameters"] == {
        "path": "first-knowledge"
    }
    assert _adapter(c, admin, second["id"], "markdown")["parameters"] == {}

    from semantica_dataagent.plugins import configured

    first_plugins = {
        plugin.id: enabled
        for category, plugin, enabled, _ in configured(first["id"], "warehouse")
        if category == "source"
    }
    second_plugins = {
        plugin.id: enabled
        for category, plugin, enabled, _ in configured(second["id"], "warehouse")
        if category == "source"
    }
    assert first_plugins["dtb"] is True
    assert second_plugins["dtb"] is False


def test_adapter_permissions(client):
    c, admin = client
    project = _create_project(c, admin, "Guarded", "warehouse")
    member = _create_token(c, admin, "Member")
    project_admin = _create_token(c, admin, "Project admin")
    stranger = _create_token(c, admin, "Stranger")
    _add_member(c, admin, project["id"], member["id"], "member")
    _add_member(c, admin, project["id"], project_admin["id"], "admin")
    url = f"/api/v1/projects/{project['id']}/adapters"
    body = {"adapters": [{"id": "markdown", "enabled": True, "parameters": {}}]}

    assert c.get(url, headers=_headers(member["token"])).status_code == 200
    assert c.put(url, headers=_headers(member["token"]), json=body).status_code == 403
    assert c.get(url, headers=_headers(stranger["token"])).status_code == 404
    assert c.put(url, headers=_headers(stranger["token"]), json=body).status_code == 404
    assert (
        c.put(url, headers=_headers(project_admin["token"]), json=body).status_code
        == 200
    )


def test_default_project_keeps_existing_global_adapter_configuration(client):
    c, admin = client
    headers = _headers(admin)
    assert (
        c.put(
            "/api/v1/plugins/source/markdown",
            headers=headers,
            json={"enabled": True, "parameters": {"path": "legacy-knowledge"}},
        ).status_code
        == 200
    )
    assert (
        c.put(
            "/api/v1/plugins/source/dtb",
            headers=headers,
            json={"enabled": True, "parameters": {"snapshot_path": "legacy-dtb"}},
        ).status_code
        == 200
    )
    assert (
        c.put(
            "/api/v1/plugins/puller/dtb",
            headers=headers,
            json={
                "enabled": True,
                "parameters": {
                    "url": "https://legacy.example",
                    "interval_seconds": 30,
                    "token": DTB_SECRET,
                },
            },
        ).status_code
        == 200
    )

    rows = {
        row["id"]: row
        for row in c.get("/api/v1/projects/default/adapters", headers=headers).json()
    }
    assert rows["markdown"]["parameters"] == {"path": "legacy-knowledge"}
    assert rows["dtb"]["enabled"] is True
    assert rows["dtb"]["parameters"] == {"snapshot_path": "legacy-dtb"}
    assert rows["dtb"]["pull"]["enabled"] is True
    assert rows["dtb"]["pull"]["parameters"]["interval_seconds"] == 30
    assert rows["dtb"]["pull"]["token_configured"] is True


def test_adapter_configuration_is_snapshotted_for_a_build(client, monkeypatch):
    from semantica_dataagent import knowledge, plugins

    c, admin = client
    project = _create_project(c, admin, "Snapshot", "warehouse")
    captured = []
    original = knowledge.configured

    def capture(project_id, project_type=None):
        result = original(project_id, project_type=project_type)
        captured.append(result)
        return result

    monkeypatch.setattr(knowledge, "configured", capture)
    assert (
        c.put(
            f"/api/v1/projects/{project['id']}/adapters",
            headers=_headers(admin),
            json={"adapters": [{"id": "markdown", "enabled": False, "parameters": {}}]},
        ).status_code
        == 200
    )
    assert (
        c.put(
            f"/api/v1/projects/{project['id']}/adapters",
            headers=_headers(admin),
            json={"adapters": [{"id": "markdown", "enabled": True, "parameters": {}}]},
        ).status_code
        == 200
    )
    snapshot = plugins.configured(project["id"], project_type="warehouse")
    assert (
        c.put(
            f"/api/v1/projects/{project['id']}/adapters",
            headers=_headers(admin),
            json={"adapters": [{"id": "markdown", "enabled": False, "parameters": {}}]},
        ).status_code
        == 200
    )
    assert {p.id: e for _, p, e, _ in snapshot if p.id == "markdown"} == {
        "markdown": True
    }
