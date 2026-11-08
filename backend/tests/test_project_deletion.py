"""Permanent deletion is archived-only, named, idempotent and resumable (#190)."""

from __future__ import annotations

import json
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


def create_user(client: TestClient, admin: str, name: str, projects: list[dict]) -> dict:
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


def seed_project_data(project_id: str, token: str) -> None:
    from semantica_dataagent.db import (
        Build,
        Conversation,
        CorrectionProposal,
        GenerationPointer,
        KnowledgeGeneration,
        ProjectSetting,
        Turn,
        sessions,
    )

    with sessions().begin() as db:
        db.add(KnowledgeGeneration(name=f"{project_id}_gen", project_id=project_id))
        db.add(
            GenerationPointer(project_id=project_id, generation=f"{project_id}_gen")
        )
        db.add(
            Build(
                project_id=project_id,
                status="succeeded",
                started_at="2026-01-01T00:00:00+00:00",
                counts="{}",
                build_type="full",
            )
        )
        db.add(
            CorrectionProposal(
                project_id=project_id,
                owner_id=owner_id(token),
                target="table",
                patch="",
                original="",
                diff="",
                reason="reason",
            )
        )
        db.add(
            Conversation(
                id="old",
                owner_id=owner_id(token),
                project_id=project_id,
                title="old",
                agent="pi",
                provider_id="provider",
                model="model",
            )
        )
        db.add(
            Turn(
                conversation_id="old",
                sequence=1,
                question="q",
                status="succeeded",
                answer="a",
                sql="[]",
                citations="[]",
                started_at="2026-01-01T00:00:00+00:00",
            )
        )


def archive(client: TestClient, admin: str, project: dict) -> None:
    response = client.post(
        f"/api/v1/projects/{project['id']}/archive", headers=headers(admin)
    )
    assert response.status_code == 200, response.text


def delete(
    client: TestClient,
    admin: str,
    project: dict,
    name: str | None = None,
):
    return client.post(
        f"/api/v1/projects/{project['id']}/delete",
        headers=headers(admin),
        json={"name": project["name"] if name is None else name},
    )


def test_deletion_requires_archive_name_and_system_admin(client):
    c, admin = client
    active = create_project(c, admin, "Active Docs")
    response = delete(c, admin, active)
    assert response.status_code == 409
    assert "归档" in response.json()["detail"]

    archive(c, admin, active)
    wrong = delete(c, admin, active, name="别的名字")
    assert wrong.status_code == 422
    assert "项目名" in wrong.json()["detail"]

    project_admin = create_user(
        c, admin, "project-admin", [{"project_id": active["id"], "role": "admin"}]
    )
    denied = delete(c, project_admin["token"], active)
    assert denied.status_code == 403

    default = next(
        row
        for row in c.get("/api/v1/projects", headers=headers(admin)).json()
        if row["id"] == DEFAULT
    )
    archive(c, admin, default)
    response = delete(c, admin, default)
    assert 400 <= response.status_code < 500
    assert "默认项目" in response.json()["detail"]


def test_deletion_removes_project_data_but_keeps_tokens_and_audit(
    client, monkeypatch
):
    c, admin = client
    project = create_project(c, admin, "Gone Docs")
    project_id = project["id"]
    user = create_user(
        c, admin, "reader", [{"project_id": project_id, "role": "member"}]
    )
    seed_project_data(project_id, admin)
    archive(c, admin, project)

    from semantica_dataagent import project_deletion
    from semantica_dataagent.db import (
        Build,
        Conversation,
        CorrectionProposal,
        GenerationPointer,
        KnowledgeGeneration,
        Project,
        ProjectAudit,
        ProjectDeletion,
        ProjectMember,
        ProjectSetting,
        Token,
        Turn,
        digest,
        sessions,
    )

    calls: list[str] = []
    monkeypatch.setattr(
        project_deletion, "_delete_source", lambda *args: calls.append("source")
    )
    monkeypatch.setattr(
        project_deletion, "_delete_neo4j", lambda *args: calls.append("neo4j")
    )
    monkeypatch.setattr(
        project_deletion, "_delete_milvus", lambda *args: calls.append("milvus")
    )
    monkeypatch.setattr(
        project_deletion, "_cleanup_attachments", lambda *args: calls.append("attachments")
    )

    response = delete(c, admin, project)
    assert response.status_code == 204, response.text
    assert calls == ["source", "neo4j", "milvus", "attachments"]

    with sessions()() as db:
        assert db.get(Project, project_id) is None
        assert db.get(ProjectMember, (project_id, user["id"])) is None
        assert db.get(Token, digest(user["token"])) is not None
        assert db.get(KnowledgeGeneration, f"{project_id}_gen") is None
        assert (
            db.query(GenerationPointer)
            .filter(GenerationPointer.project_id == project_id)
            .first()
            is None
        )
        assert (
            db.query(Build).filter(Build.project_id == project_id).first() is None
        )
        assert (
            db.query(CorrectionProposal)
            .filter(CorrectionProposal.project_id == project_id)
            .first()
            is None
        )
        assert (
            db.query(Conversation)
            .filter(Conversation.project_id == project_id)
            .first()
            is None
        )
        assert db.query(Turn).filter(Turn.conversation_id == "old").first() is None
        assert (
            db.query(ProjectSetting)
            .filter(ProjectSetting.project_id == project_id)
            .first()
            is None
        )
        deletion = db.get(ProjectDeletion, project_id)
        assert deletion is not None
        assert deletion.status == "completed"
        assert [step["name"] for step in json.loads(deletion.steps)] == [
            "source",
            "neo4j",
            "milvus",
            "attachments",
            "database",
        ]
        audit = (
            db.query(ProjectAudit)
            .filter(ProjectAudit.project_id == project_id)
            .order_by(ProjectAudit.id)
            .all()
        )
        assert [row.action for row in audit][-2:] == [
            "project.archive",
            "project.delete",
        ]

    repeated = delete(c, admin, project)
    assert repeated.status_code == 204
    assert calls == ["source", "neo4j", "milvus", "attachments"]


def test_failed_step_is_retried_without_skipping_or_touching_other_projects(
    client, monkeypatch
):
    c, admin = client
    project = create_project(c, admin, "Gone Docs")
    project_id = project["id"]
    other = create_project(c, admin, "Kept Docs")
    archive(c, admin, project)

    from semantica_dataagent import project_deletion
    from semantica_dataagent.db import Project, ProjectDeletion, sessions

    events: list[str] = []
    attempts = {"milvus": 0}
    monkeypatch.setattr(
        project_deletion, "_delete_source", lambda *args: events.append("source")
    )
    monkeypatch.setattr(
        project_deletion, "_delete_neo4j", lambda *args: events.append("neo4j")
    )

    def fail_once(*args):
        attempts["milvus"] += 1
        if attempts["milvus"] == 1:
            raise RuntimeError("milvus down")
        events.append("milvus")

    monkeypatch.setattr(project_deletion, "_delete_milvus", fail_once)
    monkeypatch.setattr(
        project_deletion, "_cleanup_attachments", lambda *args: events.append("attachments")
    )

    first = delete(c, admin, project)
    assert first.status_code == 500
    with sessions()() as db:
        deletion = db.get(ProjectDeletion, project_id)
        assert deletion.status == "failed"
        assert deletion.step == "milvus"
        assert "milvus down" in deletion.error
        assert db.get(Project, project_id) is not None
        assert db.get(Project, other["id"]) is not None

    second = delete(c, admin, project)
    assert second.status_code == 204, second.text
    assert events == ["source", "neo4j", "milvus", "attachments"]


def test_prefix_matching_never_crosses_projects():
    from semantica_dataagent.project_deletion import (
        is_project_collection,
        is_project_generation,
    )

    assert is_project_generation("ab_initial", "ab")
    assert is_project_generation("ab_123", "ab")
    assert not is_project_generation("abc_initial", "ab")
    assert not is_project_generation("ab", "ab")

    assert is_project_collection("sda_ab_initial", "ab")
    assert is_project_collection("sda_ab_123", "ab")
    assert not is_project_collection("sda_abc_initial", "ab")
    assert not is_project_collection("sda_ab", "ab")
    assert not is_project_collection("other_ab_initial", "ab")
