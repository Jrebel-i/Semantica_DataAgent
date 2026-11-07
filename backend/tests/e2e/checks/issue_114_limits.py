"""Check that oversized dimension values never enter the entity ref index.

Run this file with --generate DIRECTORY before invoking the e2e harness.
"""

import json
import sys
from pathlib import Path

SHORT_VALUES = ["A", "B"]
LONG_VALUES = ["x" * 10_000, "y" * 200_000, "中" * 300]


def generate(directory):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "tables.json").write_text(
        json.dumps(
            [
                {
                    "database": "synthetic",
                    "name": "long_values",
                    "comment": "synthetic table",
                }
            ]
        ),
        encoding="utf-8",
    )
    (directory / "fields.json").write_text(
        json.dumps(
            [
                {
                    "database": "synthetic",
                    "table": "long_values",
                    "name": "short_code",
                    "type": "string",
                    "comment": "short values",
                    "partition": False,
                },
                {
                    "database": "synthetic",
                    "table": "long_values",
                    "name": "free_text",
                    "type": "string",
                    "comment": "long values",
                    "partition": False,
                },
            ]
        ),
        encoding="utf-8",
    )
    (directory / "dimension_values.json").write_text(
        json.dumps(
            [
                {
                    "database": "synthetic",
                    "table": "long_values",
                    "field": "short_code",
                    "value": value,
                }
                for value in SHORT_VALUES
            ]
            + [
                {
                    "database": "synthetic",
                    "table": "long_values",
                    "field": "free_text",
                    "value": value,
                }
                for value in LONG_VALUES
            ]
        ),
        encoding="utf-8",
    )


def check_oversized_dimension_values(ctx):
    from neo4j import GraphDatabase

    counts = ctx.build["counts"]
    ctx.require(
        counts.get("dimension_value") == len(SHORT_VALUES),
        f"正常维度值数量错误：{counts.get('dimension_value')}",
    )
    ctx.require(
        counts.get("dimension_value_skipped") == len(LONG_VALUES),
        f"跳过计数错误：{counts.get('dimension_value_skipped')}",
    )
    driver = GraphDatabase.driver(
        ctx.config.neo4j_uri, auth=(ctx.config.neo4j_user, ctx.runtime.password)
    )
    try:
        with driver.session() as session:
            values = list(
                session.run(
                    "MATCH (v:SDADimensionValue {generation:$generation}) "
                    "RETURN v.value AS value, v.ref AS ref",
                    generation=ctx.generation,
                )
            )
            fields = {
                row["name"]: row
                for row in session.run(
                    """MATCH (f:SDAField {generation:$generation, database:'synthetic',
                    table:'long_values'})
                    RETURN f.name AS name, f.dimension_count AS count,
                    f.dimension_samples AS samples""",
                    generation=ctx.generation,
                )
            }
        ctx.require(
            {row["value"] for row in values} == set(SHORT_VALUES),
            f"超长取值仍写入了维度节点：{len(values)} 个节点",
        )
        ctx.require(
            all(len(row["ref"].encode("utf-8")) <= 2048 for row in values),
            "维度 ref 超过索引上限",
        )
        ctx.require(
            fields["short_code"]["count"] == len(SHORT_VALUES)
            and fields["short_code"]["samples"] == SHORT_VALUES,
            f"短取值字段状态错误：{fields['short_code']}",
        )
        ctx.require(
            fields["free_text"]["count"] == len(LONG_VALUES),
            f"长取值字段计数没有按全部取值计算：{fields['free_text']}",
        )
        ctx.require(
            fields["free_text"]["samples"] == [],
            f"长取值仍出现在样例中：{fields['free_text']['samples']}",
        )
        hits = ctx.mcp_call(
            "lookup_dim_values",
            {"table": "synthetic.long_values", "field": "short_code", "keyword": "A"},
        )
        ctx.require(
            any(hit["value"] == "A" for hit in hits), f"MCP 未检索到短取值：{hits}"
        )
        return {
            "dimension_values": len(values),
            "skipped": counts.get("dimension_value_skipped"),
        }
    finally:
        driver.close()


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--generate":
        raise SystemExit("usage: issue_114_limits.py --generate DIRECTORY")
    generate(Path(sys.argv[2]))
