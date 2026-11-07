"""Verify summary table lists, detail lookup, and unchanged MCP/table-full output."""

from urllib.parse import quote

SUMMARY_KEYS = {
    "name",
    "database",
    "comment",
    "ref",
    "field_count",
    "document_count",
    "has_partition",
}


def _detail(ctx, database, table):
    response = ctx.rest.get(
        f"/api/v1/tables/{quote(database, safe='')}/{quote(table, safe='')}"
    )
    ctx.require(
        response.status_code == 200,
        f"detail {database}.{table}: {response.status_code} {response.text}",
    )
    return response.json()


def check_issue_131_summary_and_detail(ctx):
    unauthorized = ctx.rest.get(
        "/api/v1/tables", params={"view": "summary"}, headers={"Authorization": ""}
    )
    ctx.require(
        unauthorized.status_code == 401, "summary list allowed anonymous access"
    )
    unauthorized = ctx.rest.get(
        "/api/v1/tables/shop/orders", headers={"Authorization": "Bearer wrong"}
    )
    ctx.require(unauthorized.status_code == 401, "detail allowed anonymous access")

    response = ctx.rest.get("/api/v1/tables", params={"view": "summary"})
    ctx.require(
        response.status_code == 200,
        f"summary list: {response.status_code} {response.text}",
    )
    summaries = response.json()
    ctx.require(summaries, "summary list is empty")
    for row in summaries:
        ctx.require(
            set(row) == SUMMARY_KEYS,
            f"{row.get('database')}.{row.get('name')} summary keys: {sorted(row)}",
        )
        detail = _detail(ctx, row["database"], row["name"])
        ctx.require(
            row["field_count"] == len(detail["fields"]),
            f"{row['database']}.{row['name']} field_count mismatch",
        )
        ctx.require(
            row["document_count"] == len(detail["documents"]),
            f"{row['database']}.{row['name']} document_count mismatch",
        )
        ctx.require(
            row["has_partition"]
            == any(field["partition"] for field in detail["fields"]),
            f"{row['database']}.{row['name']} has_partition mismatch",
        )

    filtered = ctx.rest.get(
        "/api/v1/tables",
        params={"database": "shop", "search": "order", "view": "summary"},
    )
    ctx.require(filtered.status_code == 200, filtered.text)
    ctx.require(
        filtered.json()
        and all(
            row["database"] == "shop" and "order" in row["name"]
            for row in filtered.json()
        ),
        f"database/search filtering changed: {filtered.text}",
    )

    full = ctx.rest.get("/api/v1/tables")
    ctx.require(full.status_code == 200, full.text)
    ctx.require(
        any("fields" in row and "documents" in row for row in full.json()),
        "default table list no longer returns full table rows",
    )

    orders = _detail(ctx, "shop", "orders")
    ctx.require(orders["ref"] == "table:shop.orders", f"unexpected ref: {orders}")
    ctx.require(orders["fields"], "shop.orders detail has no fields")
    ctx.require(
        any(field["name"] == "dt" and field["partition"] for field in orders["fields"]),
        "shop.orders detail is missing the partition field",
    )

    missing = ctx.rest.get("/api/v1/tables/shop/not-a-table")
    ctx.require(missing.status_code == 404, f"missing detail: {missing.status_code}")

    mcp_table = ctx.mcp_call("describe_tables", {"names": ["shop.orders"]})[0]
    ctx.require(
        mcp_table["fields"] and "documents" in mcp_table,
        "describe_tables no longer returns full fields and documents",
    )
    return {
        "summaries": len(summaries),
        "orders_fields": len(orders["fields"]),
        "orders_ref": orders["ref"],
    }
