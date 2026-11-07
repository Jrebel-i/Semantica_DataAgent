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
