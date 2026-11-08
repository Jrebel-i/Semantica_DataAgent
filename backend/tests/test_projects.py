"""Project records, membership migration behavior, and the projects REST API."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

DEFAULT_PROJECT_ID = "default"
DEFAULT_PROJECT_NAME = "业务数据"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    token = initialize()
    with TestClient(app) as test_client:
        yield test_client, token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _execute(sql: str, params: tuple = ()) -> None:
    try:
        with sqlite3.connect(os.environ["DATAAGENT_DB_PATH"]) as db:
            db.execute(sql, params)
    except sqlite3.OperationalError as exc:
        pytest.fail(f"project storage is not available: {exc}")


def _fetch(sql: str, params: tuple = ()) -> list[tuple]:
    try:
        with sqlite3.connect(os.environ["DATAAGENT_DB_PATH"]) as db:
            return db.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        pytest.fail(f"project storage is not available: {exc}")


def _add_project(project_id: str, name: str, project_type: str = "warehouse") -> None:
    _execute(
        "INSERT INTO projects (id, name, type, status, created_at) "
        "VALUES (?, ?, ?, 'active', '2026-01-01 00:00:00')",
        (project_id, name, project_type),
    )


def _add_member(project_id: str, token_id: str, role: str = "member") -> None:
    _execute(
        "INSERT INTO project_members (project_id, token_id, role) VALUES (?, ?, ?)",
        (project_id, token_id, role),
    )


def _create_token(c: TestClient, admin_token: str, name: str, role: str) -> dict:
    response = c.post(
        "/api/v1/tokens",
        headers=_headers(admin_token),
        json={"name": name, "role": role},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_projects_require_authentication(client):
    c, _ = client
    assert c.get("/api/v1/projects").status_code == 401
    assert (
        c.patch(
            f"/api/v1/projects/{DEFAULT_PROJECT_ID}", json={"name": "改名"}
        ).status_code
        == 401
    )


def test_system_admin_sees_all_projects_and_can_rename_the_default(client):
    c, admin_token = client
    headers = _headers(admin_token)
    response = c.get("/api/v1/projects", headers=headers)
    assert response.status_code == 200, response.text
    projects = response.json()
    assert len(projects) == 1
    default = projects[0]
    assert set(default) == {
        "id",
        "name",
        "type",
        "status",
        "created_at",
        "my_role",
    }
    assert default["id"] == DEFAULT_PROJECT_ID
    assert default["name"] == DEFAULT_PROJECT_NAME
    assert default["type"] == "warehouse"
    assert default["status"] == "active"
    assert default["my_role"] == "admin"
    assert default["created_at"].endswith("Z")

    renamed = c.patch(
        f"/api/v1/projects/{DEFAULT_PROJECT_ID}",
        headers=headers,
        json={"name": "业务数据平台"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "业务数据平台"
    assert renamed.json()["type"] == "warehouse"
    assert renamed.json()["status"] == "active"

    listing = c.get("/api/v1/projects", headers=headers).json()
    assert listing[0]["name"] == "业务数据平台"

    from semantica_dataagent import project_context

    assert project_context.current_project().name == "业务数据平台"


def test_project_name_is_the_only_mutable_field(client):
    c, admin_token = client
    headers = _headers(admin_token)
    for payload in (
        {"name": "x", "type": "document"},
        {"name": "x", "status": "archived"},
    ):
        response = c.patch(
            f"/api/v1/projects/{DEFAULT_PROJECT_ID}", headers=headers, json=payload
        )
        assert response.status_code == 422, response.text
    for name in ("", "   "):
        response = c.patch(
            f"/api/v1/projects/{DEFAULT_PROJECT_ID}",
            headers=headers,
            json={"name": name},
        )
        assert response.status_code == 422, response.text
    assert (
        c.patch(
            "/api/v1/projects/missing", headers=headers, json={"name": "x"}
        ).status_code
        == 404
    )
    current = c.get("/api/v1/projects", headers=headers).json()[0]
    assert current["name"] == DEFAULT_PROJECT_NAME
    assert current["type"] == "warehouse"
    assert current["status"] == "active"


def test_regular_users_only_see_projects_they_belong_to(client):
    c, admin_token = client
    admin_headers = _headers(admin_token)
    user = _create_token(c, admin_token, "Alice", "user")
    user_headers = _headers(user["token"])
    _add_project("docs", "平台文档", "document")
    _add_project("secret", "别人的项目", "warehouse")
    _add_member("docs", user["id"], "member")

    listing = c.get("/api/v1/projects", headers=user_headers)
    assert listing.status_code == 200, listing.text
    by_id = {item["id"]: item for item in listing.json()}
    assert set(by_id) == {DEFAULT_PROJECT_ID, "docs"}
    assert by_id[DEFAULT_PROJECT_ID]["my_role"] == "member"
    assert by_id["docs"]["my_role"] == "member"
    assert by_id["docs"]["type"] == "document"

    admin_listing = c.get("/api/v1/projects", headers=admin_headers)
    assert {item["id"] for item in admin_listing.json()} == {
        DEFAULT_PROJECT_ID,
        "docs",
        "secret",
    }
    assert all(item["my_role"] == "admin" for item in admin_listing.json())

    _add_member("secret", user["id"], "admin")
    updated = {item["id"]: item for item in c.get(
        "/api/v1/projects", headers=user_headers
    ).json()}
    assert updated["secret"]["my_role"] == "admin"


def test_project_managers_still_cannot_rename_projects(client):
    c, admin_token = client
    user = _create_token(c, admin_token, "Bob", "user")
    _add_member(DEFAULT_PROJECT_ID, user["id"], "admin")
    response = c.patch(
        f"/api/v1/projects/{DEFAULT_PROJECT_ID}",
        headers=_headers(user["token"]),
        json={"name": "越权改名"},
    )
    assert response.status_code == 403, response.text


def test_new_tokens_join_the_default_project_as_members(client):
    c, admin_token = client
    user = _create_token(c, admin_token, "Carol", "user")
    admin = _create_token(c, admin_token, "Second admin", "admin")

    assert _fetch(
        "SELECT project_id, role FROM project_members WHERE token_id = ?",
        (user["id"],),
    ) == [(DEFAULT_PROJECT_ID, "member")]
    assert (
        _fetch(
            "SELECT project_id FROM project_members WHERE token_id = ?",
            (admin["id"],),
        )
        == []
    )
    listed = c.get("/api/v1/projects", headers=_headers(user["token"]))
    assert listed.status_code == 200
    assert [item["my_role"] for item in listed.json()] == ["member"]
