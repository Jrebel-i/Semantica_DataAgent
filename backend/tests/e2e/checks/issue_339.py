"""Check correlated column resolution through MCP."""


def check_correlated_references(ctx):
    base = "SELECT o.id FROM shop.orders o WHERE o.dt = '2025-01-01' AND EXISTS (SELECT 1 FROM shop.customers c WHERE c.dt = '2025-01-01' AND "
    good = ctx.mcp_call("validate_sql", {"sql": base + "c.id = o.id)"})
    ctx.require(good["status"] == "pass", f"correlated EXISTS: {good}")
    for condition, message in (
        ("c.nope = o.id", "字段不存在"),
        ("c.id = typo.id", "未知别名"),
    ):
        result = ctx.mcp_call("validate_sql", {"sql": base + condition + ")"})
        ctx.require(
            any(
                i["kind"] == "field" and message in i["message"]
                for i in result["issues"]
            ),
            f"{condition}: {result}",
        )
    for sql in (
        "SELECT o.id FROM shop.orders o WHERE o.dt = '2025-01-01' AND EXISTS (WITH x AS (SELECT o.id) SELECT * FROM x)",
        "SELECT o.id FROM shop.orders o WHERE o.dt = '2025-01-01' AND EXISTS (SELECT 1 FROM (SELECT id FROM shop.customers) o WHERE o.created_at = 1)",
    ):
        result = ctx.mcp_call("validate_sql", {"sql": sql})
        ctx.require(
            any(i["kind"] == "field" for i in result["issues"]),
            f"invalid reference: {result}",
        )
