"""Project-context pre-refactor checks for #173.

The refactor must not change any observable behavior.  These checks enter
through the real REST and MCP surfaces and cover every project-scoped read
family that was migrated: generation, source directory, retrieval storage,
plugin-configured build, dimensions, lineage and build counts.
"""

from __future__ import annotations


def check_project_context_rest_surface(ctx):
    settings = ctx.rest.get("/api/v1/settings")
    ctx.require(settings.status_code == 200, settings.text)
    saved = settings.json()
    for key in (
        "knowledge_generation",
        "source_directory",
        "dimension_value_limit",
    ):
        ctx.require(saved.get(key), f"settings lost project key {key}")

    generations = ctx.rest.get("/api/v1/generations")
    ctx.require(generations.status_code == 200, generations.text)
    current = [row for row in generations.json()["generations"] if row.get("current")]
    ctx.require(len(current) == 1, "expected exactly one current generation")
    ctx.require(
        current[0]["name"] == ctx.generation,
        f"current generation is {current[0]['name']}, expected {ctx.generation}",
    )
    ctx.require(
        generations.json().get("source_head"),
        "generation view could not read the configured source directory",
    )

    categories = ctx.rest.get("/api/v1/categories")
    ctx.require(categories.status_code == 200, categories.text)
    ctx.require(categories.json(), "category read returned no rows")

    tables = ctx.rest.get("/api/v1/tables", params={"database": "shop"})
    ctx.require(tables.status_code == 200, tables.text)
    ctx.require(tables.json(), "table read returned no rows")

    lineage = ctx.rest.get("/api/v1/table-lineage", params={"table": ctx.expect_table})
    ctx.require(lineage.status_code == 200, lineage.text)
    ctx.require(lineage.json().get("upstream"), "table lineage has no upstream")
    return {
        "ok": True,
        "info": {
            "generation": current[0]["name"],
            "categories": len(categories.json()),
            "tables": len(tables.json()),
            "source_head": generations.json()["source_head"][:12],
        },
    }


def check_project_context_mcp_surface(ctx):
    capabilities = ctx.mcp_call("get_capabilities", {})
    ctx.require(
        capabilities.get("current_generation") == ctx.generation,
        "MCP capabilities reports a different generation",
    )

    values = ctx.mcp_call(
        "lookup_dim_values", {"table": "shop.channels", "keyword": "网页"}
    )
    ctx.require(
        any(item.get("value") == "网页" for item in values),
        "dimension value lookup did not return the indexed value",
    )

    hits = ctx.mcp_call(
        "search_knowledge",
        {"query": "orders", "scope": "tables", "top_k": 5},
    )
    ctx.require(
        any(hit.get("ref") == "table:shop.orders" for hit in hits),
        "search did not return table:shop.orders",
    )

    described = ctx.mcp_call("describe_tables", {"names": [ctx.expect_table]})
    ctx.require(
        described and described[0].get("fields"),
        "describe_tables lost the current generation's fields",
    )
    return {
        "ok": True,
        "info": {
            "generation": capabilities["current_generation"],
            "dimension_hits": len(values),
            "search_hits": len(hits),
            "fields": len(described[0]["fields"]),
        },
    }


def check_project_context_build_counts(ctx):
    ctx.require(ctx.build_id is not None, "build id is missing")
    response = ctx.rest.get(f"/api/v1/builds/{ctx.build_id}")
    ctx.require(response.status_code == 200, response.text)
    build = response.json()
    ctx.require(build["status"] == "succeeded", f"build status {build['status']}")
    counts = build.get("counts") or {}
    ctx.require(counts, "build counts are empty")
    ctx.require(
        counts == (ctx.build.get("counts") or {}),
        "build detail counts differ from the polled build",
    )
    for key in ("table", "field"):
        ctx.require(
            isinstance(counts.get(key), int) and counts[key] > 0,
            f"build count {key} is missing or zero",
        )
    return {"ok": True, "info": {"counts": counts}}
