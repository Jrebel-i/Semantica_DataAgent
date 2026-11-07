"""Check graph lookup plans used by the actual field lineage stage."""

from neo4j import GraphDatabase

from semantica_dataagent.task_stage import write_column_lineage


def check_field_lineage_uses_full_location_index(ctx):
    class Capture:
        build_id = 1
        query = ""

        def execute(self, query, parameters):
            if "UNWIND $rows AS row" in query:
                self.query = query
            if "RETURN count(*) AS deleted" in query:
                return [{"deleted": 0}]
            return []

    capture = Capture()
    write_column_lineage([], [], {}, {}, ctx.generation, capture)
    driver = GraphDatabase.driver(
        ctx.config.neo4j_uri, auth=(ctx.config.neo4j_user, ctx.runtime.password)
    )
    try:
        with driver.session() as session:
            plan = (
                session.run(
                    "EXPLAIN " + capture.query,
                    generation=ctx.generation,
                    rows=[
                        {
                            "upstream_table": "db.a",
                            "upstream_field": "x",
                            "downstream_table": "db.b",
                            "downstream_field": "y",
                        }
                    ],
                )
                .consume()
                .plan
            )

        def nodes(node):
            return [
                node,
                *(item for child in node.get("children", []) for item in nodes(child)),
            ]

        seeks = [n for n in nodes(plan) if "IndexSeek" in n["operatorType"]]
        details = [str(n.get("args", {})) for n in seeks]
        ctx.require(
            len(
                [d for d in details if "database" in d and "table" in d and "name" in d]
            )
            >= 2,
            f"字段上游及下游应使用完整位置索引：{details}",
        )
        return {"index_seeks": details}
    finally:
        driver.close()


def check_table_lineage_uses_location_indexes(ctx):
    from semantica_dataagent import relationships, task_stage

    class Capture:
        build_id = 1
        all_records = None
        is_incremental = False

        def __init__(self):
            self.queries = []

        def execute(self, query, parameters):
            self.queries.append(query)
            if "RETURN count(*) AS deleted" in query:
                return [{"deleted": 0}]
            return []

        def add_vectors(self, *args):
            pass

    capture = Capture()
    task_stage.write([], ctx.generation, capture)
    relationships.reconcile(capture, [], ctx.generation)
    queries = [
        q
        for q in capture.queries
        if "UNWIND $rows AS row" in q
        and (
            "MATCH (table:SDATable" in q
            or "MATCH (a:SDATable" in q
            or "MATCH (up:SDATable" in q
        )
    ]
    ctx.require(len(queries) == 4, f"无法找到全部表来源查询：{len(queries)}")
    driver = GraphDatabase.driver(
        ctx.config.neo4j_uri, auth=(ctx.config.neo4j_user, ctx.runtime.password)
    )
    try:
        plans = []
        with driver.session() as session:
            for query in queries:
                plan = (
                    session.run(
                        "EXPLAIN " + query,
                        generation=ctx.generation,
                        rows=[],
                        build_id=1,
                    )
                    .consume()
                    .plan
                )

                def nodes(node):
                    return [
                        node,
                        *(
                            n
                            for child in node.get("children", [])
                            for n in nodes(child)
                        ),
                    ]

                details = [
                    str(n.get("args", {}))
                    for n in nodes(plan)
                    if "IndexSeek" in n["operatorType"]
                ]
                table_seeks = [
                    d
                    for d in details
                    if "SDATable" in d and "database" in d and "name" in d
                ]
                expected = 1 if "MATCH (table:SDATable" in query else 2
                ctx.require(
                    len(table_seeks) >= expected, f"表来源未使用完整位置索引：{details}"
                )
                plans.append(table_seeks)
        return {"table_index_seeks": plans}
    finally:
        driver.close()


def check_qualified_names_with_dots(ctx):
    from semantica_dataagent.canonical import Record

    generation = ctx.generation + "_dots"
    driver = GraphDatabase.driver(
        ctx.config.neo4j_uri, auth=(ctx.config.neo4j_user, ctx.runtime.password)
    )
    try:
        with driver.session() as session:

            class Storage:
                build_id = 1

                def execute(self, query, parameters):
                    return [dict(row) for row in session.run(query, parameters)]

            try:
                session.run(
                    """UNWIND $rows AS row CREATE (f:SDAEntity:SDAField)
                    SET f = row, f.generation = $generation""",
                    generation=generation,
                    rows=[
                        {"database": db, "table": table, "name": name}
                        for db, table in [("a", "b.c"), ("a.b", "c")]
                        for name in ("x", "y")
                    ],
                ).consume()
                record = Record(
                    "column_lineage",
                    {
                        "upstream_table": "a.b.c",
                        "upstream_field": "x",
                        "downstream_table": "a.b.c",
                        "downstream_field": "y",
                        "task_id": "dot",
                        "evidence": "declared",
                        "parse_status": "success",
                    },
                    "synthetic",
                )
                write_column_lineage(
                    [record], [], {}, {"a.b.c": ["x", "y"]}, generation, Storage()
                )
                rows = session.run(
                    """MATCH (a:SDAField {generation:$g})-[:FIELD_DEPENDS_ON]->(b:SDAField {generation:$g})
                    RETURN a.database AS ad, a.table AS at, b.database AS bd, b.table AS bt""",
                    g=generation,
                )
                actual = {(r["ad"], r["at"], r["bd"], r["bt"]) for r in rows}
                locations = [("a", "b.c"), ("a.b", "c")]
                expected = {(a, b, c, d) for a, b in locations for c, d in locations}
                ctx.require(actual == expected, f"带点的限定名关系不完整：{actual}")
                return {"relationships": len(actual)}
            finally:
                session.run(
                    "MATCH (n:SDAEntity {generation:$g}) DETACH DELETE n", g=generation
                ).consume()
    finally:
        driver.close()


def check_incremental_build_with_existing_nodes(ctx):
    import time

    driver = GraphDatabase.driver(
        ctx.config.neo4j_uri, auth=(ctx.config.neo4j_user, ctx.runtime.password)
    )
    try:

        def count():
            with driver.session() as session:
                return session.run(
                    "MATCH (n:SDAEntity {generation:$g}) RETURN count(n) AS total",
                    g=ctx.generation,
                ).single()["total"]

        before = count()
        ctx.require(before > 0, "构建前没有可验证的既有节点")
        response = ctx.rest.post("/api/v1/builds", json={"build_type": "incremental"})
        response.raise_for_status()
        build_id = response.json()["id"]
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            response = ctx.rest.get(f"/api/v1/builds/{build_id}")
            response.raise_for_status()
            build = response.json()
            if build["status"] in ("succeeded", "failed", "cancelled"):
                ctx.require(
                    build["status"] == "succeeded", f"已有节点上的增量构建失败：{build}"
                )
                after = count()
                ctx.require(
                    after == before, f"重复构建改变了节点数：{before} -> {after}"
                )
                return {"nodes": after, "build_id": build_id}
            time.sleep(1)
        raise AssertionError("已有节点上的增量构建超时")
    finally:
        driver.close()
