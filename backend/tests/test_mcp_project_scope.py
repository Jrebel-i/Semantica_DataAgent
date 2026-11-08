"""Every registered MCP tool must read its resolved project (#178)."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from test_mcp_contract import call_registered

DEFAULT = "default"

TOOL_ARGUMENTS = {
    "describe_tables": {"names": ["shop.orders"]},
    "search_knowledge": {"query": "sales", "scope": "all", "top_k": 1},
    "lookup_dim_values": {"table": "shop.orders", "keyword": "paid"},
    "list_categories": {},
    "read_category_index": {"category": "metrics"},
    "read_document": {"doc_id": "document:metrics/sales.md"},
    "get_lineage": {"table": "shop.orders", "direction": "up", "depth": 1},
    "read_task_sql": {"task_id": "task-1"},
    "validate_sql": {"sql": "SELECT 1"},
    "find_join_path": {"tables": ["shop.orders", "shop.items"]},
    "request_clarification": {
        "questions": [
            {
                "question": "Which definition?",
                "options": [
                    {"label": "paid", "ref": "document:metrics/paid.md"},
                    {"label": "ordered", "ref": "document:metrics/ordered.md"},
                ],
            }
        ]
    },
    "report_knowledge_gap": {"question": "Which definition?", "missing": "paid"},
    "propose_correction": {
        "target": "document:metrics/sales.md",
        "patch": "new text\n",
        "reason": "fix",
    },
    "ask_dataagent": {"question": "What is sales?"},
    "sample_table_data": {"table": "shop.orders"},
    "propose_query": {"sql": "SELECT 1"},
    "execute_query": {"sql": "SELECT 1"},
    "get_capabilities": {},
}


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


def _project(c: TestClient, admin: str, name: str) -> dict:
    response = c.post(
        "/api/v1/projects",
        headers={"Authorization": f"Bearer {admin}"},
        json={"name": name, "type": "document"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _requester(token: str):
    from semantica_dataagent.auth import Requester
    from semantica_dataagent.db import Token, sessions

    with sessions()() as db:
        row = (
            db.query(Token)
            .filter(
                Token.digest == __import__("hashlib").sha256(token.encode()).hexdigest()
            )
            .one()
        )
    return Requester(id=row.id, role=row.role, token=token)


def test_every_registered_tool_reads_the_bound_project(client, monkeypatch):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import create_mcp
    from semantica_dataagent.db import KnowledgeGap, sessions
    from semantica_dataagent.mcp_context import bind_mcp_context

    c, admin = client
    project_b = _project(c, admin, "Scope B")
    seen: list[str | None] = []

    def fake_settings(project_id=None):
        seen.append(project_id)
        return {
            "knowledge_generation": "default",
            "_project_id": project_id or DEFAULT,
            "neo4j_uri": "bolt://127.0.0.1:1",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
            "milvus_uri": "http://127.0.0.1:1",
        }

    monkeypatch.setattr(knowledge, "settings", fake_settings)
    monkeypatch.setattr(knowledge, "read_graph", lambda values: object())
    monkeypatch.setattr(knowledge, "execute", lambda *args, **kwargs: [])
    monkeypatch.setattr(knowledge, "read_vectors", lambda values: object())
    monkeypatch.setattr(
        knowledge, "embed", lambda values, texts: [[0.0, 1.0] for _ in texts]
    )

    server = create_mcp()
    requester = _requester(admin)
    with bind_mcp_context(requester, project_id=project_b["id"]):
        for name, arguments in TOOL_ARGUMENTS.items():
            before = len(seen)
            result = call_registered(server, name, arguments)
            if name == "ask_dataagent":
                assert result.isError
                assert (
                    "项目问答将在对话绑定项目后支持"
                    in (result.structuredContent["message"])
                )
                continue
            if name == "report_knowledge_gap":
                with sessions()() as db:
                    row = (
                        db.query(KnowledgeGap)
                        .order_by(KnowledgeGap.created_at.desc())
                        .first()
                    )
                assert row is not None and row.project_id == project_b["id"], (
                    name,
                    row.project_id if row else None,
                )
                continue
            assert any(value == project_b["id"] for value in seen[before:]), (
                name,
                seen[before:],
                result.structuredContent
                if hasattr(result, "structuredContent")
                else result,
            )


def test_new_mcp_tools_must_use_the_project_context():
    from semantica_dataagent.app import create_mcp

    missing = []
    for tool in create_mcp()._tool_manager.list_tools():
        source = inspect.getsource(tool.fn)
        if "_mcp_project_id" not in source and "_mcp_project_arg" not in source:
            missing.append(tool.name)
    assert not missing, (
        "MCP tools bypass the project context; call _mcp_project_id() or add a "
        f"justified whitelist entry: {missing}"
    )


def _list_tools(server):
    import asyncio

    from mcp.types import ListToolsRequest

    async def run():
        handler = server._mcp_server.request_handlers[ListToolsRequest]
        return (await handler(ListToolsRequest(method="tools/list"))).root.tools

    return {tool.name for tool in asyncio.run(run())}


def test_tools_list_is_filtered_by_the_resolved_project_type(client):
    from semantica_dataagent.app import create_mcp
    from semantica_dataagent.mcp_context import bind_mcp_context
    from semantica_dataagent.project_types import (
        DOCUMENT_TOOL_NAMES,
        TOOL_PROJECT_TYPES,
    )

    c, admin = client
    project = _project(c, admin, "Document project")
    server = create_mcp()
    requester = _requester(admin)

    with bind_mcp_context(requester, project_id=DEFAULT):
        assert _list_tools(server) == set(TOOL_PROJECT_TYPES)
    with bind_mcp_context(requester, project_id=project["id"]):
        assert _list_tools(server) == DOCUMENT_TOOL_NAMES


def test_document_project_rejects_warehouse_only_tools(client):
    from semantica_dataagent.app import create_mcp
    from semantica_dataagent.mcp_context import bind_mcp_context

    c, admin = client
    project = _project(c, admin, "Document project")
    server = create_mcp()
    requester = _requester(admin)
    with bind_mcp_context(requester, project_id=project["id"]):
        result = call_registered(server, "describe_tables", {"names": ["shop.orders"]})
    assert result.isError
    assert "该项目类型不支持此工具" in result.structuredContent["message"]
