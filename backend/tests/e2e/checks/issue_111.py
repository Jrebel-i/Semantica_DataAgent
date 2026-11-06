"""Large DTB dimension build and observable field metadata checks.

Run this file with --generate DIRECTORY before invoking the e2e harness.
"""

import json
import sys
from pathlib import Path

TABLES = 120
FIELDS = 12
VALUES = 105


def generate(directory):
    directory.mkdir(parents=True, exist_ok=True)
    from openpyxl import Workbook

    snapshot = directory / "dtb"
    snapshot.mkdir(parents=True, exist_ok=True)
    (snapshot / "任务信息.json").write_text(
        json.dumps(
            {"columns": [{"name": "task_name"}, {"name": "task_src"}], "data": []}
        ),
        encoding="utf-8",
    )
    book = Workbook(write_only=True)
    sheet = book.create_sheet()
    sheet.append(["database_name", "table_name", "column_name", "column_type"])
    for table_index in range(TABLES):
        table = f"dim_{table_index:03}"
        table_dir = directory / "dimensions" / table
        (table_dir / "current").mkdir(parents=True, exist_ok=True)
        names = [f"field_{i:02}" for i in range(FIELDS)]
        (table_dir / "meta.json").write_text(
            json.dumps(
                {
                    "database": "issue111",
                    "table": table,
                    "columns": [{"name": name} for name in names],
                }
            ),
            encoding="utf-8",
        )
        for name in names:
            sheet.append(["issue111", table, name, "string"])
        with (table_dir / "current" / "rows.jsonl").open(
            "w", encoding="utf-8"
        ) as output:
            for i in range(VALUES):
                output.write(
                    json.dumps({name: f"value_{i:04}" for name in names}) + "\n"
                )
    book.save(snapshot / "表信息.xlsx")


def check_large_dimensions(ctx):
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        ctx.config.neo4j_uri, auth=(ctx.config.neo4j_user, ctx.runtime.password)
    )
    try:
        with driver.session() as session:
            count = session.run(
                "MATCH (v:SDADimensionValue {generation:$generation}) RETURN count(v) AS count",
                generation=ctx.generation,
            ).single()["count"]
            fields = list(
                session.run(
                    "MATCH (f:SDAField {generation:$generation, database:'issue111'}) RETURN f.dimension_count AS count, f.dimension_samples AS samples",
                    generation=ctx.generation,
                )
            )
        with driver.session() as session:
            linked = session.run(
                "MATCH (:SDAField {generation:$generation})-[r:HAS_VALUE]->(:SDADimensionValue {generation:$generation}) RETURN count(r) AS count",
                generation=ctx.generation,
            ).single()["count"]
        ctx.require(linked == TABLES * FIELDS * VALUES, f"维度值关系数量错误：{linked}")
        hits = ctx.mcp_call(
            "lookup_dim_values",
            {"table": "issue111.dim_119", "field": "field_11", "keyword": "value_0104"},
        )
        ctx.require(
            any(hit["value"] == "value_0104" for hit in hits),
            f"MCP 未检索到维度值：{hits}",
        )
        ctx.require(count == TABLES * FIELDS * VALUES, f"维度值数量错误：{count}")
        ctx.require(len(fields) == TABLES * FIELDS, f"字段数量错误：{len(fields)}")
        ctx.require(
            all(
                row["count"] == VALUES
                and row["samples"] == [f"value_{i:04}" for i in range(5)]
                for row in fields
            ),
            "字段维度计数或样例错误",
        )
        return {"dimension_values": count, "fields": len(fields)}
    finally:
        driver.close()


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--generate":
        raise SystemExit("usage: issue_111.py --generate DIRECTORY")
    generate(Path(sys.argv[2]))
