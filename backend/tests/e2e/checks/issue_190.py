"""Project archive, restore and permanent deletion (#190).

The harness has already built the default project.  This check creates two
document projects whose ids are deliberate prefix lookalikes (``ab...`` and
``abc...``), seeds real Neo4j/Milvus data for both, then observes the whole
lifecycle through REST and MCP:

- only system administrators can archive/restore, and members are rejected;
- an archived project rejects ask/upload/build/download-link/MCP while its
  conversations stay readable and can still be deleted by their owner;
- other projects and the token itself keep working while one project is
  archived;
- deletion rejects the default project, active projects and a wrong name;
- permanent deletion removes the source directory, generations, Neo4j, Milvus,
  builds, corrections and conversations, while the lookalike project's storage
  stays intact; repeating the call is safe and audit rows are retained.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

_CHECKS_DIR = str(Path(__file__).resolve().parent)
if _CHECKS_DIR not in sys.path:
    sys.path.append(_CHECKS_DIR)

from neo4j import GraphDatabase
from pymilvus import MilvusClient

ARCHIVED = "项目已归档"
LOGGER = logging.getLogger(__name__)


def _detail(response):
    try:
        body = response.json()
    except ValueError:
        return response.text[:400]
    return body.get("detail") if isinstance(body, dict) else str(body)


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def _status(ctx, response, expected, label):
    ctx.require(
        response.status_code == expected,
        f"{label} 应 {expected}，实际 {response.status_code}: {response.text[:400]}",
    )


def _create_project(ctx, name, id_prefix):
    response = ctx.rest.post(
        "/api/v1/projects", json={"name": name, "type": "document"}
    )
    ctx.require(response.status_code == 201, response.text[:400])
    project = response.json()
    ctx.require(
        project["id"].startswith(id_prefix),
        f"项目 id 不符合预期前缀 {id_prefix}: {project}",
    )
    return project


def _issue_token(ctx, name, projects):
    response = ctx.rest.post(
        "/api/v1/tokens",
        json={"name": name, "role": "user", "projects": projects},
    )
    ctx.require(response.status_code == 201, response.text[:400])
    return response.json()


def _owner_id(token):
    from semantica_dataagent.db import Token, digest, sessions

    with sessions()() as db:
        return db.get(Token, digest(token)).id


def _seed_project(ctx, project, generation, owner_id, suffix):
    from semantica_dataagent.db import (
        Build,
        Conversation,
        CorrectionProposal,
        GenerationPointer,
        KnowledgeGeneration,
        QueryExecution,
        Turn,
        sessions,
    )
    from semantica_dataagent.project_context import project_source_directory

    project_id = project["id"]
    conversation_id = f"{project_id}-conversation-{suffix}"
    query_id = f"{project_id}-query-{suffix}"
    source = project_source_directory(project_id)
    source.mkdir(parents=True, exist_ok=True)
    (source / "knowledge.md").write_text(
        f"# {project['name']}\n\n归档删除检查。\n", encoding="utf-8"
    )

    now = datetime.now(UTC)
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
                started_at=now,
                finished_at=now,
                counts="{}",
                build_type="full",
                generation=generation,
            )
        )
        db.add(
            CorrectionProposal(
                project_id=project_id,
                owner_id=owner_id,
                target="document",
                patch="",
                original="",
                diff="",
                reason=f"issue 190 {suffix}",
            )
        )
        db.add(
            Conversation(
                id=conversation_id,
                owner_id=owner_id,
                project_id=project_id,
                title=f"Issue 190 {suffix}",
                agent="pi",
                provider_id="provider",
                model="model",
            )
        )
        db.add(
            Turn(
                conversation_id=conversation_id,
                sequence=1,
                question="旧问题",
                status="succeeded",
                answer="旧答案",
                sql="[]",
                citations="[]",
                started_at=now,
                finished_at=now,
            )
        )
        db.add(
            QueryExecution(
                id=query_id,
                owner_id=owner_id,
                conversation_id=conversation_id,
                sql="select 1",
                status="succeeded",
                rows=json.dumps({"columns": ["x"], "data": [[1]]}),
                row_count=1,
            )
        )
        db.flush()

    driver = GraphDatabase.driver(
        ctx.config.neo4j_uri,
        auth=(ctx.config.neo4j_user, ctx.runtime.password),
    )
    vectors = MilvusClient(uri=ctx.config.milvus_uri)
    try:
        driver.execute_query(
            "CREATE (:SDAEntity {generation:$generation, project:$project})",
            generation=generation,
            project=project_id,
        )
        vectors.create_collection("sda_" + generation, dimension=2)
    finally:
        driver.close()
        vectors.close()
    return conversation_id, query_id


def _storage(ctx, project_id):
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
        from semantica_dataagent.project_context import project_source_directory

        return nodes, collections, project_source_directory(project_id).exists()
    finally:
        driver.close()
        vectors.close()


def _tree_is_clean(ctx, project_id, label):
    nodes, collections, source_exists = _storage(ctx, project_id)
    ctx.require(
        (nodes, collections, source_exists) == (0, [], False),
        f"{label} 仍有残留: neo4j={nodes} milvus={collections} source={source_exists}",
    )


def _db_rows(project_id):
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
        sessions,
    )

    with sessions()() as db:
        return {
            "project": db.get(Project, project_id),
            "deletion": db.get(ProjectDeletion, project_id),
            "builds": db.query(Build).filter(Build.project_id == project_id).count(),
            "corrections": db.query(CorrectionProposal)
            .filter(CorrectionProposal.project_id == project_id)
            .count(),
            "conversations": db.query(Conversation)
            .filter(Conversation.project_id == project_id)
            .count(),
            "generations": db.query(KnowledgeGeneration)
            .filter(KnowledgeGeneration.project_id == project_id)
            .count(),
            "pointers": db.query(GenerationPointer)
            .filter(GenerationPointer.project_id == project_id)
            .count(),
            "members": db.query(ProjectMember)
            .filter(ProjectMember.project_id == project_id)
            .count(),
            "audit_actions": [
                row.action
                for row in db.query(ProjectAudit)
                .filter(ProjectAudit.project_id == project_id)
                .order_by(ProjectAudit.id)
                .all()
            ],
        }


def _mcp_text(result):
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return json.dumps(structured, ensure_ascii=False)
    return " ".join(
        getattr(item, "text", "") or "" for item in getattr(result, "content", [])
    )


def _mcp_value(result):
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured.get("result", structured)
    return None


def _force_cleanup(ctx, project_ids, generations):
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

    try:
        driver = GraphDatabase.driver(
            ctx.config.neo4j_uri,
            auth=(ctx.config.neo4j_user, ctx.runtime.password),
        )
        try:
            for project_id in project_ids:
                driver.execute_query(
                    "MATCH (n:SDAEntity) WHERE n.generation STARTS WITH $prefix "
                    "DETACH DELETE n",
                    prefix=project_id + "_",
                )
        finally:
            driver.close()
    except Exception:
        LOGGER.warning("issue 190 cleanup could not clear Neo4j", exc_info=True)
    try:
        vectors = MilvusClient(uri=ctx.config.milvus_uri)
        try:
            for name in list(vectors.list_collections()):
                if any(
                    name == "sda_" + generation
                    or name.startswith("sda_" + project_id + "_")
                    for project_id in project_ids
                    for generation in generations
                ):
                    vectors.drop_collection(name)
        finally:
            vectors.close()
    except Exception:
        LOGGER.warning("issue 190 cleanup could not clear Milvus", exc_info=True)
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
                db.query(Build).filter(Build.project_id == project_id).delete(
                    synchronize_session=False
                )
                db.query(CorrectionProposal).filter(
                    CorrectionProposal.project_id == project_id
                ).delete(synchronize_session=False)
                db.query(KnowledgeGeneration).filter(
                    KnowledgeGeneration.project_id == project_id
                ).delete(synchronize_session=False)
                db.query(GenerationPointer).filter(
                    GenerationPointer.project_id == project_id
                ).delete(synchronize_session=False)
                db.query(ProjectSetting).filter(
                    ProjectSetting.project_id == project_id
                ).delete(synchronize_session=False)
                db.query(ProjectMember).filter(
                    ProjectMember.project_id == project_id
                ).delete(synchronize_session=False)
                db.query(ProjectAudit).filter(
                    ProjectAudit.project_id == project_id
                ).delete(synchronize_session=False)
                db.query(ProjectDeletion).filter(
                    ProjectDeletion.project_id == project_id
                ).delete(synchronize_session=False)
                db.query(Project).filter(Project.id == project_id).delete(
                    synchronize_session=False
                )
        except Exception:
            LOGGER.warning("issue 190 cleanup could not clear rows", exc_info=True)


def check_issue_190_project_lifecycle(ctx):
    os.environ["DATAAGENT_DB_PATH"] = str(ctx.runtime.db_path)
    suffix = uuid.uuid4().hex[:8]
    projects: list[str] = []
    generations: list[str] = []
    created_a = created_b = None
    try:
        created_a = _create_project(ctx, f"Issue 190 ab {suffix}", "issue190ab")
        created_b = _create_project(ctx, f"Issue 190 ab{suffix}c", created_a["id"])
        project_a, project_b = created_a["id"], created_b["id"]
        projects = [project_a, project_b]
        generation_a, generation_b = f"{project_a}_e2e", f"{project_b}_e2e"
        generations = [generation_a, generation_b]
        ctx.require(
            project_b.startswith(project_a)
            and not project_b.startswith(project_a + "_"),
            f"相似前缀项目构造失败: {project_a} / {project_b}",
        )

        projects_now = ctx.rest.get("/api/v1/projects")
        _status(ctx, projects_now, 200, "项目列表")
        default = next(row for row in projects_now.json() if row["id"] == "default")

        member = _issue_token(
            ctx,
            f"issue190-member-{suffix}",
            [
                {"project_id": project_a, "role": "member"},
                {"project_id": project_b, "role": "member"},
            ],
        )
        project_admin = _issue_token(
            ctx,
            f"issue190-admin-{suffix}",
            [{"project_id": project_a, "role": "admin"}],
        )
        member_headers = _headers(member["token"])
        owner_id = _owner_id(member["token"])
        conversation_a, query_a = _seed_project(
            ctx, created_a, generation_a, owner_id, suffix
        )
        _seed_project(ctx, created_b, generation_b, owner_id, suffix)
        ctx.require(
            _storage(ctx, project_a)[:2] == (1, ["sda_" + generation_a]),
            "项目 A 的测试存储未就绪",
        )
        ctx.require(
            _storage(ctx, project_b)[:2] == (1, ["sda_" + generation_b]),
            "项目 B 的测试存储未就绪",
        )

        denied_default = ctx.rest.post(
            "/api/v1/projects/default/delete", json={"name": default["name"]}
        )
        _status(ctx, denied_default, 409, "删除默认项目")
        ctx.require("默认项目不可删除" in denied_default.text, denied_default.text)

        active_delete = ctx.rest.post(
            f"/api/v1/projects/{project_b}/delete",
            json={"name": created_b["name"]},
        )
        _status(ctx, active_delete, 409, "删除未归档项目")
        ctx.require("项目未归档" in active_delete.text, active_delete.text)

        for token in (member["token"], project_admin["token"]):
            denied = ctx.rest.post(
                f"/api/v1/projects/{project_a}/archive", headers=_headers(token)
            )
            _status(ctx, denied, 403, "非系统管理员归档")

        archived = ctx.rest.post(
            f"/api/v1/projects/{project_a}/archive", headers=_headers(ctx.token)
        )
        _status(ctx, archived, 200, "归档项目")
        ctx.require(archived.json()["status"] == "archived", archived.text)

        listing = ctx.rest.get("/api/v1/projects", headers=member_headers)
        _status(ctx, listing, 200, "成员项目列表")
        statuses = {row["id"]: row["status"] for row in listing.json()}
        ctx.require(statuses[project_a] == "archived", f"归档状态未体现: {statuses}")
        ctx.require(statuses[project_b] == "active", f"其它项目被改动: {statuses}")

        ask = ctx.rest.post(
            "/api/v1/conversations/ask",
            headers=member_headers,
            json={"question": "归档后还能问吗", "project": project_a},
        )
        _status(ctx, ask, 409, "归档项目提问")
        ctx.require(ARCHIVED in ask.text, ask.text)

        upload = ctx.rest.post(
            "/api/v1/source/upload",
            params={"project": project_a},
            headers=_headers(ctx.token),
            json={"target": "knowledge/metrics", "files": []},
        )
        _status(ctx, upload, 409, "归档项目上传")
        ctx.require(ARCHIVED in upload.text, upload.text)

        build = ctx.rest.post(
            "/api/v1/builds",
            params={"project": project_a},
            headers=_headers(ctx.token),
            json={"build_type": "incremental"},
        )
        _status(ctx, build, 409, "归档项目构建")
        ctx.require(ARCHIVED in build.text, build.text)

        download = ctx.rest.post(
            f"/api/v1/queries/{query_a}/download-links", headers=member_headers
        )
        _status(ctx, download, 409, "归档项目下载链接")
        ctx.require(ARCHIVED in download.text, download.text)

        detail = ctx.rest.get(
            f"/api/v1/conversations/{conversation_a}", headers=member_headers
        )
        _status(ctx, detail, 200, "归档项目会话详情")
        ctx.require(detail.json()["project_id"] == project_a, detail.text[:400])

        filtered = ctx.rest.get(
            "/api/v1/conversations",
            params={"project": project_a},
            headers=member_headers,
        )
        _status(ctx, filtered, 200, "归档项目会话列表")
        ctx.require(
            conversation_a in {row["id"] for row in filtered.json()},
            "归档项目的已有会话不可见",
        )

        deleted_conversation = ctx.rest.delete(
            f"/api/v1/conversations/{conversation_a}", headers=member_headers
        )
        _status(ctx, deleted_conversation, 204, "所有者删除归档项目会话")

        archived_mcp = ctx.open_mcp(member["token"], project=project_a)
        archived_result = archived_mcp.call_tool("get_capabilities", {})
        ctx.require(getattr(archived_result, "isError", False), "归档项目 MCP 未报错")
        ctx.require(
            ARCHIVED in _mcp_text(archived_result),
            f"归档项目 MCP 错误信息错误: {_mcp_text(archived_result)[:400]}",
        )

        other_mcp = ctx.open_mcp(member["token"], project=project_b)
        other_result = other_mcp.call_tool("get_capabilities", {})
        ctx.require(
            not getattr(other_result, "isError", False),
            f"归档项目影响了其它项目 MCP: {_mcp_text(other_result)[:400]}",
        )
        other_value = _mcp_value(other_result) or {}
        ctx.require(
            (other_value.get("project") or {}).get("id") == project_b,
            f"MCP 未解析到其它项目: {other_value!r}",
        )

        restored = ctx.rest.post(
            f"/api/v1/projects/{project_a}/restore", headers=_headers(ctx.token)
        )
        _status(ctx, restored, 200, "恢复项目")
        ctx.require(restored.json()["status"] == "active", restored.text)

        open_mcp = ctx.open_mcp(member["token"], project=project_a)
        open_result = open_mcp.call_tool("get_capabilities", {})
        ctx.require(
            not getattr(open_result, "isError", False),
            f"恢复后 MCP 仍被冻结: {_mcp_text(open_result)[:400]}",
        )

        archived_again = ctx.rest.post(
            f"/api/v1/projects/{project_a}/archive", headers=_headers(ctx.token)
        )
        _status(ctx, archived_again, 200, "再次归档项目")

        wrong_name = ctx.rest.post(
            f"/api/v1/projects/{project_a}/delete",
            json={"name": created_a["name"] + "x"},
        )
        _status(ctx, wrong_name, 422, "项目名不匹配删除")
        ctx.require("项目名不匹配" in wrong_name.text, wrong_name.text)

        deleted = ctx.rest.post(
            f"/api/v1/projects/{project_a}/delete",
            json={"name": created_a["name"]},
        )
        _status(ctx, deleted, 204, "永久删除项目")
        repeated = ctx.rest.post(
            f"/api/v1/projects/{project_a}/delete",
            json={"name": created_a["name"]},
        )
        _status(ctx, repeated, 204, "重复删除项目")

        _tree_is_clean(ctx, project_a, "项目 A")
        nodes_b, collections_b, source_b = _storage(ctx, project_b)
        ctx.require(
            (nodes_b, collections_b, source_b) == (1, ["sda_" + generation_b], True),
            f"删除 A 误伤相似前缀项目 B: "
            f"neo4j={nodes_b} milvus={collections_b} source={source_b}",
        )
        project_b_detail = ctx.rest.get("/api/v1/projects")
        _status(ctx, project_b_detail, 200, "删除后项目列表")
        ctx.require(
            {row["id"] for row in project_b_detail.json()} >= {"default", project_b},
            "删除 A 后 B 或默认项目消失",
        )

        rows = _db_rows(project_a)
        ctx.require(rows["project"] is None, f"项目 A 数据库行仍在: {rows}")
        for key in (
            "builds",
            "corrections",
            "conversations",
            "generations",
            "pointers",
            "members",
        ):
            ctx.require(rows[key] == 0, f"项目 A 的 {key} 未清理: {rows}")
        ctx.require(rows["deletion"] is not None, "删除状态行被移除，重复调用无法幂等")
        ctx.require(
            {"project.archive", "project.restore", "project.delete"}
            <= set(rows["audit_actions"]),
            f"项目审计未保留: {rows['audit_actions']}",
        )

        tokens = ctx.rest.get("/api/v1/tokens")
        _status(ctx, tokens, 200, "令牌列表")
        member_row = next(row for row in tokens.json() if row["id"] == member["id"])
        token_projects = {row["project_id"] for row in member_row["projects"]}
        ctx.require(
            project_a not in token_projects and project_b in token_projects,
            f"令牌项目列表未正确更新: {token_projects}",
        )

        assert_other = ctx.rest.get(
            "/api/v1/conversations",
            params={"project": project_b},
            headers=member_headers,
        )
        _status(ctx, assert_other, 200, "令牌访问其它项目")

        _status(
            ctx,
            ctx.rest.post(
                f"/api/v1/projects/{project_b}/archive",
                headers=_headers(ctx.token),
            ),
            200,
            "清理前归档项目 B",
        )
        _status(
            ctx,
            ctx.rest.post(
                f"/api/v1/projects/{project_b}/delete",
                json={"name": created_b["name"]},
            ),
            204,
            "清理项目 B",
        )
        _tree_is_clean(ctx, project_b, "项目 B")

        tokens = ctx.rest.get("/api/v1/tokens")
        _status(ctx, tokens, 200, "删除后令牌列表")
        member_row = next(row for row in tokens.json() if row["id"] == member["id"])
        ctx.require(
            member_row["projects"] == [], f"令牌未保留或项目未移除: {member_row}"
        )
    finally:
        _force_cleanup(ctx, projects, generations)
