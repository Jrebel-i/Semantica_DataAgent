"""Reusing a project id after permanent deletion (#217).

The old completed deletion row must not shadow a newly created project with the
same id.  This check drives the whole lifecycle through REST against real
Neo4j/Milvus data:

- create -> archive -> delete removes the first project;
- creating the same name again reuses the id, clears the stale completed row,
  and the new project is visible through GET /projects;
- deleting the new project while active returns 409, a wrong name returns 422
  without changing its status, and a successful delete really cleans its
  source directory, generations, Neo4j nodes, Milvus collections and DB rows;
- the second deletion records exactly one fresh round of steps;
- crash recovery ignores residual completed/failed rows while the recreated
  project is not in the deleting state;
- a project whose old deletion is still in progress cannot reuse the id.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import uuid
from pathlib import Path

_CHECKS_DIR = str(Path(__file__).resolve().parent)
if _CHECKS_DIR not in sys.path:
    sys.path.append(_CHECKS_DIR)

from neo4j import GraphDatabase
from pymilvus import MilvusClient

STEPS = ("source", "neo4j", "milvus", "attachments", "database")
LOGGER = logging.getLogger(__name__)


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def _status(ctx, response, expected, label):
    ctx.require(
        response.status_code == expected,
        f"{label} 应 {expected}，实际 {response.status_code}: {response.text[:400]}",
    )


def _create_project(ctx, name):
    response = ctx.rest.post(
        "/api/v1/projects", json={"name": name, "type": "document"}
    )
    ctx.require(response.status_code == 201, response.text[:400])
    return response.json()


def _seed(ctx, project, generation, conversation_id):
    from datetime import UTC, datetime

    from semantica_dataagent.db import (
        Conversation,
        GenerationPointer,
        KnowledgeGeneration,
        Turn,
        sessions,
    )
    from semantica_dataagent.project_context import project_source_directory

    project_id = project["id"]
    source = project_source_directory(project_id)
    source.mkdir(parents=True, exist_ok=True)
    (source / "knowledge.md").write_text(
        f"# {project['name']}\n\nid 复用检查。\n", encoding="utf-8"
    )
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
        conversation = db.get(Conversation, conversation_id)
        if conversation is None:
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
        turn = db.query(Turn).filter(Turn.conversation_id == conversation_id).first()
        if turn is None:
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

    driver = GraphDatabase.driver(
        ctx.config.neo4j_uri,
        auth=(ctx.config.neo4j_user, ctx.runtime.password),
    )
    vectors = MilvusClient(uri=ctx.config.milvus_uri)
    try:
        driver.execute_query(
            "CREATE (:SDAEntity {generation:$generation})", generation=generation
        )
        vectors.create_collection("sda_" + generation, dimension=2)
    finally:
        driver.close()
        vectors.close()


def _storage(ctx, project_id):
    from semantica_dataagent.project_context import project_source_directory

    driver = GraphDatabase.driver(
        ctx.config.neo4j_uri,
        auth=(ctx.config.neo4j_user, ctx.runtime.password),
    )
    vectors = MilvusClient(uri=ctx.config.milvus_uri)
    try:
        rows, _, _ = driver.execute_query(
            "MATCH (n:SDAEntity) WHERE n.generation STARTS WITH $prefix "
            "RETURN count(n) AS count",
            prefix=project_id + "_",
        )
        nodes = rows[0]["count"] if rows else 0
        collections = [
            name
            for name in vectors.list_collections()
            if name.startswith("sda_" + project_id + "_")
        ]
        return nodes, collections, project_source_directory(project_id).exists()
    finally:
        driver.close()
        vectors.close()


def _assert_clean(ctx, project_id, label):
    nodes, collections, source_exists = _storage(ctx, project_id)
    ctx.require(
        (nodes, collections, source_exists) == (0, [], False),
        f"{label} 仍有残留: neo4j={nodes} milvus={collections} source={source_exists}",
    )


def _db_state(project_id):
    from semantica_dataagent.db import (
        Conversation,
        Project,
        ProjectDeletion,
        sessions,
    )

    with sessions()() as db:
        deletion = db.get(ProjectDeletion, project_id)
        return {
            "project": db.get(Project, project_id),
            "deletion": deletion,
            "steps": json.loads(deletion.steps) if deletion is not None else [],
            "conversations": db.query(Conversation)
            .filter(Conversation.project_id == project_id)
            .count(),
        }


def _cleanup(ctx, project_ids, generations):
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
        QueryExecution,
        Turn,
        sessions,
    )
    from semantica_dataagent.project_context import project_source_directory

    driver = GraphDatabase.driver(
        ctx.config.neo4j_uri,
        auth=(ctx.config.neo4j_user, ctx.runtime.password),
    )
    vectors = MilvusClient(uri=ctx.config.milvus_uri)
    try:
        for project_id in project_ids:
            driver.execute_query(
                "MATCH (n:SDAEntity) WHERE n.generation STARTS WITH $prefix "
                "DETACH DELETE n",
                prefix=project_id + "_",
            )
        wanted = tuple("sda_" + generation for generation in generations)
        prefixes = tuple("sda_" + project_id + "_" for project_id in project_ids)
        for name in list(vectors.list_collections()):
            if name in wanted or name.startswith(prefixes):
                vectors.drop_collection(name)
    finally:
        driver.close()
        vectors.close()
    for project_id in project_ids:
        shutil.rmtree(project_source_directory(project_id), ignore_errors=True)
        try:
            with sessions().begin() as db:
                conversation_ids = [
                    row.id
                    for row in db.query(Conversation.id)
                    .filter(Conversation.project_id == project_id)
                    .all()
                ]
                if conversation_ids:
                    db.query(Turn).filter(
                        Turn.conversation_id.in_(conversation_ids)
                    ).delete(synchronize_session=False)
                    db.query(QueryExecution).filter(
                        QueryExecution.conversation_id.in_(conversation_ids)
                    ).delete(synchronize_session=False)
                db.query(Conversation).filter(
                    Conversation.project_id == project_id
                ).delete(synchronize_session=False)
                for model in (
                    Build,
                    CorrectionProposal,
                    KnowledgeGeneration,
                    GenerationPointer,
                    ProjectSetting,
                    ProjectMember,
                    ProjectAudit,
                    ProjectDeletion,
                ):
                    db.query(model).filter(model.project_id == project_id).delete(
                        synchronize_session=False
                    )
                db.query(Project).filter(Project.id == project_id).delete(
                    synchronize_session=False
                )
        except Exception:
            LOGGER.warning("issue 217 cleanup could not clear rows", exc_info=True)


def check_issue_217_reused_project_id(ctx):
    os.environ["DATAAGENT_DB_PATH"] = str(ctx.runtime.db_path)
    suffix = uuid.uuid4().hex[:8]
    name = f"Issue 217 reuse {suffix}"
    projects: list[str] = []
    generations: list[str] = []
    try:
        first = _create_project(ctx, name)
        project_id = first["id"]
        projects = [project_id]
        first_generation = f"{project_id}_e2e1"
        generations.append(first_generation)
        _seed(ctx, first, first_generation, f"{project_id}-first")
        _status(
            ctx,
            ctx.rest.post(f"/api/v1/projects/{project_id}/archive"),
            200,
            "归档第一个项目",
        )
        _status(
            ctx,
            ctx.rest.post(f"/api/v1/projects/{project_id}/delete", json={"name": name}),
            204,
            "删除第一个项目",
        )
        _assert_clean(ctx, project_id, "第一个项目")
        first_state = _db_state(project_id)
        ctx.require(first_state["project"] is None, first_state)
        ctx.require(first_state["deletion"].status == "completed", first_state)

        recreated = _create_project(ctx, name)
        ctx.require(
            recreated["id"] == project_id,
            f"同 id 项目没有被复用: {recreated['id']} vs {project_id}",
        )
        listing = ctx.rest.get("/api/v1/projects")
        _status(ctx, listing, 200, "创建后项目列表")
        rows = [row for row in listing.json() if row["id"] == project_id]
        ctx.require(len(rows) == 1, f"项目列表没有唯一的新项目: {rows}")
        ctx.require(rows[0]["status"] == "active", f"新项目状态错误: {rows[0]}")
        ctx.require(rows[0]["name"] == name, f"新项目名称错误: {rows[0]}")
        after_create = _db_state(project_id)
        ctx.require(
            after_create["deletion"] is None,
            f"创建同 id 项目后仍残留旧删除记录: {after_create['deletion']}",
        )

        second_generation = f"{project_id}_e2e2"
        generations.append(second_generation)
        _seed(ctx, recreated, second_generation, f"{project_id}-second")

        active = ctx.rest.post(
            f"/api/v1/projects/{project_id}/delete", json={"name": name}
        )
        _status(ctx, active, 409, "删除未归档的新项目")
        ctx.require("未归档" in active.text, active.text)
        still_active = ctx.rest.get("/api/v1/projects")
        _status(ctx, still_active, 200, "未归档删除后项目列表")
        row = next(item for item in still_active.json() if item["id"] == project_id)
        ctx.require(row["status"] == "active", f"未归档删除改变了状态: {row}")

        _status(
            ctx,
            ctx.rest.post(f"/api/v1/projects/{project_id}/archive"),
            200,
            "归档新项目",
        )
        wrong = ctx.rest.post(
            f"/api/v1/projects/{project_id}/delete",
            json={"name": "完全不同的名字"},
        )
        _status(ctx, wrong, 422, "删除新项目时名字不匹配")
        ctx.require("项目名不匹配" in wrong.text, wrong.text)
        after_wrong = ctx.rest.get("/api/v1/projects")
        row = next(item for item in after_wrong.json() if item["id"] == project_id)
        ctx.require(row["status"] == "archived", f"名字错误改变了状态: {row}")

        _status(
            ctx,
            ctx.rest.post(f"/api/v1/projects/{project_id}/delete", json={"name": name}),
            204,
            "真正删除新项目",
        )
        _assert_clean(ctx, project_id, "新项目")
        final_state = _db_state(project_id)
        ctx.require(final_state["project"] is None, final_state)
        ctx.require(final_state["conversations"] == 0, final_state)
        step_names = [step.get("name") for step in final_state["steps"]]
        ctx.require(
            step_names == list(STEPS),
            f"第二次删除没有使用新一轮步骤记录: {step_names}",
        )
        ctx.require(
            all(step.get("status") == "succeeded" for step in final_state["steps"]),
            f"第二次删除存在未完成步骤: {final_state['steps']}",
        )

        third = _create_project(ctx, name)
        ctx.require(third["id"] == project_id, third)
        third_generation = f"{project_id}_e2e3"
        generations.append(third_generation)
        _seed(ctx, third, third_generation, f"{project_id}-third")
        from semantica_dataagent.db import ProjectDeletion, sessions

        with sessions().begin() as db:
            db.add(
                ProjectDeletion(
                    project_id=project_id,
                    project_name=name,
                    status="completed",
                    step="done",
                    steps=json.dumps(
                        [{"name": step, "status": "succeeded"} for step in STEPS]
                    ),
                    requested_by="owner",
                )
            )
        from semantica_dataagent.project_deletion import recover_project_deletions

        recover_project_deletions()
        recovered = _db_state(project_id)
        ctx.require(
            recovered["project"] is not None
            and recovered["project"].status == "active",
            f"恢复流程因完成记录删除了新项目: {recovered}",
        )
        ctx.require(recovered["conversations"] == 1, recovered)
        nodes, collections, source_exists = _storage(ctx, project_id)
        ctx.require(
            (nodes, collections, source_exists)
            == (1, ["sda_" + third_generation], True),
            f"恢复流程破坏了新项目存储: "
            f"neo4j={nodes} milvus={collections} source={source_exists}",
        )

        with sessions().begin() as db:
            row = db.get(ProjectDeletion, project_id)
            row.status = "failed"
            row.step = "milvus"
        recover_project_deletions()
        recovered = _db_state(project_id)
        ctx.require(
            recovered["project"] is not None,
            f"恢复流程因失败残留记录删除了新项目: {recovered}",
        )

        _status(
            ctx,
            ctx.rest.post(f"/api/v1/projects/{project_id}/archive"),
            200,
            "清理前归档第三个项目",
        )
        _status(
            ctx,
            ctx.rest.post(f"/api/v1/projects/{project_id}/delete", json={"name": name}),
            204,
            "清理第三个项目",
        )

        race = _create_project(ctx, f"Issue 217 race {suffix}")
        race_id = race["id"]
        projects.append(race_id)
        _status(
            ctx,
            ctx.rest.post(f"/api/v1/projects/{race_id}/archive"),
            200,
            "归档并发检查项目",
        )
        from semantica_dataagent.project_deletion import prepare_deletion

        ctx.require(
            prepare_deletion(race_id, race["name"], "owner", "Owner") == "active",
            "无法开始并发检查项目的删除",
        )
        variant = ctx.rest.post(
            "/api/v1/projects",
            json={"name": f"Issue 217 race-{suffix}", "type": "document"},
        )
        _status(ctx, variant, 201, "旧删除进行中创建同类项目")
        variant_id = variant.json()["id"]
        projects.append(variant_id)
        ctx.require(
            variant_id != race_id,
            f"旧删除仍在 deleting 时复用了项目 id: {variant_id}",
        )
        return {
            "ok": True,
            "info": {
                "project_id": project_id,
                "round_two_steps": step_names,
                "variant_id": variant_id,
            },
        }
    finally:
        _cleanup(ctx, projects, generations)
