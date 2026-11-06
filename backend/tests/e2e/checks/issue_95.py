"""Check shell mysql lineage through an incremental build."""

import json
import time

from neo4j import GraphDatabase


def check_mysql_shell_lineage(ctx):
    path = ctx.source_dir / "tasks.json"
    tasks = json.loads(path.read_text())
    tasks[0].update(
        type="shell",
        sql='source_db=src\nmysql -h example -e "INSERT INTO dst.orders '
        'SELECT id AS order_id, amount AS total FROM ${source_db}.orders"',
    )
    path.write_text(json.dumps(tasks))
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
            break
        time.sleep(1)
    else:
        raise AssertionError("增量构建超时")
    driver = GraphDatabase.driver(
        ctx.config.neo4j_uri, auth=(ctx.config.neo4j_user, ctx.runtime.password)
    )
    try:
        with driver.session() as session:
            rows = session.run(
                """MATCH (a:SDAField {generation:$generation})-[r:FIELD_DEPENDS_ON]->
                (b:SDAField {generation:$generation})
                WHERE r.task_id = 'load'
                RETURN a.name AS source, b.name AS target""",
                generation=ctx.generation,
            )
            edges = {(r["source"], r["target"]) for r in rows}
        ctx.require(edges == {("id", "order_id"), ("amount", "total")}, f"字段边错误：{edges}")
        return {"edges": sorted(edges)}
    finally:
        driver.close()
