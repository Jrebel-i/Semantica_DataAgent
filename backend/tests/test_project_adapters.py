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


def _set_pull_config(
    project_id: str,
    *,
    url: str,
    token: str | None,
    layer: str,
    max_tables: int,
    enabled: bool = True,
) -> None:
    from semantica_dataagent import project_context
    from semantica_dataagent.crypto import encrypt
    from semantica_dataagent.db import sessions

    with sessions().begin() as db:
        project_context.upsert_plugin_config(
            db,
            "puller",
            "dtb",
            enabled,
            {
                "url": url,
                "layer": layer,
                "max_tables": max_tables,
                "interval_seconds": 0,
            },
            project_id,
        )
        if token is not None:
            project_context.set_plugin_secret(
                db, project_id, "puller.dtb.token", encrypt(token)
            )


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


def test_run_pull_uses_each_projects_own_dtb_config_secret_and_source(
    client, tmp_path, monkeypatch
):
    from semantica_dataagent import project_context, pull_service
    from semantica_dataagent.crypto import encrypt
    from semantica_dataagent.db import Build, sessions
    from semantica_dataagent.dtb_puller import DtbPuller
    from semantica_dataagent.source_directory import git

    c, admin = client
    first = _create_project(c, admin, "First warehouse", "warehouse")
    second = _create_project(c, admin, "Second warehouse", "warehouse")
    default_source = tmp_path / "default-source"
    response = c.put(
        "/api/v1/settings",
        headers=_headers(admin),
        json={"values": {"source_directory": str(default_source)}},
    )
    assert response.status_code == 200, response.text

    projects = [
        (
            first["id"],
            f"https://{first['id']}.dtb.example",
            "first-layer",
            "first-token",
            1,
        ),
        (
            second["id"],
            f"https://{second['id']}.dtb.example",
            "second-layer",
            "second-token",
            2,
        ),
        (
            "default",
            "https://default.dtb.example",
            "default-layer",
            "default-token",
            3,
        ),
    ]
    for project_id, url, layer, token, max_tables in projects:
        with sessions().begin() as db:
            project_context.upsert_plugin_config(
                db,
                "puller",
                "dtb",
                True,
                {
                    "url": url,
                    "layer": layer,
                    "max_tables": max_tables,
                    "interval_seconds": 0,
                },
                project_id,
            )
            project_context.set_plugin_secret(
                db, project_id, "puller.dtb.token", encrypt(token)
            )

    calls = []

    def fake_fetch(http_client, url, token, parameters=None):
        calls.append({"url": url, "token": token, "parameters": parameters or {}})
        return (
            {"任务信息.json": b"task", "表信息.xlsx": b"table"},
            {"tables": 1, "tasks": 0},
        )

    monkeypatch.setattr(DtbPuller, "fetch", staticmethod(fake_fetch))
    monkeypatch.setattr(project_context, "wake_build_queue", lambda: None)

    for project_id, url, layer, token, max_tables in projects:
        record = pull_service.run_pull(project_id)

        assert record.status == "succeeded", record.error
        assert calls[-1] == {
            "url": url,
            "token": token,
            "parameters": {"layer": layer, "max_tables": max_tables},
        }
        directory = project_context.source_directory_value(project_id)
        assert record.source_directory == directory
        with sessions()() as db:
            build = db.get(Build, record.build_id)
            assert build.project_id == project_id
        assert (
            git(Path(directory), "rev-parse", "HEAD").decode().strip() == record.commit
        )


def test_run_pull_does_not_fall_back_to_default_project_config(
    client, tmp_path, monkeypatch
):
    from semantica_dataagent import project_context, pull_service
    from semantica_dataagent.dtb_puller import DtbPuller

    c, admin = client
    project = _create_project(c, admin, "Disabled pull", "warehouse")
    response = c.put(
        "/api/v1/settings",
        headers=_headers(admin),
        json={"values": {"source_directory": str(tmp_path / "default-source")}},
    )
    assert response.status_code == 200, response.text
    _set_pull_config(
        "default",
        url="https://default.dtb.example",
        token="default-token",
        layer="default-layer",
        max_tables=1,
    )
    _set_pull_config(
        project["id"],
        url="https://project.dtb.example",
        token="project-token",
        layer="project-layer",
        max_tables=2,
        enabled=False,
    )
    calls = []

    def fake_fetch(*args):
        calls.append(args)
        return ({"任务信息.json": b"task"}, {"tables": 1, "tasks": 0})

    monkeypatch.setattr(DtbPuller, "fetch", staticmethod(fake_fetch))
    monkeypatch.setattr(project_context, "wake_build_queue", lambda: None)

    record = pull_service.run_pull(project["id"])

    assert record.status == "failed"
    assert record.error == "DTB puller configuration is incomplete"
    assert record.build_id is None
    assert calls == []


def test_run_pull_does_not_fall_back_to_default_project_token(
    client, tmp_path, monkeypatch
):
    from semantica_dataagent import project_context, pull_service
    from semantica_dataagent.dtb_puller import DtbPuller

    c, admin = client
    project = _create_project(c, admin, "Tokenless pull", "warehouse")
    response = c.put(
        "/api/v1/settings",
        headers=_headers(admin),
        json={"values": {"source_directory": str(tmp_path / "default-source")}},
    )
    assert response.status_code == 200, response.text
    _set_pull_config(
        "default",
        url="https://default.dtb.example",
        token="default-token",
        layer="default-layer",
        max_tables=1,
    )
    _set_pull_config(
        project["id"],
        url="https://project.dtb.example",
        token=None,
        layer="project-layer",
        max_tables=2,
    )
    calls = []

    def fake_fetch(*args):
        calls.append(args)
        return ({"任务信息.json": b"task"}, {"tables": 1, "tasks": 0})

    monkeypatch.setattr(DtbPuller, "fetch", staticmethod(fake_fetch))
    monkeypatch.setattr(project_context, "wake_build_queue", lambda: None)

    record = pull_service.run_pull(project["id"])

    assert record.status == "failed"
    assert record.error == "DTB puller configuration is incomplete"
    assert record.build_id is None
    assert calls == []


def test_run_pull_does_not_fall_back_to_default_project_source_directory(
    client, tmp_path, monkeypatch
):
    from semantica_dataagent import project_context, pull_service
    from semantica_dataagent.db import ProjectSetting, sessions
    from semantica_dataagent.dtb_puller import DtbPuller

    c, admin = client
    project = _create_project(c, admin, "Sourceless pull", "warehouse")
    response = c.put(
        "/api/v1/settings",
        headers=_headers(admin),
        json={"values": {"source_directory": str(tmp_path / "default-source")}},
    )
    assert response.status_code == 200, response.text
    _set_pull_config(
        "default",
        url="https://default.dtb.example",
        token="default-token",
        layer="default-layer",
        max_tables=1,
    )
    _set_pull_config(
        project["id"],
        url="https://project.dtb.example",
        token="project-token",
        layer="project-layer",
        max_tables=2,
    )
    with sessions().begin() as db:
        db.query(ProjectSetting).filter(
            ProjectSetting.project_id == project["id"],
            ProjectSetting.key == "source_directory",
        ).delete()
    calls = []

    def fake_fetch(*args):
        calls.append(args)
        return ({"任务信息.json": b"task"}, {"tables": 1, "tasks": 0})

    monkeypatch.setattr(DtbPuller, "fetch", staticmethod(fake_fetch))
    monkeypatch.setattr(project_context, "wake_build_queue", lambda: None)

    record = pull_service.run_pull(project["id"])

    assert record.status == "failed"
    assert record.error == "DTB puller configuration is incomplete"
    assert record.build_id is None
    assert calls == []


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
