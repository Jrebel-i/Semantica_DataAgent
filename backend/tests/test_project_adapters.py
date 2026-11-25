"""Per-project source adapter configuration and pull-instance permissions (#187, #316)."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


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


def test_list_adapters_reports_project_type_compatibility_and_retires_dtb_source(
    client,
):
    c, admin = client
    default = c.get("/api/v1/projects/default/adapters", headers=_headers(admin))
    assert default.status_code == 200, default.text
    rows = {row["id"]: row for row in default.json()}
    assert set(rows) == {"canonical", "markdown", "ossie"}
    assert rows["markdown"]["enabled"] is True
    assert rows["canonical"]["enabled"] is True
    assert rows["ossie"]["enabled"] is False
    assert rows["ossie"]["project_types"] == ["warehouse"]
    assert rows["ossie"]["compatible"] is True
    assert rows["ossie"]["reason"] is None
    assert "pull" not in rows["ossie"]

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
    assert rows["ossie"]["compatible"] is False
    assert rows["ossie"]["enabled"] is False
    assert "数仓" in rows["ossie"]["reason"]
    assert rows["canonical"]["compatible"] is False
    assert rows["canonical"]["enabled"] is False


def test_document_project_rejects_enabling_ossie_adapter(client):
    c, admin = client
    document = _create_project(c, admin, "Handbook", "document")
    url = f"/api/v1/projects/{document['id']}/adapters"

    response = c.put(
        url,
        headers=_headers(admin),
        json={"adapters": [{"id": "ossie", "enabled": True, "parameters": {}}]},
    )
    assert response.status_code == 422, response.text
    assert "数仓" in response.text

    response = c.put(
        url,
        headers=_headers(admin),
        json={"adapters": [{"id": "markdown", "enabled": True, "parameters": {}}]},
    )
    assert response.status_code == 200, response.text


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
                    "id": "ossie",
                    "enabled": True,
                    "parameters": {"path": "first-ossie", "category": "first_cat"},
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

    assert _adapter(c, admin, first["id"], "ossie")["enabled"] is True
    assert _adapter(c, admin, second["id"], "ossie")["enabled"] is False
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
    assert first_plugins["ossie"] is True
    assert second_plugins["ossie"] is False


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


def test_project_scoped_pulls_list_and_project_admin_trigger(client, monkeypatch):
    from semantica_dataagent import project_context, pull_service
    from semantica_dataagent.plugins import PullArtifact

    c, admin = client
    alpha = _create_project(c, admin, "Alpha Pull", "warehouse")
    beta = _create_project(c, admin, "Beta Pull", "warehouse")
    member = _create_token(c, admin, "PullMember")
    project_admin = _create_token(c, admin, "PullProjectAdmin")
    stranger = _create_token(c, admin, "PullStranger")
    _add_member(c, admin, alpha["id"], member["id"], "member")
    _add_member(c, admin, alpha["id"], project_admin["id"], "admin")

    class _FakePuller:
        id = "dtb_ai_gateway"
        version = "1"
        display_name = "DTB (AI Gov 网关)"
        parameters_schema: ClassVar[dict[str, object]] = {
            "type": "object",
            "properties": {
                "url": {"type": "string", "format": "uri"},
                "token": {"type": "string", "format": "password"},
            },
            "additionalProperties": False,
        }

        def pull(self, context):
            return PullArtifact(
                ossie_models={"dtb.yaml": b"name: ok\n"},
                table_count=1,
            )

    monkeypatch.setattr(
        pull_service,
        "discover",
        lambda: {("puller", "dtb_ai_gateway"): _FakePuller()},
    )
    monkeypatch.setattr(project_context, "wake_build_queue", lambda: None)

    for pid in (alpha["id"], beta["id"]):
        created = c.post(
            f"/api/v1/projects/{pid}/pull-instances",
            headers=_headers(admin),
            json={
                "id": "inst-main",
                "plugin_id": "dtb_ai_gateway",
                "display_name": "Main",
                "parameters": {"url": "https://dtb.example", "token": "secret"},
            },
        )
        assert created.status_code == 201, created.text

    assert (
        c.post(
            f"/api/v1/projects/{alpha['id']}/pull-instances/inst-main/pull",
            headers=_headers(member["token"]),
        ).status_code
        == 403
    )
    assert (
        c.post(
            f"/api/v1/projects/{alpha['id']}/pull-instances/inst-main/pull",
            headers=_headers(stranger["token"]),
        ).status_code
        == 404
    )

    p_alpha = c.post(
        f"/api/v1/projects/{alpha['id']}/pull-instances/inst-main/pull",
        headers=_headers(project_admin["token"]),
    )
    assert p_alpha.status_code == 200, p_alpha.text
    p_beta = c.post(
        f"/api/v1/projects/{beta['id']}/pull-instances/inst-main/pull",
        headers=_headers(admin),
    )
    assert p_beta.status_code == 200, p_beta.text

    alpha_rows = c.get(
        "/api/v1/pulls",
        headers=_headers(member["token"]),
        params={"project": alpha["id"]},
    )
    assert alpha_rows.status_code == 200
    assert [row["id"] for row in alpha_rows.json()] == [p_alpha.json()["id"]]

    assert (
        c.get(
            "/api/v1/pulls",
            headers=_headers(member["token"]),
            params={"project": beta["id"]},
        ).status_code
        == 404
    )

    default_member_rows = c.get("/api/v1/pulls", headers=_headers(member["token"]))
    assert default_member_rows.status_code == 200
    assert [row["id"] for row in default_member_rows.json()] == [p_alpha.json()["id"]]

    all_admin_rows = c.get("/api/v1/pulls", headers=_headers(admin))
    assert all_admin_rows.status_code == 200
    assert {row["id"] for row in all_admin_rows.json()} >= {
        p_alpha.json()["id"],
        p_beta.json()["id"],
    }


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
            "/api/v1/plugins/source/ossie",
            headers=headers,
            json={"enabled": True, "parameters": {"path": "legacy-ossie"}},
        ).status_code
        == 200
    )

    rows = {
        row["id"]: row
        for row in c.get("/api/v1/projects/default/adapters", headers=headers).json()
    }
    assert rows["markdown"]["parameters"] == {"path": "legacy-knowledge"}
    assert rows["ossie"]["enabled"] is True
    assert rows["ossie"]["parameters"] == {"path": "legacy-ossie"}


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


def test_archived_project_adapters_are_read_only(client):
    c, admin = client
    project = _create_project(c, admin, "Frozen", "warehouse")
    url = f"/api/v1/projects/{project['id']}/adapters"
    body = {"adapters": [{"id": "markdown", "enabled": False, "parameters": {}}]}

    assert c.put(url, headers=_headers(admin), json=body).status_code == 200
    archived = c.post(
        f"/api/v1/projects/{project['id']}/archive", headers=_headers(admin)
    )
    assert archived.status_code == 200, archived.text

    rejected = c.put(url, headers=_headers(admin), json=body)
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"] == "项目已归档"

    listed = c.get(url, headers=_headers(admin))
    assert listed.status_code == 200, listed.text
    markdown = next(row for row in listed.json() if row["id"] == "markdown")
    assert markdown["enabled"] is False
