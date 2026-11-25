"""Seam 3 tests: project-scoped optional query executors and strict credential isolation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from test_mcp_contract import call_registered

from semantica_dataagent import (
    discovery_executor,
    knowledge,
    plugins,
    pull_instances,
    sample_api,
    sql_validation,
)
from semantica_dataagent.auth import Requester
from semantica_dataagent.db import QueryAudit, QueryExecution, Token, digest, sessions
from semantica_dataagent.mcp_context import bind_mcp_context


class _StubPullerPlugin:
    id = "stub_puller"
    version = "1.0.0"
    display_name = "Stub Puller"
    parameters_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "format": "uri"},
            "token": {"type": "string", "format": "password"},
        },
        "required": ["url", "token"],
        "additionalProperties": False,
    }

    def pull(self, context):  # pragma: no cover - not invoked by executor tests
        raise NotImplementedError


class _RecordingExecutor:
    def __init__(self, endpoint: str, token: str, calls: list[dict]):
        self.endpoint = endpoint
        self.token = token
        self.calls = calls

    async def estimate(self, sql: str) -> dict:
        self.calls.append(
            {
                "op": "estimate",
                "endpoint": self.endpoint,
                "token": self.token,
                "sql": sql,
            }
        )
        return {"partitions": 1, "rows": 10}

    async def submit(self, sql: str, limit: int) -> str:
        self.calls.append(
            {
                "op": "submit",
                "endpoint": self.endpoint,
                "token": self.token,
                "sql": sql,
                "limit": limit,
            }
        )
        return f"key-{len(self.calls)}"

    async def poll(self, key: str) -> dict:
        return {
            "progress": 1.0,
            "columns": [{"name": "v", "type": "long"}],
            "data": [[1], [2], [3]],
        }

    async def wait(self, key: str) -> dict:
        self.calls.append(
            {
                "op": "wait",
                "endpoint": self.endpoint,
                "token": self.token,
                "key": key,
            }
        )
        return {
            "columns": [{"name": "v", "type": "long"}],
            "data": [[1], [2], [3]],
        }

    async def download(self, key: str, format: str) -> tuple[bytes, str | None]:
        self.calls.append(
            {
                "op": "download",
                "endpoint": self.endpoint,
                "token": self.token,
                "key": key,
                "format": format,
            }
        )
        return b"v\n1\n2\n3\n", "text/csv"


class _StubExecutorPlugin:
    id = "stub_exec"
    version = "0.1.0"
    display_name = "Stub Executor"
    parameters_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "endpoint": {"type": "string", "format": "uri"},
            "row_limit": {"type": "integer", "minimum": 1, "default": 100},
            "download_limit": {"type": "integer", "minimum": 1, "default": 1000},
            "timeout_seconds": {"type": "integer", "minimum": 1, "default": 30},
            "interpret_results": {"type": "boolean", "default": True},
        },
        "required": ["endpoint"],
        "additionalProperties": False,
    }

    def __init__(self, calls: list[dict]):
        self._calls = calls

    def bind(self, parameters: dict, token: str) -> _RecordingExecutor:
        return _RecordingExecutor(parameters["endpoint"], token, self._calls)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _requester(token: str) -> Requester:
    with sessions()() as db:
        row = db.query(Token).filter(Token.digest == digest(token)).one()
        return Requester(id=row.id, role=row.role, token=token)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    admin_token = initialize()
    calls: list[dict] = []
    base = dict(plugins.discover())
    base[("puller", "stub_puller")] = _StubPullerPlugin()
    base[("executor", "stub_exec")] = _StubExecutorPlugin(calls)
    monkeypatch.setattr(plugins, "discover", lambda: dict(base))
    monkeypatch.setattr(pull_instances, "discover", lambda: dict(base))

    with TestClient(app) as client:
        yield client, admin_token, calls


def _create_project(c: TestClient, admin: str, name: str, project_type: str) -> str:
    resp = c.post(
        "/api/v1/projects",
        headers=_headers(admin),
        json={"name": name, "type": project_type},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _enable_stub_executor(
    c: TestClient,
    admin: str,
    project_id: str,
    endpoint: str,
    *,
    row_limit: int = 10,
) -> None:
    resp = c.put(
        f"/api/v1/projects/{project_id}/executors",
        headers=_headers(admin),
        json={
            "executors": [
                {
                    "id": "stub_exec",
                    "enabled": True,
                    "parameters": {
                        "endpoint": endpoint,
                        "row_limit": row_limit,
                        "download_limit": 50,
                    },
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text


def _setup_three_projects(c: TestClient, admin: str) -> tuple[str, str, str]:
    proj_a = _create_project(c, admin, "Warehouse A", "warehouse")
    proj_b = _create_project(c, admin, "Warehouse B", "warehouse")
    proj_doc = _create_project(c, admin, "Document Proj", "document")

    # Enable executors on both default and proj_a with distinct endpoints;
    # leave proj_b without any executor enabled.
    _enable_stub_executor(
        c, admin, "default", "http://default.executor.test", row_limit=10
    )
    _enable_stub_executor(c, admin, proj_a, "http://proj-a.executor.test", row_limit=2)
    return proj_a, proj_b, proj_doc


def test_project_scoped_plan_execute_poll_download_and_degradation(env, monkeypatch):
    c, admin, calls = env
    proj_a, proj_b, proj_doc = _setup_three_projects(c, admin)

    # Mock table metadata for proj_a only
    monkeypatch.setattr(
        knowledge,
        "describe",
        lambda **kwargs: (
            [
                {
                    "database": "shop",
                    "name": "orders",
                    "fields": [{"name": "id", "partition": False}],
                }
            ]
            if kwargs.get("project_id") == proj_a
            else []
        ),
    )

    assert (
        c.put(
            "/api/v1/me/dtb-token",
            headers=_headers(admin),
            json={"token": "personal-tok-a"},
        ).status_code
        == 200
    )

    # 1. proj_a plans, executes, polls (respecting proj_a row_limit=2), and downloads
    plan_resp = c.post(
        f"/api/v1/queries/plan?project={proj_a}",
        headers=_headers(admin),
        json={"sql": "SELECT id FROM shop.orders"},
    )
    assert plan_resp.status_code == 200, plan_resp.text
    plan_data = plan_resp.json()
    assert plan_data["project_id"] == proj_a
    assert plan_data["row_limit"] == 2
    query_id = plan_data["id"]

    exec_resp = c.post(
        f"/api/v1/queries/{query_id}/execute",
        headers=_headers(admin),
        json={},
    )
    assert exec_resp.status_code == 200, exec_resp.text
    assert exec_resp.json()["rows"] == [[1], [2]]
    assert exec_resp.json()["row_count"] == 3

    poll_resp = c.get(f"/api/v1/queries/{query_id}", headers=_headers(admin))
    assert poll_resp.status_code == 200, poll_resp.text
    assert poll_resp.json()["rows"] == [[1], [2]]

    dl_resp = c.get(
        f"/api/v1/queries/{query_id}/download?format=csv",
        headers=_headers(admin),
    )
    assert dl_resp.status_code == 200, dl_resp.text
    assert dl_resp.content == b"v\n1\n2\n3\n"

    assert [call["op"] for call in calls] == [
        "estimate",
        "submit",
        "wait",
        "download",
    ]
    assert all(call["endpoint"] == "http://proj-a.executor.test" for call in calls)
    assert calls[0]["token"] == ""
    assert all(call["token"] == "personal-tok-a" for call in calls[1:])

    calls.clear()

    # 2. proj_b has NO executor -> gracefully degrades to Q&A-only with 503
    #    even though default and proj_a BOTH have executors enabled!
    plan_b = c.post(
        f"/api/v1/queries/plan?project={proj_b}",
        headers=_headers(admin),
        json={"sql": "SELECT 1"},
    )
    assert plan_b.status_code == 503, plan_b.text
    assert "尚未启用查询执行器" in plan_b.text

    # Pre-existing planned query in proj_b is also rejected with 503 on execute
    owner = _requester(admin)
    with sessions().begin() as db:
        db.add(
            QueryExecution(
                id="planned-b",
                owner_id=owner.id,
                sql="SELECT 1",
                status="planned",
                plan_data=f'{{"project_id": "{proj_b}", "execution_limit": 100}}',
            )
        )
    exec_b = c.post(
        "/api/v1/queries/planned-b/execute",
        headers=_headers(admin),
        json={},
    )
    assert exec_b.status_code == 503, exec_b.text
    assert "尚未启用查询执行器" in exec_b.text
    assert calls == []
    with sessions()() as db:
        assert db.get(QueryExecution, "planned-b").status == "planned"
        assert (
            db.query(QueryAudit).filter(QueryAudit.status == "rejected_503").first()
            is not None
        )

    # 3. proj_doc rejects executor configuration and query plan/execution
    doc_put = c.put(
        f"/api/v1/projects/{proj_doc}/executors",
        headers=_headers(admin),
        json={
            "executors": [
                {
                    "id": "stub_exec",
                    "enabled": True,
                    "parameters": {"endpoint": "http://doc.executor.test"},
                }
            ]
        },
    )
    assert doc_put.status_code == 422

    plan_doc = c.post(
        f"/api/v1/queries/plan?project={proj_doc}",
        headers=_headers(admin),
        json={"sql": "SELECT 1"},
    )
    assert plan_doc.status_code == 422, plan_doc.text
    assert "文档型项目" in plan_doc.text

    with sessions().begin() as db:
        db.add(
            QueryExecution(
                id="planned-doc",
                owner_id=owner.id,
                sql="SELECT 1",
                status="planned",
                plan_data=f'{{"project_id": "{proj_doc}", "execution_limit": 100}}',
            )
        )
    exec_doc = c.post(
        "/api/v1/queries/planned-doc/execute",
        headers=_headers(admin),
        json={},
    )
    assert exec_doc.status_code == 422, exec_doc.text
    assert calls == []


def test_plan_rejects_unconfigured_or_document_project_without_knowledge_backend(env):
    c, admin, _calls = env
    _, proj_b, proj_doc = _setup_three_projects(c, admin)

    # Even when Neo4j / knowledge.describe is not configured, proj_b returns 503
    # (never masks executor absence as Neo4j metadata error) and proj_doc returns 422.
    resp_b = c.post(
        f"/api/v1/queries/plan?project={proj_b}",
        headers=_headers(admin),
        json={"sql": "SELECT 1"},
    )
    assert resp_b.status_code == 503, resp_b.text
    assert "尚未启用查询执行器" in resp_b.text

    resp_doc = c.post(
        f"/api/v1/queries/plan?project={proj_doc}",
        headers=_headers(admin),
        json={"sql": "SELECT 1"},
    )
    assert resp_doc.status_code == 422, resp_doc.text
    assert "文档型项目" in resp_doc.text


def test_mcp_query_tools_respect_per_project_executor_isolation(env, monkeypatch):
    from semantica_dataagent.app import create_mcp

    c, admin, calls = env
    proj_a, proj_b, proj_doc = _setup_three_projects(c, admin)
    monkeypatch.setattr(knowledge, "describe", lambda **kwargs: [])
    monkeypatch.setattr(
        sql_validation,
        "validate_sql",
        lambda sql, dialect="doris", **kwargs: {"status": "pass"},
    )
    assert (
        c.put(
            "/api/v1/me/dtb-token",
            headers=_headers(admin),
            json={"token": "mcp-personal-tok"},
        ).status_code
        == 200
    )

    server = create_mcp()
    req = _requester(admin)

    # proj_a succeeds and uses proj_a's endpoint + personal token
    with bind_mcp_context(req, project_id=proj_a):
        plan_res = call_registered(server, "execute_query", {"sql": "SELECT 1"})
        assert not plan_res.isError, plan_res.model_dump()
        qid = plan_res.structuredContent["id"]
        exec_res = call_registered(server, "execute_query", {"confirmation_id": qid})
        assert not exec_res.isError, exec_res.model_dump()
        assert exec_res.structuredContent["rows"] == [[1], [2]]

    assert [call["op"] for call in calls] == ["estimate", "submit", "wait"]
    assert all(call["endpoint"] == "http://proj-a.executor.test" for call in calls)
    assert calls[1]["token"] == "mcp-personal-tok"
    calls.clear()

    # Cross-project confirmation_id cannot be executed from another project
    with bind_mcp_context(req, project_id=proj_a):
        plan_res2 = call_registered(server, "propose_query", {"sql": "SELECT 1"})
        assert not plan_res2.isError, plan_res2.model_dump()
        qid2 = plan_res2.structuredContent["id"]
    assert [call["op"] for call in calls] == ["estimate"]
    assert calls[0]["endpoint"] == "http://proj-a.executor.test"
    calls.clear()
    with bind_mcp_context(req, project_id=proj_b):
        prop_b = call_registered(server, "propose_query", {"sql": "SELECT 1"})
        assert prop_b.isError
        assert prop_b.structuredContent["code"] == "unavailable"
        assert "尚未启用查询执行器" in prop_b.structuredContent["message"]

        exec_b = call_registered(server, "execute_query", {"sql": "SELECT 1"})
        assert exec_b.isError
        assert exec_b.structuredContent["code"] == "unavailable"
        assert "尚未启用查询执行器" in exec_b.structuredContent["message"]

        cross_exec = call_registered(server, "execute_query", {"confirmation_id": qid2})
        assert cross_exec.isError

    # proj_doc rejects both via MCP contract and direct tool calls
    with bind_mcp_context(req, project_id=proj_doc):
        doc_res = call_registered(server, "execute_query", {"sql": "SELECT 1"})
        assert doc_res.isError
        assert doc_res.structuredContent["code"] == "invalid_input"
    assert calls == []


def test_strict_credential_separation_never_borrows_pull_instance_secret(
    env, monkeypatch
):
    c, admin, calls = env
    proj_a, _, _ = _setup_three_projects(c, admin)
    monkeypatch.setattr(knowledge, "describe", lambda **kwargs: [])

    # Configure a pull instance with a project service token on proj_a
    pull_resp = c.post(
        f"/api/v1/projects/{proj_a}/pull-instances",
        headers=_headers(admin),
        json={
            "id": "nightly-pull",
            "plugin_id": "stub_puller",
            "display_name": "Nightly Pull",
            "parameters": {
                "url": "https://dtb.internal.example",
                "token": "project-pull-service-secret-xyz",
            },
        },
    )
    assert pull_resp.status_code == 201, pull_resp.text

    # Caller has NO PersonalDtbToken saved
    assert c.get("/api/v1/me/dtb-token", headers=_headers(admin)).json() == {
        "configured": False
    }

    # 1. Interactive SQL execution must fail with 400 (never borrow pull service secret)
    plan_resp = c.post(
        f"/api/v1/queries/plan?project={proj_a}",
        headers=_headers(admin),
        json={"sql": "SELECT 1"},
    )
    assert plan_resp.status_code == 200, plan_resp.text
    query_id = plan_resp.json()["id"]
    exec_resp = c.post(
        f"/api/v1/queries/{query_id}/execute",
        headers=_headers(admin),
        json={},
    )
    assert exec_resp.status_code == 400, exec_resp.text
    assert "Dev Key" in exec_resp.text

    # 2. Sampling must fail with ToolInvalidInput (missing personal token)
    monkeypatch.setattr(
        knowledge,
        "describe",
        lambda **kwargs: [
            {
                "database": "shop",
                "name": "orders",
                "fields": [
                    {"name": "id", "type": "bigint", "partition": False},
                ],
            }
        ],
    )
    req = _requester(admin)
    with bind_mcp_context(req, project_id=proj_a):
        with pytest.raises(sample_api.ToolInvalidInput) as sample_exc:
            asyncio.run(sample_api.sample_table_data("shop.orders", project_id=proj_a))
        assert "Dev Key" in str(sample_exc.value)

    # 3. Discovery executor must fail with 400 DiscoveryTokenMissingError
    with pytest.raises(discovery_executor.DiscoveryTokenMissingError) as disc_exc:
        asyncio.run(
            discovery_executor.execute_readonly_dtb_query(
                owner_id=req.id,
                sql="SELECT id FROM shop.orders LIMIT 5",
                limit=5,
                project_id=proj_a,
            )
        )
    assert disc_exc.value.status_code == 400

    # Pull service token must NEVER appear in any executor call
    assert all(call["token"] != "project-pull-service-secret-xyz" for call in calls)
    assert not any(call["op"] == "submit" for call in calls)


def test_sample_table_data_and_discovery_executor_respect_project_isolation(
    env, monkeypatch
):
    from semantica_dataagent.app import create_mcp

    c, admin, calls = env
    proj_a, proj_b, proj_doc = _setup_three_projects(c, admin)

    monkeypatch.setattr(
        knowledge,
        "describe",
        lambda **kwargs: [
            {
                "database": "shop",
                "name": "orders",
                "fields": [
                    {"name": "v", "type": "bigint", "partition": False},
                ],
            }
        ],
    )
    assert (
        c.put(
            "/api/v1/me/dtb-token",
            headers=_headers(admin),
            json={"token": "caller-personal-tok"},
        ).status_code
        == 200
    )

    server = create_mcp()
    req = _requester(admin)

    # sample_table_data on proj_a hits proj_a's endpoint with personal token
    with bind_mcp_context(req, project_id=proj_a):
        sample_a = call_registered(
            server,
            "sample_table_data",
            {"table": "shop.orders", "columns": ["v"], "limit": 2},
        )
    assert not sample_a.isError, sample_a.model_dump()
    assert sample_a.structuredContent["rows"] == [[1], [2]]
    assert any(
        call["op"] == "submit"
        and call["endpoint"] == "http://proj-a.executor.test"
        and call["token"] == "caller-personal-tok"
        for call in calls
    )
    calls.clear()

    # sample_table_data on proj_b fails with unavailable (does NOT use default or proj_a)
    with bind_mcp_context(req, project_id=proj_b):
        sample_b = call_registered(
            server,
            "sample_table_data",
            {"table": "shop.orders", "columns": ["v"], "limit": 2},
        )
    assert sample_b.isError
    assert sample_b.structuredContent["code"] == "unavailable"
    assert "尚未启用查询执行器" in sample_b.structuredContent["message"]
    assert calls == []

    # sample_table_data on proj_doc fails cleanly
    with bind_mcp_context(req, project_id=proj_doc):
        sample_doc = call_registered(
            server,
            "sample_table_data",
            {"table": "shop.orders", "columns": ["v"], "limit": 2},
        )
    assert sample_doc.isError
    with bind_mcp_context(req, project_id=proj_doc):
        with pytest.raises(sample_api.ToolInvalidInput) as direct_doc:
            asyncio.run(
                sample_api.sample_table_data("shop.orders", project_id=proj_doc)
            )
        assert "文档型项目" in str(direct_doc.value)
    assert calls == []

    # discovery_executor on proj_a uses proj_a's endpoint + personal token
    res_a = asyncio.run(
        discovery_executor.execute_readonly_dtb_query(
            owner_id=req.id,
            sql="SELECT v FROM shop.orders LIMIT 5",
            limit=5,
            project_id=proj_a,
        )
    )
    assert res_a.rows == [[1], [2], [3]]
    assert [call["op"] for call in calls] == ["submit", "wait"]
    assert all(
        call["endpoint"] == "http://proj-a.executor.test"
        and call["token"] == "caller-personal-tok"
        for call in calls
    )
    calls.clear()

    # discovery_executor on proj_b raises 503 DiscoveryExecutorUnavailableError
    with pytest.raises(discovery_executor.DiscoveryExecutorUnavailableError) as exc_b:
        asyncio.run(
            discovery_executor.execute_readonly_dtb_query(
                owner_id=req.id,
                sql="SELECT v FROM shop.orders LIMIT 5",
                limit=5,
                project_id=proj_b,
            )
        )
    assert exc_b.value.status_code == 503
    assert "尚未启用查询执行器" in str(exc_b.value)

    # discovery_executor on proj_doc raises 422 DiscoveryExecutorUnavailableError
    with pytest.raises(discovery_executor.DiscoveryExecutorUnavailableError) as exc_doc:
        asyncio.run(
            discovery_executor.execute_readonly_dtb_query(
                owner_id=req.id,
                sql="SELECT v FROM shop.orders LIMIT 5",
                limit=5,
                project_id=proj_doc,
            )
        )
    assert exc_doc.value.status_code == 422
    assert "文档型项目" in str(exc_doc.value)
    assert calls == []


def test_conversation_confirm_query_uses_conversation_project_executor(
    env, monkeypatch
):
    import json

    from semantica_dataagent import agent_runner
    from semantica_dataagent.agent_runner import DONE, AgentEvent
    from semantica_dataagent.db import Conversation, Turn, TurnEvent

    c, admin, calls = env
    proj_a = _create_project(c, admin, "Conv Warehouse", "warehouse")

    # default has NO executor enabled; only proj_a has stub_exec with interpret_results=False
    assert (
        c.put(
            f"/api/v1/projects/{proj_a}/executors",
            headers=_headers(admin),
            json={
                "executors": [
                    {
                        "id": "stub_exec",
                        "enabled": True,
                        "parameters": {
                            "endpoint": "http://conv-proj-a.executor.test",
                            "row_limit": 10,
                            "download_limit": 50,
                            "interpret_results": False,
                        },
                    }
                ]
            },
        ).status_code
        == 200
    )
    assert (
        c.put(
            "/api/v1/me/dtb-token",
            headers=_headers(admin),
            json={"token": "conv-personal-tok"},
        ).status_code
        == 200
    )
    monkeypatch.setattr(knowledge, "describe", lambda **kwargs: [])

    seen_prompts: list[str] = []

    class _Runner:
        agent_id = "pi"
        effort_levels = ("low", "medium", "high")

        def available(self):
            return True

        async def run(self, request):
            seen_prompts.append(request.prompt)
            yield AgentEvent(DONE, {"text": "done", "sql": [], "citations": []})

    monkeypatch.setitem(agent_runner.RUNNERS, "pi", _Runner())
    prov = c.post(
        "/api/v1/providers",
        headers=_headers(admin),
        json={
            "name": "p1",
            "base_url": "http://127.0.0.1:1",
            "models": ["m1"],
        },
    )
    assert prov.status_code == 201, prov.text
    provider_id = prov.json()["id"]

    owner = _requester(admin)
    with sessions().begin() as db:
        db.add(
            Conversation(
                id="conv-a",
                owner_id=owner.id,
                project_id=proj_a,
                title="conv",
                agent="pi",
                provider_id=provider_id,
                model="m1",
            )
        )
        db.flush()
        turn = Turn(
            conversation_id="conv-a",
            provider_id=provider_id,
            model="m1",
            sequence=1,
            question="q",
            status="awaiting_confirmation",
            answer="",
            sql="",
            citations="",
        )
        db.add(turn)
        db.flush()
        turn_id = turn.id
        db.add(
            QueryExecution(
                id="conv-q-1",
                owner_id=owner.id,
                conversation_id="conv-a",
                turn_id=turn_id,
                plan_data=json.dumps({"project_id": proj_a, "execution_limit": 50}),
                sql="SELECT 1",
                status="planned",
            )
        )
        db.add(
            TurnEvent(
                turn_id=turn_id,
                seq=1,
                type="query_plan",
                data=json.dumps({"id": "conv-q-1"}),
            )
        )

    resp = c.post(
        f"/api/v1/conversations/conv-a/turns/{turn_id}/queries/conv-q-1/execute",
        headers=_headers(admin),
    )
    assert resp.status_code == 200, resp.text
    assert [call["op"] for call in calls] == ["submit", "wait"]
    assert all(
        call["endpoint"] == "http://conv-proj-a.executor.test"
        and call["token"] == "conv-personal-tok"
        for call in calls
    )
    # interpret_results=False from proj_a's executor config must be respected
    assert seen_prompts and '"rows"' not in seen_prompts[0]


def test_discovery_runner_and_fk_probing_use_target_project_executor(env, monkeypatch):
    from semantica_dataagent import discovery_runner, fk_probing
    from semantica_dataagent.fk_probing import FkCandidate

    c, admin, calls = env
    proj_a = _create_project(c, admin, "Disc A", "warehouse")
    proj_b = _create_project(c, admin, "Disc B", "warehouse")
    _enable_stub_executor(c, admin, proj_a, "http://disc-a.executor.test")

    assert (
        c.put(
            "/api/v1/me/dtb-token",
            headers=_headers(admin),
            json={"token": "disc-personal-tok"},
        ).status_code
        == 200
    )
    owner = _requester(admin)

    # default has NO executor, proj_b has NO executor, proj_a HAS stub_exec:
    # 1. discovery_runner pre-flight and async execution use proj_a's executor, not default
    run_a = discovery_runner.create_discovery_run(
        proj_a,
        owner_id=owner.id,
        mine_query_history=True,
        probe_relationships=False,
    )
    monkeypatch.setattr(knowledge, "describe", lambda **kwargs: [])
    asyncio.run(discovery_runner._execute_discovery_run_async(run_a["id"], owner.id))
    assert [call["op"] for call in calls] == ["submit", "wait"]
    assert all(
        call["endpoint"] == "http://disc-a.executor.test"
        and call["token"] == "disc-personal-tok"
        for call in calls
    )
    calls.clear()

    # 2. proj_b fails with 503 even if default & proj_a both have executors
    _enable_stub_executor(c, admin, "default", "http://default.executor.test")
    with pytest.raises(discovery_executor.DiscoveryExecutorUnavailableError) as exc_b:
        discovery_runner.create_discovery_run(proj_b, owner_id=owner.id)
    assert exc_b.value.status_code == 503

    r503 = c.post(f"/api/v1/discovery/runs?project={proj_b}", headers=_headers(admin))
    assert r503.status_code == 503, r503.text
    assert calls == []

    # 3. fk_probing.probe_fk_candidates resolves proj_a's executor when runtime=None,
    #    and rejects proj_b with 503 instead of falling back to default
    tables = [
        {
            "database": "shop",
            "name": "orders",
            "fields": [{"name": "user_id", "type": "bigint", "partition": False}],
        },
        {
            "database": "shop",
            "name": "users",
            "fields": [{"name": "id", "type": "bigint", "partition": False}],
        },
    ]
    cand = FkCandidate(
        from_table="shop.orders",
        from_catalog=None,
        from_database="shop",
        from_table_name="orders",
        from_columns=["user_id"],
        to_table="shop.users",
        to_catalog=None,
        to_database="shop",
        to_table_name="users",
        to_columns=["id"],
        recall_sources=["naming"],
    )
    asyncio.run(
        fk_probing.probe_fk_candidates(
            candidates=[cand],
            tables=tables,
            owner_id=owner.id,
            project_id=proj_a,
            runtime=None,
        )
    )
    assert calls and all(
        call["endpoint"] == "http://disc-a.executor.test"
        and call["token"] == "disc-personal-tok"
        for call in calls
    )
    calls.clear()

    with pytest.raises(discovery_executor.DiscoveryExecutorUnavailableError) as probe_b:
        asyncio.run(
            fk_probing.probe_fk_candidates(
                candidates=[cand],
                tables=tables,
                owner_id=owner.id,
                project_id=proj_b,
                runtime=None,
            )
        )
    assert probe_b.value.status_code == 503
    assert calls == []


def test_non_string_executor_url_is_rejected_with_503(env):
    from fastapi import HTTPException

    from semantica_dataagent import query_api

    with pytest.raises(HTTPException) as exc:
        query_api._validate_executor_url(None)
    assert exc.value.status_code == 503
    with pytest.raises(HTTPException) as exc2:
        query_api._validate_executor_url(123)
    assert exc2.value.status_code == 503
