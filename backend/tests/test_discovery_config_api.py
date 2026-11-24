"""REST & project_context tests for project discovery config (#303)."""

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    admin_token = initialize()
    with TestClient(app) as client:
        yield client, admin_token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_token(
    client: TestClient, admin_token: str, name: str, projects: list[dict]
) -> tuple[str, str]:
    resp = client.post(
        "/api/v1/tokens",
        headers=_headers(admin_token),
        json={"name": name, "role": "user", "projects": projects},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    return data["id"], data["token"]


def _create_project(
    client: TestClient, admin_token: str, name: str, project_type: str = "warehouse"
) -> str:
    resp = client.post(
        "/api/v1/projects",
        headers=_headers(admin_token),
        json={"name": name, "type": project_type},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_discovery_config_unset_returns_explicit_defaults(env):
    client, admin_token = env
    from semantica_dataagent import project_context

    resp = client.get(
        "/api/v1/projects/default/discovery-config",
        headers=_headers(admin_token),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["preset"] == "doris_audit_log"
    assert data["source_table"] == "__internal_schema.audit_log"
    assert data["sql_column"] == "stmt"
    assert data["status_column"] == "state"
    assert data["time_column"] == "time"
    assert data["database_column"] == "db"
    assert data["caller_column"] == "user"
    assert data["dialect_column"] is None
    assert data["success_statuses"] == ["EOF", "OK"]
    assert data["lookback_days"] == 7
    assert data["max_queries"] == 5000
    assert data["min_frequency"] == 3
    assert data["max_fk_candidates"] == 20
    assert data["sample_limit"] == 1000
    assert data["min_inclusion_ratio"] == 0.8
    assert data["probe_timeout_seconds"] == 30
    assert data["probe_concurrency"] == 2
    assert data["provider_id"] is None
    assert data["model"] is None
    assert set(data["presets"]) == {"doris_audit_log", "dtb_tracking_table"}

    # Verify direct project_context entry point matches REST output
    ctx_cfg = project_context.discovery_config("default")
    for key, val in ctx_cfg.items():
        assert data[key] == val


def test_discovery_config_preset_switch_custom_override_and_audit(env):
    client, admin_token = env

    # Switch to dtb_tracking_table preset without overriding columns -> auto-applies preset defaults
    resp = client.put(
        "/api/v1/projects/default/discovery-config",
        headers=_headers(admin_token),
        json={"preset": "dtb_tracking_table", "lookback_days": 14, "max_queries": 1200},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["preset"] == "dtb_tracking_table"
    assert data["source_table"] == "mysql.data_tool_box_emr.query_history"
    assert data["sql_column"] == "query_sql"
    assert data["status_column"] == "status"
    assert data["time_column"] == "create_time"
    assert data["database_column"] == "database_name"
    assert data["caller_column"] == "user_name"
    assert data["dialect_column"] == "engine_type"
    assert data["success_statuses"] == ["SUCCESS", "SUCCEEDED", "FINISHED"]
    assert data["lookback_days"] == 14
    assert data["max_queries"] == 1200

    # Customize table/columns and explicitly clear optional dialect_column
    resp2 = client.put(
        "/api/v1/projects/default/discovery-config",
        headers=_headers(admin_token),
        json={
            "source_table": "analytics.custom_query_log",
            "sql_column": "raw_sql",
            "dialect_column": "",
            "success_statuses": ["DONE", "DONE", "OK"],
            "min_frequency": 5,
            "max_fk_candidates": 0,
            "sample_limit": 2500,
            "min_inclusion_ratio": 0.9,
            "probe_timeout_seconds": 60,
            "probe_concurrency": 1,
        },
    )
    assert resp2.status_code == 200, resp2.text
    data2 = resp2.json()
    assert data2["preset"] == "dtb_tracking_table"
    assert data2["source_table"] == "analytics.custom_query_log"
    assert data2["sql_column"] == "raw_sql"
    assert data2["dialect_column"] is None
    assert data2["success_statuses"] == ["DONE", "OK"]
    assert data2["min_frequency"] == 5
    assert data2["max_fk_candidates"] == 0
    assert data2["sample_limit"] == 2500
    assert data2["min_inclusion_ratio"] == 0.9
    assert data2["probe_timeout_seconds"] == 60
    assert data2["probe_concurrency"] == 1

    # Persisted reload preserves cleared optional column
    reloaded = client.get(
        "/api/v1/projects/default/discovery-config",
        headers=_headers(admin_token),
    )
    assert reloaded.status_code == 200
    assert reloaded.json()["dialect_column"] is None
    assert reloaded.json()["source_table"] == "analytics.custom_query_log"

    # ProjectAudit recorded both updates
    audits = client.get(
        "/api/v1/projects/default/audit",
        headers=_headers(admin_token),
    )
    assert audits.status_code == 200
    discovery_audits = [
        item for item in audits.json() if item["action"] == "project.discovery_config"
    ]
    assert len(discovery_audits) == 2
    assert discovery_audits[0]["detail"]["source_table"] == "analytics.custom_query_log"


@pytest.mark.parametrize(
    "payload",
    [
        {"unknown_extra_field": 1},
        {"preset": "unknown_preset"},
        {"preset": None},
        {"source_table": "audit_log; DROP TABLE users"},
        {"source_table": "db.tbl WHERE 1=1"},
        {"source_table": "a.b.c.d"},
        {"source_table": "db.`audit_log`"},
        {"source_table": ""},
        {"source_table": None},
        {"sql_column": "stmt; --"},
        {"sql_column": "stmt "},
        {"status_column": "state' OR '1'='1"},
        {"time_column": "123invalid"},
        {"caller_column": "user name"},
        {"database_column": "db;select 1"},
        {"success_statuses": []},
        {"success_statuses": None},
        {"success_statuses": ["EOF", "OK'; DROP TABLE x--"]},
        {"success_statuses": ["has space"]},
        {"success_statuses": ['a"b']},
        {"lookback_days": 0},
        {"lookback_days": 91},
        {"lookback_days": True},
        {"max_queries": 49},
        {"max_queries": 20001},
        {"min_frequency": 0},
        {"min_frequency": 501},
        {"max_fk_candidates": -1},
        {"max_fk_candidates": 51},
        {"sample_limit": 99},
        {"sample_limit": 5001},
        {"min_inclusion_ratio": 0.49},
        {"min_inclusion_ratio": 1.01},
        {"probe_timeout_seconds": 4},
        {"probe_timeout_seconds": 121},
        {"probe_concurrency": 0},
        {"probe_concurrency": 3},
    ],
)
def test_discovery_config_rejects_injection_bounds_and_extra_fields(env, payload):
    client, admin_token = env
    resp = client.put(
        "/api/v1/projects/default/discovery-config",
        headers=_headers(admin_token),
        json=payload,
    )
    assert resp.status_code == 422, (
        f"Expected 422 for {payload!r}, got {resp.status_code}: {resp.text}"
    )


def test_discovery_config_provider_model_validation(env):
    client, admin_token = env
    from semantica_dataagent.db import Provider, sessions

    with sessions().begin() as db:
        db.add(
            Provider(
                id="prov-1",
                name="TestProvider",
                type="openai-completions",
                base_url="http://127.0.0.1:9999/v1",
                models=json.dumps(["gpt-4o-mini", "gpt-4.1"]),
            )
        )

    # Model without provider_id -> 422
    r1 = client.put(
        "/api/v1/projects/default/discovery-config",
        headers=_headers(admin_token),
        json={"model": "gpt-4o-mini"},
    )
    assert r1.status_code == 422

    # Unknown provider_id -> 422
    r2 = client.put(
        "/api/v1/projects/default/discovery-config",
        headers=_headers(admin_token),
        json={"provider_id": "missing-prov", "model": "gpt-4o-mini"},
    )
    assert r2.status_code == 422

    # Model not in provider.models -> 422
    r3 = client.put(
        "/api/v1/projects/default/discovery-config",
        headers=_headers(admin_token),
        json={"provider_id": "prov-1", "model": "not-configured"},
    )
    assert r3.status_code == 422

    # Valid provider + model -> 200
    r4 = client.put(
        "/api/v1/projects/default/discovery-config",
        headers=_headers(admin_token),
        json={"provider_id": "prov-1", "model": "gpt-4o-mini"},
    )
    assert r4.status_code == 200, r4.text
    assert r4.json()["provider_id"] == "prov-1"
    assert r4.json()["model"] == "gpt-4o-mini"

    # Clearing provider_id also clears model cleanly
    r5 = client.put(
        "/api/v1/projects/default/discovery-config",
        headers=_headers(admin_token),
        json={"provider_id": None},
    )
    assert r5.status_code == 200, r5.text
    assert r5.json()["provider_id"] is None
    assert r5.json()["model"] is None


def test_discovery_config_permissions_archived_and_document_boundaries(env):
    client, admin_token = env

    wh_id = _create_project(client, admin_token, "数仓项目A", "warehouse")
    doc_id = _create_project(client, admin_token, "文档项目B", "document")

    _, proj_admin_tok = _create_token(
        client, admin_token, "proj-admin", [{"project_id": wh_id, "role": "admin"}]
    )
    _, member_tok = _create_token(
        client, admin_token, "proj-member", [{"project_id": wh_id, "role": "member"}]
    )
    _, outsider_tok = _create_token(client, admin_token, "outsider", [])

    # Project admin can GET & PUT
    assert (
        client.get(
            f"/api/v1/projects/{wh_id}/discovery-config",
            headers=_headers(proj_admin_tok),
        ).status_code
        == 200
    )
    assert (
        client.put(
            f"/api/v1/projects/{wh_id}/discovery-config",
            headers=_headers(proj_admin_tok),
            json={"lookback_days": 10},
        ).status_code
        == 200
    )

    # Regular member gets 403
    assert (
        client.get(
            f"/api/v1/projects/{wh_id}/discovery-config",
            headers=_headers(member_tok),
        ).status_code
        == 403
    )
    assert (
        client.put(
            f"/api/v1/projects/{wh_id}/discovery-config",
            headers=_headers(member_tok),
            json={"lookback_days": 12},
        ).status_code
        == 403
    )

    # Outsider and nonexistent project both return 404
    assert (
        client.get(
            f"/api/v1/projects/{wh_id}/discovery-config",
            headers=_headers(outsider_tok),
        ).status_code
        == 404
    )
    assert (
        client.put(
            f"/api/v1/projects/{wh_id}/discovery-config",
            headers=_headers(outsider_tok),
            json={"lookback_days": 12},
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/api/v1/projects/nonexistent99/discovery-config",
            headers=_headers(admin_token),
        ).status_code
        == 404
    )

    # Document project returns 422 for both GET and PUT
    doc_get = client.get(
        f"/api/v1/projects/{doc_id}/discovery-config",
        headers=_headers(admin_token),
    )
    assert doc_get.status_code == 422, doc_get.text
    doc_put = client.put(
        f"/api/v1/projects/{doc_id}/discovery-config",
        headers=_headers(admin_token),
        json={"lookback_days": 10},
    )
    assert doc_put.status_code == 422, doc_put.text

    # Archived warehouse project: GET 200 (read-only), PUT 409
    arch = client.post(
        f"/api/v1/projects/{wh_id}/archive",
        headers=_headers(admin_token),
    )
    assert arch.status_code == 200, arch.text

    arch_get = client.get(
        f"/api/v1/projects/{wh_id}/discovery-config",
        headers=_headers(proj_admin_tok),
    )
    assert arch_get.status_code == 200, arch_get.text
    assert arch_get.json()["lookback_days"] == 10

    arch_put = client.put(
        f"/api/v1/projects/{wh_id}/discovery-config",
        headers=_headers(proj_admin_tok),
        json={"lookback_days": 15},
    )
    assert arch_put.status_code == 409, arch_put.text
    assert "项目已归档" in arch_put.json()["detail"]


def test_discovery_config_provider_model_used_by_project_discovery_run_trigger(
    env, monkeypatch: pytest.MonkeyPatch
):
    client, admin_token = env
    from semantica_dataagent import discovery_runner, project_context
    from semantica_dataagent.crypto import encrypt
    from semantica_dataagent.db import (
        PersonalDtbToken,
        Provider,
        Token,
        digest,
        sessions,
    )

    with sessions().begin() as db:
        owner_id = db.get(Token, digest(admin_token)).id
        db.add(
            PersonalDtbToken(owner_id=owner_id, encrypted_token=encrypt("tok-cfg-run"))
        )
        project_context.upsert_plugin_config(
            db,
            "executor",
            "dtb",
            True,
            {"base_url": "http://dtb.local", "timeout_seconds": 30},
        )
        db.add(
            Provider(
                id="prov-cfg",
                name="CfgProvider",
                type="openai-completions",
                base_url="http://llm-cfg.local/v1",
                models=json.dumps(["gpt-4o-mini", "gpt-4.1"]),
            )
        )

    monkeypatch.setattr(
        discovery_runner, "execute_discovery_run", lambda run_id, **kwargs: None
    )

    cfg_resp = client.put(
        "/api/v1/projects/default/discovery-config",
        headers=_headers(admin_token),
        json={"provider_id": "prov-cfg", "model": "gpt-4o-mini"},
    )
    assert cfg_resp.status_code == 200, cfg_resp.text

    # Trigger without explicit provider_id/model -> inherits discovery-config
    r1 = client.post(
        "/api/v1/projects/default/discovery/runs",
        headers=_headers(admin_token),
        json={"top_templates": 10},
    )
    assert r1.status_code == 202, r1.text
    d1 = r1.json()
    assert d1["config_snapshot"]["provider_id"] == "prov-cfg"
    assert d1["config_snapshot"]["model"] == "gpt-4o-mini"
    assert d1["top_templates"] == 10
    assert d1["enable_fk_probing"] is False

    # Mark first run completed so second run can be triggered with model-only override
    from semantica_dataagent.db import DiscoveryRun

    with sessions().begin() as db:
        db.get(DiscoveryRun, d1["id"]).status = "completed"

    r2 = client.post(
        "/api/v1/projects/default/discovery/runs",
        headers=_headers(admin_token),
        json={"model": "gpt-4.1"},
    )
    assert r2.status_code == 202, r2.text
    assert r2.json()["config_snapshot"]["provider_id"] == "prov-cfg"
    assert r2.json()["config_snapshot"]["model"] == "gpt-4.1"
