"""Project resolution and context binding for MCP requests (#178)."""

from __future__ import annotations

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


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _project(c: TestClient, admin: str, name: str) -> dict:
    response = c.post(
        "/api/v1/projects",
        headers=_headers(admin),
        json={"name": name, "type": "document"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _token(c: TestClient, admin: str, name: str, projects: list[dict] | None):
    payload: dict = {"name": name, "role": "user"}
    if projects is not None:
        payload["projects"] = projects
    response = c.post("/api/v1/tokens", headers=_headers(admin), json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _requester(token: dict):
    from semantica_dataagent.auth import Requester

    return Requester(id=token["id"], role=token["role"], token=token["token"])


def _error(function, *args):
    from semantica_dataagent.mcp_contract import ToolInvalidInput

    with pytest.raises(ToolInvalidInput) as caught:
        function(*args)
    return str(caught.value)


def test_bind_mcp_context_binds_and_resets_project_after_exceptions():
    from semantica_dataagent.auth import Requester
    from semantica_dataagent.mcp_context import (
        bind_mcp_context,
        current_project_id,
    )

    requester = Requester(id="owner", role="user", token="secret")
    assert current_project_id() == DEFAULT
    with bind_mcp_context(requester, project_id="project-b"):
        assert current_project_id() == "project-b"
    assert current_project_id() == DEFAULT

    with (
        pytest.raises(RuntimeError),
        bind_mcp_context(requester, project_id="project-c"),
    ):
        assert current_project_id() == "project-c"
        raise RuntimeError("tool crashed")
    assert current_project_id() == DEFAULT


def test_bind_mcp_context_raises_the_bound_selection_error():
    from semantica_dataagent.auth import Requester
    from semantica_dataagent.mcp_context import bind_mcp_context, current_project_id
    from semantica_dataagent.mcp_contract import ToolInvalidInput

    requester = Requester(id="owner", role="user", token="secret")
    with (
        bind_mcp_context(requester, project_error="请指定项目：可访问的项目有 b（B）"),
        pytest.raises(ToolInvalidInput, match="请指定项目"),
    ):
        current_project_id()


def test_credential_project_wins_and_cannot_be_overridden(client):
    from semantica_dataagent.mcp_project import resolve_mcp_project

    c, admin = client
    project_b = _project(c, admin, "Context B")
    admin_requester = _requester(
        {"id": "admin", "role": "admin", "token": admin}
    )

    assert (
        resolve_mcp_project(admin_requester, DEFAULT, None) == DEFAULT
    )
    assert _error(resolve_mcp_project, admin_requester, DEFAULT, project_b["id"]) == (
        "项目不存在"
    )


def test_request_selection_uses_the_rest_access_rule(client):
    from semantica_dataagent.mcp_project import resolve_mcp_project

    c, admin = client
    project_b = _project(c, admin, "Context Select B")
    only_a = _requester(
        _token(c, admin, "only-a", [{"project_id": DEFAULT, "role": "member"}])
    )
    only_b = _requester(
        _token(c, admin, "only-b", [{"project_id": project_b["id"], "role": "member"}])
    )

    assert resolve_mcp_project(only_b, None, project_b["id"]) == project_b["id"]
    assert resolve_mcp_project(only_b, None, None) == project_b["id"]
    assert _error(resolve_mcp_project, only_a, None, project_b["id"]) == "项目不存在"
    assert _error(resolve_mcp_project, only_a, None, "missing") == "项目不存在"


def test_multi_project_token_must_choose_and_error_lists_names(client):
    from semantica_dataagent.mcp_project import resolve_mcp_project

    c, admin = client
    project_b = _project(c, admin, "Context Multi B")
    both = _requester(
        _token(
            c,
            admin,
            "both",
            [
                {"project_id": DEFAULT, "role": "member"},
                {"project_id": project_b["id"], "role": "member"},
            ],
        )
    )

    message = _error(resolve_mcp_project, both, None, None)

    assert "请指定项目" in message
    assert DEFAULT in message and "业务数据" in message
    assert project_b["id"] in message and project_b["name"] in message


def test_system_administrator_defaults_to_default_but_can_select(client):
    from semantica_dataagent.mcp_project import resolve_mcp_project

    c, admin = client
    project_b = _project(c, admin, "Context Admin B")
    requester = _requester({"id": "admin", "role": "admin", "token": admin})

    assert resolve_mcp_project(requester, None, None) == DEFAULT
    assert resolve_mcp_project(requester, None, project_b["id"]) == project_b["id"]


def test_token_without_projects_gets_a_clear_selection_error(client):
    from semantica_dataagent.mcp_project import resolve_mcp_project

    c, admin = client
    outsider = _requester(_token(c, admin, "outsider", []))

    assert "请指定项目" in _error(resolve_mcp_project, outsider, None, None)
    assert _error(resolve_mcp_project, outsider, None, DEFAULT) == "项目不存在"
