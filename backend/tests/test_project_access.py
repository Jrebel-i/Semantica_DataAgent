"""Project member access, management rules, token project lists and audit (#177)."""

from __future__ import annotations

import sqlite3
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


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create_project(client: TestClient, admin: str, name: str) -> dict:
    response = client.post(
        "/api/v1/projects",
        headers=headers(admin),
        json={"name": name, "type": "document"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_token(
    client: TestClient, admin: str, name: str, projects: list[dict] | None
) -> dict:
    payload: dict = {"name": name, "role": "user"}
    if projects is not None:
        payload["projects"] = projects
    response = client.post("/api/v1/tokens", headers=headers(admin), json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def fetch(sql: str, params: tuple = ()) -> list[tuple]:
    with sqlite3.connect(__import__("os").environ["DATAAGENT_DB_PATH"]) as db:
        return db.execute(sql, params).fetchall()


def test_membership_rows_drive_read_and_manage_access(client):
    c, admin = client
    project = create_project(c, admin, "Access Docs")
    project_id = project["id"]
    pa = create_token(c, admin, "pa", [{"project_id": project_id, "role": "admin"}])
    ua = create_token(c, admin, "ua", [{"project_id": project_id, "role": "member"}])
    outsider = create_token(c, admin, "outsider", [])

    assert (
        c.get(
            "/api/v1/builds",
            params={"project": project_id},
            headers=headers(pa["token"]),
        ).status_code
        == 200
    )
    assert (
        c.get(
            "/api/v1/builds",
            params={"project": project_id},
            headers=headers(ua["token"]),
        ).status_code
        == 200
    )
    denied = c.get(
        "/api/v1/builds",
        params={"project": project_id},
        headers=headers(outsider["token"]),
    )
    missing = c.get(
        "/api/v1/builds",
        params={"project": "missing"},
        headers=headers(outsider["token"]),
    )
    assert denied.status_code == missing.status_code == 404
    assert denied.json() == missing.json() == {"detail": "Project not found"}

    for token, expected in ((pa["token"], 200), (ua["token"], 403)):
        response = c.get(
            "/api/v1/source/files",
            params={"project": project_id},
            headers=headers(token),
        )
        assert response.status_code == expected, response.text
    assert (
        c.get(
            "/api/v1/source/files",
            params={"project": project_id},
            headers=headers(outsider["token"]),
        ).status_code
        == 404
    )
    assert (
        c.post(
            "/api/v1/builds",
            params={"project": project_id},
            headers=headers(ua["token"]),
            json={"build_type": "full"},
        ).status_code
        == 403
    )
    assert (
        c.post(
            "/api/v1/builds",
            params={"project": project_id},
            headers=headers(pa["token"]),
            json={"build_type": "full"},
        ).status_code
        == 202
    )

    # A user whose only project is a non-default project uses it by default.
    assert c.get("/api/v1/builds", headers=headers(pa["token"])).status_code == 200
    none = c.get("/api/v1/builds", headers=headers(outsider["token"]))
    assert none.status_code == 422
    assert none.json()["detail"] == "请指定项目"


def test_default_project_prefers_default_then_single_membership(client):
    c, admin = client
    project_a = create_project(c, admin, "Project A")
    project_b = create_project(c, admin, "Project B")
    both = create_token(
        c,
        admin,
        "both",
        [
            {"project_id": DEFAULT, "role": "member"},
            {"project_id": project_a["id"], "role": "member"},
        ],
    )
    only_a = create_token(
        c, admin, "only-a", [{"project_id": project_a["id"], "role": "member"}]
    )
    two_without_default = create_token(
        c,
        admin,
        "two",
        [
            {"project_id": project_a["id"], "role": "member"},
            {"project_id": project_b["id"], "role": "member"},
        ],
    )
    no_projects = create_token(c, admin, "none", [])

    # The listing still exposes my_role so clients can select a project.
    assert {
        row["id"]: row["my_role"]
        for row in c.get("/api/v1/projects", headers=headers(both["token"])).json()
    } == {DEFAULT: "member", project_a["id"]: "member"}
    # Default is accessible and therefore wins.
    assert c.get("/api/v1/builds", headers=headers(both["token"])).status_code == 200
    assert c.get("/api/v1/builds", headers=headers(only_a["token"])).status_code == 200
    assert (
        c.get(
            "/api/v1/builds", headers=headers(two_without_default["token"])
        ).status_code
        == 422
    )
    assert (
        c.get("/api/v1/builds", headers=headers(no_projects["token"])).status_code
        == 422
    )


def test_member_management_rules_and_audit(client):
    c, admin = client
    project = create_project(c, admin, "Members")
    project_id = project["id"]
    pa = create_token(c, admin, "pa", [{"project_id": project_id, "role": "admin"}])
    pa2 = create_token(c, admin, "pa2", [{"project_id": project_id, "role": "admin"}])
    ua = create_token(c, admin, "ua", [{"project_id": project_id, "role": "member"}])
    outsider = create_token(c, admin, "outsider", [])

    listing = c.get(
        f"/api/v1/projects/{project_id}/members", headers=headers(pa["token"])
    )
    assert listing.status_code == 200, listing.text
    assert {row["id"] for row in listing.json()} == {pa["id"], pa2["id"], ua["id"]}
    assert all(set(row) == {"id", "name", "role"} for row in listing.json())
    assert (
        c.get(
            f"/api/v1/projects/{project_id}/members",
            headers=headers(ua["token"]),
        ).status_code
        == 403
    )
    assert (
        c.get(
            f"/api/v1/projects/{project_id}/members",
            headers=headers(outsider["token"]),
        ).status_code
        == 404
    )

    added = c.put(
        f"/api/v1/projects/{project_id}/members/{ua['id']}",
        headers=headers(pa["token"]),
        json={"role": "member"},
    )
    assert added.status_code == 200, added.text
    updated = c.put(
        f"/api/v1/projects/{project_id}/members/{ua['id']}",
        headers=headers(pa["token"]),
        json={"role": "admin"},
    )
    assert updated.status_code == 200, updated.text
    # With two remaining admins, self-demotion is allowed.
    demoted = c.put(
        f"/api/v1/projects/{project_id}/members/{pa['id']}",
        headers=headers(pa["token"]),
        json={"role": "member"},
    )
    assert demoted.status_code == 200, demoted.text
    assert (
        c.put(
            f"/api/v1/projects/{project_id}/members/{pa['id']}",
            headers=headers(pa["token"]),
            json={"role": "member"},
        ).status_code
        == 403
    )
    restored = c.put(
        f"/api/v1/projects/{project_id}/members/{pa['id']}",
        headers=headers(admin),
        json={"role": "admin"},
    )
    assert restored.status_code == 200, restored.text
    assert (
        c.delete(
            f"/api/v1/projects/{project_id}/members/{pa2['id']}",
            headers=headers(admin),
        ).status_code
        == 204
    )
    assert (
        c.delete(
            f"/api/v1/projects/{project_id}/members/{ua['id']}",
            headers=headers(admin),
        ).status_code
        == 204
    )
    assert (
        c.delete(
            f"/api/v1/projects/{project_id}/members/{pa2['id']}",
            headers=headers(admin),
        ).status_code
        == 404
    )

    # Last project administrator cannot be demoted or removed.
    assert (
        c.put(
            f"/api/v1/projects/{project_id}/members/{pa['id']}",
            headers=headers(admin),
            json={"role": "member"},
        ).status_code
        == 409
    )
    assert (
        c.delete(
            f"/api/v1/projects/{project_id}/members/{pa['id']}",
            headers=headers(admin),
        ).status_code
        == 409
    )
    # System administrator tokens are implicit admins and cannot gain rows.
    admin_row = next(
        row
        for row in c.get("/api/v1/tokens", headers=headers(admin)).json()
        if row["role"] == "admin"
    )
    assert (
        c.put(
            f"/api/v1/projects/{project_id}/members/{admin_row['id']}",
            headers=headers(admin),
            json={"role": "admin"},
        ).status_code
        == 422
    )
    revoked = create_token(
        c, admin, "revoked", [{"project_id": project_id, "role": "member"}]
    )
    assert (
        c.delete(f"/api/v1/tokens/{revoked['id']}", headers=headers(admin)).status_code
        == 204
    )
    assert (
        c.put(
            f"/api/v1/projects/{project_id}/members/{revoked['id']}",
            headers=headers(admin),
            json={"role": "member"},
        ).status_code
        == 404
    )
    assert (
        c.put(
            f"/api/v1/projects/{project_id}/members/missing",
            headers=headers(admin),
            json={"role": "member"},
        ).status_code
        == 404
    )
    # A token without a row exercises the add branch and its audit event.
    assert (
        c.put(
            f"/api/v1/projects/{project_id}/members/{outsider['id']}",
            headers=headers(admin),
            json={"role": "member"},
        ).status_code
        == 200
    )
    assert (
        c.delete(
            f"/api/v1/projects/{project_id}/members/{outsider['id']}",
            headers=headers(admin),
        ).status_code
        == 204
    )

    audit = c.get(f"/api/v1/projects/{project_id}/audit", headers=headers(admin))
    assert audit.status_code == 200, audit.text
    actions = [row["action"] for row in audit.json()]
    assert {"project.create", "member.add", "member.update", "member.remove"} <= set(
        actions
    )
    assert all(
        set(row)
        == {
            "id",
            "project_id",
            "actor_id",
            "actor_name",
            "action",
            "detail",
            "created_at",
        }
        for row in audit.json()
    )
    page = c.get(
        f"/api/v1/projects/{project_id}/audit",
        params={"limit": 2, "offset": 1},
        headers=headers(admin),
    )
    assert page.status_code == 200
    assert len(page.json()) == 2
    assert page.json()[0]["id"] == audit.json()[1]["id"]
    reader = create_token(
        c, admin, "reader", [{"project_id": project_id, "role": "member"}]
    )
    assert (
        c.get(
            f"/api/v1/projects/{project_id}/audit", headers=headers(reader["token"])
        ).status_code
        == 403
    )
    assert (
        c.get(
            f"/api/v1/projects/{project_id}/audit", headers=headers(outsider["token"])
        ).status_code
        == 404
    )


def test_rename_writes_audit(client):
    c, admin = client
    project = create_project(c, admin, "Rename Me")
    response = c.patch(
        f"/api/v1/projects/{project['id']}",
        headers=headers(admin),
        json={"name": "Renamed"},
    )
    assert response.status_code == 200, response.text
    audit = c.get(
        f"/api/v1/projects/{project['id']}/audit", headers=headers(admin)
    ).json()
    rename = next(row for row in audit if row["action"] == "project.rename")
    assert rename["detail"] == {"from": "Rename Me", "to": "Renamed"}
    assert rename["actor_id"]
    assert rename["actor_name"]


def test_revoked_project_admin_does_not_count_as_last_admin(client):
    c, admin = client
    project = create_project(c, admin, "Revoked admin")
    pa = create_token(c, admin, "pa", [{"project_id": project["id"], "role": "admin"}])
    pa2 = create_token(
        c, admin, "pa2", [{"project_id": project["id"], "role": "admin"}]
    )
    assert (
        c.delete(f"/api/v1/tokens/{pa2['id']}", headers=headers(admin)).status_code
        == 204
    )
    assert (
        c.put(
            f"/api/v1/projects/{project['id']}/members/{pa['id']}",
            headers=headers(admin),
            json={"role": "member"},
        ).status_code
        == 409
    )
    assert (
        c.delete(
            f"/api/v1/projects/{project['id']}/members/{pa['id']}",
            headers=headers(admin),
        ).status_code
        == 409
    )


def test_users_endpoint_requires_project_admin_and_returns_three_fields(client):
    c, admin = client
    project = create_project(c, admin, "Users")
    pa = create_token(c, admin, "pa", [{"project_id": project["id"], "role": "admin"}])
    ua = create_token(c, admin, "ua", [{"project_id": project["id"], "role": "member"}])
    outsider = create_token(c, admin, "outsider", [])
    for token in (pa["token"], admin):
        response = c.get("/api/v1/users", headers=headers(token))
        assert response.status_code == 200, response.text
        assert all(set(row) == {"id", "name", "role"} for row in response.json())
        assert any(row["id"] == ua["id"] for row in response.json())
    for token in (ua["token"], outsider["token"]):
        assert c.get("/api/v1/users", headers=headers(token)).status_code == 403


def test_token_project_lists_and_revocation(client):
    c, admin = client
    project = create_project(c, admin, "Tokens")
    default_user = create_token(c, admin, "default-user", None)
    explicit = create_token(
        c,
        admin,
        "explicit",
        [
            {"project_id": project["id"], "role": "admin"},
            {"project_id": DEFAULT, "role": "member"},
        ],
    )
    no_projects = create_token(c, admin, "none", [])
    admin_ignored = c.post(
        "/api/v1/tokens",
        headers=headers(admin),
        json={
            "name": "second admin",
            "role": "admin",
            "projects": [{"project_id": project["id"], "role": "admin"}],
        },
    ).json()

    rows = {
        row["id"]: row for row in c.get("/api/v1/tokens", headers=headers(admin)).json()
    }
    assert rows[default_user["id"]]["projects"] == [
        {"project_id": DEFAULT, "role": "member"}
    ]
    assert rows[explicit["id"]]["projects"] == [
        {"project_id": DEFAULT, "role": "member"},
        {"project_id": project["id"], "role": "admin"},
    ]
    assert rows[no_projects["id"]]["projects"] == []
    assert rows[admin_ignored["id"]]["projects"] == []
    assert (
        fetch(
            "SELECT project_id FROM project_members WHERE token_id = ?",
            (admin_ignored["id"],),
        )
        == []
    )

    assert (
        c.post(
            "/api/v1/tokens",
            headers=headers(admin),
            json={
                "name": "missing",
                "role": "user",
                "projects": [{"project_id": "missing", "role": "member"}],
            },
        ).status_code
        == 422
    )
    assert (
        c.post(
            "/api/v1/tokens",
            headers=headers(admin),
            json={
                "name": "duplicate",
                "role": "user",
                "projects": [
                    {"project_id": project["id"], "role": "member"},
                    {"project_id": project["id"], "role": "admin"},
                ],
            },
        ).status_code
        == 422
    )

    assert (
        c.delete(f"/api/v1/tokens/{explicit['id']}", headers=headers(admin)).status_code
        == 204
    )
    assert (
        c.get(
            "/api/v1/builds",
            params={"project": project["id"]},
            headers=headers(explicit["token"]),
        ).status_code
        == 401
    )
    retained = next(
        row
        for row in c.get("/api/v1/tokens", headers=headers(admin)).json()
        if row["id"] == explicit["id"]
    )
    assert retained["projects"] == [
        {"project_id": DEFAULT, "role": "member"},
        {"project_id": project["id"], "role": "admin"},
    ]
