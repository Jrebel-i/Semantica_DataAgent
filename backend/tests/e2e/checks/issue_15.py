"""Switch backward and forward through real REST and MCP interfaces."""

import time


def check_generation_switch_and_rollback(ctx):
    first = ctx.generation
    original = ctx.mcp_call(
        "search_knowledge",
        {"query": ctx.config.search_query, "scope": "all", "top_k": 10},
    )
    ctx.require(original, "initial generation must be searchable")
    response = ctx.rest.post("/api/v1/builds", json={"build_type": "full"})
    ctx.require(response.status_code == 202, response.text)
    build_id = response.json()["id"]
    deadline = time.monotonic() + ctx.config.build_timeout
    while time.monotonic() < deadline:
        build = ctx.rest.get(f"/api/v1/builds/{build_id}").json()
        if build["status"] in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.2)
    ctx.require(build["status"] == "succeeded", f"second build failed: {build}")
    response = ctx.rest.get("/api/v1/generations")
    ctx.require(response.status_code == 200, response.text)
    rows = response.json()["generations"]
    current = next(row["name"] for row in rows if row["current"])
    ctx.require(current != first, "full build did not switch generation")
    for name in (first, current):
        response = ctx.rest.post(f"/api/v1/generations/{name}/switch")
        ctx.require(response.status_code == 200, response.text)
        capabilities = ctx.mcp_call("get_capabilities", {})
        ctx.require(
            capabilities["current_generation"] == name, "switch did not reach MCP"
        )
        hits = ctx.mcp_call(
            "search_knowledge",
            {"query": ctx.config.search_query, "scope": "all", "top_k": 10},
        )
        ctx.require(hits, f"generation {name} is not searchable")
    return {"first": first, "second": current}
