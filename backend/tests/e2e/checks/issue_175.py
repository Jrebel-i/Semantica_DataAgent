"""End-to-end isolation checks for projects and storage (#175).

The harness has already built the default project from its sample source.
This check creates a second, document-type project through REST, uploads a
Markdown source into that project only, builds it, and verifies both directions
of isolation: new content is invisible from the default project and default
content is invisible from the new project.  It also exercises the transitional
administrator-only rule.  Every generation created here is cleaned up in
batches at the end.
"""

from __future__ import annotations

import base64
import sqlite3
import time
import uuid

BATCH = 2000


def _project_id(ctx, project):
    return project["id"]


def _wait_build(ctx, project_id, build_id):
    deadline = time.monotonic() + ctx.config.build_timeout
    while time.monotonic() < deadline:
        response = ctx.rest.get(
            f"/api/v1/builds/{build_id}", params={"project": project_id}
        )
        ctx.require(
            response.status_code == 200,
            f"构建 {build_id} 详情 HTTP {response.status_code}: {response.text[:200]}",
        )
        detail = response.json()
        if detail.get("status") in {"succeeded", "failed", "cancelled"}:
            return detail
        time.sleep(ctx.config.poll_interval)
    raise AssertionError(f"项目 {project_id} 的构建 {build_id} 超时")


def _search(ctx, project_id, query, scope="docs"):
    response = ctx.rest.get(
        "/api/v1/search",
        params={"query": query, "scope": scope, "top_k": 10, "project": project_id},
    )
    ctx.require(
        response.status_code == 200,
        f"项目 {project_id} 检索 HTTP {response.status_code}: {response.text[:200]}",
    )
    return response.json()


def _generations(ctx, project_id):
    with sqlite3.connect(ctx.runtime.db_path) as db:
        rows = db.execute(
            "SELECT generation FROM builds WHERE project_id = ? AND generation IS NOT NULL",
            (project_id,),
        ).fetchall()
        pointer = db.execute(
            "SELECT generation FROM generation_pointer WHERE project_id = ?",
            (project_id,),
        ).fetchall()
    return sorted({row[0] for row in rows} | {row[0] for row in pointer})


def _cleanup(ctx, project_id):
    """Delete this project's graph nodes and vector collections in batches."""
    generations = _generations(ctx, project_id)
    result = {"generations": generations}
    try:
        from neo4j import GraphDatabase

        with GraphDatabase.driver(
            ctx.runtime.config.neo4j_uri,
            auth=(ctx.runtime.config.neo4j_user, ctx.runtime.password),
            connection_timeout=10,
        ) as driver:
            query = "MATCH (n:SDAEntity) WHERE n.generation IN $generations"
            for deletion in (
                " MATCH (n)-[r]-() WITH DISTINCT r LIMIT $batch DELETE r RETURN count(*) AS n",
                " WITH n LIMIT $batch DETACH DELETE n RETURN count(*) AS n",
            ):
                while True:
                    deleted, _, _ = driver.execute_query(
                        query + deletion,
                        generations=generations,
                        batch=BATCH,
                    )
                    if not deleted[0]["n"]:
                        break
            remaining, _, _ = driver.execute_query(
                query + " RETURN count(n) AS n", generations=generations
            )
            result["neo4j_remaining"] = remaining[0]["n"]
    except Exception as exc:  # noqa: BLE001 - the harness retries cleanup too
        result["neo4j_error"] = f"{type(exc).__name__}: {exc}"
    try:
        from pymilvus import MilvusClient

        client = MilvusClient(uri=ctx.runtime.config.milvus_uri, timeout=10)
        try:
            names = set(client.list_collections())
            for generation in generations:
                collection = "sda_" + generation
                if collection in names:
                    client.drop_collection(collection)
            remaining = {"sda_" + generation for generation in generations} & set(
                client.list_collections()
            )
            result["milvus_remaining"] = sorted(remaining)
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001
        result["milvus_error"] = f"{type(exc).__name__}: {exc}"
    return result


def check_project_storage_isolation(ctx):
    suffix = uuid.uuid4().hex[:8]
    token_name = f"issue175-user-{suffix}"
    user_token = None
    project = None
    default_generations = None
    default_builds = None
    try:
        default_generations = ctx.rest.get("/api/v1/generations").json()
        default_builds = ctx.rest.get("/api/v1/builds").json()
        default_categories = ctx.rest.get("/api/v1/categories").json()
        default_docs = [
            doc
            for category in default_categories
            for doc in category.get("documents", [])
        ]
        ctx.require(default_categories, "默认项目没有类别，无法验证反向隔离")
        ctx.require(default_docs, "默认项目没有文档，无法验证反向隔离")
        default_ref = default_docs[0]["ref"]

        created = ctx.rest.post(
            "/api/v1/projects",
            json={"name": f"Issue 175 Docs {suffix}", "type": "document"},
        )
        ctx.require(created.status_code == 201, created.text[:300])
        project = created.json()
        project_id = _project_id(ctx, project)
        ctx.require(project["type"] == "document", project)
        ctx.require(project["my_role"] == "admin", project)

        category = f"issue{suffix}"
        marker = f"issue175marker{suffix}"
        upload = ctx.rest.post(
            "/api/v1/source/upload",
            params={"project": project_id},
            json={
                "target": f"knowledge/{category}",
                "files": [
                    {
                        "path": "probe.md",
                        "content_base64": base64.b64encode(
                            f"# Probe\n\n{marker} belongs to {project_id}.\n".encode()
                        ).decode(),
                    }
                ],
            },
        )
        ctx.require(upload.status_code == 200, upload.text[:300])

        started = ctx.rest.post(
            "/api/v1/builds",
            params={"project": project_id},
            json={"build_type": "full"},
        )
        ctx.require(started.status_code == 202, started.text[:300])
        build = _wait_build(ctx, project_id, started.json()["id"])
        ctx.require(
            build["status"] == "succeeded",
            f"项目构建未成功：{build.get('status')} {build.get('error')}",
        )
        ctx.require(build["project_id"] == project_id, build)

        rows = ctx.rest.get("/api/v1/builds", params={"project": project_id}).json()
        ctx.require(
            [row["id"] for row in rows] == [build["id"]],
            f"项目构建列表不隔离：{rows}",
        )
        ctx.require(
            ctx.rest.get("/api/v1/builds").json() == default_builds,
            "新项目构建污染了默认项目的构建列表",
        )

        probe_ref = f"document:{category}/probe.md"
        hits = _search(ctx, project_id, marker)
        ctx.require(
            any(
                hit.get("doc_id") == probe_ref
                or str(hit.get("ref", "")).startswith(probe_ref)
                for hit in hits
            ),
            f"新项目检索不到自己的文档：{hits}",
        )
        document = ctx.rest.get(
            "/api/v1/document",
            params={"doc_id": probe_ref, "project": project_id},
        )
        ctx.require(document.status_code == 200, document.text[:300])
        categories = ctx.rest.get(
            "/api/v1/categories", params={"project": project_id}
        ).json()
        ctx.require(
            any(item["name"] == category for item in categories),
            f"新项目类别缺失：{categories}",
        )
        index = ctx.rest.get(
            "/api/v1/category-index",
            params={"category": category, "project": project_id},
        )
        ctx.require(index.status_code == 200, index.text[:300])

        default_hits = _search(ctx, "default", marker)
        ctx.require(
            all(
                hit.get("doc_id") != probe_ref
                and not str(hit.get("ref", "")).startswith(probe_ref)
                for hit in default_hits
            )
            and marker not in str(default_hits),
            f"默认项目看到了新项目内容：{default_hits}",
        )
        missing_doc = ctx.rest.get("/api/v1/document", params={"doc_id": probe_ref})
        ctx.require(missing_doc.status_code == 404, missing_doc.text[:200])
        default_category_names = {
            item["name"] for item in ctx.rest.get("/api/v1/categories").json()
        }
        ctx.require(category not in default_category_names, "默认项目看到了新项目类别")

        default_hits_in_project = _search(ctx, project_id, ctx.config.search_query)
        ctx.require(
            all(
                hit.get("doc_id") != default_ref
                and not str(hit.get("ref", "")).startswith(default_ref)
                for hit in default_hits_in_project
            ),
            f"新项目看到了默认项目内容：{default_hits_in_project}",
        )
        default_doc_in_project = ctx.rest.get(
            "/api/v1/document",
            params={"doc_id": default_ref, "project": project_id},
        )
        ctx.require(
            default_doc_in_project.status_code == 404,
            default_doc_in_project.text[:200],
        )
        default_index_in_project = ctx.rest.get(
            "/api/v1/category-index",
            params={"category": default_categories[0]["name"], "project": project_id},
        )
        ctx.require(
            default_index_in_project.status_code == 404,
            default_index_in_project.text[:200],
        )

        after_generations = ctx.rest.get("/api/v1/generations").json()
        ctx.require(
            after_generations == default_generations,
            "新项目构建改变了默认项目的代列表",
        )
        ctx.require(
            _search(ctx, "default", ctx.config.search_query), "默认项目检索失效"
        )

        issued = ctx.rest.post(
            "/api/v1/tokens", json={"name": token_name, "role": "user"}
        )
        ctx.require(issued.status_code == 201, issued.text[:200])
        user_token = issued.json()
        headers = {"Authorization": f"Bearer {user_token['token']}"}
        denied = [
            ctx.rest.get(
                "/api/v1/search",
                params={"query": marker, "project": project_id},
                headers=headers,
            ),
            ctx.rest.get(
                "/api/v1/categories", params={"project": project_id}, headers=headers
            ),
            ctx.rest.get(
                "/api/v1/document",
                params={"doc_id": probe_ref, "project": project_id},
                headers=headers,
            ),
            ctx.rest.post(
                "/api/v1/builds",
                params={"project": project_id},
                json={"build_type": "incremental"},
                headers=headers,
            ),
            ctx.rest.get(
                "/api/v1/source/files", params={"project": project_id}, headers=headers
            ),
        ]
        for response in denied:
            ctx.require(
                response.status_code == 404,
                f"非管理员访问非默认项目应得到 404，实际 {response.status_code}: "
                f"{response.text[:200]}",
            )
        visible = ctx.rest.get("/api/v1/projects", headers=headers).json()
        ctx.require(
            [item["id"] for item in visible] == ["default"],
            f"普通用户看到了非默认项目：{visible}",
        )

        return {
            "project": project_id,
            "build": build["id"],
            "marker": marker,
            "probe_ref": probe_ref,
            "default_ref": default_ref,
        }
    finally:
        if user_token is not None:
            ctx.rest.delete(f"/api/v1/tokens/{user_token['id']}")
        if project is not None:
            cleanup = _cleanup(ctx, project["id"])
            ctx.logger.line(f"issue175 cleanup: {cleanup}")
            ctx.runtime.storage_touched = True
