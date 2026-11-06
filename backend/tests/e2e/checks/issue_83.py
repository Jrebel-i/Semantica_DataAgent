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


def check_depth_and_directions(ctx):
    def query(table, column, direction, depth):
        return ctx.mcp_call(
            "get_lineage",
            {"table": table, "column": column, "direction": direction, "depth": depth},
        )

    up = query("report.orders", "id", "up", 2)
    ctx.require(
        {e["task_id"] for e in up} == {"load", "report_load"}, f"upstream depth 2: {up}"
    )
    down = query("src.orders", "id", "down", 2)
    ctx.require(
        {e["task_id"] for e in down} == {"load", "stage_load", "report_load"},
        f"downstream depth 2: {down}",
    )
    one = query("src.orders", "id", "down", 1)
    ctx.require(
        {e["task_id"] for e in one} == {"load", "stage_load"},
        f"downstream depth 1: {one}",
    )
    empty = query("dst.orders", "absent", "up", 2)
    ctx.require(empty == [], f"missing field should have no lineage: {empty}")
    table = ctx.mcp_call("get_lineage", {"table": "dst.orders", "direction": "up"})
    ctx.require(
        any(
            e["upstream"] == "src.orders" and e["task_id"] == "load" and e["ref"]
            for e in table
        ),
        f"table lineage compatibility: {table}",
    )
    return {"up": up, "down": down}
