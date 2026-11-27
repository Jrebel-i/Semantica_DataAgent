"""Check join evidence through the public MCP entry point."""


def check_join_evidence(ctx):
    base = "SELECT o.id FROM shop.orders o JOIN shop.customers c ON "
    where = " WHERE o.dt = '2025-01-01' AND c.dt = '2025-01-01'"
    for condition, expected in (
        ("o.id = c.id AND o.created_at = c.created_at", "supported"),
        ("o.id = c.created_at", "weak"),
        ("o.id = c.id", "weak"),
    ):
        result = ctx.mcp_call("validate_sql", {"sql": base + condition + where})
        ctx.require(result["joins"][0]["status"] == expected, f"{condition}: {result}")
        if expected == "weak":
            ctx.require(
                any(
                    "血缘存在但连接列未确认" in issue["message"]
                    for issue in result["issues"]
                ),
                f"missing lineage explanation: {result}",
            )


def check_column_lineage(ctx):
    sql = "SELECT i.id FROM shop.order_items i JOIN shop.customers c ON i.created_at = c.id WHERE i.dt = '2025-01-01' AND c.dt = '2025-01-01'"
    result = ctx.mcp_call("validate_sql", {"sql": sql})
    ctx.require(
        result["joins"][0]["status"] == "supported", f"column lineage: {result}"
    )
    ctx.require(
        any(
            proof["type"] == "column_lineage"
            for proof in result["joins"][0]["evidence"]
        ),
        f"missing column lineage: {result}",
    )


if __name__ == "__main__":
    import json
    import shutil
    import sys
    from pathlib import Path

    target = Path(sys.argv[1])
    demo = Path(__file__).resolve().parents[4] / "samples/demo"
    shutil.copytree(demo, target, dirs_exist_ok=True)
    lineage_path = target / "lineage.json"
    lineage = json.loads(lineage_path.read_text())
    lineage.append(
        {
            "upstream": "shop.orders",
            "downstream": "shop.customers",
            "evidence": "declared",
            "parse_status": "success",
            "task_id": "i344_lineage",
        }
    )
    lineage_path.write_text(json.dumps(lineage))
    (target / "relationships.json").write_text(
        json.dumps(
            [
                {
                    "name": "i344_fk",
                    "from_database": "shop",
                    "from_table": "orders",
                    "from_columns": ["id", "created_at"],
                    "to_database": "shop",
                    "to_table": "customers",
                    "to_columns": ["id", "created_at"],
                }
            ]
        )
    )
    (target / "column_lineage.json").write_text(
        json.dumps(
            [
                {
                    "upstream_table": "shop.order_items",
                    "upstream_field": "created_at",
                    "downstream_table": "shop.customers",
                    "downstream_field": "id",
                    "task_id": "i344_orphan",
                    "evidence": "declared",
                    "parse_status": "success",
                }
            ]
        )
    )
