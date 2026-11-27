"""Verify partition validation through the published MCP interface."""


def check_partition_expressions(ctx):
    for predicate, expected in [
        ("'2025-01-01' <= dt", "pass"),
        ("dt >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)", "pass"),
        ("dt <= CURRENT_DATE()", "issues"),
    ]:
        result = ctx.mcp_call(
            "validate_sql",
            {
                "sql": f"SELECT id FROM shop.orders WHERE {predicate}",
                "dialect": "doris",
            },
        )
        ctx.require(result["status"] == expected, f"{predicate}: {result}")
    return {"checked": 3}


def check_join_partition_sides(ctx):
    for join, on, where, missing in [
        ("LEFT", "c.dt = '2025-01-01'", "o.dt = '2025-01-01'", set()),
        ("RIGHT", "o.dt = '2025-01-01'", "c.dt = '2025-01-01'", set()),
        (
            "FULL",
            "o.dt = '2025-01-01' AND c.dt = '2025-01-01'",
            "1=1",
            {"shop.orders", "shop.customers"},
        ),
    ]:
        sql = f"SELECT o.id FROM shop.orders o {join} JOIN shop.customers c ON o.id = c.id AND {on} WHERE {where}"
        result = ctx.mcp_call("validate_sql", {"sql": sql, "dialect": "doris"})
        actual = {i["table"] for i in result["issues"] if i["kind"] == "partition"}
        ctx.require(actual == missing, f"{join}: expected {missing}, got {result}")
    return {"checked": 3}
