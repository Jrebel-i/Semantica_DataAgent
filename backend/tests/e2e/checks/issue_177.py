"""End-to-end permission matrix for project members and roles (#177).

The harness has already built the default project (project A).  This check
adds one distinguishable Markdown document to A, creates a second document
project (B) with its own document, builds both, then walks every REST endpoint
that accepts a ``project`` parameter through a table-driven isolation matrix.

Actors:
- system administrator (the harness token)
- pa: project admin of A
- ua: member of A
- ub: member of B
- uz: no project membership at all
- revoked: member of A whose token was revoked

Every allowed read must return the content of its own project.  Every denied
read must return the same 404 as a missing project.  Management endpoints are
admin-only for project admins and return 403/404 to ordinary members.
"""

from __future__ import annotations

import base64
import time
import uuid

BATCH = 2000
DENIED = {404}
MANAGE_DENIED = {403, 404}


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def _upload(ctx, project_id, category, marker, filename="probe.md"):
    response = ctx.rest.post(
        "/api/v1/source/upload",
        params={"project": project_id},
        json={
            "target": f"knowledge/{category}",
            "files": [
                {
                    "path": filename,
                    "content_base64": base64.b64encode(
                        f"# {category}\n\n{marker} belongs to {project_id}.\n".encode()
                    ).decode(),
                }
            ],
        },
    )
    ctx.require(
        response.status_code == 200, f"上传 {project_id} 失败: {response.text[:300]}"
    )
    return response.json()["commit"]


def _wait_build(ctx, project_id, build_id, timeout=None):
    deadline = time.monotonic() + (timeout or ctx.config.build_timeout)
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


def _build(ctx, project_id, build_type="incremental"):
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


def _request(ctx, method, path, *, project=None, params=None, json=None, token=None):
    query = dict(params or {})
    if project is not None:
        query["project"] = project
    headers = _headers(token) if token else None
    return ctx.rest.request(method, path, params=query, json=json, headers=headers)


def _search(ctx, query, *, project=None, token=None):
    return _request(
        ctx,
        "GET",
        "/api/v1/search",
        project=project,
        params={"query": query, "scope": "docs", "top_k": 10},
        token=token,
    )


def _generations(ctx, project, token=None):
    return _request(ctx, "GET", "/api/v1/generations", project=project, token=token)


def _current_generation(ctx, project, token=None):
    response = _generations(ctx, project, token)
    ctx.require(response.status_code == 200, response.text[:300])
    rows = response.json()["generations"]
    current = next((row for row in rows if row["current"]), None)
    ctx.require(current, f"项目 {project} 没有当前知识代: {rows}")
    return current["name"]


def _cleanup(ctx, project_id):
    """Delete this project's graph nodes and vector collections in batches."""
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


def check_issue_177_isolation_matrix(ctx):
    suffix = uuid.uuid4().hex[:8]
    marker_a = f"issue177markerA{suffix}"
    marker_b = f"issue177markerB{suffix}"
    category_a = f"issue177a{suffix}"
    category_b = f"issue177b{suffix}"
    project_b = None
    issued = []
    try:
        # Project A is the default project already built by the harness.
        _upload(ctx, "default", category_a, marker_a)
        build_a = _build(ctx, "default", "incremental")
        ref_a = f"document:{category_a}/probe.md"

        # Project B is a fresh document project with distinguishable content.
        created = ctx.rest.post(
            "/api/v1/projects",
            json={"name": f"Issue 177 Project B {suffix}", "type": "document"},
        )
        ctx.require(created.status_code == 201, created.text[:300])
        project_b = created.json()
        project_b_id = project_b["id"]
        _upload(ctx, project_b_id, category_b, marker_b)
        build_b = _build(ctx, project_b_id, "full")
        ref_b = f"document:{category_b}/probe.md"

        def issue(name, projects):
            response = ctx.rest.post(
                "/api/v1/tokens",
                json={"name": name, "role": "user", "projects": projects},
            )
            ctx.require(response.status_code == 201, response.text[:300])
            token = response.json()
            issued.append(token)
            return token

        pa = issue(
            f"issue177-pa-{suffix}", [{"project_id": "default", "role": "admin"}]
        )
        ua = issue(
            f"issue177-ua-{suffix}", [{"project_id": "default", "role": "member"}]
        )
        ub = issue(
            f"issue177-ub-{suffix}", [{"project_id": project_b_id, "role": "member"}]
        )
        uz = issue(f"issue177-uz-{suffix}", [])
        revoked = issue(
            f"issue177-revoked-{suffix}",
            [{"project_id": "default", "role": "member"}],
        )
        ctx.require(
            ctx.rest.delete(f"/api/v1/tokens/{revoked['id']}").status_code == 204,
            "吊销测试令牌失败",
        )

        admin_tokens = ctx.rest.get("/api/v1/tokens").json()
        admin_token = next(row for row in admin_tokens if row["role"] == "admin")
        ctx.require(
            next(row for row in admin_tokens if row["id"] == pa["id"])["projects"]
            == [{"project_id": "default", "role": "admin"}],
            "令牌列表没有返回项目与角色",
        )

        # GET /users exposes only id, name and role.
        users = _request(ctx, "GET", "/api/v1/users", token=pa["token"])
        ctx.require(users.status_code == 200, users.text[:300])
        ctx.require(
            all(set(row) == {"id", "name", "role"} for row in users.json()),
            f"/users 返回了额外字段: {users.json()}",
        )
        for actor in (ua, ub, uz):
            denied = _request(ctx, "GET", "/api/v1/users", token=actor["token"])
            ctx.require(
                denied.status_code in MANAGE_DENIED,
                f"{actor['name']} 不应访问 /users: {denied.status_code}",
            )
        ctx.require(
            _request(ctx, "GET", "/api/v1/users").status_code == 200,
            "系统管理员不能访问 /users",
        )

        # Table-driven read matrix.  Every row is (name, path, params, content).
        a_content = {
            "tables": lambda body: any(row.get("name") == "orders" for row in body),
            "table_detail": lambda body: body.get("name") == "orders",
            "search": lambda body: any(
                str(row.get("ref", "")).startswith(ref_a) for row in body
            ),
            "document": lambda body: marker_a in body.get("content", ""),
            "categories": lambda body: any(
                row.get("name") == category_a for row in body
            ),
            "category_index": lambda body: body.get("ref", "").startswith(
                f"document:{category_a}/"
            ),
            "column_lineage": lambda body: set(body) == {"upstream", "downstream"},
            "table_lineage": lambda body: (
                set(body) == {"upstream", "downstream", "tasks"}
            ),
            "generations": lambda body: any(
                row.get("current") for row in body["generations"]
            ),
            "builds": lambda body: any(row.get("id") == build_a["id"] for row in body),
            "build_detail": lambda body: body.get("id") == build_a["id"],
        }
        a_rows = [
            ("tables", "/api/v1/tables", {}, a_content["tables"]),
            (
                "table_detail",
                "/api/v1/tables/shop/orders",
                {},
                a_content["table_detail"],
            ),
            (
                "search",
                "/api/v1/search",
                {"query": marker_a, "scope": "docs"},
                a_content["search"],
            ),
            (
                "document",
                "/api/v1/document",
                {"doc_id": ref_a},
                a_content["document"],
            ),
            ("categories", "/api/v1/categories", {}, a_content["categories"]),
            (
                "category_index",
                "/api/v1/category-index",
                {"category": category_a},
                a_content["category_index"],
            ),
            (
                "column_lineage",
                "/api/v1/column-lineage",
                {"table": "shop.orders", "column": "id"},
                a_content["column_lineage"],
            ),
            (
                "table_lineage",
                "/api/v1/table-lineage",
                {"table": "shop.orders"},
                a_content["table_lineage"],
            ),
            ("generations", "/api/v1/generations", {}, a_content["generations"]),
            ("builds", "/api/v1/builds", {}, a_content["builds"]),
            (
                "build_detail",
                f"/api/v1/builds/{build_a['id']}",
                {},
                a_content["build_detail"],
            ),
        ]
        for name, path, params, validate in a_rows:
            for actor in (pa, ua):
                response = _request(
                    ctx,
                    "GET",
                    path,
                    project="default",
                    params=params,
                    token=actor["token"],
                )
                ctx.require(
                    response.status_code == 200,
                    f"{actor['name']} 读取 A 的 {name} 应 200，实际 {response.status_code}: "
                    f"{response.text[:200]}",
                )
                ctx.require(validate(response.json()), f"{name} 返回内容不属于 A")
            for actor in (ub, uz, revoked):
                response = _request(
                    ctx,
                    "GET",
                    path,
                    project="default",
                    params=params,
                    token=actor["token"],
                )
                expected = {401} if actor is revoked else DENIED
                ctx.require(
                    response.status_code in expected,
                    f"{actor['name']} 越权读取 A 的 {name} 应 404，实际 {response.status_code}",
                )
            admin_response = _request(
                ctx, "GET", path, project="default", params=params
            )
            ctx.require(
                admin_response.status_code == 200,
                f"系统管理员读取 A 的 {name} 失败: {admin_response.text[:200]}",
            )

        # The source endpoints are management-only.
        source_files = _request(
            ctx, "GET", "/api/v1/source/files", project="default", token=pa["token"]
        )
        ctx.require(source_files.status_code == 200, source_files.text[:300])
        a_files = source_files.json()
        a_source_path = next(
            path for path in a_files if path.endswith("probe.md") and category_a in path
        )
        for actor in (ua, ub, uz):
            response = _request(
                ctx,
                "GET",
                "/api/v1/source/files",
                project="default",
                token=actor["token"],
            )
            ctx.require(
                response.status_code in MANAGE_DENIED,
                f"{actor['name']} 越权读取 A 的源文件: {response.status_code}",
            )
        revoked_source = _request(
            ctx,
            "GET",
            "/api/v1/source/files",
            project="default",
            token=revoked["token"],
        )
        ctx.require(revoked_source.status_code == 401, revoked_source.status_code)
        for actor in (pa,):
            response = _request(
                ctx,
                "GET",
                "/api/v1/source/file",
                project="default",
                params={"path": a_source_path},
                token=actor["token"],
            )
            ctx.require(response.status_code == 200, response.text[:300])
            ctx.require(marker_a in response.json()["content"], "源文件内容不属于 A")
        for actor in (ua, ub, uz):
            response = _request(
                ctx,
                "GET",
                "/api/v1/source/file",
                project="default",
                params={"path": a_source_path},
                token=actor["token"],
            )
            ctx.require(
                response.status_code in MANAGE_DENIED,
                f"{actor['name']} 越权读取 A 的源文件内容: {response.status_code}",
            )
        revoked_source_file = _request(
            ctx,
            "GET",
            "/api/v1/source/file",
            project="default",
            params={"path": a_source_path},
            token=revoked["token"],
        )
        ctx.require(
            revoked_source_file.status_code == 401, revoked_source_file.status_code
        )
        for actor in (ua, ub, uz):
            response = _request(
                ctx,
                "GET",
                "/api/v1/source/files",
                project=project_b_id,
                token=actor["token"],
            )
            ctx.require(
                response.status_code in MANAGE_DENIED,
                f"{actor['name']} 越权读取 B 的源文件: {response.status_code}",
            )
        b_source = _request(ctx, "GET", "/api/v1/source/files", project=project_b_id)
        ctx.require(b_source.status_code == 200, b_source.text[:300])
        b_source_path = next(
            path for path in b_source.json() if path.endswith("probe.md")
        )
        b_file = _request(
            ctx,
            "GET",
            "/api/v1/source/file",
            project=project_b_id,
            params={"path": b_source_path},
        )
        ctx.require(b_file.status_code == 200, b_file.text[:300])
        ctx.require(marker_b in b_file.json()["content"], "源文件内容不属于 B")
        for actor in (pa, ua, uz):
            response = _request(
                ctx,
                "GET",
                "/api/v1/source/file",
                project=project_b_id,
                params={"path": b_source_path},
                token=actor["token"],
            )
            ctx.require(
                response.status_code in MANAGE_DENIED,
                f"{actor['name']} 越权读取 B 的源文件内容: {response.status_code}",
            )
        ub_source_file = _request(
            ctx,
            "GET",
            "/api/v1/source/file",
            project=project_b_id,
            params={"path": b_source_path},
            token=ub["token"],
        )
        ctx.require(
            ub_source_file.status_code in MANAGE_DENIED, ub_source_file.status_code
        )

        # B matrix: only ub and the system administrator may read.
        b_content = {
            "tables": lambda body: body == [],
            "search": lambda body: any(
                str(row.get("ref", "")).startswith(ref_b) for row in body
            ),
            "document": lambda body: marker_b in body.get("content", ""),
            "categories": lambda body: any(
                row.get("name") == category_b for row in body
            ),
            "category_index": lambda body: body.get("ref", "").startswith(
                f"document:{category_b}/"
            ),
            "column_lineage": lambda body: set(body) == {"upstream", "downstream"},
            "table_lineage": lambda body: (
                set(body) == {"upstream", "downstream", "tasks"}
            ),
            "generations": lambda body: all(
                not row["name"].startswith("default") for row in body["generations"]
            ),
            "builds": lambda body: any(row.get("id") == build_b["id"] for row in body),
            "build_detail": lambda body: body.get("id") == build_b["id"],
        }
        b_rows = [
            ("tables", "/api/v1/tables", {}, b_content["tables"]),
            (
                "search",
                "/api/v1/search",
                {"query": marker_b, "scope": "docs"},
                b_content["search"],
            ),
            ("document", "/api/v1/document", {"doc_id": ref_b}, b_content["document"]),
            ("categories", "/api/v1/categories", {}, b_content["categories"]),
            (
                "category_index",
                "/api/v1/category-index",
                {"category": category_b},
                b_content["category_index"],
            ),
            (
                "column_lineage",
                "/api/v1/column-lineage",
                {"table": "shop.orders", "column": "id"},
                b_content["column_lineage"],
            ),
            (
                "table_lineage",
                "/api/v1/table-lineage",
                {"table": "shop.orders"},
                b_content["table_lineage"],
            ),
            ("generations", "/api/v1/generations", {}, b_content["generations"]),
            ("builds", "/api/v1/builds", {}, b_content["builds"]),
            (
                "build_detail",
                f"/api/v1/builds/{build_b['id']}",
                {},
                b_content["build_detail"],
            ),
        ]
        for name, path, params, validate in b_rows:
            response = _request(
                ctx, "GET", path, project=project_b_id, params=params, token=ub["token"]
            )
            ctx.require(
                response.status_code == 200,
                f"ub 读取 B 的 {name} 应 200，实际 {response.status_code}: "
                f"{response.text[:200]}",
            )
            ctx.require(validate(response.json()), f"B 的 {name} 返回了错误内容")
            for actor in (pa, ua, uz):
                denied = _request(
                    ctx,
                    "GET",
                    path,
                    project=project_b_id,
                    params=params,
                    token=actor["token"],
                )
                ctx.require(
                    denied.status_code in DENIED,
                    f"{actor['name']} 越权读取 B 的 {name}: {denied.status_code}",
                )
            admin_response = _request(
                ctx, "GET", path, project=project_b_id, params=params
            )
            ctx.require(admin_response.status_code == 200, admin_response.text[:200])

        # Management endpoints: only the project admin or system admin.
        for actor in (ua, ub, uz):
            upload = _request(
                ctx,
                "POST",
                "/api/v1/source/upload",
                project="default",
                json={"target": f"knowledge/{category_a}", "files": []},
                token=actor["token"],
            )
            ctx.require(
                upload.status_code in MANAGE_DENIED,
                f"{actor['name']} 越权上传: {upload.status_code}",
            )
            build = _request(
                ctx,
                "POST",
                "/api/v1/builds",
                project="default",
                json={"build_type": "incremental"},
                token=actor["token"],
            )
            ctx.require(
                build.status_code in MANAGE_DENIED,
                f"{actor['name']} 越权构建: {build.status_code}",
            )
            members = _request(
                ctx,
                "GET",
                "/api/v1/projects/default/members",
                token=actor["token"],
            )
            ctx.require(
                members.status_code in MANAGE_DENIED,
                f"{actor['name']} 越权读取成员: {members.status_code}",
            )
        for actor in (ua, ub):
            switch = _request(
                ctx,
                "POST",
                f"/api/v1/generations/{build_a['id'] and _current_generation(ctx, 'default')}/switch",
                project="default",
                token=actor["token"],
            )
            ctx.require(
                switch.status_code in MANAGE_DENIED,
                f"{actor['name']} 越权切换知识代: {switch.status_code}",
            )
        for actor in (uz,):
            switch = _request(
                ctx,
                "POST",
                f"/api/v1/generations/{_current_generation(ctx, 'default')}/switch",
                project="default",
                token=actor["token"],
            )
            ctx.require(
                switch.status_code in MANAGE_DENIED,
                f"{actor['name']} 越权切换知识代: {switch.status_code}",
            )
        revoked_switch = _request(
            ctx,
            "POST",
            f"/api/v1/generations/{_current_generation(ctx, 'default')}/switch",
            project="default",
            token=revoked["token"],
        )
        ctx.require(revoked_switch.status_code == 401, revoked_switch.status_code)
        revoked_manage = [
            _request(
                ctx,
                "POST",
                "/api/v1/source/upload",
                project="default",
                json={"target": f"knowledge/{category_a}", "files": []},
                token=revoked["token"],
            ),
            _request(
                ctx,
                "POST",
                "/api/v1/builds",
                project="default",
                json={"build_type": "incremental"},
                token=revoked["token"],
            ),
            _request(
                ctx,
                "GET",
                "/api/v1/projects/default/members",
                token=revoked["token"],
            ),
        ]
        ctx.require(
            all(row.status_code == 401 for row in revoked_manage),
            [row.status_code for row in revoked_manage],
        )

        pa_upload = _request(
            ctx,
            "POST",
            "/api/v1/source/upload",
            project="default",
            json={
                "target": f"knowledge/{category_a}",
                "files": [
                    {
                        "path": "pa-probe.md",
                        "content_base64": base64.b64encode(
                            b"# pa\n\npa upload\n"
                        ).decode(),
                    }
                ],
            },
            token=pa["token"],
        )
        ctx.require(pa_upload.status_code == 200, pa_upload.text[:300])
        pa_build = _request(
            ctx,
            "POST",
            "/api/v1/builds",
            project="default",
            json={"build_type": "incremental"},
            token=pa["token"],
        )
        ctx.require(pa_build.status_code == 202, pa_build.text[:300])
        pa_build_detail = _wait_build(ctx, "default", pa_build.json()["id"])
        ctx.require(
            pa_build_detail["status"] == "succeeded", pa_build_detail.get("error")
        )
        current_a = _current_generation(ctx, "default")
        pa_switch = _request(
            ctx,
            "POST",
            f"/api/v1/generations/{current_a}/switch",
            project="default",
            token=pa["token"],
        )
        ctx.require(pa_switch.status_code == 200, pa_switch.text[:300])
        admin_b_build = _request(
            ctx,
            "POST",
            "/api/v1/builds",
            project=project_b_id,
            json={"build_type": "incremental"},
        )
        ctx.require(admin_b_build.status_code == 202, admin_b_build.text[:300])
        admin_b_detail = _wait_build(ctx, project_b_id, admin_b_build.json()["id"])
        ctx.require(
            admin_b_detail["status"] == "succeeded", admin_b_detail.get("error")
        )

        # Default project selection without an explicit project parameter.
        pa_default = _request(ctx, "GET", "/api/v1/tables", token=pa["token"])
        ctx.require(pa_default.status_code == 200, pa_default.text[:300])
        ctx.require(
            any(row.get("name") == "orders" for row in pa_default.json()),
            "pa 的缺省项目不是 A",
        )
        ua_default = _search(ctx, marker_a, token=ua["token"])
        ctx.require(ua_default.status_code == 200, ua_default.text[:300])
        ctx.require(
            any(str(row.get("ref", "")).startswith(ref_a) for row in ua_default.json()),
            "ua 的缺省项目不是 A",
        )
        ub_default = _search(ctx, marker_b, token=ub["token"])
        ctx.require(ub_default.status_code == 200, ub_default.text[:300])
        ctx.require(
            any(str(row.get("ref", "")).startswith(ref_b) for row in ub_default.json()),
            "只属于 B 的用户缺省项目不是 B",
        )
        uz_default = _request(ctx, "GET", "/api/v1/tables", token=uz["token"])
        ctx.require(
            uz_default.status_code == 422,
            f"没有任何项目的用户缺省项目应 422，实际 {uz_default.status_code}",
        )
        admin_default = _request(ctx, "GET", "/api/v1/tables")
        ctx.require(admin_default.status_code == 200, admin_default.text[:300])

        # Membership rules.
        members = _request(
            ctx, "GET", "/api/v1/projects/default/members", token=pa["token"]
        )
        ctx.require(members.status_code == 200, members.text[:300])
        ctx.require(
            {row["id"] for row in members.json()} >= {pa["id"], ua["id"]},
            members.json(),
        )
        # No projects: the membership is created by the project admin below.
        temp = issue(f"issue177-temp-{suffix}", [])
        added = _request(
            ctx,
            "PUT",
            f"/api/v1/projects/default/members/{temp['id']}",
            json={"role": "member"},
            token=pa["token"],
        )
        ctx.require(added.status_code == 200, added.text[:300])
        promoted = _request(
            ctx,
            "PUT",
            f"/api/v1/projects/default/members/{temp['id']}",
            json={"role": "admin"},
            token=pa["token"],
        )
        ctx.require(promoted.status_code == 200, promoted.text[:300])
        removed = _request(
            ctx,
            "DELETE",
            f"/api/v1/projects/default/members/{temp['id']}",
            token=pa["token"],
        )
        ctx.require(removed.status_code == 204, removed.text[:300])
        demote_last = _request(
            ctx,
            "PUT",
            f"/api/v1/projects/default/members/{pa['id']}",
            json={"role": "member"},
            token=pa["token"],
        )
        ctx.require(demote_last.status_code == 409, demote_last.text[:300])
        remove_last = _request(
            ctx,
            "DELETE",
            f"/api/v1/projects/default/members/{pa['id']}",
            token=pa["token"],
        )
        ctx.require(remove_last.status_code == 409, remove_last.text[:300])
        global_admin_member = _request(
            ctx,
            "PUT",
            f"/api/v1/projects/default/members/{admin_token['id']}",
            json={"role": "member"},
            token=pa["token"],
        )
        ctx.require(
            global_admin_member.status_code == 422, global_admin_member.text[:300]
        )

        audit = _request(
            ctx, "GET", "/api/v1/projects/default/audit", token=pa["token"]
        )
        ctx.require(audit.status_code == 200, audit.text[:300])
        actions = {row["action"] for row in audit.json()}
        ctx.require(
            {"member.add", "member.update", "member.remove"} <= actions, actions
        )
        audit_b = _request(
            ctx,
            "GET",
            f"/api/v1/projects/{project_b_id}/audit",
            token=ub["token"],
        )
        ctx.require(audit_b.status_code in MANAGE_DENIED, audit_b.text[:200])
        audit_b_admin = _request(ctx, "GET", f"/api/v1/projects/{project_b_id}/audit")
        ctx.require(audit_b_admin.status_code == 200, audit_b_admin.text[:300])
        ctx.require(
            "project.create" in {row["action"] for row in audit_b_admin.json()},
            audit_b_admin.json(),
        )

        # Revoked token is rejected even though its membership row remains.
        revoked_access = _request(
            ctx, "GET", "/api/v1/tables", project="default", token=revoked["token"]
        )
        ctx.require(revoked_access.status_code == 401, revoked_access.status_code)
        revoked_listed = next(
            row
            for row in ctx.rest.get("/api/v1/tokens").json()
            if row["id"] == revoked["id"]
        )
        ctx.require(
            revoked_listed["projects"] == [{"project_id": "default", "role": "member"}],
            f"吊销后成员行不应丢失: {revoked_listed}",
        )

        return {
            "project_a": "default",
            "project_b": project_b_id,
            "matrix_rows": len(a_rows) + len(b_rows),
            "actors": {
                "pa": pa["id"],
                "ua": ua["id"],
                "ub": ub["id"],
                "uz": uz["id"],
                "revoked": revoked["id"],
            },
            "builds": {"a": build_a["id"], "b": build_b["id"]},
        }
    finally:
        for token in issued:
            try:
                ctx.rest.delete(f"/api/v1/tokens/{token['id']}")
            except Exception as exc:  # noqa: BLE001 - cleanup is best effort
                ctx.logger.line(f"issue177 token cleanup failed: {exc}")
        if project_b is not None:
            cleanup = _cleanup(ctx, project_b["id"])
            ctx.logger.line(f"issue177 cleanup: {cleanup}")
            ctx.runtime.storage_touched = True
