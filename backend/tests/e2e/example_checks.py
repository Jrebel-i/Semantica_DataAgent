"""Example ticket-specific checks for the end-to-end harness.

Pass this file with ``--checks``. Copy the needed functions into the ticket's
own check file; do not edit the harness for ticket-specific assertions.
"""

from __future__ import annotations


def check_rest_tables(ctx):
    response = ctx.rest.get("/api/v1/tables", params={"database": "shop"})
    response.raise_for_status()
    tables = response.json()
    ctx.require(tables, "expected at least one shop table over REST")
    return {"tables": len(tables), "names": sorted(item["name"] for item in tables)}


def check_metric_document_search(ctx):
    hits = ctx.mcp_call(
        "search_knowledge",
        {
            "query": "销售额",
            "scope": "docs",
            "category": "metrics",
            "top_k": 5,
        },
    )
    ctx.require(hits, "expected a metrics document hit")
    return {"hits": len(hits), "refs": [hit["ref"] for hit in hits]}


def check_lineage_task_edges(ctx):
    edges = ctx.mcp_call(
        "get_lineage", {"table": "shop.orders", "direction": "up", "depth": 1}
    )
    ctx.require(edges, "expected upstream lineage for shop.orders")
    ctx.require(
        any(edge["upstream"] == "staging.orders" for edge in edges),
        "expected staging.orders to be an upstream table",
    )
    return {
        "edges": len(edges),
        "task_ids": sorted({edge["task_id"] for edge in edges}),
    }
