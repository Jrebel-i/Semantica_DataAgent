"""Real Neo4j/Milvus deletion survives a SIGKILL after every step (#190)."""

from __future__ import annotations

import json
import os
import signal
import uuid
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from neo4j import GraphDatabase
from pymilvus import MilvusClient

NEEDED = ("TEST_NEO4J_URI", "TEST_NEO4J_USER", "TEST_NEO4J_PASSWORD", "TEST_MILVUS_URI")
pytestmark = pytest.mark.skipif(
    not all(os.getenv(key) for key in NEEDED), reason="test storage variables required"
)

STEPS = ("source", "neo4j", "milvus", "attachments", "database")


def _seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    monkeypatch.setenv(
        "DATAAGENT_NEO4J_AUTH",
        os.environ["TEST_NEO4J_USER"] + "/" + os.environ["TEST_NEO4J_PASSWORD"],
    )
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.db import Setting, sessions

    token = initialize()
    with sessions().begin() as db:
        values = {
            "neo4j_uri": os.environ["TEST_NEO4J_URI"],
            "neo4j_user": os.environ["TEST_NEO4J_USER"],
            "milvus_uri": os.environ["TEST_MILVUS_URI"],
            "embedding_url": os.environ.get(
                "TEST_EMBEDDING_URL", "http://127.0.0.1:17998"
            ),
            "embedding_model": os.environ.get(
                "TEST_EMBEDDING_MODEL", "model/bge-small-zh-v1.5"
            ),
        }
        for key, value in values.items():
            db.add(Setting(key=key, value=value))
    return token


def _dispose_engine() -> None:
    from semantica_dataagent.db import sessions

    sessions().kw["bind"].dispose()


def _make_project(tmp_path: Path, project_id: str, name: str) -> None:
    from semantica_dataagent.db import GenerationPointer, Project, sessions
    from semantica_dataagent.project_context import project_source_directory

    with sessions().begin() as db:
        db.add(Project(id=project_id, name=name, type="document", status="archived"))
        pointer = (
            db.query(GenerationPointer)
            .filter(GenerationPointer.project_id == project_id)
            .first()
        )
        if pointer is None:
            db.add(
                GenerationPointer(
                    project_id=project_id, generation=f"{project_id}_initial"
                )
            )
    source = project_source_directory(project_id)
    source.mkdir(parents=True, exist_ok=True)
    (source / "doc.md").write_text("# doc\n", encoding="utf-8")


def _seed_rows(project_id: str) -> None:
    from semantica_dataagent.db import (
        Build,
        Conversation,
        CorrectionProposal,
        KnowledgeGeneration,
        sessions,
    )

    with sessions().begin() as db:
        db.add(KnowledgeGeneration(name=f"{project_id}_initial", project_id=project_id))
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
                owner_id="owner",
                target="table",
                patch="",
                original="",
                diff="",
                reason="reason",
            )
        )
        db.add(
            Conversation(
                id=project_id + "-conversation",
                owner_id="owner",
                project_id=project_id,
                title="old",
                agent="pi",
                provider_id="provider",
                model="model",
            )
        )


def _seed_storage(project_id: str, lookalike: str) -> None:
    driver = GraphDatabase.driver(
        os.environ["TEST_NEO4J_URI"],
        auth=(os.environ["TEST_NEO4J_USER"], os.environ["TEST_NEO4J_PASSWORD"]),
    )
    vectors = MilvusClient(uri=os.environ["TEST_MILVUS_URI"])
    try:
        for generation in (f"{project_id}_initial", f"{project_id}_gen2"):
            driver.execute_query(
                "CREATE (:SDAEntity {generation:$generation})",
                generation=generation,
            )
            vectors.create_collection("sda_" + generation, dimension=2)
        lookalike_generation = f"{lookalike}_gen"
        driver.execute_query(
            "CREATE (:SDAEntity {generation:$generation})",
            generation=lookalike_generation,
        )
        vectors.create_collection("sda_" + lookalike_generation, dimension=2)
    finally:
        driver.close()
        vectors.close()


def _seed_generation(project_id: str, generation: str) -> None:
    driver = GraphDatabase.driver(
        os.environ["TEST_NEO4J_URI"],
        auth=(os.environ["TEST_NEO4J_USER"], os.environ["TEST_NEO4J_PASSWORD"]),
    )
    vectors = MilvusClient(uri=os.environ["TEST_MILVUS_URI"])
    try:
        driver.execute_query(
            "CREATE (:SDAEntity {generation:$generation})",
            generation=generation,
        )
        vectors.create_collection("sda_" + generation, dimension=2)
    finally:
        driver.close()
        vectors.close()


def _drop_storage(project_id: str) -> None:
    driver = GraphDatabase.driver(
        os.environ["TEST_NEO4J_URI"],
        auth=(os.environ["TEST_NEO4J_USER"], os.environ["TEST_NEO4J_PASSWORD"]),
    )
    vectors = MilvusClient(uri=os.environ["TEST_MILVUS_URI"])
    try:
        driver.execute_query(
            "MATCH (n:SDAEntity) WHERE n.generation STARTS WITH $prefix "
            "DETACH DELETE n",
            prefix=project_id + "_",
        )
        for name in list(vectors.list_collections()):
            if name.startswith("sda_" + project_id + "_"):
                vectors.drop_collection(name)
    finally:
        driver.close()
        vectors.close()


def _crash_after(step: str) -> None:
    from semantica_dataagent import project_deletion

    name = "_cleanup_attachments" if step == "attachments" else "_delete_" + step
    original = getattr(project_deletion, name)

    def crash(*args):
        original(*args)
        os.kill(os.getpid(), signal.SIGKILL)

    setattr(project_deletion, name, crash)


def _fork_child(action, **kwargs) -> int:
    _dispose_engine()
    pid = os.fork()
    if pid == 0:
        try:
            action(**kwargs)
        except BaseException:  # noqa: BLE001 - the test parent sees the exit code
            import traceback

            traceback.print_exc()
            os._exit(2)
        finally:
            os._exit(0)
    _, status = os.waitpid(pid, 0)
    return status


def _recover_in_fresh_process() -> int:
    def recover():
        from semantica_dataagent import project_context
        from semantica_dataagent.project_deletion import recover_project_deletions

        project_context.recover_projects()
        recover_project_deletions()

    return _fork_child(recover)


def _residual(project_id: str) -> tuple[int, int, bool]:
    driver = GraphDatabase.driver(
        os.environ["TEST_NEO4J_URI"],
        auth=(os.environ["TEST_NEO4J_USER"], os.environ["TEST_NEO4J_PASSWORD"]),
    )
    vectors = MilvusClient(uri=os.environ["TEST_MILVUS_URI"])
    try:
        rows, _, _ = driver.execute_query(
            "MATCH (n:SDAEntity) WHERE n.generation STARTS WITH $prefix "
            "RETURN count(n) AS count",
            prefix=project_id + "_",
        )
        nodes = rows[0]["count"]
        collections = [
            name
            for name in vectors.list_collections()
            if name.startswith("sda_" + project_id + "_")
        ]
        from semantica_dataagent.project_context import project_source_directory

        source = project_source_directory(project_id)
        return nodes, len(collections), source.exists()
    finally:
        driver.close()
        vectors.close()


def _cleanup(project_ids: list[str], lookalike: str) -> None:
    from semantica_dataagent.db import (
        Build,
        Conversation,
        CorrectionProposal,
        GenerationPointer,
        KnowledgeGeneration,
        Project,
        ProjectDeletion,
        ProjectMember,
        sessions,
    )

    driver = GraphDatabase.driver(
        os.environ["TEST_NEO4J_URI"],
        auth=(os.environ["TEST_NEO4J_USER"], os.environ["TEST_NEO4J_PASSWORD"]),
    )
    vectors = MilvusClient(uri=os.environ["TEST_MILVUS_URI"])
    try:
        driver.execute_query(
            "MATCH (n) WHERE n.generation STARTS WITH $prefix DETACH DELETE n",
            prefix=lookalike + "_",
        )
        for name in list(vectors.list_collections()):
            if name.startswith("sda_" + lookalike + "_"):
                vectors.drop_collection(name)
    finally:
        driver.close()
        vectors.close()
    with sessions().begin() as db:
        for project_id in project_ids:
            db.query(ProjectDeletion).filter(
                ProjectDeletion.project_id == project_id
            ).delete()
            db.query(ProjectMember).filter(
                ProjectMember.project_id == project_id
            ).delete()
            db.query(Build).filter(Build.project_id == project_id).delete()
            db.query(CorrectionProposal).filter(
                CorrectionProposal.project_id == project_id
            ).delete()
            db.query(Conversation).filter(
                Conversation.project_id == project_id
            ).delete()
            db.query(KnowledgeGeneration).filter(
                KnowledgeGeneration.project_id == project_id
            ).delete()
            db.query(GenerationPointer).filter(
                GenerationPointer.project_id == project_id
            ).delete()
            db.query(Project).filter(Project.id == project_id).delete()


@pytest.mark.parametrize("step", STEPS)
def test_sigkill_after_each_deletion_step_resumes_without_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
):
    _seed(tmp_path, monkeypatch)
    suffix = uuid.uuid4().hex[:10]
    project_id = "p" + suffix
    lookalike = project_id + "c"
    _make_project(tmp_path, project_id, f"Delete {suffix}")
    _seed_rows(project_id)
    _seed_storage(project_id, lookalike)

    from semantica_dataagent.project_deletion import prepare_deletion, run_deletion

    state = prepare_deletion(project_id, f"Delete {suffix}", "owner", "Owner")
    assert state == "active"

    try:

        def child(project_id: str, step: str):
            _crash_after(step)
            run_deletion(project_id)

        status = _fork_child(child, project_id=project_id, step=step)
        assert os.WIFSIGNALED(status), f"child did not die by SIGKILL at {step}"

        recovered = _recover_in_fresh_process()
        assert os.WIFEXITED(recovered) and os.WEXITSTATUS(recovered) == 0

        nodes, collections, source_exists = _residual(project_id)
        assert (nodes, collections, source_exists) == (0, 0, False)
        nodes, collections, _ = _residual(lookalike)
        assert nodes == 1
        assert collections == 1
    finally:
        _cleanup([project_id], lookalike)


def _seed_reused_rows(project_id: str, conversation_id: str) -> None:
    from semantica_dataagent.db import Build, Conversation, sessions

    with sessions().begin() as db:
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
            Conversation(
                id=conversation_id,
                owner_id="owner",
                project_id=project_id,
                title=conversation_id,
                agent="pi",
                provider_id="provider",
                model="model",
            )
        )


def test_reused_project_id_deletes_new_real_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    token = _seed(tmp_path, monkeypatch)
    suffix = uuid.uuid4().hex[:10]
    name = f"Reuse real {suffix}"
    lookalike = "p" + suffix + "c"
    project_id = None
    try:
        from semantica_dataagent.app import app
        from semantica_dataagent.db import (
            Build,
            Conversation,
            Project,
            ProjectDeletion,
            sessions,
        )

        headers = {"Authorization": f"Bearer {token}"}
        with TestClient(app) as client:
            created = client.post(
                "/api/v1/projects",
                headers=headers,
                json={"name": name, "type": "document"},
            )
            assert created.status_code == 201, created.text
            project_id = created.json()["id"]

            _seed_reused_rows(project_id, f"{project_id}-first")
            _seed_generation(project_id, f"{project_id}_initial")

            archived = client.post(
                f"/api/v1/projects/{project_id}/archive", headers=headers
            )
            assert archived.status_code == 200, archived.text
            deleted = client.post(
                f"/api/v1/projects/{project_id}/delete",
                headers=headers,
                json={"name": name},
            )
            assert deleted.status_code == 204, deleted.text
            assert _residual(project_id) == (0, 0, False)

            recreated = client.post(
                "/api/v1/projects",
                headers=headers,
                json={"name": name, "type": "document"},
            )
            assert recreated.status_code == 201, recreated.text
            assert recreated.json()["id"] == project_id
            _seed_reused_rows(project_id, f"{project_id}-second")
            _seed_generation(project_id, f"{project_id}_initial")

            active = client.post(
                f"/api/v1/projects/{project_id}/delete",
                headers=headers,
                json={"name": name},
            )
            assert active.status_code == 409, active.text
            archived = client.post(
                f"/api/v1/projects/{project_id}/archive", headers=headers
            )
            assert archived.status_code == 200, archived.text
            deleted = client.post(
                f"/api/v1/projects/{project_id}/delete",
                headers=headers,
                json={"name": name},
            )
            assert deleted.status_code == 204, deleted.text
            assert _residual(project_id) == (0, 0, False)

        with sessions()() as db:
            assert db.get(Project, project_id) is None
            assert db.query(Build).filter(Build.project_id == project_id).count() == 0
            assert (
                db.query(Conversation)
                .filter(Conversation.project_id == project_id)
                .count()
                == 0
            )
            deletion = db.get(ProjectDeletion, project_id)
            assert deletion is not None and deletion.status == "completed"
            steps = [item.get("name") for item in json.loads(deletion.steps)]
            assert steps == list(STEPS), "第二次删除没有使用新一轮步骤记录"
    finally:
        if project_id is not None:
            _drop_storage(project_id)
        _cleanup([project_id] if project_id else [], lookalike)
