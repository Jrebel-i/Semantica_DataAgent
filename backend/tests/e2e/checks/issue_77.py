"""Check field lineage and incremental replacement using isolated test storage."""

import json
import time

from neo4j import GraphDatabase


def check_column_lineage_and_incremental_rebuild(ctx):
    driver = GraphDatabase.driver(
        ctx.config.neo4j_uri,
        auth=(ctx.config.neo4j_user, ctx.runtime.password),
    )

    def edges():
        with driver.session() as session:
            rows = session.run(
                """MATCH (a:SDAField {generation:$generation})-[r:FIELD_DEPENDS_ON]->
                (b:SDAField {generation:$generation})
                RETURN a.database AS source_db, a.name AS source, b.name AS target,
                       r.task_id AS task, r.evidence AS evidence, r.parse_status AS status""",
                generation=ctx.generation,
            )
            return {
                (
                    r["source_db"],
                    r["source"],
                    r["target"],
                    r["task"],
                    r["evidence"],
                    r["status"],
                )
                for r in rows
            }

    def incremental():
        response = ctx.rest.post("/api/v1/builds", json={"build_type": "incremental"})
        response.raise_for_status()
        build_id = response.json()["id"]
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            response = ctx.rest.get(f"/api/v1/builds/{build_id}")
            response.raise_for_status()
            build = response.json()
            if build["status"] in ("succeeded", "failed", "cancelled"):
                ctx.require(build["status"] == "succeeded", f"增量构建失败：{build}")
                return build
            time.sleep(1)
        raise AssertionError("增量构建超时")

    try:
        expected = {
            ("src", "id", "order_id", "load", "parsed", "success"),
            ("src", "amount", "total", "load", "parsed", "success"),
        }
        initial = edges()
        ctx.require(initial == expected, f"字段来源错误：{initial}")
        failures = ctx.build.get("parse_failures", [])
        ctx.require(
            any(x["task_id"] == "broken" and x["reason"] for x in failures),
            f"无法解析的任务没有原因：{failures}",
        )
        path = ctx.source_dir / "tasks.json"
        tasks = json.loads(path.read_text())
        tasks[0]["sql"] = (
            "INSERT INTO dst.orders SELECT amount AS order_id, id AS total FROM src.orders"
        )
        path.write_text(json.dumps(tasks))
        incremental()
        changed = edges()
        ctx.require(
            changed
            == {
                ("src", "amount", "order_id", "load", "parsed", "success"),
                ("src", "id", "total", "load", "parsed", "success"),
            },
            f"增量构建没有替换旧字段边：{changed}",
        )
        tasks = [task for task in tasks if task["name"] != "load"]
        path.write_text(json.dumps(tasks))
        incremental()
        remaining = edges()
        ctx.require(not remaining, f"删除任务后仍有字段边：{remaining}")
        return {
            "full": sorted(initial),
            "incremental": sorted(changed),
            "deleted": len(remaining),
        }
    finally:
        driver.close()
