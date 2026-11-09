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


def seed_project_data(
    project_id: str,
    token: str,
    *,
    generation: str | None = None,
    conversation_id: str = "old",
) -> None:
    from semantica_dataagent.db import (
        Build,
        Conversation,
        CorrectionProposal,
        GenerationPointer,
        KnowledgeGeneration,
        Turn,
        sessions,
    )

    generation = generation or f"{project_id}_gen"
    with sessions().begin() as db:
        db.add(KnowledgeGeneration(name=generation, project_id=project_id))
        pointer = (
            db.query(GenerationPointer)
            .filter(GenerationPointer.project_id == project_id)
            .first()
        )
        if pointer is None:
            db.add(GenerationPointer(project_id=project_id, generation=generation))
        else:
            pointer.generation = generation
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
                id=conversation_id,
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
                conversation_id=conversation_id,
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


def test_reused_project_id_deletes_the_new_project_data(client, monkeypatch):
    c, admin = client
    project = create_project(c, admin, "Reused Docs")
    project_id = project["id"]
    seed_project_data(project_id, admin)
    archive(c, admin, project)

    from semantica_dataagent import project_context, project_deletion
    from semantica_dataagent.db import (
        Conversation,
        KnowledgeGeneration,
        Project,
        ProjectDeletion,
        sessions,
    )

    calls: list[str] = []
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

    first = delete(c, admin, project)
    assert first.status_code == 204, first.text
    assert not project_context.project_source_directory(project_id).exists()

    recreated = create_project(c, admin, "Reused Docs")
    assert recreated["id"] == project_id, "同 id 项目没有被复用，测试前提不成立"
    seed_project_data(
        project_id,
        admin,
        generation=f"{project_id}_second",
        conversation_id="second",
    )
    source = project_context.project_source_directory(project_id)
    assert source.exists()
    archive(c, admin, recreated)

    calls.clear()
    second = delete(c, admin, recreated)
    assert second.status_code == 204, second.text
    assert calls == ["neo4j", "milvus", "attachments"], "第二次删除跳过了真实清理"

    with sessions()() as db:
        assert db.get(Project, project_id) is None
        assert db.get(KnowledgeGeneration, f"{project_id}_second") is None
        assert db.get(Conversation, "second") is None
        assert (
            db.query(Conversation).filter(Conversation.project_id == project_id).count()
            == 0
        )
        deletion = db.get(ProjectDeletion, project_id)
        assert deletion is not None and deletion.status == "completed"
        steps = json.loads(deletion.steps)
        assert [step["name"] for step in steps] == [
            "source",
            "neo4j",
            "milvus",
            "attachments",
            "database",
        ], "第二次删除沿用了上一轮的步骤记录"
        assert all(step["status"] == "succeeded" for step in steps)
    assert not source.exists(), "第二次删除没有清理新项目的源目录"


def test_reused_project_id_still_requires_archive_and_matching_name(
    client, monkeypatch
):
    c, admin = client
    project = create_project(c, admin, "Guarded Docs")
    from semantica_dataagent import project_deletion

    for name in ("_delete_neo4j", "_delete_milvus", "_cleanup_attachments"):
        monkeypatch.setattr(project_deletion, name, lambda *args: None)
    archive(c, admin, project)
    assert delete(c, admin, project).status_code == 204

    recreated = create_project(c, admin, "Guarded Docs")
    assert recreated["id"] == project["id"]

    from semantica_dataagent.db import Project, sessions

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

    active = delete(c, admin, recreated)
    assert active.status_code == 409, active.text
    assert "归档" in active.json()["detail"]
    with sessions()() as db:
        assert db.get(Project, recreated["id"]).status == "active"

    archive(c, admin, recreated)
    wrong = delete(c, admin, recreated, name="别的名字")
    assert wrong.status_code == 422, wrong.text
    assert "项目名" in wrong.json()["detail"]
    with sessions()() as db:
        assert db.get(Project, recreated["id"]).status == "archived"
    assert calls == [], "被拒绝的删除请求仍然执行了清理"

    accepted = delete(c, admin, recreated)
    assert accepted.status_code == 204, accepted.text
    assert calls == ["source", "neo4j", "milvus", "attachments"]


def test_existing_residual_completed_record_does_not_shadow_new_project(
    client, monkeypatch
):
    c, admin = client
    project = create_project(c, admin, "Legacy Docs")
    project_id = project["id"]
    archive(c, admin, project)

    from semantica_dataagent import project_context, project_deletion

    for name in ("_delete_neo4j", "_delete_milvus", "_cleanup_attachments"):
        monkeypatch.setattr(project_deletion, name, lambda *args: None)
    assert delete(c, admin, project).status_code == 204

    recreated = create_project(c, admin, "Legacy Docs")
    assert recreated["id"] == project_id

    from semantica_dataagent.db import Project, ProjectDeletion, sessions

    stale_steps = json.dumps(
        [
            {"name": step, "status": "succeeded"}
            for step in ("source", "neo4j", "milvus", "attachments", "database")
        ]
    )
    with sessions().begin() as db:
        db.add(
            ProjectDeletion(
                project_id=project_id,
                project_name=project["name"],
                status="completed",
                step="done",
                steps=stale_steps,
                requested_by="owner",
            )
        )

    active = delete(c, admin, recreated)
    assert active.status_code == 409, active.text
    assert "归档" in active.json()["detail"]
    with sessions()() as db:
        assert db.get(Project, project_id).status == "active"

    archive(c, admin, recreated)
    wrong = delete(c, admin, recreated, name="别的名字")
    assert wrong.status_code == 422, wrong.text
    with sessions()() as db:
        assert db.get(Project, project_id).status == "archived"

    calls: list[str] = []
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
    accepted = delete(c, admin, recreated)
    assert accepted.status_code == 204, accepted.text
    assert calls == ["neo4j", "milvus", "attachments"], "残留记录遮住了新项目的删除"
    with sessions()() as db:
        assert db.get(Project, project_id) is None
        deletion = db.get(ProjectDeletion, project_id)
        assert deletion.status == "completed"
        assert [step["name"] for step in json.loads(deletion.steps)] == [
            "source",
            "neo4j",
            "milvus",
            "attachments",
            "database",
        ]
    assert not project_context.project_source_directory(project_id).exists()


def test_completed_deletion_is_idempotent_only_without_a_project_row(
    client, monkeypatch
):
    c, admin = client
    project = create_project(c, admin, "Idempotent Docs")
    from semantica_dataagent import project_deletion

    for name in ("_delete_neo4j", "_delete_milvus", "_cleanup_attachments"):
        monkeypatch.setattr(project_deletion, name, lambda *args: None)
    archive(c, admin, project)
    assert delete(c, admin, project).status_code == 204

    from semantica_dataagent.db import Project, ProjectDeletion, sessions

    with sessions()() as db:
        assert db.get(Project, project["id"]) is None
        assert db.get(ProjectDeletion, project["id"]).status == "completed"

    repeated = delete(c, admin, project, name="完全不同的名字")
    assert repeated.status_code == 204, repeated.text


def test_creating_a_project_clears_the_previous_completed_deletion(client, monkeypatch):
    c, admin = client
    project = create_project(c, admin, "Cleared Docs")
    from semantica_dataagent import project_deletion

    for name in ("_delete_neo4j", "_delete_milvus", "_cleanup_attachments"):
        monkeypatch.setattr(project_deletion, name, lambda *args: None)
    archive(c, admin, project)
    assert delete(c, admin, project).status_code == 204

    recreated = create_project(c, admin, "Cleared Docs")
    assert recreated["id"] == project["id"]

    from semantica_dataagent.db import ProjectDeletion, sessions

    with sessions()() as db:
        assert db.get(ProjectDeletion, recreated["id"]) is None, (
            "创建同 id 项目后仍残留上一轮的完成记录"
        )
    listing = c.get("/api/v1/projects", headers=headers(admin))
    assert listing.status_code == 200, listing.text
    rows = [row for row in listing.json() if row["id"] == recreated["id"]]
    assert len(rows) == 1
    assert rows[0]["status"] == "active"
    assert rows[0]["name"] == "Cleared Docs"


def test_recovery_ignores_residual_records_for_a_recreated_project(client, monkeypatch):
    c, admin = client
    project = create_project(c, admin, "Recovered Docs")
    project_id = project["id"]
    seed_project_data(project_id, admin, conversation_id="kept")

    from semantica_dataagent import project_context, project_deletion
    from semantica_dataagent.db import (
        Conversation,
        GenerationPointer,
        KnowledgeGeneration,
        Project,
        ProjectDeletion,
        sessions,
    )

    with sessions().begin() as db:
        db.add(
            ProjectDeletion(
                project_id=project_id,
                project_name=project["name"],
                status="completed",
                step="done",
                steps=json.dumps(
                    [
                        {"name": step, "status": "succeeded"}
                        for step in (
                            "source",
                            "neo4j",
                            "milvus",
                            "attachments",
                            "database",
                        )
                    ]
                ),
                requested_by="owner",
            )
        )
    resumed: list[str] = []
    monkeypatch.setattr(
        project_deletion, "run_deletion", lambda project_id: resumed.append(project_id)
    )
    project_deletion.recover_project_deletions()
    assert resumed == [], "恢复流程处理了已完成记录"

    with sessions().begin() as db:
        row = db.get(ProjectDeletion, project_id)
        row.status = "failed"
        row.step = "milvus"
        row.steps = json.dumps([{"name": "source", "status": "succeeded"}])
    resumed.clear()
    project_deletion.recover_project_deletions()
    assert resumed == [], "恢复流程处理了新项目的失败残留记录"

    with sessions()() as db:
        assert db.get(Project, project_id) is not None, "恢复流程删除了新项目"
        assert db.get(KnowledgeGeneration, f"{project_id}_gen") is not None
        assert (
            db.query(GenerationPointer)
            .filter(GenerationPointer.project_id == project_id)
            .first()
            is not None
        )
        assert db.get(Conversation, "kept") is not None
    assert project_context.project_source_directory(project_id).exists()


def test_cannot_create_the_same_id_while_the_old_deletion_is_in_progress(
    client, monkeypatch
):
    c, admin = client
    project = create_project(c, admin, "Race Docs")
    archive(c, admin, project)

    from semantica_dataagent import project_deletion
    from semantica_dataagent.db import Project, sessions

    def fail(*args):
        raise RuntimeError("storage down")

    monkeypatch.setattr(project_deletion, "_delete_source", fail)
    assert delete(c, admin, project).status_code == 500
    with sessions()() as db:
        assert db.get(Project, project["id"]).status == "deleting"

    same_name = c.post(
        "/api/v1/projects",
        headers=headers(admin),
        json={"name": "Race Docs", "type": "document"},
    )
    assert same_name.status_code == 409, same_name.text

    variant = c.post(
        "/api/v1/projects",
        headers=headers(admin),
        json={"name": "Race-Docs", "type": "document"},
    )
    assert variant.status_code == 201, variant.text
    assert variant.json()["id"] != project["id"], "旧删除进行中仍复用了项目 id"
