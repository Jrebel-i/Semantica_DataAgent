"""Archiving freezes one project while leaving its history intact (#190)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

ARCHIVED = "项目已归档"
DEFAULT = "default"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("DATAAGENT_AGENT_ROOT", str(tmp_path / "agents"))
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


def create_user(
    client: TestClient, admin: str, name: str, projects: list[dict]
) -> dict:
    response = client.post(
        "/api/v1/tokens",
        headers=headers(admin),
        json={"name": name, "role": "user", "projects": projects},
    )
    assert response.status_code == 201, response.text
    return response.json()


def owner_id(token: str) -> str:
    from semantica_dataagent.db import Token, digest, sessions

    with sessions()() as db:
        return db.get(Token, digest(token)).id


def seed_history(token: str, project_id: str) -> tuple[str, str]:
    from semantica_dataagent.db import (
        Conversation,
        Provider,
        QueryExecution,
        Turn,
        sessions,
    )

    owner = owner_id(token)
    with sessions().begin() as db:
        provider = db.get(Provider, "provider")
        if provider is None:
            db.add(
                Provider(
                    id="provider",
                    name="Provider",
                    type="openai-completions",
                    base_url="http://localhost",
                    models=json.dumps(["model"]),
                )
            )
        db.add(
            Conversation(
                id="history",
                owner_id=owner,
                project_id=project_id,
                title="旧会话",
                agent="pi",
                provider_id="provider",
                model="model",
            )
        )
        db.add(
            Turn(
                conversation_id="history",
                sequence=1,
                question="旧问题",
                status="succeeded",
                answer="旧答案",
                sql="[]",
                citations="[]",
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        db.add(
            QueryExecution(
                id="query",
                owner_id=owner,
                conversation_id="history",
                sql="select 1",
                status="succeeded",
                rows=json.dumps({"columns": ["x"], "data": [[1]]}),
                row_count=1,
            )
        )
    return "history", "query"


def test_archive_freezes_access_and_restore_reopens(client):
    c, admin = client
    project = create_project(c, admin, "Frozen Docs")
    project_id = project["id"]
    conversation_id, query_id = seed_history(admin, project_id)

    archived = c.post(f"/api/v1/projects/{project_id}/archive", headers=headers(admin))
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "archived"

    listing = c.get("/api/v1/projects", headers=headers(admin))
    assert listing.status_code == 200
    assert {row["id"]: row["status"] for row in listing.json()}[
        project_id
    ] == "archived"

    ask = c.post(
        "/api/v1/conversations/ask",
        headers=headers(admin),
        json={"question": "还能问吗", "project": project_id},
    )
    assert ask.status_code == 409
    assert ask.json()["detail"] == ARCHIVED

    build = c.post(
        "/api/v1/builds",
        params={"project": project_id},
        headers=headers(admin),
        json={"build_type": "incremental"},
    )
    assert build.status_code == 409
    assert build.json()["detail"] == ARCHIVED

    upload = c.post(
        "/api/v1/source/upload",
        params={"project": project_id},
        headers=headers(admin),
        json={"target": "knowledge/metrics", "files": []},
    )
    assert upload.status_code == 409
    assert upload.json()["detail"] == ARCHIVED

    download = c.post(
        f"/api/v1/queries/{query_id}/download-links", headers=headers(admin)
    )
    assert download.status_code == 409
    assert download.json()["detail"] == ARCHIVED

    detail = c.get(f"/api/v1/conversations/{conversation_id}", headers=headers(admin))
    assert detail.status_code == 200, detail.text
    assert detail.json()["project_id"] == project_id
    assert len(detail.json()["turns"]) == 1

    restored = c.post(f"/api/v1/projects/{project_id}/restore", headers=headers(admin))
    assert restored.status_code == 200, restored.text
    assert restored.json()["status"] == "active"
    assert (
        c.get(
            f"/api/v1/conversations/{conversation_id}", headers=headers(admin)
        ).status_code
        == 200
    )


def test_owner_can_still_delete_an_archived_projects_conversation(client):
    c, admin = client
    project = create_project(c, admin, "Frozen Docs")
    conversation_id, _ = seed_history(admin, project["id"])
    assert (
        c.post(
            f"/api/v1/projects/{project['id']}/archive", headers=headers(admin)
        ).status_code
        == 200
    )
    deleted = c.delete(
        f"/api/v1/conversations/{conversation_id}", headers=headers(admin)
    )
    assert deleted.status_code == 204, deleted.text


def test_only_system_administrators_can_archive_or_restore(client):
    c, admin = client
    project = create_project(c, admin, "Frozen Docs")
    project_admin = create_user(
        c, admin, "project-admin", [{"project_id": project["id"], "role": "admin"}]
    )
    member = create_user(
        c, admin, "member", [{"project_id": project["id"], "role": "member"}]
    )
    for token in (project_admin["token"], member["token"]):
        response = c.post(
            f"/api/v1/projects/{project['id']}/archive", headers=headers(token)
        )
        assert response.status_code == 403
    assert (
        c.post(
            f"/api/v1/projects/{project['id']}/archive", headers=headers(admin)
        ).status_code
        == 200
    )
    for token in (project_admin["token"], member["token"]):
        assert (
            c.post(
                f"/api/v1/projects/{project['id']}/restore", headers=headers(token)
            ).status_code
            == 403
        )


def test_archiving_discards_pending_and_queued_builds(client):
    c, admin = client
    project = create_project(c, admin, "Frozen Docs")
    project_id = project["id"]
    from semantica_dataagent.db import Build, ProjectPendingBuild, sessions

    with sessions().begin() as db:
        db.add(
            ProjectPendingBuild(
                project_id=project_id,
                due_at="2026-01-01T00:00:00+00:00",
                requested_by="owner",
                requested_at="2026-01-01T00:00:00+00:00",
            )
        )
        db.add(
            Build(
                project_id=project_id,
                status="queued",
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
                counts="{}",
                build_type="incremental",
            )
        )
    assert (
        c.post(
            f"/api/v1/projects/{project_id}/archive", headers=headers(admin)
        ).status_code
        == 200
    )
    with sessions()() as db:
        assert db.get(ProjectPendingBuild, project_id) is None
        build = db.query(Build).filter(Build.project_id == project_id).one()
        assert build.status == "cancelled"


def test_archived_project_rejects_mcp_and_agent_credentials(client):
    c, admin = client
    project = create_project(c, admin, "Frozen Docs")
    project_id = project["id"]
    conversation_id, _ = seed_history(admin, project_id)
    from semantica_dataagent.db import Turn, sessions

    with sessions().begin() as db:
        db.query(Turn).filter(Turn.conversation_id == conversation_id).update(
            {"status": "running"}
        )
    assert (
        c.post(
            f"/api/v1/projects/{project_id}/archive", headers=headers(admin)
        ).status_code
        == 200
    )

    from fastapi import HTTPException, status

    from semantica_dataagent.auth import Requester, issue_agent_credential
    from semantica_dataagent.mcp_contract import ToolInvalidInput
    from semantica_dataagent.mcp_project import resolve_mcp_project

    with pytest.raises(ToolInvalidInput) as exc:
        resolve_mcp_project(
            Requester(id=owner_id(admin), role="admin", token=""), None, project_id
        )
    assert str(exc.value) == ARCHIVED

    with pytest.raises(HTTPException) as credential_error:
        issue_agent_credential(owner_id(admin), conversation_id, 1, project_id)
    assert credential_error.value.status_code == status.HTTP_409_CONFLICT
    assert credential_error.value.detail == ARCHIVED
