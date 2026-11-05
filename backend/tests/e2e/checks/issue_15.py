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


def check_scheduled_incremental_build(ctx):
    response = ctx.rest.put(
        "/api/v1/settings", json={"values": {"schedule_interval_seconds": "1"}}
    )
    ctx.require(response.status_code == 200, response.text)
    scheduled = None
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            rows = ctx.rest.get("/api/v1/builds").json()
            scheduled = next(
                (row for row in rows if row["triggered_by"] == "scheduler"), None
            )
            if scheduled:
                break
            time.sleep(0.2)
        ctx.require(scheduled is not None, "scheduler did not submit a build")
        response = ctx.rest.put(
            "/api/v1/settings", json={"values": {"schedule_interval_seconds": "60"}}
        )
        ctx.require(response.status_code == 200, response.text)
        deadline = time.monotonic() + ctx.config.build_timeout
        while time.monotonic() < deadline:
            scheduled = ctx.rest.get(f"/api/v1/builds/{scheduled['id']}").json()
            if scheduled["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.2)
        ctx.require(
            scheduled["status"] == "succeeded", f"scheduled build failed: {scheduled}"
        )
        ctx.require(
            scheduled["build_type"] == "incremental",
            "scheduler did not request an incremental build",
        )
        ctx.require(
            ctx.rest.get("/api/v1/settings").json()["schedule_interval_seconds"]
            == "60",
            "updated interval was not saved",
        )
        return {"build_id": scheduled["id"], "triggered_by": scheduled["triggered_by"]}
    finally:
        ctx.rest.put(
            "/api/v1/settings", json={"values": {"schedule_interval_seconds": "0"}}
        )
