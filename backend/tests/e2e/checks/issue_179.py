"""Project-type driven MCP tools and retrieval skills (#179).

The harness builds the default warehouse project.  This check adds a marker
document to it, creates a document project with its own Markdown source, builds
both, then enters through the real MCP HTTP endpoint:

- the default project must expose every tool registered before this change;
- the document project must hide all warehouse-only tools and reject calls to
  them with an explicit error;
- document search/read must work while warehouse search/read keeps working;
- ``GET /api/v1/skill?project=`` must return different skills for the two
  project types.
"""

from __future__ import annotations

import base64
import time
import uuid

BATCH = 2000
UNSUPPORTED = "该项目类型不支持此工具"

WAREHOUSE_TOOLS = {
    "describe_tables",
    "search_knowledge",
    "lookup_dim_values",
    "list_categories",
    "read_category_index",
    "read_document",
    "get_lineage",
    "read_task_sql",
    "validate_sql",
    "find_join_path",
    "request_clarification",
    "report_knowledge_gap",
    "propose_correction",
    "ask_dataagent",
    "sample_table_data",
    "propose_query",
    "execute_query",
    "get_capabilities",
}
DOCUMENT_TOOLS = {
    "search_knowledge",
    "read_document",
    "list_categories",
    "read_category_index",
    "request_clarification",
    "propose_correction",
    "report_knowledge_gap",
    "get_capabilities",
}


def _upload(ctx, project_id, category, marker):
    response = ctx.rest.post(
        "/api/v1/source/upload",
        params={"project": project_id},
        json={
            "target": f"knowledge/{category}",
            "files": [
                {
                    "path": "probe.md",
                    "content_base64": base64.b64encode(
                        f"# {category}\n\n{marker} appears in this document.\n".encode()
                    ).decode(),
                }
            ],
        },
    )
    ctx.require(
        response.status_code == 200, f"上传 {project_id} 失败: {response.text[:300]}"
    )
    return response.json()["commit"]


def _wait_build(ctx, project_id, build_id):
    deadline = time.monotonic() + ctx.config.build_timeout
    while time.monotonic() < deadline:
        response = ctx.rest.get(
            f"/api/v1/builds/{build_id}", params={"project": project_id}
        )
        ctx.require(response.status_code == 200, response.text[:200])
        detail = response.json()
        if detail.get("status") in {"succeeded", "failed", "cancelled"}:
            return detail
        time.sleep(ctx.config.poll_interval)
    raise AssertionError(f"项目 {project_id} 的构建 {build_id} 超时")


def _build(ctx, project_id, build_type):
    response = ctx.rest.post(
        "/api/v1/builds",
        params={"project": project_id},
        json={"build_type": build_type},
    )
    ctx.require(response.status_code == 202, response.text[:300])
    build = _wait_build(ctx, project_id, response.json()["id"])
    ctx.require(
        build["status"] == "succeeded",
        f"项目 {project_id} 构建失败: {build.get('status')} {build.get('error')}",
    )
    return build


def _value(result):
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured.get("result", structured)
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if not text:
            continue
        import json

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return None


def _outcome(session, name, arguments=None):
    result = session.call_tool(name, arguments or {})
    if getattr(result, "isError", False):
        body = getattr(result, "structuredContent", None) or {}
        return {"error": body.get("code"), "message": body.get("message", "")}
    return {"ok": _value(result)}


def _cleanup(ctx, project_id):
    import sqlite3

    with sqlite3.connect(ctx.runtime.db_path) as db:
        builds = db.execute(
            "SELECT generation FROM builds WHERE project_id = ? AND generation IS NOT NULL",
            (project_id,),
        ).fetchall()
        pointer = db.execute(
            "SELECT generation FROM generation_pointer WHERE project_id = ?",
            (project_id,),
        ).fetchall()
    generations = sorted({row[0] for row in builds} | {row[0] for row in pointer})
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
                        query + deletion, generations=generations, batch=BATCH
                    )
                    if not deleted[0]["n"]:
                        break
            remaining, _, _ = driver.execute_query(
                query + " RETURN count(n) AS n", generations=generations
            )
            result["neo4j_remaining"] = remaining[0]["n"]
    except Exception as exc:  # noqa: BLE001 - reported through the check result
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


def check_issue_179_project_types(ctx):
    suffix = uuid.uuid4().hex[:8]
    marker_a = f"issue179warehouse{suffix}"
    marker_b = f"issue179document{suffix}"
    category_a = f"issue179a{suffix}"
    category_b = f"issue179b{suffix}"
    project_b = None
    sessions = []
    try:
        _upload(ctx, "default", category_a, marker_a)
        build_a = _build(ctx, "default", "incremental")
        ref_a = f"document:{category_a}/probe.md"

        created = ctx.rest.post(
            "/api/v1/projects",
            json={"name": f"Issue 179 Docs {suffix}", "type": "document"},
        )
        ctx.require(created.status_code == 201, created.text[:300])
        project_b = created.json()
        project_b_id = project_b["id"]
        _upload(ctx, project_b_id, category_b, marker_b)
        build_b = _build(ctx, project_b_id, "full")
        ref_b = f"document:{category_b}/probe.md"

        warehouse = ctx.open_mcp(ctx.token, project="default")
        document = ctx.open_mcp(ctx.token, project=project_b_id)
        sessions.extend([warehouse, document])

        warehouse_tools = {tool.name for tool in warehouse.list_tools()}
        document_tools = {tool.name for tool in document.list_tools()}
        ctx.require(
            warehouse_tools == WAREHOUSE_TOOLS,
            f"数仓型工具列表发生变化: 缺少 {WAREHOUSE_TOOLS - warehouse_tools}，"
            f"多出 {warehouse_tools - WAREHOUSE_TOOLS}",
        )
        ctx.require(
            document_tools == DOCUMENT_TOOLS,
            f"文档型工具列表不正确: {sorted(document_tools)}",
        )
        excluded = WAREHOUSE_TOOLS - DOCUMENT_TOOLS
        ctx.require(
            not (document_tools & excluded),
            f"文档型仍暴露: {document_tools & excluded}",
        )

        for name, arguments in (
            ("describe_tables", {"names": ["shop.orders"]}),
            ("validate_sql", {"sql": "SELECT 1"}),
            ("lookup_dim_values", {"table": "shop.orders", "keyword": "x"}),
        ):
            outcome = _outcome(document, name, arguments)
            ctx.require(
                outcome.get("error") == "invalid_input"
                and UNSUPPORTED in outcome.get("message", ""),
                f"文档型调用 {name} 应返回“{UNSUPPORTED}”，实际 {outcome}",
            )

        warehouse_caps = _outcome(warehouse, "get_capabilities")
        ctx.require("ok" in warehouse_caps, warehouse_caps)
        ctx.require(
            set(warehouse_caps["ok"]["tools"]) == WAREHOUSE_TOOLS,
            f"数仓型 capabilities 工具列表变化: {warehouse_caps['ok']['tools']}",
        )
        document_caps = _outcome(document, "get_capabilities")
        ctx.require("ok" in document_caps, document_caps)
        ctx.require(
            set(document_caps["ok"]["tools"]) == DOCUMENT_TOOLS,
            f"文档型 capabilities 工具列表不正确: {document_caps['ok']['tools']}",
        )

        document_search = _outcome(
            document,
            "search_knowledge",
            {"query": marker_b, "scope": "docs", "top_k": 10},
        )
        ctx.require("ok" in document_search, document_search)
        ctx.require(
            any(
                str(row.get("ref", "")).startswith(ref_b)
                for row in document_search["ok"]
            ),
            f"文档型检索没有命中 {ref_b}: {document_search['ok']}",
        )
        document_read = _outcome(document, "read_document", {"doc_id": ref_b})
        ctx.require("ok" in document_read, document_read)
        ctx.require(
            marker_b in document_read["ok"].get("content", ""),
            f"文档型读取内容不正确: {document_read['ok']}",
        )

        warehouse_search = _outcome(
            warehouse,
            "search_knowledge",
            {"query": marker_a, "scope": "docs", "top_k": 10},
        )
        ctx.require("ok" in warehouse_search, warehouse_search)
        ctx.require(
            any(
                str(row.get("ref", "")).startswith(ref_a)
                for row in warehouse_search["ok"]
            ),
            f"数仓型检索没有命中 {ref_a}: {warehouse_search['ok']}",
        )
        warehouse_read = _outcome(warehouse, "read_document", {"doc_id": ref_a})
        ctx.require("ok" in warehouse_read, warehouse_read)
        ctx.require(
            marker_a in warehouse_read["ok"].get("content", ""),
            f"数仓型读取内容不正确: {warehouse_read['ok']}",
        )

        warehouse_skill = ctx.rest.get("/api/v1/skill", params={"project": "default"})
        document_skill = ctx.rest.get("/api/v1/skill", params={"project": project_b_id})
        ctx.require(warehouse_skill.status_code == 200, warehouse_skill.text[:200])
        ctx.require(document_skill.status_code == 200, document_skill.text[:200])
        ctx.require(
            warehouse_skill.text != document_skill.text,
            "两种项目类型返回了同一份 skill",
        )
        ctx.require(
            "name: dataagent-document-retrieval" in document_skill.text
            and "SQL" not in document_skill.text,
            "文档型 skill 不是文档问答版本",
        )
        ctx.require(
            "validate_sql" in warehouse_skill.text,
            "数仓型 skill 发生变化",
        )

        plugins = ctx.rest.get("/api/v1/plugins")
        ctx.require(plugins.status_code == 200, plugins.text[:200])
        source_types = {
            row["id"]: row.get("project_types")
            for row in plugins.json()
            if row["category"] == "source"
        }
        ctx.require(
            source_types.get("markdown") == ["warehouse", "document"],
            source_types,
        )
        ctx.require(source_types.get("dtb") == ["warehouse"], source_types)

        return {
            "project_a": "default",
            "project_b": project_b_id,
            "builds": {"a": build_a["id"], "b": build_b["id"]},
            "tools": {
                "warehouse": len(warehouse_tools),
                "document": len(document_tools),
                "hidden": sorted(excluded),
            },
            "skills": {
                "warehouse": len(warehouse_skill.text),
                "document": len(document_skill.text),
            },
        }
    finally:
        for session in sessions:
            try:
                session.close()
            except Exception as exc:  # noqa: BLE001 - cleanup is best effort
                ctx.logger.line(f"issue179 mcp cleanup failed: {exc}")
        if project_b is not None:
            cleanup = _cleanup(ctx, project_b["id"])
            ctx.logger.line(f"issue179 cleanup: {cleanup}")
            ctx.runtime.storage_touched = True
