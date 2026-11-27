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


def check_duplicate_alias_returns_issue(ctx):
    sql = (
        "SELECT a.id FROM shop.orders a JOIN shop.customers a ON a.id = a.id "
        "WHERE a.dt = '2025-01-01'"
    )
    result = ctx.mcp_call("validate_sql", {"sql": sql})
    ctx.require(
        result.get("status") == "issues" and bool(result.get("issues")),
        f"duplicate alias must produce a structured issue: {result}",
    )


def check_cte_star_column_aliases(ctx):
    sql = (
        "WITH c(customer_key, created_time, partition_day) AS "
        "(SELECT * FROM shop.customers WHERE dt = '2025-01-01') "
        "SELECT c.customer_key FROM c"
    )
    result = ctx.mcp_call("validate_sql", {"sql": sql})
    ctx.require(result.get("status") == "pass", f"CTE column aliases: {result}")


def check_duplicate_projected_fields_are_ambiguous(ctx):
    sql = (
        "SELECT x.id FROM (SELECT o.*, d.* FROM shop.orders o "
        "JOIN shop.daily_sales d ON o.id = d.id "
        "WHERE o.dt = '2025-01-01' AND d.dt = '2025-01-01') x"
    )
    result = ctx.mcp_call("validate_sql", {"sql": sql})
    ctx.require(
        any(
            f.get("name") == "x.id" and f.get("status") == "ambiguous"
            for f in result.get("fields", [])
        ),
        f"duplicate projected field must be ambiguous: {result}",
    )


def check_using_merged_field_is_visible(ctx):
    sql = (
        "SELECT x.id FROM (SELECT * FROM shop.orders o "
        "JOIN shop.daily_sales d USING (id) "
        "WHERE o.dt = '2025-01-01' AND d.dt = '2025-01-01') x"
    )
    result = ctx.mcp_call("validate_sql", {"sql": sql})
    ctx.require(result.get("status") == "pass", f"merged USING field: {result}")


def check_derived_star_join_uses_source_evidence(ctx):
    sql = (
        "SELECT o.id FROM shop.orders o JOIN "
        "(SELECT * FROM shop.daily_sales WHERE dt = '2025-01-01') d USING (id) "
        "WHERE o.dt = '2025-01-01'"
    )
    result = ctx.mcp_call("validate_sql", {"sql": sql})
    ctx.require(result.get("status") == "pass", f"derived USING: {result}")
