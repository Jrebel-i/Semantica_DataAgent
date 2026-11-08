"""Conversations bind to one project from creation until deletion (#184)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient

from semantica_dataagent.agent_runner import DONE, AgentEvent
from semantica_dataagent.auth import authenticate_mcp

DEFAULT = "default"
DEFAULT_NAME = "业务数据"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_AGENT_ROOT", str(tmp_path / "agents"))
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    token = initialize()
    with TestClient(app) as test_client:
        yield test_client, token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name is not None:
            events.append((name, data))
    return events


class CapturingRunner:
    agent_id = "pi"

    def __init__(self):
        self.requests = []
        self.credential_projects: list[str | None] = []

    def available(self):
        return True

    async def run(self, request):
        self.requests.append(request)
        requirement = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=request.mcp_token
        )
        requester = authenticate_mcp(
            requirement, request.conversation_id, str(request.turn_id)
        )
        self.credential_projects.append(requester.project_id)
        yield AgentEvent(
            DONE,
            {
                "text": "已按项目知识回答。",
                "sql": [],
                "citations": [],
                "stop_reason": "stop",
            },
        )


def _project(c: TestClient, admin: str, name: str, kind: str = "document") -> dict:
    response = c.post(
        "/api/v1/projects",
        headers=_headers(admin),
        json={"name": name, "type": kind},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _user_token(c: TestClient, admin: str, name: str, projects: list[dict]) -> dict:
    response = c.post(
        "/api/v1/tokens",
        headers=_headers(admin),
        json={"name": name, "role": "user", "projects": projects},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _provider(c: TestClient, admin: str) -> dict:
    response = c.post(
        "/api/v1/providers",
        headers=_headers(admin),
        json={
            "name": "Local gateway",
            "base_url": "http://127.0.0.1:3425",
            "models": ["project-model"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _ask(c: TestClient, token: str, body: dict) -> tuple[int, str]:
    response = c.post(
        "/api/v1/conversations/ask",
        headers=_headers(token),
        json=body,
    )
    return response.status_code, response.text


def _start(c: TestClient, token: str, provider: dict, project: str | None, q="销售额"):
    payload = {
        "question": q,
        "agent": "pi",
        "provider_id": provider["id"],
        "model": "project-model",
    }
    if project is not None:
        payload["project"] = project
    status, text = _ask(c, token, payload)
    assert status == 200, text
    return _sse(text)[0][1]["conversation_id"]


def test_new_conversation_binds_its_project_and_agent_credential(client, monkeypatch):
    from semantica_dataagent import agent_runner

    c, admin = client
    project_b = _project(c, admin, "Issue 184 B")
    member = _user_token(
        c, admin, "member-b", [{"project_id": project_b["id"], "role": "member"}]
    )
    provider = _provider(c, admin)
    runner = CapturingRunner()
    monkeypatch.setitem(agent_runner.RUNNERS, "pi", runner)

    conversation_id = _start(c, member["token"], provider, project_b["id"])

    detail = c.get(
        f"/api/v1/conversations/{conversation_id}", headers=_headers(member["token"])
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["project_id"] == project_b["id"]
    assert detail.json()["project_name"] == "Issue 184 B"
    listing = c.get("/api/v1/conversations", headers=_headers(member["token"]))
    row = next(item for item in listing.json() if item["id"] == conversation_id)
    assert row["project_id"] == project_b["id"]
    assert row["project_name"] == "Issue 184 B"
    assert runner.credential_projects == [project_b["id"]]


def test_new_conversation_without_project_uses_the_default_rule(client, monkeypatch):
    from semantica_dataagent import agent_runner

    c, admin = client
    provider = _provider(c, admin)
    monkeypatch.setitem(agent_runner.RUNNERS, "pi", CapturingRunner())

    conversation_id = _start(c, admin, provider, None)

    detail = c.get(
        f"/api/v1/conversations/{conversation_id}", headers=_headers(admin)
    ).json()
    assert detail["project_id"] == DEFAULT
    assert detail["project_name"] == DEFAULT_NAME


def test_new_conversation_without_project_uses_the_only_accessible_project(
    client, monkeypatch
):
    from semantica_dataagent import agent_runner

    c, admin = client
    project_b = _project(c, admin, "Issue 184 Only B")
    member = _user_token(
        c, admin, "only-b", [{"project_id": project_b["id"], "role": "member"}]
    )
    provider = _provider(c, admin)
    monkeypatch.setitem(agent_runner.RUNNERS, "pi", CapturingRunner())

    conversation_id = _start(c, member["token"], provider, None)

    detail = c.get(
        f"/api/v1/conversations/{conversation_id}", headers=_headers(member["token"])
    ).json()
    assert detail["project_id"] == project_b["id"]


def test_cannot_start_a_conversation_in_a_project_without_access(client, monkeypatch):
    from semantica_dataagent import agent_runner

    c, admin = client
    project_a = _project(c, admin, "Issue 184 A")
    project_b = _project(c, admin, "Issue 184 B2")
    member_a = _user_token(
        c, admin, "member-a", [{"project_id": project_a["id"], "role": "member"}]
    )
    provider = _provider(c, admin)
    monkeypatch.setitem(agent_runner.RUNNERS, "pi", CapturingRunner())

    status, text = _ask(
        c,
        member_a["token"],
        {
            "question": "销售额",
            "agent": "pi",
            "provider_id": provider["id"],
            "model": "project-model",
            "project": project_b["id"],
        },
    )
    assert status == 404, text
    assert (
        c.get("/api/v1/conversations", headers=_headers(member_a["token"])).json() == []
    )

    status, text = _ask(
        c,
        member_a["token"],
        {
            "question": "销售额",
            "agent": "pi",
            "provider_id": provider["id"],
            "model": "project-model",
            "project": "does-not-exist",
        },
    )
    assert status == 404, text


def test_follow_up_cannot_change_the_project(client, monkeypatch):
    from semantica_dataagent import agent_runner

    c, admin = client
    project_a = _project(c, admin, "Issue 184 Follow A")
    project_b = _project(c, admin, "Issue 184 Follow B")
    member = _user_token(
        c,
        admin,
        "follow-member",
        [
            {"project_id": project_a["id"], "role": "member"},
            {"project_id": project_b["id"], "role": "member"},
        ],
    )
    provider = _provider(c, admin)
    runner = CapturingRunner()
    monkeypatch.setitem(agent_runner.RUNNERS, "pi", runner)
    conversation_id = _start(c, member["token"], provider, project_a["id"])

    status, text = _ask(
        c,
        member["token"],
        {
            "question": "换项目",
            "conversation_id": conversation_id,
            "project": project_b["id"],
        },
    )
    assert status == 409, text
    assert "对话已绑定其它项目，请新建对话" in text

    status, text = _ask(
        c,
        member["token"],
        {
            "question": "继续",
            "conversation_id": conversation_id,
            "project": project_a["id"],
        },
    )
    assert status == 200, text
    status, text = _ask(
        c,
        member["token"],
        {"question": "沿用", "conversation_id": conversation_id},
    )
    assert status == 200, text
    detail = c.get(
        f"/api/v1/conversations/{conversation_id}", headers=_headers(member["token"])
    ).json()
    assert detail["project_id"] == project_a["id"]
    assert len(detail["turns"]) == 3
    assert runner.credential_projects == [
        project_a["id"],
        project_a["id"],
        project_a["id"],
    ]


def test_list_filter_and_lost_project_access(client, monkeypatch):
    from semantica_dataagent import agent_runner

    c, admin = client
    project_a = _project(c, admin, "Issue 184 List A")
    project_b = _project(c, admin, "Issue 184 List B")
    member = _user_token(
        c,
        admin,
        "list-member",
        [
            {"project_id": project_a["id"], "role": "member"},
            {"project_id": project_b["id"], "role": "member"},
        ],
    )
    provider = _provider(c, admin)
    monkeypatch.setitem(agent_runner.RUNNERS, "pi", CapturingRunner())
    conv_a = _start(c, member["token"], provider, project_a["id"])
    conv_b = _start(c, member["token"], provider, project_b["id"], q="客户")

    rows = c.get("/api/v1/conversations", headers=_headers(member["token"])).json()
    assert {row["id"] for row in rows} == {conv_a, conv_b}

    filtered = c.get(
        f"/api/v1/conversations?project={project_b['id']}",
        headers=_headers(member["token"]),
    )
    assert [row["id"] for row in filtered.json()] == [conv_b]

    removed = c.delete(
        f"/api/v1/projects/{project_b['id']}/members/{member['id']}",
        headers=_headers(admin),
    )
    assert removed.status_code == 204, removed.text

    remaining = c.get("/api/v1/conversations", headers=_headers(member["token"]))
    assert [row["id"] for row in remaining.json()] == [conv_a]
    assert (
        c.get(
            f"/api/v1/conversations?project={project_b['id']}",
            headers=_headers(member["token"]),
        ).status_code
        == 404
    )
    detail = c.get(f"/api/v1/conversations/{conv_b}", headers=_headers(member["token"]))
    assert detail.status_code == 200
    assert detail.json()["project_id"] == project_b["id"]
    assert (
        c.get(
            f"/api/v1/conversations/{conv_b}",
            headers=_headers(admin),
        ).status_code
        == 404
    )


def test_conversation_management_still_only_needs_ownership(client, monkeypatch):
    from semantica_dataagent import agent_runner

    c, admin = client
    project_b = _project(c, admin, "Issue 184 Manage B")
    member = _user_token(
        c, admin, "manage-b", [{"project_id": project_b["id"], "role": "member"}]
    )
    provider = _provider(c, admin)
    monkeypatch.setitem(agent_runner.RUNNERS, "pi", CapturingRunner())
    conversation_id = _start(c, member["token"], provider, project_b["id"])

    renamed = c.patch(
        f"/api/v1/conversations/{conversation_id}",
        headers=_headers(member["token"]),
        json={"title": "新标题", "pinned": True},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["project_id"] == project_b["id"]
    assert renamed.json()["title"] == "新标题"
    archived = c.patch(
        f"/api/v1/conversations/{conversation_id}",
        headers=_headers(member["token"]),
        json={"archived": True},
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived"] is True
    assert (
        c.get("/api/v1/conversations", headers=_headers(member["token"])).json() == []
    )
    assert [
        row["id"]
        for row in c.get(
            "/api/v1/conversations?archived=true", headers=_headers(member["token"])
        ).json()
    ] == [conversation_id]


def _rpc(
    c: TestClient,
    token: str,
    name: str,
    arguments: dict | None = None,
    *,
    project: str | None = None,
):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Host": "localhost:8000",
        "Content-Type": "application/json",
    }
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


def test_mcp_ask_creates_a_conversation_in_the_resolved_project(client, monkeypatch):
    from semantica_dataagent import agent_runner

    c, admin = client
    project_b = _project(c, admin, "Issue 184 MCP B")
    member = _user_token(
        c, admin, "mcp-b", [{"project_id": project_b["id"], "role": "member"}]
    )
    provider = _provider(c, admin)
    settings = c.put(
        "/api/v1/settings",
        headers=_headers(admin),
        json={
            "values": {
                "assistant_agent": "pi",
                "assistant_provider_id": provider["id"],
                "assistant_model": "project-model",
            }
        },
    )
    assert settings.status_code == 200, settings.text
    runner = CapturingRunner()
    monkeypatch.setitem(agent_runner.RUNNERS, "pi", runner)

    result = _rpc(
        c,
        member["token"],
        "ask_dataagent",
        {"question": "这个项目里有什么？"},
        project=project_b["id"],
    )
    assert result.get("isError") is not True, result
    conversation_id = result["structuredContent"]["conversation_id"]

    detail = c.get(
        f"/api/v1/conversations/{conversation_id}", headers=_headers(member["token"])
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["project_id"] == project_b["id"]
    assert runner.credential_projects == [project_b["id"]]


def test_mcp_follow_up_rejects_a_different_project(client, monkeypatch):
    from semantica_dataagent import agent_runner

    c, admin = client
    project_a = _project(c, admin, "Issue 184 MCP A")
    project_b = _project(c, admin, "Issue 184 MCP B2")
    member = _user_token(
        c,
        admin,
        "mcp-both",
        [
            {"project_id": project_a["id"], "role": "member"},
            {"project_id": project_b["id"], "role": "member"},
        ],
    )
    provider = _provider(c, admin)
    c.put(
        "/api/v1/settings",
        headers=_headers(admin),
        json={
            "values": {
                "assistant_agent": "pi",
                "assistant_provider_id": provider["id"],
                "assistant_model": "project-model",
            }
        },
    )
    monkeypatch.setitem(agent_runner.RUNNERS, "pi", CapturingRunner())
    first = _rpc(
        c,
        member["token"],
        "ask_dataagent",
        {"question": "在 A 提问"},
        project=project_a["id"],
    )
    conversation_id = first["structuredContent"]["conversation_id"]

    second = _rpc(
        c,
        member["token"],
        "ask_dataagent",
        {"question": "换到 B", "conversation_id": conversation_id},
        project=project_b["id"],
    )
    assert second.get("isError") is True, second
    assert "对话已绑定其它项目，请新建对话" in second["structuredContent"]["message"]
