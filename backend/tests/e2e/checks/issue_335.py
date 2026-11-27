"""Validate derived projections and JOIN evidence through MCP."""


def check_derived_star(ctx):
    base = (
        "WITH c1 AS (SELECT * FROM shop.orders WHERE dt = '2025-01-01'), "
        "c2 AS (SELECT c1.*, COUNT(*) AS cnt FROM c1) "
    )
    good = ctx.mcp_call("validate_sql", {"sql": base + "SELECT c2.id, c2.cnt FROM c2"})
    ctx.require(good["status"] == "pass", f"nested star: {good}")
    bad = ctx.mcp_call("validate_sql", {"sql": base + "SELECT c2.nope FROM c2"})
    ctx.require(
        any(f["name"] == "c2.nope" and f["status"] == "error" for f in bad["fields"]),
        f"missing derived field: {bad}",
    )
    unknown = ctx.mcp_call(
        "validate_sql", {"sql": "SELECT x.nope FROM (SELECT * FROM mystery) x"}
    )
    ctx.require(
        any(
            f["name"] == "x.nope" and f["status"] == "unknown"
            for f in unknown["fields"]
        ),
        f"unknown source: {unknown}",
    )


def check_join_using(ctx):
    sql = (
        "SELECT o.id FROM shop.orders o JOIN shop.daily_sales d USING (id) "
        "WHERE o.dt = '2025-01-01' AND d.dt = '2025-01-01'"
    )
    result = ctx.mcp_call("validate_sql", {"sql": sql})
    ctx.require(result["status"] == "pass", f"USING with lineage: {result}")


def check_ambiguous_using(ctx):
    sql = (
        "SELECT o.id FROM shop.orders o JOIN shop.daily_sales d USING (id) "
        "JOIN shop.customers c USING (id) WHERE o.dt = '2025-01-01' "
        "AND d.dt = '2025-01-01' AND c.dt = '2025-01-01'"
    )
    result = ctx.mcp_call("validate_sql", {"sql": sql})
    ctx.require(
        len(result["joins"]) == 2 and result["joins"][1]["status"] != "supported",
        f"ambiguous USING was accepted: {result}",
    )
