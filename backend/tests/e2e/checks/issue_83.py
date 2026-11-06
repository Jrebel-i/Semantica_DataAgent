"""Exercise field lineage through MCP and REST on the synthetic fixture."""


def check_field_lineage(ctx):
    mcp = ctx.mcp_call("get_lineage", {"table": "dst.orders", "column": "order_id"})
    response = ctx.rest.get(
        "/api/v1/column-lineage", params={"table": "dst.orders", "column": "order_id"}
    )
    response.raise_for_status()
    upstream = response.json()["upstream"]
    ctx.require(
        any(
            e["upstream_table"] == "src.orders"
            and e["upstream_field"] == "id"
            and e["task_id"] == "load"
            and e["task_type"] == "spark"
            for e in upstream
        ),
        f"REST missing source: {upstream}",
    )
    ctx.require(mcp == upstream, f"MCP and REST differ: {mcp}, {upstream}")
    return {"upstream": upstream}
