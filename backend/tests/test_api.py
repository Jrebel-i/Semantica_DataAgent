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
        c.put(
            "/api/v1/settings",
            headers={"Authorization": f"Bearer {token}"},
            json={"values": {"knowledge_generation": "bad-name"}},
        ).status_code
        == 422
    )
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
    assert c.post("/api/v1/builds", headers=headers).status_code == 403
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


def test_embedding_settings_are_validated_and_saved(client):
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    for key, value in [
        ("embedding_concurrency", "0"),
        ("embedding_concurrency", "many"),
        ("embedding_timeout_seconds", "0"),
        ("embedding_timeout_per_input_seconds", "-1"),
        ("embedding_retries", "11"),
        ("embedding_backoff_seconds", "nan"),
    ]:
        response = c.put(
            "/api/v1/settings", headers=headers, json={"values": {key: value}}
        )
        assert response.status_code == 422, (key, value, response.text)
        assert key in response.json()["detail"]
    saved = {
        "embedding_concurrency": "1",
        "embedding_timeout_seconds": "90",
        "embedding_timeout_per_input_seconds": "15",
        "embedding_retries": "0",
        "embedding_backoff_seconds": "0.5",
    }
    assert (
        c.put("/api/v1/settings", headers=headers, json={"values": saved}).status_code
        == 200
    )
    settings = c.get("/api/v1/settings", headers=headers).json()
    for key, value in saved.items():
        assert settings[key] == value


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


def test_build_requires_admin_and_reports_failure_when_unconfigured(client):
    import time

    c, token = client
    assert c.post("/api/v1/builds").status_code == 401
    for path in ("/mcp", "/mcp/"):
        for authorization in (None, "Basic bad", "Bearer bad"):
            headers = {"Authorization": authorization} if authorization else {}
            assert (
                c.post(path, headers=headers, follow_redirects=False).status_code == 401
            )
    headers = {"Authorization": f"Bearer {token}"}
    response = c.post("/api/v1/builds", headers=headers)
    assert response.status_code == 202
    build_id = response.json()["id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        build = c.get(f"/api/v1/builds/{build_id}", headers=headers).json()
        if build["status"] in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.02)
    assert build["status"] == "failed"
    assert build["error"]
    assert c.get("/api/v1/builds", headers=headers).json()[0]["id"] == build_id


def test_revoked_personal_token_is_denied_by_mcp(client):
    c, admin_token = client
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    issued = c.post(
        "/api/v1/tokens",
        headers=admin_headers,
        json={"name": "MCP user", "role": "user"},
    ).json()
    headers = {
        "Authorization": f"Bearer {issued['token']}",
        "Accept": "application/json, text/event-stream",
        "Host": "localhost:8000",
    }
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    assert c.post("/mcp", headers=headers, follow_redirects=False).status_code == 307
    assert c.post("/mcp/", headers=headers, json=initialize).status_code == 200
    assert (
        c.delete(f"/api/v1/tokens/{issued['id']}", headers=admin_headers).status_code
        == 204
    )
    assert c.post("/mcp", headers=headers, follow_redirects=False).status_code == 401
    assert c.post("/mcp/", headers=headers, json=initialize).status_code == 401


def test_startup_marks_interrupted_build_failed_and_allows_another(
    tmp_path, monkeypatch
):
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.db import Build, sessions

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "recovery.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    token = initialize()
    with sessions().begin() as db:
        db.add_all(
            [
                Build(
                    status="running",
                    started_at="2026-01-01T00:00:00+00:00",
                    counts='{"table": 2}',
                ),
                Build(
                    status="succeeded",
                    started_at="2025-01-01T00:00:00+00:00",
                    finished_at="2025-01-01T00:01:00+00:00",
                    counts="{}",
                ),
            ]
        )
    with TestClient(app) as c:
        headers = {"Authorization": f"Bearer {token}"}
        builds = c.get("/api/v1/builds", headers=headers).json()
        interrupted = next(b for b in builds if b["started_at"].startswith("2026"))
        finished = next(b for b in builds if b["started_at"].startswith("2025"))
        assert interrupted["status"] == "failed"
        assert "interrupted" in interrupted["error"].lower()
        assert interrupted["finished_at"] and interrupted["counts"] == {"table": 2}
        assert finished["status"] == "succeeded" and finished["error"] is None
        assert c.post("/api/v1/builds", headers=headers).status_code == 202


def test_plugin_configuration_and_filter_defaults(client):
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    plugins = c.get("/api/v1/plugins", headers=headers)
    assert plugins.status_code == 200
    canonical = next(p for p in plugins.json() if p["id"] == "canonical")
    assert canonical["enabled"] is True
    response = c.put(
        "/api/v1/plugins/source/canonical",
        headers=headers,
        json={"enabled": False, "parameters": {"bad": 1}},
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["path"] == []
    assert (
        c.put(
            "/api/v1/plugins/source/canonical",
            headers=headers,
            json={"enabled": False, "parameters": {}},
        ).status_code
        == 200
    )
    assert not next(
        p
        for p in c.get("/api/v1/plugins", headers=headers).json()
        if p["id"] == "canonical"
    )["enabled"]
    assert c.get("/api/v1/filters", headers=headers).json()["database_exclude"] == [
        "tmp",
        "test",
    ]


def test_parser_without_matching_type_is_unsupported(client):
    from semantica_dataagent.canonical import Record
    from semantica_dataagent.plugins import parse_task

    result = parse_task(
        Record("task", {"name": "job", "sql": "select 1", "type": "unknown"})
    )
    assert result.status == "unsupported"
    assert result.reads == [] and result.writes == []


def test_third_party_plugin_validation_and_next_build(client, tmp_path, monkeypatch):
    from contextlib import nullcontext

    from semantica_dataagent import knowledge, relationships, stages
    from semantica_dataagent.db import Build, sessions
    from semantica_dataagent.stage_contract import StageResult, StorageHandles

    fixture = Path(__file__).parent / "fixtures" / "third_party"
    monkeypatch.syspath_prepend(str(fixture))
    info = tmp_path / "dataagent_test_plugin-1.0.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Name: dataagent-test-plugin\nVersion: 1.0.0\n")
    (info / "entry_points.txt").write_text(
        "[semantica_dataagent.source_adapters]\nthird_party = third_party:Adapter\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    listing = c.get("/api/v1/plugins", headers=headers).json()
    assert any(p["id"] == "third_party" and not p["enabled"] for p in listing)
    invalid = c.put(
        "/api/v1/plugins/source/third_party",
        headers=headers,
        json={"enabled": True, "parameters": {"prefix": "x"}},
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"][0]["path"] == ["prefix"]
    assert (
        c.put(
            "/api/v1/plugins/source/third_party",
            headers=headers,
            json={"enabled": True, "parameters": {"prefix": "abc"}},
        ).status_code
        == 200
    )
    assert (
        c.put(
            "/api/v1/plugins/source/canonical",
            headers=headers,
            json={"enabled": False, "parameters": {}},
        ).status_code
        == 200
    )
    seen = []
    (tmp_path / "third_party.source").write_text("fixture data")
    monkeypatch.setattr(
        knowledge,
        "settings",
        lambda: {
            "source_directory": str(tmp_path),
            "knowledge_generation": "test",
            "embedding_model": "test",
            "embedding_url": "http://localhost",
            "neo4j_uri": "bolt://localhost",
            "neo4j_user": "neo4j",
            "milvus_uri": "http://localhost",
        },
    )
    monkeypatch.setattr(
        knowledge,
        "build_storage",
        lambda values, stats=None: nullcontext(
            StorageHandles("test", None, None, values)
        ),
    )
    monkeypatch.setattr(relationships, "reconcile", lambda *args: None)
    monkeypatch.setattr(knowledge, "read_graph", lambda values: None)
    monkeypatch.setattr(knowledge, "read_vectors", lambda values: None)
    monkeypatch.setattr(
        stages,
        "STAGES",
        [lambda records, generation, values: seen.extend(records) or StageResult()],
    )
    with sessions().begin() as db:
        row = Build(status="running", started_at="now", counts="{}")
        db.add(row)
        db.flush()
        build_id = row.id
    knowledge.run_build(build_id)
    assert [r.data["name"] for r in seen] == ["abc"]
    assert (
        c.put(
            "/api/v1/plugins/source/third_party",
            headers=headers,
            json={"enabled": True, "parameters": {"prefix": "changed"}},
        ).status_code
        == 200
    )
    seen.clear()
    knowledge.run_build(build_id)
    assert [r.data["name"] for r in seen] == ["changed"]
    assert (
        c.put(
            "/api/v1/plugins/source/third_party",
            headers=headers,
            json={"enabled": False, "parameters": {"prefix": "changed"}},
        ).status_code
        == 200
    )
    seen.clear()
    knowledge.run_build(build_id)
    assert seen == []


def test_task_parser_dispatch_uses_enabled_matching_parser(client, monkeypatch):
    from semantica_dataagent import plugins
    from semantica_dataagent.canonical import Record

    class Parser:
        id = "example"
        task_types = ("sql",)

        def parse(self, task, parameters):
            return plugins.ParseResult(
                [parameters["input"]], [task.data["name"]], "success"
            )

    parser = Parser()
    monkeypatch.setattr(
        plugins, "configured", lambda: [("parser", parser, True, {"input": "source"})]
    )
    result = plugins.parse_task(
        Record("task", {"name": "target", "sql": "...", "type": "sql"})
    )
    assert (result.status, result.reads, result.writes) == (
        "success",
        ["source"],
        ["target"],
    )
    assert (
        plugins.parse_task(
            Record("task", {"name": "target", "sql": "...", "type": "other"})
        ).status
        == "unsupported"
    )


def test_dimension_limit_setting(client):
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    for value in ("0", "-1", "1.5", "1000001"):
        assert (
            c.put(
                "/api/v1/settings",
                headers=headers,
                json={"values": {"dimension_value_limit": value}},
            ).status_code
            == 422
        )
    assert (
        c.put(
            "/api/v1/settings",
            headers=headers,
            json={"values": {"dimension_value_limit": "2"}},
        ).json()["dimension_value_limit"]
        == "2"
    )
    assert (
        c.get("/api/v1/settings", headers=headers).json()["dimension_value_limit"]
        == "2"
    )


def test_search_route_requires_auth_and_returns_fusion_hits(client, monkeypatch):
    from semantica_dataagent import knowledge

    c, token = client
    expected = [
        {
            "ref": "table:shop.orders",
            "type": "table",
            "score": 0.04,
            "sources": ["exact", "vector"],
            "confidence": "high",
            "ambiguous": False,
            "ambiguity_refs": [],
        }
    ]
    monkeypatch.setattr(
        knowledge,
        "search",
        lambda query, scope, top_k, category: (
            expected
            if (query, scope, top_k, category) == ("orders", "all", 5, "metrics")
            else []
        ),
    )
    assert (
        c.get("/api/v1/search", params={"query": "orders", "scope": "all"}).status_code
        == 401
    )
    response = c.get(
        "/api/v1/search",
        params={"query": "orders", "scope": "all", "category": "metrics"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json() == expected
    invalid = c.get(
        "/api/v1/search",
        params={"query": "orders", "scope": "tables", "category": "metrics"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert invalid.status_code == 422


def test_generation_listing_and_switch(client):
    from semantica_dataagent.db import GenerationPointer, KnowledgeGeneration, sessions
    from semantica_dataagent.knowledge import settings

    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    with sessions().begin() as db:
        db.add_all([KnowledgeGeneration(name="old"), KnowledgeGeneration(name="new")])
        db.add(GenerationPointer(id=1, generation="new"))
    assert c.get("/api/v1/generations").status_code == 401
    response = c.get("/api/v1/generations", headers=headers)
    assert response.status_code == 200
    assert {row["name"]: row["current"] for row in response.json()["generations"]} == {
        "old": False,
        "new": True,
    }
    assert (
        c.post("/api/v1/generations/missing/switch", headers=headers).status_code == 404
    )
    assert c.post("/api/v1/generations/old/switch", headers=headers).status_code == 200
    assert settings()["knowledge_generation"] == "old"
    rejected = c.put(
        "/api/v1/settings",
        headers=headers,
        json={"values": {"knowledge_generation": "other"}},
    )
    assert rejected.status_code == 409
    assert settings()["knowledge_generation"] == "old"
