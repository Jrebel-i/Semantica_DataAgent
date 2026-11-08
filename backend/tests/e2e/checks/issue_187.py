"""Per-project source adapter configuration (#187).

The harness already built the default warehouse project.  This check creates
two more warehouse projects and one document project, then enters through REST,
real builds and the real MCP HTTP endpoint:

- each project's adapter configuration is independent; changing project A does
  not change project B's next build result;
- changing a configuration only takes effect on the next build;
- a document project cannot enable the DTB source or its pull configuration;
- DTB pull credentials never come back in plaintext;
- project members can read but not write adapters; non-members get 404;
- the default project keeps the adapters configured by the harness.
"""

from __future__ import annotations

import base64
import json
import time
import uuid

BATCH = 2000


def _create_project(ctx, name, project_type):
    response = ctx.rest.post(
        "/api/v1/projects", json={"name": name, "type": project_type}
    )
    ctx.require(response.status_code == 201, response.text[:300])
    return response.json()


def _set_adapters(ctx, project_id, adapters, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else None
    return ctx.rest.put(
        f"/api/v1/projects/{project_id}/adapters",
        headers=headers,
        json={"adapters": adapters},
    )


def _upload(ctx, project_id, category, marker):
    response = ctx.rest.post(
        "/api/v1/source/upload",
        params={"project": project_id},
        json={
            "category": category,
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


def _build(ctx, project_id, build_type="full"):
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


def _search_refs(ctx, project_id, marker):
    session = ctx.open_mcp(ctx.token, project=project_id)
    try:
        outcome = _outcome(
            session,
            "search_knowledge",
            {"query": marker, "scope": "docs", "top_k": 10},
        )
    finally:
        session.close()
    ctx.require("ok" in outcome, f"项目 {project_id} 检索失败: {outcome}")
    return {str(row.get("ref", "")) for row in outcome["ok"]}


def _cleanup(ctx, project_ids):
    import sqlite3

    if not project_ids:
        return {}
    placeholders = ",".join("?" for _ in project_ids)
    with sqlite3.connect(ctx.runtime.db_path) as db:
        builds = db.execute(
            "SELECT generation FROM builds "
            f"WHERE project_id IN ({placeholders}) AND generation IS NOT NULL",
            project_ids,
        ).fetchall()
        pointer = db.execute(
            "SELECT generation FROM generation_pointer "
            f"WHERE project_id IN ({placeholders})",
            project_ids,
        ).fetchall()
    generations = sorted({row[0] for row in builds} | {row[0] for row in pointer})
    result = {"generations": generations}
    if not generations:
        return result
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
            result["milvus_remaining"] = sorted(
                {"sda_" + generation for generation in generations}
                & set(client.list_collections())
            )
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001
        result["milvus_error"] = f"{type(exc).__name__}: {exc}"
    return result


def check_issue_187_project_adapters(ctx):
    suffix = uuid.uuid4().hex[:8]
    marker_a = f"issue187alpha{suffix}"
    marker_b = f"issue187beta{suffix}"
    marker_a2 = f"issue187alpha2{suffix}"
    category_a = f"issue187a{suffix}"
    category_b = f"issue187b{suffix}"
    category_a2 = f"issue187a2{suffix}"
    created = []
    created_tokens = []
    secret = f"issue187-secret-{suffix}"
    try:
        default = ctx.rest.get("/api/v1/projects/default/adapters")
        ctx.require(default.status_code == 200, default.text[:300])
        default_rows = {row["id"]: row for row in default.json()}
        ctx.require(default_rows["markdown"]["enabled"] is True, default_rows)
        ctx.require(default_rows["canonical"]["enabled"] is True, default_rows)

        project_a = _create_project(ctx, f"Issue 187 Alpha {suffix}", "document")
        project_b = _create_project(ctx, f"Issue 187 Beta {suffix}", "document")
        document = _create_project(ctx, f"Issue 187 Docs {suffix}", "document")
        warehouse = _create_project(ctx, f"Issue 187 DTB {suffix}", "warehouse")
        created = [
            project_a["id"],
            project_b["id"],
            document["id"],
            warehouse["id"],
        ]

        for project, path in (
            (project_a, "knowledge-alpha"),
            (project_b, "knowledge-beta"),
        ):
            response = _set_adapters(
                ctx,
                project["id"],
                [
                    {
                        "id": "markdown",
                        "enabled": True,
                        "parameters": {"path": path},
                    }
                ],
            )
            ctx.require(response.status_code == 200, response.text[:300])

        _upload(ctx, project_a["id"], category_a, marker_a)
        _upload(ctx, project_b["id"], category_b, marker_b)
        build_a = _build(ctx, project_a["id"])
        build_b = _build(ctx, project_b["id"])

        refs_a = _search_refs(ctx, project_a["id"], marker_a)
        refs_b = _search_refs(ctx, project_b["id"], marker_b)
        ctx.require(
            any(f"{category_a}/probe.md" in ref for ref in refs_a),
            f"项目 A 未命中自己的文档: {refs_a}",
        )
        ctx.require(
            not any(marker_b in ref for ref in refs_a) and refs_a,
            f"项目 A 看到了项目 B 的内容: {refs_a}",
        )
        ctx.require(
            any(f"{category_b}/probe.md" in ref for ref in refs_b),
            f"项目 B 未命中自己的文档: {refs_b}",
        )

        no_effects = ctx.rest.get(f"/api/v1/projects/{document['id']}/adapters")
        ctx.require(no_effects.status_code == 200, no_effects.text[:300])
        document_rows = {row["id"]: row for row in no_effects.json()}
        ctx.require(
            document_rows["dtb"]["compatible"] is False
            and document_rows["dtb"]["reason"],
            document_rows["dtb"],
        )
        rejected = _set_adapters(
            ctx,
            document["id"],
            [{"id": "dtb", "enabled": True, "parameters": {}}],
        )
        ctx.require(rejected.status_code == 422, rejected.text[:300])
        ctx.require("数仓" in rejected.text, rejected.text[:300])
        rejected_pull = _set_adapters(
            ctx,
            document["id"],
            [
                {
                    "id": "dtb",
                    "enabled": False,
                    "parameters": {},
                    "pull": {
                        "enabled": True,
                        "parameters": {"url": "https://dtb.example"},
                    },
                }
            ],
        )
        ctx.require(rejected_pull.status_code == 422, rejected_pull.text[:300])

        pull = _set_adapters(
            ctx,
            warehouse["id"],
            [
                {
                    "id": "dtb",
                    "enabled": True,
                    "parameters": {"snapshot_path": "dtb"},
                    "pull": {
                        "enabled": True,
                        "parameters": {
                            "url": "https://dtb.example",
                            "interval_seconds": 900,
                            "layer": "dimensions",
                            "token": secret,
                        },
                    },
                }
            ],
        )
        ctx.require(pull.status_code == 200, pull.text[:300])
        listed = ctx.rest.get(f"/api/v1/projects/{warehouse['id']}/adapters")
        ctx.require(listed.status_code == 200, listed.text[:200])
        ctx.require(secret not in listed.text, "适配器接口回显了 DTB 凭据明文")
        dtb = {row["id"]: row for row in listed.json()}["dtb"]
        ctx.require(dtb["enabled"] is True, dtb)
        ctx.require(dtb["pull"]["enabled"] is True, dtb)
        ctx.require(dtb["pull"]["token_configured"] is True, dtb)
        with open(ctx.runtime.db_path, "rb") as handle:
            ctx.require(secret.encode() not in handle.read(), "凭据以明文写入了数据库")

        member_token = ctx.rest.post(
            "/api/v1/tokens",
            json={"name": f"issue187 member {suffix}", "role": "user"},
        )
        ctx.require(member_token.status_code == 201, member_token.text[:300])
        member = member_token.json()
        created_tokens.append(member["id"])
        admin_token = ctx.rest.post(
            "/api/v1/tokens",
            json={"name": f"issue187 admin {suffix}", "role": "user"},
        )
        ctx.require(admin_token.status_code == 201, admin_token.text[:300])
        project_admin = admin_token.json()
        created_tokens.append(project_admin["id"])
        stranger_token = ctx.rest.post(
            "/api/v1/tokens",
            json={"name": f"issue187 stranger {suffix}", "role": "user"},
        )
        ctx.require(stranger_token.status_code == 201, stranger_token.text[:300])
        stranger = stranger_token.json()
        created_tokens.append(stranger["id"])
        for token_id, role in (
            (member["id"], "member"),
            (project_admin["id"], "admin"),
        ):
            response = ctx.rest.put(
                f"/api/v1/projects/{project_a['id']}/members/{token_id}",
                json={"role": role},
            )
            ctx.require(response.status_code == 200, response.text[:300])

        member_headers = {"Authorization": f"Bearer {member['token']}"}
        readable = ctx.rest.get(
            f"/api/v1/projects/{project_a['id']}/adapters", headers=member_headers
        )
        ctx.require(readable.status_code == 200, readable.text[:300])
        denied = ctx.rest.put(
            f"/api/v1/projects/{project_a['id']}/adapters",
            headers=member_headers,
            json={"adapters": [{"id": "markdown", "enabled": True, "parameters": {}}]},
        )
        ctx.require(denied.status_code == 403, denied.text[:300])
        stranger_headers = {"Authorization": f"Bearer {stranger['token']}"}
        missing = ctx.rest.get(
            f"/api/v1/projects/{project_a['id']}/adapters", headers=stranger_headers
        )
        ctx.require(missing.status_code == 404, missing.text[:300])
        project_admin_headers = {"Authorization": f"Bearer {project_admin['token']}"}
        allowed = ctx.rest.put(
            f"/api/v1/projects/{project_a['id']}/adapters",
            headers=project_admin_headers,
            json={
                "adapters": [
                    {
                        "id": "markdown",
                        "enabled": True,
                        "parameters": {"path": "knowledge-alpha"},
                    }
                ]
            },
        )
        ctx.require(allowed.status_code == 200, allowed.text[:300])

        change = _set_adapters(
            ctx,
            project_a["id"],
            [
                {
                    "id": "markdown",
                    "enabled": True,
                    "parameters": {"path": "knowledge-alpha-2"},
                }
            ],
        )
        ctx.require(change.status_code == 200, change.text[:300])
        _upload(ctx, project_a["id"], category_a2, marker_a2)
        before = _search_refs(ctx, project_a["id"], marker_a)
        ctx.require(
            any(f"{category_a}/probe.md" in ref for ref in before),
            f"配置修改在重建前就影响了检索: {before}",
        )
        stale = _search_refs(ctx, project_a["id"], marker_a2)
        ctx.require(
            not any(f"{category_a2}/probe.md" in ref for ref in stale),
            f"未重建的新来源已经可检索: {stale}",
        )
        rebuild_a = _build(ctx, project_a["id"])
        after = _search_refs(ctx, project_a["id"], marker_a2)
        ctx.require(
            any(f"{category_a2}/probe.md" in ref for ref in after),
            f"新配置没有在下一次构建生效: {after}",
        )
        rebuild_b = _build(ctx, project_b["id"])
        refs_b_after = _search_refs(ctx, project_b["id"], marker_b)
        ctx.require(
            any(f"{category_b}/probe.md" in ref for ref in refs_b_after),
            f"修改 A 后 B 的构建结果发生了变化: {refs_b_after}",
        )

        return {
            "projects": {
                "a": project_a["id"],
                "b": project_b["id"],
                "document": document["id"],
                "warehouse": warehouse["id"],
            },
            "builds": {
                "a": build_a["id"],
                "b": build_b["id"],
                "a_after_change": rebuild_a["id"],
                "b_after_change": rebuild_b["id"],
            },
            "markers": {
                "a": marker_a,
                "b": marker_b,
                "a_next": marker_a2,
            },
            "permissions": {"member_read": 200, "member_write": 403, "stranger": 404},
        }
    finally:
        for token in created_tokens:
            try:
                ctx.rest.delete(f"/api/v1/tokens/{token}")
            except Exception as exc:  # noqa: BLE001 - cleanup is best effort
                ctx.logger.line(f"issue187 token cleanup failed: {exc}")
        cleanup = _cleanup(ctx, created)
        ctx.logger.line(f"issue187 cleanup: {cleanup}")
        if cleanup.get("generations"):
            ctx.runtime.storage_touched = True
