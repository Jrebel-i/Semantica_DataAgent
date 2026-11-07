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
