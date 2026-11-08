"""MCP tool calls resolve and enforce one project (#178).

Every check enters through the real MCP HTTP boundary so it also covers the
middleware that binds the caller, conversation and project.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

DEFAULT = "default"


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


def _headers(token: str, **extra: str) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Host": "localhost:8000",
        "Content-Type": "application/json",
    }
    headers.update(extra)
    return headers


def _rpc(
    c: TestClient,
    token: str,
    name: str,
    arguments: dict | None = None,
    *,
    project: str | None = None,
    project_header: str | None = None,
    extra_headers: dict[str, str] | None = None,
):
    headers = _headers(token, **(extra_headers or {}))
    if project_header is not None:
        headers["X-Dataagent-Project"] = project_header
    path = "/mcp/"
    if project is not None:
        path += f"?project={project}"
    response = c.post(
        path,
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "result" in body, body
    return body["result"]


def _create_project(c: TestClient, admin: str, name: str) -> dict:
    response = c.post(
        "/api/v1/projects",
        headers=_headers(admin),
        json={"name": name, "type": "document"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_token(
    c: TestClient, admin: str, name: str, projects: list[dict] | None
) -> dict:
    payload: dict = {"name": name, "role": "user"}
    if projects is not None:
        payload["projects"] = projects
    response = c.post("/api/v1/tokens", headers=_headers(admin), json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _tool_error(result: dict) -> dict:
    assert result.get("isError") is True, result
    return result["structuredContent"]


def _credential(owner_id: str, conversation_id: str, turn_id: int) -> str:
    from semantica_dataagent.auth import issue_agent_credential

    return issue_agent_credential(owner_id, conversation_id, turn_id)


def _add_running_turn(owner_id: str) -> tuple[str, int]:
    from semantica_dataagent.db import Conversation, Turn, sessions

    conversation_id = "conversation-" + owner_id
    with sessions().begin() as db:
        db.add(
            Conversation(
                id=conversation_id,
                owner_id=owner_id,
                title="project scope",
                agent="pi",
                provider_id="provider",
                model="model",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        turn = Turn(
            conversation_id=conversation_id,
            provider_id="provider",
            model="model",
            sequence=1,
            question="project scope",
            status="running",
            answer="",
            sql="[]",
            citations="[]",
            started_at=datetime.now(UTC),
        )
        db.add(turn)
        db.flush()
        return conversation_id, turn.id


def test_single_project_token_uses_its_only_project_without_selection(client):
    c, admin = client
    project_b = _create_project(c, admin, "Issue 178 B")
    only_b = _create_token(
        c, admin, "only-b", [{"project_id": project_b["id"], "role": "member"}]
    )

    capabilities = _rpc(c, only_b["token"], "get_capabilities")

    assert capabilities.get("isError") is not True, capabilities
    assert capabilities["project"]["id"] == project_b["id"]
    assert [row["id"] for row in capabilities["projects"]] == [project_b["id"]]


def test_multi_project_token_requires_selection_and_lists_its_projects(client):
    c, admin = client
    project_b = _create_project(c, admin, "Issue 178 Multi B")
    both = _create_token(
        c,
        admin,
        "both",
        [
            {"project_id": DEFAULT, "role": "member"},
            {"project_id": project_b["id"], "role": "member"},
        ],
    )

    error = _tool_error(_rpc(c, both["token"], "get_capabilities"))

    assert error["code"] == "invalid_input"
    assert "请指定项目" in error["message"]
    assert DEFAULT in error["message"]
    assert project_b["id"] in error["message"]
    assert "业务数据" in error["message"]
    assert project_b["name"] in error["message"]


def test_project_can_be_selected_by_query_or_header(client):
    c, admin = client
    project_b = _create_project(c, admin, "Issue 178 Select B")
    both = _create_token(
        c,
        admin,
        "both-select",
        [
            {"project_id": DEFAULT, "role": "member"},
            {"project_id": project_b["id"], "role": "member"},
        ],
    )

    by_query = _rpc(c, both["token"], "get_capabilities", project=project_b["id"])
    by_header = _rpc(
        c,
        both["token"],
        "get_capabilities",
        project_header=project_b["id"],
    )

    assert by_query["project"]["id"] == project_b["id"]
    assert by_header["project"]["id"] == project_b["id"]


def test_missing_and_forbidden_projects_are_indistinguishable(client):
    c, admin = client
    project_b = _create_project(c, admin, "Issue 178 Hidden B")
    only_a = _create_token(
        c, admin, "only-a", [{"project_id": DEFAULT, "role": "member"}]
    )

    forbidden = _tool_error(
        _rpc(c, only_a["token"], "get_capabilities", project=project_b["id"])
    )
    missing = _tool_error(
        _rpc(c, only_a["token"], "get_capabilities", project="does-not-exist")
    )

    assert forbidden["code"] == missing["code"] == "invalid_input"
    assert forbidden["message"] == missing["message"] == "项目不存在"


def test_agent_credential_project_cannot_be_overridden(client):
    c, admin = client
    project_b = _create_project(c, admin, "Issue 178 Credential B")
    tokens = c.get("/api/v1/tokens", headers=_headers(admin)).json()
    owner = next(row for row in tokens if row["role"] == "admin")
    conversation_id, turn_id = _add_running_turn(owner["id"])
    credential = _credential(owner["id"], conversation_id, turn_id)
    credential_headers = {
        "X-Dataagent-Conversation-Id": conversation_id,
        "X-Dataagent-Turn-Id": str(turn_id),
    }

    bound = _rpc(
        c,
        credential,
        "get_capabilities",
        extra_headers=credential_headers,
    )
    assert bound["project"]["id"] == DEFAULT

    for selection in (
        {"project": project_b["id"]},
        {"project_header": project_b["id"]},
    ):
        error = _tool_error(
            _rpc(
                c,
                credential,
                "get_capabilities",
                extra_headers=credential_headers,
                **selection,
            )
        )
        assert error["code"] == "invalid_input"
        assert error["message"] == "项目不存在"


def test_mcp_tools_do_not_accept_a_project_argument_bypass(client):
    c, admin = client
    project_b = _create_project(c, admin, "Issue 178 Argument B")

    error = _tool_error(
        _rpc(
            c,
            admin,
            "search_knowledge",
            {
                "query": "anything",
                "scope": "docs",
                "project_id": project_b["id"],
            },
        )
    )

    assert error["code"] == "invalid_input"
    assert "project_id" in error["message"] or "schema" in error["message"]


def test_get_capabilities_lists_projects_for_a_system_administrator(client):
    c, admin = client
    project_b = _create_project(c, admin, "Issue 178 Admin B")

    capabilities = _rpc(c, admin, "get_capabilities")

    assert capabilities["project"]["id"] == DEFAULT
    listed = {row["id"]: row for row in capabilities["projects"]}
    assert set(listed) == {DEFAULT, project_b["id"]}
    assert listed[DEFAULT]["name"] == "业务数据"
    assert listed[DEFAULT]["type"] == "warehouse"
    assert listed[project_b["id"]]["type"] == "document"
