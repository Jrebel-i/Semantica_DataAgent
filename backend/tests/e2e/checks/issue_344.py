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
