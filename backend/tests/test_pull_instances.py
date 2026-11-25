"""Tests for M1 puller plugin protocol, project pull instances, and executors."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from semantica_dataagent import plugins, project_context, pull_instances
from semantica_dataagent.db import ProjectPullInstance, sessions
from semantica_dataagent.plugins import (
    PullArtifact,
    PullContext,
    PullItemFailure,
    secret_parameter_keys,
)


class _StubPullerPlugin:
    id = "stub"
    version = "1.2.0"
    display_name = "Stub Data Source"
    parameters_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "format": "uri", "title": "URL"},
            "token": {"type": "string", "format": "password", "title": "Token"},
            "api_secret": {"type": "string", "x-secret": True, "title": "API Secret"},
            "layer": {"type": "string", "title": "Layer"},
        },
        "required": ["url", "token"],
        "additionalProperties": False,
    }

    def pull(self, context: PullContext) -> PullArtifact:
        return PullArtifact(ossie_models={})


class _StubExecutorPlugin:
    id = "stub_exec"
    version = "0.1.0"
    display_name = "Stub Executor"
    parameters_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "endpoint": {"type": "string", "title": "Endpoint"},
        },
        "required": ["endpoint"],
        "additionalProperties": False,
    }

    def bind(self, parameters: dict, token: str):
        raise NotImplementedError


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


@pytest.fixture
def stub_plugins(monkeypatch: pytest.MonkeyPatch):
    base = dict(plugins.discover())
    base[("puller", "stub")] = _StubPullerPlugin()
    base[("executor", "stub_exec")] = _StubExecutorPlugin()
    monkeypatch.setattr(plugins, "discover", lambda: dict(base))
    monkeypatch.setattr(pull_instances, "discover", lambda: dict(base))
    return base


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_project(c: TestClient, admin: str, name: str, project_type: str) -> dict:
    resp = c.post(
        "/api/v1/projects",
        headers=_headers(admin),
        json={"name": name, "type": project_type},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_user(c: TestClient, admin: str, name: str, projects: list[dict]) -> dict:
    resp = c.post(
        "/api/v1/tokens",
        headers=_headers(admin),
        json={"name": name, "role": "user", "projects": projects},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_pull_protocol_and_secret_parameter_keys():
    ctx_token = PullContext(
        project_id="p1",
        instance_id="inst1",
        parameters={"url": "https://a"},
        secrets={"token": "t1", "api_secret": "s1"},
    )
    assert ctx_token.secret == "t1"

    ctx_single = PullContext(
        project_id="p1",
        instance_id="inst1",
        parameters={},
        secrets={"api_secret": "only"},
    )
    assert ctx_single.secret == "only"

    ctx_multi = PullContext(
        project_id="p1",
        instance_id="inst1",
        parameters={},
        secrets={"a": "1", "b": "2"},
    )
    assert ctx_multi.secret is None

    artifact = PullArtifact(
        ossie_models={"m.yaml": b"name: m\n"},
        failures=[PullItemFailure(kind="dimension", target="dim_a", reason="timeout")],
    )
    assert artifact.tasks == []
    assert artifact.dimension_values == []
    assert secret_parameter_keys(_StubPullerPlugin.parameters_schema) == {
        "api_secret",
        "token",
    }


def test_schedule_cron_timezone_and_threshold_validation():
    assert pull_instances.normalize_schedule_cron(None) is None
    assert pull_instances.normalize_schedule_cron("   ") is None
    assert pull_instances.normalize_schedule_cron("02:30") == "30 2 * * *"
    assert pull_instances.normalize_schedule_cron("0 2 * * *") == "0 2 * * *"
    assert (
        pull_instances.normalize_schedule_cron("*/15 1-5 1,15 1-12/2 0-6")
        == "*/15 1-5 1,15 1-12/2 0-6"
    )
    for bad_cron in (
        "24:00",
        "12:60",
        "0 2 * *",
        "60 * * * *",
        "0 2 0 * *",
        "5-2 * * * *",
        "0/15 * * * *",
        "*/0 * * * *",
        "not a cron",
    ):
        with pytest.raises(ValueError):
            pull_instances.normalize_schedule_cron(bad_cron)

    assert pull_instances.validate_timezone(None) == "Asia/Shanghai"
    assert pull_instances.validate_timezone("  ") == "Asia/Shanghai"
    assert pull_instances.validate_timezone("UTC") == "UTC"
    with pytest.raises(ValueError):
        pull_instances.validate_timezone("Mars/Phobos")

    assert pull_instances.validate_drop_threshold(None) == 0.5
    assert pull_instances.validate_drop_threshold(0.25) == 0.25
    assert pull_instances.validate_drop_threshold(1.0) == 1.0
    for bad_threshold in (0.0, -0.1, 1.1, True, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            pull_instances.validate_drop_threshold(bad_threshold)


def test_list_puller_plugins_exposes_schema_and_secret_keys(client, stub_plugins):
    c, admin = client
    member = _create_user(
        c, admin, "viewer", [{"project_id": "default", "role": "member"}]
    )
    resp = c.get("/api/v1/puller-plugins", headers=_headers(member["token"]))
    assert resp.status_code == 200, resp.text
    by_id = {row["id"]: row for row in resp.json()}
    assert "stub" in by_id
    assert by_id["stub"]["display_name"] == "Stub Data Source"
    assert by_id["stub"]["secret_keys"] == ["api_secret", "token"]


def test_warehouse_project_multi_instance_crud_and_secret_masking(client, stub_plugins):
    c, admin = client
    project = _create_project(c, admin, "Flight Warehouse", "warehouse")
    pid = project["id"]
    base_url = f"/api/v1/projects/{pid}/pull-instances"

    assert c.get(base_url, headers=_headers(admin)).json() == []

    # Missing required password secret fails schema validation
    bad_missing_secret = c.post(
        base_url,
        headers=_headers(admin),
        json={
            "id": "src-1",
            "plugin_id": "stub",
            "display_name": "Source 1",
            "parameters": {"url": "https://a.example"},
        },
    )
    assert bad_missing_secret.status_code == 422

    # Unset optional fields fall back to expected defaults
    created1 = c.post(
        base_url,
        headers=_headers(admin),
        json={
            "id": "src-1",
            "plugin_id": "stub",
            "display_name": "  Source One  ",
            "parameters": {
                "url": "https://a.example",
                "token": "secret-one",
                "api_secret": "api-one",
                "layer": "dimensions",
            },
        },
    )
    assert created1.status_code == 201, created1.text
    body1 = created1.json()
    assert body1["id"] == "src-1"
    assert body1["display_name"] == "Source One"
    assert body1["enabled"] is True
    assert body1["schedule_cron"] is None
    assert body1["timezone"] == "Asia/Shanghai"
    assert body1["drop_threshold"] == 0.5
    assert body1["parameters"] == {"url": "https://a.example", "layer": "dimensions"}
    assert body1["configured_secrets"] == ["api_secret", "token"]
    assert body1["secret_configured"] is True
    assert "secret-one" not in created1.text
    assert "api-one" not in created1.text

    # Second instance in same warehouse project with cron + isolation prefixes
    created2 = c.post(
        base_url,
        headers=_headers(admin),
        json={
            "id": "src-2",
            "plugin_id": "stub",
            "display_name": "Source Two",
            "parameters": {"url": "https://b.example", "token": "secret-two"},
            "schedule_cron": "02:15",
            "timezone": "UTC",
            "catalog_prefix": "lake_b",
            "task_namespace": "team.b",
            "drop_threshold": 0.3,
        },
    )
    assert created2.status_code == 201, created2.text
    body2 = created2.json()
    assert body2["schedule_cron"] == "15 2 * * *"
    assert body2["timezone"] == "UTC"
    assert body2["catalog_prefix"] == "lake_b"
    assert body2["task_namespace"] == "team.b"
    assert body2["drop_threshold"] == 0.3

    # Duplicate id returns 409
    dup = c.post(
        base_url,
        headers=_headers(admin),
        json={
            "id": "src-1",
            "plugin_id": "stub",
            "display_name": "Dup",
            "parameters": {"url": "https://a.example", "token": "x"},
        },
    )
    assert dup.status_code == 409

    # PUT preserves '***' secret and clears '' secret
    updated = c.put(
        f"{base_url}/src-1",
        headers=_headers(admin),
        json={
            "schedule_cron": "04:00",
            "parameters": {
                "url": "https://a-new.example",
                "token": "***",
                "api_secret": "",
            },
        },
    )
    assert updated.status_code == 200, updated.text
    up_body = updated.json()
    assert up_body["schedule_cron"] == "0 4 * * *"
    assert up_body["configured_secrets"] == ["token"]
    assert up_body["parameters"] == {"url": "https://a-new.example"}

    with sessions()() as db:
        row = db.get(ProjectPullInstance, (pid, "src-1"))
        decrypted = pull_instances.decrypt_instance_secrets(row.encrypted_secrets)
        assert decrypted == {"token": "secret-one"}

    # Clear schedule explicitly
    cleared = c.put(
        f"{base_url}/src-1",
        headers=_headers(admin),
        json={"schedule_cron": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["schedule_cron"] is None

    # Invalid parameters rejected
    for bad_payload in (
        {
            "id": "../escape",
            "plugin_id": "stub",
            "display_name": "X",
            "parameters": {"url": "https://x", "token": "t"},
        },
        {"id": "x3", "plugin_id": "missing", "display_name": "X", "parameters": {}},
        {
            "id": "x3",
            "plugin_id": "stub",
            "display_name": "X",
            "parameters": {"url": "https://x", "token": "t"},
            "schedule_cron": "99:99",
        },
        {
            "id": "x3",
            "plugin_id": "stub",
            "display_name": "X",
            "parameters": {"url": "https://x", "token": "t"},
            "timezone": "Bad/Zone",
        },
        {
            "id": "x3",
            "plugin_id": "stub",
            "display_name": "X",
            "parameters": {"url": "https://x", "token": "t"},
            "drop_threshold": 0,
        },
        {
            "id": "x3",
            "plugin_id": "stub",
            "display_name": "X",
            "parameters": {"url": "https://x", "token": "t"},
            "catalog_prefix": "1bad",
        },
    ):
        assert (
            c.post(base_url, headers=_headers(admin), json=bad_payload).status_code
            == 422
        )

    # Delete
    assert c.delete(f"{base_url}/src-1", headers=_headers(admin)).status_code == 204
    assert c.delete(f"{base_url}/src-1", headers=_headers(admin)).status_code == 404
    assert [row["id"] for row in c.get(base_url, headers=_headers(admin)).json()] == [
        "src-2"
    ]


def test_document_archived_and_member_boundaries_and_deletion(
    client, stub_plugins, monkeypatch
):
    from semantica_dataagent import project_deletion

    c, admin = client
    doc = _create_project(c, admin, "Docs", "document")
    wh = _create_project(c, admin, "Warehouse", "warehouse")

    # Document projects reject pull-instances and enabling executors
    assert (
        c.post(
            f"/api/v1/projects/{doc['id']}/pull-instances",
            headers=_headers(admin),
            json={
                "id": "src-1",
                "plugin_id": "stub",
                "display_name": "X",
                "parameters": {"url": "https://x", "token": "t"},
            },
        ).status_code
        == 422
    )
    doc_execs = c.get(
        f"/api/v1/projects/{doc['id']}/executors", headers=_headers(admin)
    ).json()
    assert doc_execs and all(
        not row["compatible"] and not row["enabled"] for row in doc_execs
    )
    assert (
        c.put(
            f"/api/v1/projects/{doc['id']}/executors",
            headers=_headers(admin),
            json={
                "executors": [
                    {
                        "id": "stub_exec",
                        "enabled": True,
                        "parameters": {"endpoint": "http://e"},
                    }
                ]
            },
        ).status_code
        == 422
    )
    assert [
        cat
        for cat, _, _, _ in plugins.configured(doc["id"], project_type="document")
        if cat == "executor"
    ] == []

    # Member vs admin vs outsider
    member = _create_user(c, admin, "m1", [{"project_id": wh["id"], "role": "member"}])
    outsider = _create_user(
        c, admin, "out", [{"project_id": doc["id"], "role": "member"}]
    )

    assert (
        c.post(
            f"/api/v1/projects/{wh['id']}/pull-instances",
            headers=_headers(admin),
            json={
                "id": "src-1",
                "plugin_id": "stub",
                "display_name": "One",
                "parameters": {"url": "https://a.example", "token": "t"},
            },
        ).status_code
        == 201
    )
    assert (
        c.get(
            f"/api/v1/projects/{wh['id']}/pull-instances",
            headers=_headers(member["token"]),
        ).status_code
        == 200
    )
    assert (
        c.get(
            f"/api/v1/projects/{wh['id']}/pull-instances",
            headers=_headers(outsider["token"]),
        ).status_code
        == 404
    )
    assert (
        c.post(
            f"/api/v1/projects/{wh['id']}/pull-instances",
            headers=_headers(member["token"]),
            json={
                "id": "src-2",
                "plugin_id": "stub",
                "display_name": "Two",
                "parameters": {"url": "https://b.example", "token": "t"},
            },
        ).status_code
        == 403
    )

    # Project-scoped optional executor configuration + mutual exclusion
    assert (
        c.put(
            f"/api/v1/projects/{wh['id']}/executors",
            headers=_headers(admin),
            json={
                "executors": [
                    {
                        "id": "stub_exec",
                        "enabled": True,
                        "parameters": {"endpoint": "http://exec.wh"},
                    }
                ]
            },
        ).status_code
        == 200
    )
    wh_execs = {
        plugin.id: (enabled, params)
        for cat, plugin, enabled, params in plugins.configured(wh["id"], "warehouse")
        if cat == "executor"
    }
    assert wh_execs["stub_exec"] == (True, {"endpoint": "http://exec.wh"})
    default_execs = {
        plugin.id: enabled
        for cat, plugin, enabled, _ in plugins.configured("default", "warehouse")
        if cat == "executor"
    }
    assert default_execs["stub_exec"] is False

    # Enabling two executors at once returns 422
    assert (
        c.put(
            f"/api/v1/projects/{wh['id']}/executors",
            headers=_headers(admin),
            json={
                "executors": [
                    {
                        "id": "dtb_ai_gateway",
                        "enabled": True,
                        "parameters": {"base_url": "https://dtb.example"},
                    }
                ]
            },
        ).status_code
        == 422
    )

    # Archived project allows reads, blocks writes with 409
    assert (
        c.post(
            f"/api/v1/projects/{wh['id']}/archive", headers=_headers(admin)
        ).status_code
        == 200
    )
    assert (
        c.get(
            f"/api/v1/projects/{wh['id']}/pull-instances", headers=_headers(admin)
        ).status_code
        == 200
    )
    archived_put = c.put(
        f"/api/v1/projects/{wh['id']}/executors",
        headers=_headers(admin),
        json={
            "executors": [
                {
                    "id": "stub_exec",
                    "enabled": False,
                    "parameters": {"endpoint": "http://exec.wh"},
                }
            ]
        },
    )
    assert archived_put.status_code == 409
    assert archived_put.json()["detail"] == project_context.ARCHIVED_DETAIL

    # Permanent deletion removes project's ProjectPullInstance rows
    monkeypatch.setattr(project_deletion, "_delete_source", lambda *args: None)
    monkeypatch.setattr(project_deletion, "_delete_neo4j", lambda *args: None)
    monkeypatch.setattr(project_deletion, "_delete_milvus", lambda *args: None)
    monkeypatch.setattr(project_deletion, "_cleanup_attachments", lambda *args: None)
    assert (
        c.post(
            f"/api/v1/projects/{wh['id']}/delete",
            headers=_headers(admin),
            json={"name": wh["name"]},
        ).status_code
        == 204
    )
    with sessions()() as db:
        assert (
            db.query(ProjectPullInstance)
            .filter(ProjectPullInstance.project_id == wh["id"])
            .all()
            == []
        )
