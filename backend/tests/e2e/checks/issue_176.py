"""End-to-end checks for per-project builds, schedules and corrections (#176).

The harness has already built the default project.  This check creates two
document projects, schedules them independently, builds both, approves a
correction in one project, and verifies the other project's source and build
queue are untouched.  It also checks that MCP knowledge gaps still land in the
default project and that project filtering keeps the lists separate.
"""

from __future__ import annotations

import base64
import sqlite3
import time
import uuid

BATCH = 2000


def _wait_build(ctx, project_id, build_id):
    deadline = time.monotonic() + ctx.config.build_timeout
    while time.monotonic() < deadline:
        response = ctx.rest.get(
            f"/api/v1/builds/{build_id}", params={"project": project_id}
        )
        ctx.require(
            response.status_code == 200,
            f"构建 {build_id} HTTP {response.status_code}: {response.text[:200]}",
        )
        row = response.json()
        if row.get("status") in {"succeeded", "failed", "cancelled"}:
            return row
        time.sleep(ctx.config.poll_interval)
    raise AssertionError(f"项目 {project_id} 的构建 {build_id} 超时")


def _wait_incremental(ctx, project_id):
    deadline = time.monotonic() + ctx.config.build_timeout
    candidate = None
    while time.monotonic() < deadline:
        rows = ctx.rest.get("/api/v1/builds", params={"project": project_id}).json()
        incremental = [row for row in rows if row.get("build_type") == "incremental"]
        if incremental:
            candidate = max(incremental, key=lambda row: row["id"])
            if candidate["status"] in {"succeeded", "failed", "cancelled"}:
                return candidate
        time.sleep(ctx.config.poll_interval)
    raise AssertionError(f"项目 {project_id} 没有完成的增量构建: {candidate}")


def _upload(ctx, project_id, target, name, content):
    response = ctx.rest.post(
        "/api/v1/source/upload",
        params={"project": project_id},
        json={
            "target": target,
            "files": [
                {
                    "path": name,
                    "content_base64": base64.b64encode(content.encode()).decode(),
                }
            ],
        },
    )
    ctx.require(response.status_code == 200, response.text[:300])


def _generations(ctx, project_ids):
    names = set()
    with sqlite3.connect(ctx.runtime.db_path) as db:
        for project_id in project_ids:
            names.update(
                row[0]
                for row in db.execute(
                    "SELECT generation FROM builds "
                    "WHERE project_id = ? AND generation IS NOT NULL",
                    (project_id,),
                )
            )
            names.update(
                row[0]
                for row in db.execute(
                    "SELECT generation FROM generation_pointer WHERE project_id = ?",
                    (project_id,),
                )
            )
    return sorted(names)


def _cleanup(ctx, project_ids):
    generations = _generations(ctx, project_ids)
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
    except Exception as exc:  # noqa: BLE001 - harness reports cleanup leftovers
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


def check_project_schedules_builds_and_corrections(ctx):
    suffix = uuid.uuid4().hex[:8]
    projects = []
    try:
        for letter in ("a", "b"):
            response = ctx.rest.post(
                "/api/v1/projects",
                json={"name": f"Issue 176 {letter} {suffix}", "type": "document"},
            )
            ctx.require(response.status_code == 201, response.text[:300])
            projects.append(response.json())
        project_a, project_b = projects

        schedule = ctx.rest.get(f"/api/v1/projects/{project_a['id']}/schedule")
        ctx.require(
            schedule.status_code == 200 and schedule.json()["interval_seconds"] == 0,
            f"新项目调度默认值异常: {schedule.text[:200]}",
        )
        saved = ctx.rest.put(
            f"/api/v1/projects/{project_a['id']}/schedule",
            json={"interval_seconds": 7},
        )
        ctx.require(
            saved.status_code == 200 and saved.json()["interval_seconds"] == 7,
            saved.text[:300],
        )
        ctx.require(
            ctx.rest.get(f"/api/v1/projects/{project_a['id']}/schedule").json()[
                "interval_seconds"
            ]
            == 7,
            "项目调度没有持久化",
        )
        invalid = ctx.rest.put(
            f"/api/v1/projects/{project_a['id']}/schedule",
            json={"interval_seconds": 604801},
        )
        ctx.require(invalid.status_code == 422, invalid.text[:200])
        disabled = ctx.rest.put(
            f"/api/v1/projects/{project_a['id']}/schedule",
            json={"interval_seconds": 0},
        )
        ctx.require(
            disabled.status_code == 200 and disabled.json()["interval_seconds"] == 0,
            disabled.text[:200],
        )

        marker_a = f"issue176a{suffix}"
        marker_b = f"issue176b{suffix}"
        _upload(
            ctx,
            project_a["id"],
            f"knowledge/issue176a{suffix}",
            "probe.md",
            f"# Probe A\n\n{marker_a} before\n",
        )
        _upload(
            ctx,
            project_b["id"],
            f"knowledge/issue176b{suffix}",
            "probe.md",
            f"# Probe B\n\n{marker_b} before\n",
        )

        builds = {}
        for project in projects:
            started = ctx.rest.post(
                "/api/v1/builds",
                params={"project": project["id"]},
                json={"build_type": "full"},
            )
            ctx.require(started.status_code == 202, started.text[:300])
            builds[project["id"]] = started.json()["id"]
        for project in projects:
            build = _wait_build(ctx, project["id"], builds[project["id"]])
            ctx.require(
                build["status"] == "succeeded",
                f"项目构建失败: {build.get('status')} {build.get('error')}",
            )
        rows_a = ctx.rest.get(
            "/api/v1/builds", params={"project": project_a["id"]}
        ).json()
        rows_b = ctx.rest.get(
            "/api/v1/builds", params={"project": project_b["id"]}
        ).json()
        ctx.require(
            {row["id"] for row in rows_a} == {builds[project_a["id"]]},
            f"项目 A 构建列表混入其它项目: {rows_a}",
        )
        ctx.require(
            {row["id"] for row in rows_b} == {builds[project_b["id"]]},
            f"项目 B 构建列表混入其它项目: {rows_b}",
        )

        target_a = f"document:issue176a{suffix}/probe.md"
        target_b = f"document:issue176b{suffix}/probe.md"
        proposal = ctx.rest.post(
            "/api/v1/corrections",
            params={"project": project_a["id"]},
            json={
                "target": target_a,
                "patch": f"# Probe A\n\n{marker_a} after\n",
                "reason": "issue 176 project approval",
            },
        )
        ctx.require(proposal.status_code == 201, proposal.text[:300])
        proposal_id = proposal.json()["proposal_id"]
        ctx.require(
            [
                row["id"]
                for row in ctx.rest.get(
                    "/api/v1/corrections", params={"project": project_a["id"]}
                ).json()
            ]
            == [proposal_id],
            "项目 A 看不到自己的纠错提案",
        )
        ctx.require(
            ctx.rest.get(
                "/api/v1/corrections", params={"project": project_b["id"]}
            ).json()
            == [],
            "项目 B 看到了项目 A 的纠错提案",
        )
        ctx.require(
            ctx.rest.get(
                f"/api/v1/corrections/{proposal_id}",
                params={"project": project_b["id"]},
            ).status_code
            == 404,
            "跨项目读取纠错提案没有返回 404",
        )
        approved = ctx.rest.post(
            f"/api/v1/corrections/{proposal_id}/approve",
            params={"project": project_a["id"]},
        )
        ctx.require(approved.status_code == 200, approved.text[:300])
        ctx.require(approved.json()["status"] == "approved", approved.text[:300])

        incremental = _wait_incremental(ctx, project_a["id"])
        ctx.require(
            incremental["status"] == "succeeded",
            f"纠错后的增量构建失败: {incremental.get('error')}",
        )
        source_a = ctx.rest.get(
            "/api/v1/source/file",
            params={
                "path": f"knowledge/issue176a{suffix}/probe.md",
                "project": project_a["id"],
            },
        )
        ctx.require(source_a.status_code == 200, source_a.text[:200])
        ctx.require(
            f"{marker_a} after" in source_a.json()["content"],
            f"项目 A 源文件没有写回: {source_a.text[:300]}",
        )
        source_b = ctx.rest.get(
            "/api/v1/source/file",
            params={
                "path": f"knowledge/issue176b{suffix}/probe.md",
                "project": project_b["id"],
            },
        )
        ctx.require(source_b.status_code == 200, source_b.text[:200])
        ctx.require(
            f"{marker_b} before" in source_b.json()["content"],
            f"项目 B 源文件被误写: {source_b.text[:300]}",
        )
        ctx.require(
            all(
                row["status"] != "queued" and row["build_type"] != "incremental"
                for row in ctx.rest.get(
                    "/api/v1/builds", params={"project": project_b["id"]}
                ).json()
            ),
            "项目 A 的纠错审批给项目 B 入队了构建",
        )

        question = f"issue176 gap {suffix}"
        ctx.mcp_call(
            "report_knowledge_gap",
            {"question": question, "missing": "project scoped gap"},
        )
        default_gaps = ctx.rest.get("/api/v1/knowledge-gaps").json()
        ctx.require(
            any(row["question"] == question for row in default_gaps),
            "MCP 知识缺口没有落到默认项目",
        )
        ctx.require(
            all(
                row["question"] != question
                for row in ctx.rest.get(
                    "/api/v1/knowledge-gaps", params={"project": project_a["id"]}
                ).json()
            ),
            "默认项目的知识缺口出现在项目 A",
        )

        return {
            "projects": [project["id"] for project in projects],
            "targets": [target_a, target_b],
            "incremental_build": incremental["id"],
        }
    finally:
        cleanup = _cleanup(ctx, [project["id"] for project in projects])
        ctx.logger.line(f"issue176 cleanup: {cleanup}")
