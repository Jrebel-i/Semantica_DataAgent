"""Permanent deletion is archived-only, named, idempotent and resumable (#190)."""

from __future__ import annotations

import json
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


def seed_project_data(project_id: str, token: str) -> None:
    from semantica_dataagent.db import (
        Build,
        Conversation,
        CorrectionProposal,
        GenerationPointer,
        KnowledgeGeneration,
        Turn,
        sessions,
    )

    with sessions().begin() as db:
        db.add(KnowledgeGeneration(name=f"{project_id}_gen", project_id=project_id))
        pointer = (
            db.query(GenerationPointer)
            .filter(GenerationPointer.project_id == project_id)
            .first()
        )
        if pointer is None:
            db.add(
                GenerationPointer(project_id=project_id, generation=f"{project_id}_gen")
            )
        else:
            pointer.generation = f"{project_id}_gen"
        db.add(
            Build(
                project_id=project_id,
                status="succeeded",
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
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
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
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


def test_deletion_removes_project_data_but_keeps_tokens_and_audit(client, monkeypatch):
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
        PluginConfig,
        Project,
        ProjectAudit,
        ProjectDeletion,
        ProjectMember,
        ProjectSetting,
        PullRecord,
        Token,
        Turn,
        digest,
        sessions,
    )

    with sessions().begin() as db:
        db.add(
            PluginConfig(
                project_id=project_id,
                category="source",
                plugin_id="markdown",
                enabled=True,
                parameters="{}",
            )
        )
        db.add(
            PluginConfig(
                project_id=DEFAULT,
                category="source",
                plugin_id="markdown",
                enabled=True,
                parameters="{}",
            )
        )
        db.add(
            PullRecord(
                project_id=project_id,
                status="succeeded",
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        db.add(
            PullRecord(
                project_id=DEFAULT,
                status="succeeded",
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
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
        project_deletion,
        "_cleanup_attachments",
        lambda *args: calls.append("attachments"),
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
        assert db.query(Build).filter(Build.project_id == project_id).first() is None
        assert (
            db.query(CorrectionProposal)
            .filter(CorrectionProposal.project_id == project_id)
            .first()
            is None
        )
        assert (
            db.query(Conversation).filter(Conversation.project_id == project_id).first()
            is None
        )
        assert db.query(Turn).filter(Turn.conversation_id == "old").first() is None
        assert (
            db.query(ProjectSetting)
            .filter(ProjectSetting.project_id == project_id)
            .first()
            is None
        )
        assert (
            db.query(PluginConfig).filter(PluginConfig.project_id == project_id).first()
            is None
        )
        assert (
            db.query(PullRecord).filter(PullRecord.project_id == project_id).first()
            is None
        )
        assert db.query(PluginConfig).count() == 1
        assert db.query(PullRecord).count() == 1
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
        project_deletion,
        "_cleanup_attachments",
        lambda *args: events.append("attachments"),
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


def test_deletion_removes_conversation_rows_even_without_turns(client, monkeypatch):
    c, admin = client
    project = create_project(c, admin, "Turnless Docs")
    project_id = project["id"]
    archive(c, admin, project)

    from semantica_dataagent.db import (
        AgentCredential,
        Clarification,
        Conversation,
        QueryExecution,
        sessions,
    )

    owner = owner_id(admin)
    with sessions().begin() as db:
        db.add(
            Conversation(
                id="turnless",
                owner_id=owner,
                project_id=project_id,
                title="没有轮次",
                agent="pi",
                provider_id="provider",
                model="model",
            )
        )
        db.add(
            Clarification(
                id="clarification",
                owner_id=owner,
                conversation_id="turnless",
                questions="[]",
            )
        )
        db.add(
            AgentCredential(
                digest="credential",
                owner_id=owner,
                conversation_id="turnless",
                turn_id=0,
                project_id=project_id,
            )
        )
        db.add(
            QueryExecution(
                id="turnless-query",
                owner_id=owner,
                conversation_id="turnless",
                turn_id=None,
                sql="select 1",
                status="succeeded",
            )
        )

    from semantica_dataagent import project_deletion

    monkeypatch.setattr(project_deletion, "_delete_source", lambda *args: None)
    monkeypatch.setattr(project_deletion, "_delete_neo4j", lambda *args: None)
    monkeypatch.setattr(project_deletion, "_delete_milvus", lambda *args: None)
    monkeypatch.setattr(project_deletion, "_cleanup_attachments", lambda *args: None)

    response = delete(c, admin, project)
    assert response.status_code == 204, response.text

    with sessions()() as db:
        assert db.get(Conversation, "turnless") is None
        assert db.get(Clarification, "clarification") is None
        assert db.get(AgentCredential, "credential") is None
        assert db.get(QueryExecution, "turnless-query") is None


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


def test_startup_recovery_does_not_recreate_source_for_deleting_projects(client):
    c, admin = client
    project = create_project(c, admin, "Deleting Docs")
    archive(c, admin, project)

    from semantica_dataagent import project_context, project_deletion
    from semantica_dataagent.db import ProjectDeletion, sessions

    state = project_deletion.prepare_deletion(
        project["id"], project["name"], "owner", "Owner"
    )
    assert state == "active"
    source = project_context.project_source_directory(project["id"])
    assert source.exists()
    project_deletion._delete_source(project["id"], {})
    assert not source.exists()
    with sessions().begin() as db:
        row = db.get(ProjectDeletion, project["id"])
        row.steps = json.dumps([{"name": "source", "status": "succeeded"}])
        row.step = "neo4j"
        row.status = "deleting"

    project_context.recover_projects()

    assert not source.exists(), "重启恢复了删除中项目的源目录，留下残留"


def test_startup_recovery_still_restores_source_for_archived_projects(client):
    c, admin = client
    project = create_project(c, admin, "Archived Docs")
    archive(c, admin, project)

    from semantica_dataagent import project_context

    source = project_context.project_source_directory(project["id"])
    assert source.exists()
    source.rmdir()

    project_context.recover_projects()

    assert source.exists(), "归档项目的数据应保留并可恢复源目录"
