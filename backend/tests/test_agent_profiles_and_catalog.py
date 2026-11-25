"""Tests for provider catalog discovery, named agent profiles, and runner registry (#328)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    admin_token = initialize()
    with TestClient(app) as test_client:
        user_resp = test_client.post(
            "/api/v1/tokens",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"name": "member", "role": "user"},
        )
        assert user_resp.status_code == 201, user_resp.text
        user_token = user_resp.json()["token"]
        yield test_client, admin_token, user_token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_model_protocol_suffix_mapping():
    from semantica_dataagent.provider_api import model_protocol

    assert model_protocol("glm-5.3-flash#claude") == "Claude"
    assert model_protocol("gpt-5.4#codex") == "Codex"
    assert model_protocol("gpt-4o#oai") == "OpenAI"
    assert model_protocol("plain-model") == "未知"
    assert model_protocol("model#other") == "未知"
    assert model_protocol("model#claude#extra") == "未知"


def test_create_provider_without_manual_models_and_base_url_key_guard(client):
    c, admin_token, _ = client
    created = c.post(
        "/api/v1/providers",
        headers=_headers(admin_token),
        json={
            "name": "Shared Gateway",
            "type": "anthropic-messages",
            "base_url": "https://gateway.example.test",
            "api_key": "sk-initial-secret",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["models"] == []
    assert body["catalog_models"] == []
    assert body["catalog_fetched_at"] is None
    assert body["catalog_status"] is None
    assert body["catalog_error"] is None
    assert body["has_api_key"] is True
    assert "sk-initial-secret" not in created.text

    provider_id = body["id"]

    # Unchanged base_url (even with trailing slash difference) allows updating name without api_key
    same_url = c.put(
        f"/api/v1/providers/{provider_id}",
        headers=_headers(admin_token),
        json={"name": "Renamed Gateway", "base_url": "https://gateway.example.test/"},
    )
    assert same_url.status_code == 200, same_url.text
    assert same_url.json()["has_api_key"] is True

    # Changing base_url without api_key in body is rejected with 422
    rejected = c.put(
        f"/api/v1/providers/{provider_id}",
        headers=_headers(admin_token),
        json={"base_url": "https://other.example.test"},
    )
    assert rejected.status_code == 422, rejected.text

    # Changing base_url with explicit empty api_key clears stored key and succeeds
    cleared = c.put(
        f"/api/v1/providers/{provider_id}",
        headers=_headers(admin_token),
        json={"base_url": "https://other.example.test", "api_key": ""},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["base_url"] == "https://other.example.test"
    assert cleared.json()["has_api_key"] is False

    # Changing base_url with new api_key succeeds without leaking secret
    rekeyed = c.put(
        f"/api/v1/providers/{provider_id}",
        headers=_headers(admin_token),
        json={
            "base_url": "https://third.example.test",
            "api_key": "sk-new-secret",
        },
    )
    assert rekeyed.status_code == 200, rekeyed.text
    assert rekeyed.json()["has_api_key"] is True
    assert "sk-new-secret" not in rekeyed.text


@pytest.mark.parametrize(
    ("provider_type", "expected_path", "expected_header", "payload", "expected_models"),
    [
        (
            "anthropic-messages",
            "/v1/models",
            "x-api-key",
            {
                "data": [
                    {"id": "glm-5.3-flash#claude"},
                    {"id": "qwen3.8-flash#claude"},
                    {"id": "glm-5.3-flash#claude"},
                ]
            },
            ["glm-5.3-flash#claude", "qwen3.8-flash#claude"],
        ),
        (
            "openai-completions",
            "/models",
            "authorization",
            {"data": [{"id": "gpt-4o#oai"}, {"id": "plain-model"}]},
            ["gpt-4o#oai", "plain-model"],
        ),
        (
            "openai-responses",
            "/models",
            "authorization",
            {"data": [{"id": "gpt-5.4#codex"}]},
            ["gpt-5.4#codex"],
        ),
        (
            "google-generative-ai",
            "/models",
            "x-goog-api-key",
            {
                "models": [
                    {"name": "models/gemini-2.5-flash"},
                    {"name": "gemini-2.5-pro"},
                ]
            },
            ["gemini-2.5-flash", "gemini-2.5-pro"],
        ),
    ],
)
def test_refresh_models_fetches_catalog_by_provider_protocol(
    client,
    monkeypatch,
    provider_type,
    expected_path,
    expected_header,
    payload,
    expected_models,
):
    from semantica_dataagent import provider_api

    c, admin_token, user_token = client
    secret = "sk-refresh-secret-xyz"
    created = c.post(
        "/api/v1/providers",
        headers=_headers(admin_token),
        json={
            "name": f"GW {provider_type}",
            "type": provider_type,
            "base_url": "https://gw.example.test",
            "api_key": secret,
        },
    ).json()
    provider_id = created["id"]

    # Non-admin cannot refresh models
    assert (
        c.post(
            f"/api/v1/providers/{provider_id}/refresh-models",
            headers=_headers(user_token),
        ).status_code
        == 403
    )

    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        provider_api, "_catalog_transport", httpx.MockTransport(handler)
    )

    refreshed = c.post(
        f"/api/v1/providers/{provider_id}/refresh-models",
        headers=_headers(admin_token),
    )
    assert refreshed.status_code == 200, refreshed.text
    body = refreshed.json()
    assert len(seen_requests) == 1
    assert seen_requests[0].method == "GET"
    assert seen_requests[0].url.path == expected_path
    assert expected_header in seen_requests[0].headers
    assert secret in seen_requests[0].headers[expected_header]

    assert body["catalog_models"] == expected_models
    assert body["models"] == expected_models
    assert body["catalog_status"] == "ok"
    assert body["catalog_error"] is None
    assert body["catalog_fetched_at"].endswith("Z")
    assert secret not in refreshed.text


def test_refresh_models_preserves_previous_catalog_on_errors_and_redacts_secrets(
    client, monkeypatch
):
    from semantica_dataagent import provider_api

    c, admin_token, _ = client
    secret = "sk-super-secret-value"
    created = c.post(
        "/api/v1/providers",
        headers=_headers(admin_token),
        json={
            "name": "GW",
            "type": "openai-completions",
            "base_url": "https://gw.example.test",
            "api_key": secret,
        },
    ).json()
    provider_id = created["id"]

    # First, succeed with initial models
    monkeypatch.setattr(
        provider_api,
        "_catalog_transport",
        httpx.MockTransport(
            lambda req: httpx.Response(
                200, json={"data": [{"id": "m1#oai"}, {"id": "m2#oai"}]}
            )
        ),
    )
    ok_resp = c.post(
        f"/api/v1/providers/{provider_id}/refresh-models",
        headers=_headers(admin_token),
    )
    assert ok_resp.status_code == 200
    initial_fetched_at = ok_resp.json()["catalog_fetched_at"]

    # 401 auth_failed echoes secret in upstream body -> redacted, catalog preserved
    monkeypatch.setattr(
        provider_api,
        "_catalog_transport",
        httpx.MockTransport(
            lambda req: httpx.Response(
                401, json={"error": {"message": f"Invalid token {secret}"}}
            )
        ),
    )
    auth_resp = c.post(
        f"/api/v1/providers/{provider_id}/refresh-models",
        headers=_headers(admin_token),
    )
    assert auth_resp.status_code == 200
    auth_body = auth_resp.json()
    assert auth_body["catalog_status"] == "auth_failed"
    assert secret not in (auth_body["catalog_error"] or "")
    assert "***" in (auth_body["catalog_error"] or "")
    assert auth_body["catalog_models"] == ["m1#oai", "m2#oai"]
    assert auth_body["models"] == ["m1#oai", "m2#oai"]
    assert auth_body["catalog_fetched_at"] == initial_fetched_at

    # Timeout -> catalog_status="timeout", preserves previous catalog
    def timeout_handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=req)

    monkeypatch.setattr(
        provider_api, "_catalog_transport", httpx.MockTransport(timeout_handler)
    )
    timeout_resp = c.post(
        f"/api/v1/providers/{provider_id}/refresh-models",
        headers=_headers(admin_token),
    )
    assert timeout_resp.status_code == 200
    assert timeout_resp.json()["catalog_status"] == "timeout"
    assert timeout_resp.json()["catalog_models"] == ["m1#oai", "m2#oai"]

    # 404 -> unsupported_protocol, preserves previous catalog
    monkeypatch.setattr(
        provider_api,
        "_catalog_transport",
        httpx.MockTransport(
            lambda req: httpx.Response(404, json={"error": "Not found"})
        ),
    )
    unsupported_resp = c.post(
        f"/api/v1/providers/{provider_id}/refresh-models",
        headers=_headers(admin_token),
    )
    assert unsupported_resp.status_code == 200
    assert unsupported_resp.json()["catalog_status"] == "unsupported_protocol"
    assert unsupported_resp.json()["catalog_models"] == ["m1#oai", "m2#oai"]

    # 302 cross-site redirect is NOT followed -> error, preserves previous catalog
    visited_urls: list[str] = []

    def redirect_handler(req: httpx.Request) -> httpx.Response:
        visited_urls.append(str(req.url))
        return httpx.Response(
            302, headers={"location": "https://evil.example.test/steal"}
        )

    monkeypatch.setattr(
        provider_api, "_catalog_transport", httpx.MockTransport(redirect_handler)
    )
    redir_resp = c.post(
        f"/api/v1/providers/{provider_id}/refresh-models",
        headers=_headers(admin_token),
    )
    assert redir_resp.status_code == 200
    assert visited_urls == ["https://gw.example.test/models"]
    assert redir_resp.json()["catalog_status"] == "error"
    assert redir_resp.json()["catalog_models"] == ["m1#oai", "m2#oai"]

    # Oversized body -> error, preserves previous catalog
    monkeypatch.setattr(provider_api, "MAX_CATALOG_BYTES", 100)
    monkeypatch.setattr(
        provider_api,
        "_catalog_transport",
        httpx.MockTransport(
            lambda req: httpx.Response(200, content=b"{" + b"a" * 200 + b"}")
        ),
    )
    big_resp = c.post(
        f"/api/v1/providers/{provider_id}/refresh-models",
        headers=_headers(admin_token),
    )
    assert big_resp.status_code == 200
    assert big_resp.json()["catalog_status"] == "error"
    assert big_resp.json()["catalog_models"] == ["m1#oai", "m2#oai"]

    # Empty catalog (200 with []) -> catalog_status="empty" and clears models
    monkeypatch.setattr(provider_api, "MAX_CATALOG_BYTES", 2 * 1024 * 1024)
    monkeypatch.setattr(
        provider_api,
        "_catalog_transport",
        httpx.MockTransport(lambda req: httpx.Response(200, json={"data": []})),
    )
    empty_resp = c.post(
        f"/api/v1/providers/{provider_id}/refresh-models",
        headers=_headers(admin_token),
    )
    assert empty_resp.status_code == 200
    assert empty_resp.json()["catalog_status"] == "empty"
    assert empty_resp.json()["catalog_models"] == []
    assert empty_resp.json()["models"] == []


def test_agent_runners_registry_and_structured_validation(client, monkeypatch):
    from semantica_dataagent import agent_runner

    c, admin_token, user_token = client

    # Admin-only access
    assert (
        c.get("/api/v1/agent-runners", headers=_headers(user_token)).status_code == 403
    )

    listed = c.get("/api/v1/agent-runners", headers=_headers(admin_token))
    assert listed.status_code == 200, listed.text
    by_id = {item["id"]: item for item in listed.json()}
    assert set(by_id) >= {"pi", "claude", "codex"}
    assert by_id["pi"]["npm_package"] == "@earendil-works/pi-coding-agent"
    assert by_id["pi"]["package_version"] == "1.2.0"
    assert by_id["claude"]["npm_package"] == "@anthropic-ai/claude-code"
    assert by_id["claude"]["package_version"] == "2.1.318"
    assert by_id["codex"]["npm_package"] == "@openai/codex"
    assert by_id["codex"]["package_version"] == "0.166.0"
    assert by_id["claude"]["adapter_registered"] is True

    # Reject shell injection / unpinned version / unsafe executable
    bad_payloads = [
        {
            "id": "evil",
            "display_name": "Evil",
            "npm_package": "@org/pkg; rm -rf /",
            "package_version": "1.0.0",
            "executable": "evil",
            "adapter_id": "evil",
        },
        {
            "id": "evil2",
            "display_name": "Evil 2",
            "npm_package": "@org/pkg",
            "package_version": "latest",
            "executable": "evil2",
            "adapter_id": "evil2",
        },
        {
            "id": "evil3",
            "display_name": "Evil 3",
            "npm_package": "@org/pkg",
            "package_version": "1.0.0",
            "executable": "/bin/sh -c",
            "adapter_id": "evil3",
        },
    ]
    for payload in bad_payloads:
        resp = c.post(
            "/api/v1/agent-runners",
            headers=_headers(admin_token),
            json=payload,
        )
        assert resp.status_code == 422, resp.text

    # Register valid structured runner -> starts pending_build & unavailable
    created = c.post(
        "/api/v1/agent-runners",
        headers=_headers(admin_token),
        json={
            "id": "goose",
            "display_name": "Goose",
            "npm_package": "@block/goose-cli",
            "package_version": "1.4.2",
            "executable": "goose",
            "adapter_id": "goose",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "pending_build"
    assert created.json()["available"] is False
    assert created.json()["cli_deployed"] is False
    assert created.json()["adapter_registered"] is False

    # Duplicate id -> 409
    assert (
        c.post(
            "/api/v1/agent-runners",
            headers=_headers(admin_token),
            json={
                "id": "goose",
                "display_name": "Goose 2",
                "npm_package": "@block/goose-cli",
                "package_version": "1.4.2",
                "executable": "goose",
                "adapter_id": "goose",
            },
        ).status_code
        == 409
    )

    # Image deployed without backend adapter -> adapter_missing
    deployed = c.put(
        "/api/v1/agent-runners/goose",
        headers=_headers(admin_token),
        json={"status_override": "image_deployed"},
    )
    assert deployed.status_code == 200, deployed.text
    assert deployed.json()["cli_deployed"] is True
    assert deployed.json()["adapter_registered"] is False
    assert deployed.json()["status"] == "adapter_missing"
    assert deployed.json()["available"] is False

    # Once backend adapter is registered & available, status becomes available
    class GooseRunner:
        agent_id = "goose"
        effort_levels = ()

        def available(self):
            return True

        async def run(self, request):
            return
            yield

    monkeypatch.setitem(agent_runner.RUNNERS, "goose", GooseRunner())
    listed_after = {
        item["id"]: item
        for item in c.get("/api/v1/agent-runners", headers=_headers(admin_token)).json()
    }
    assert listed_after["goose"]["cli_deployed"] is True
    assert listed_after["goose"]["adapter_registered"] is True
    assert listed_after["goose"]["available"] is True
    assert listed_after["goose"]["status"] == "available"


def test_agent_profiles_crud_locking_defaults_and_missing_models(client, monkeypatch):
    from semantica_dataagent import agent_runner, provider_api

    class AvailableRunner:
        def __init__(self, agent_id: str, ok: bool = True):
            self.agent_id = agent_id
            self._ok = ok

        def available(self):
            return self._ok

        async def run(self, request):
            return
            yield

    monkeypatch.setitem(agent_runner.RUNNERS, "claude", AvailableRunner("claude", True))
    monkeypatch.setitem(agent_runner.RUNNERS, "codex", AvailableRunner("codex", False))

    c, admin_token, user_token = client

    # Admin-only
    assert (
        c.get("/api/v1/agent-profiles", headers=_headers(user_token)).status_code == 403
    )

    # Create provider and refresh catalog
    prov = c.post(
        "/api/v1/providers",
        headers=_headers(admin_token),
        json={
            "name": "Company GW",
            "type": "anthropic-messages",
            "base_url": "https://gw.example.test",
            "api_key": "sk-test",
        },
    ).json()
    pid = prov["id"]
    monkeypatch.setattr(
        provider_api,
        "_catalog_transport",
        httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "claude-a#claude"},
                        {"id": "claude-b#claude"},
                    ]
                },
            )
        ),
    )
    refreshed = c.post(
        f"/api/v1/providers/{pid}/refresh-models", headers=_headers(admin_token)
    )
    assert refreshed.status_code == 200

    # Cannot create enabled profile with unavailable runner (codex)
    unavailable_create = c.post(
        "/api/v1/agent-profiles",
        headers=_headers(admin_token),
        json={
            "name": "Codex Unavailable",
            "runner_id": "codex",
            "enabled": True,
            "default_provider_id": pid,
            "default_model": "claude-a#claude",
            "provider_models": {pid: ["claude-a#claude"]},
        },
    )
    assert unavailable_create.status_code == 422, unavailable_create.text

    # Cannot create profile with uncataloged manual model
    manual_model_create = c.post(
        "/api/v1/agent-profiles",
        headers=_headers(admin_token),
        json={
            "name": "Bad Manual",
            "runner_id": "claude",
            "enabled": True,
            "default_provider_id": pid,
            "default_model": "invented-model",
            "provider_models": {pid: ["invented-model"]},
        },
    )
    assert manual_model_create.status_code == 422, manual_model_create.text

    # Create two named profiles on the same runner ("claude")
    daily = c.post(
        "/api/v1/agent-profiles",
        headers=_headers(admin_token),
        json={
            "name": "Claude · 日常",
            "runner_id": "claude",
            "enabled": True,
            "is_system_default": True,
            "default_provider_id": pid,
            "default_model": "claude-a#claude",
            "provider_models": {pid: ["claude-a#claude", "claude-b#claude"]},
        },
    )
    assert daily.status_code == 201, daily.text
    daily_body = daily.json()
    assert daily_body["is_system_default"] is True
    assert daily_body["version"] == 1
    assert daily_body["missing_models"] == {}
    assert daily_body["runner_available"] is True

    # Seeded pi profile should no longer be system default
    all_profiles = {
        p["id"]: p
        for p in c.get("/api/v1/agent-profiles", headers=_headers(admin_token)).json()
    }
    assert all_profiles["pi"]["is_system_default"] is False
    assert all_profiles[daily_body["id"]]["is_system_default"] is True

    # Create disabled second profile for same runner with empty selection
    longdoc = c.post(
        "/api/v1/agent-profiles",
        headers=_headers(admin_token),
        json={
            "name": "Claude · 长文档",
            "runner_id": "claude",
            "enabled": False,
            "provider_models": {},
        },
    )
    assert longdoc.status_code == 201, longdoc.text

    # Immutable runner_id
    immutable = c.put(
        f"/api/v1/agent-profiles/{daily_body['id']}",
        headers=_headers(admin_token),
        json={"runner_id": "pi", "version": daily_body["version"]},
    )
    assert immutable.status_code == 422, immutable.text

    # Unchecking active default model without replacement -> 409 affected_defaults
    blocked_uncheck = c.put(
        f"/api/v1/agent-profiles/{daily_body['id']}",
        headers=_headers(admin_token),
        json={
            "provider_models": {pid: ["claude-b#claude"]},
            "version": daily_body["version"],
        },
    )
    assert blocked_uncheck.status_code == 409, blocked_uncheck.text
    detail = blocked_uncheck.json()["detail"]
    assert len(detail["affected_defaults"]) >= 1
    assert any(d["kind"] == "system_default" for d in detail["affected_defaults"])

    # Unchecking active default WITH valid replacement in the same update -> 200 & version increments
    replaced = c.put(
        f"/api/v1/agent-profiles/{daily_body['id']}",
        headers=_headers(admin_token),
        json={
            "provider_models": {pid: ["claude-b#claude"]},
            "default_provider_id": pid,
            "default_model": "claude-b#claude",
            "version": daily_body["version"],
        },
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["version"] == 2
    assert replaced.json()["default_model"] == "claude-b#claude"

    # Optimistic locking: stale version=1 returns 409
    stale = c.put(
        f"/api/v1/agent-profiles/{daily_body['id']}",
        headers=_headers(admin_token),
        json={"name": "Stale Edit", "version": 1},
    )
    assert stale.status_code == 409, stale.text

    # Catalog refresh removes claude-b#claude -> selection preserved, flagged in missing_models
    monkeypatch.setattr(
        provider_api,
        "_catalog_transport",
        httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={"data": [{"id": "claude-a#claude"}, {"id": "claude-c#claude"}]},
            )
        ),
    )
    c.post(f"/api/v1/providers/{pid}/refresh-models", headers=_headers(admin_token))

    fetched_daily = c.get(
        f"/api/v1/agent-profiles/{daily_body['id']}", headers=_headers(admin_token)
    )
    assert fetched_daily.status_code == 200
    assert fetched_daily.json()["provider_models"] == {pid: ["claude-b#claude"]}
    assert fetched_daily.json()["missing_models"] == {pid: ["claude-b#claude"]}

    # Cannot delete active system default (409), CAN delete non-default profile (204)
    assert (
        c.delete(
            f"/api/v1/agent-profiles/{daily_body['id']}", headers=_headers(admin_token)
        ).status_code
        == 409
    )
    assert (
        c.delete(
            f"/api/v1/agent-profiles/{longdoc.json()['id']}",
            headers=_headers(admin_token),
        ).status_code
        == 204
    )


def test_migrated_pending_verification_resolves_after_refresh_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import json
    import sqlite3

    from alembic import command
    from alembic.config import Config

    from semantica_dataagent import agent_runner, cli, provider_api

    class ReadyRunner:
        def __init__(self, agent_id: str):
            self.agent_id = agent_id

        def available(self):
            return True

        async def run(self, request):
            return
            yield

    monkeypatch.setitem(agent_runner.RUNNERS, "claude", ReadyRunner("claude"))

    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(db_path))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())

    cfg = Config()
    cfg.set_main_option(
        "script_location", str(Path(cli.__file__).parent / "migrations")
    )
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "0049")

    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO tokens (digest, name, role, id, created_at, revoked) "
            "VALUES (?, 'admin', 'admin', 'admin-id', '2026-01-01', 0)",
            (
                __import__("semantica_dataagent.db", fromlist=["digest"]).digest(
                    "admin-tok"
                ),
            ),
        )
        db.execute(
            "INSERT INTO providers "
            "(id, name, type, base_url, api_key, models, model_capabilities, created_at, updated_at) "
            "VALUES ('p-old', 'Legacy GW', 'openai-completions', 'https://old.example.test', NULL, ?, NULL, '2026-01-01', '2026-01-01')",
            (json.dumps(["kept-model", "removed-model"]),),
        )
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('assistant_agent', 'claude')"
        )
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('assistant_provider_id', 'p-old')"
        )
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('assistant_model', 'kept-model')"
        )

    command.upgrade(cfg, "0050")

    from semantica_dataagent.app import app

    with TestClient(app) as c:
        headers = _headers("admin-tok")
        before = c.get("/api/v1/agent-profiles/claude", headers=headers)
        assert before.status_code == 200, before.text
        assert before.json()["pending_verification"] == {
            "p-old": ["kept-model", "removed-model"]
        }
        assert before.json()["missing_models"] == {}

        # Refresh provider catalog: kept-model exists, removed-model is gone, brand-new-model appears
        monkeypatch.setattr(
            provider_api,
            "_catalog_transport",
            httpx.MockTransport(
                lambda req: httpx.Response(
                    200,
                    json={"data": [{"id": "kept-model"}, {"id": "brand-new-model"}]},
                )
            ),
        )
        refreshed = c.post("/api/v1/providers/p-old/refresh-models", headers=headers)
        assert refreshed.status_code == 200, refreshed.text

        after = c.get("/api/v1/agent-profiles/claude", headers=headers)
        assert after.status_code == 200, after.text
        body = after.json()
        # Refreshing catalog must not auto-select brand-new-model
        assert body["provider_models"] == {"p-old": ["kept-model", "removed-model"]}
        assert body["pending_verification"] == {}
        assert body["missing_models"] == {"p-old": ["removed-model"]}
