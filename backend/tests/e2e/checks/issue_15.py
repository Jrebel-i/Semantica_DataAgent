"""Switch backward and forward through real REST and MCP interfaces."""

import base64
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


def check_incremental_generation_freshness(ctx):
    def upload(content):
        response = ctx.rest.post(
            "/api/v1/source/upload",
            json={
                "target": "knowledge/issue15",
                "files": [
                    {
                        "path": "freshness.md",
                        "content_base64": base64.b64encode(content.encode()).decode(),
                    }
                ],
                "replace": False,
            },
        )
        ctx.require(response.status_code == 200, response.text)
        return response.json()["commit"]

    def build(kind):
        response = ctx.rest.post("/api/v1/builds", json={"build_type": kind})
        ctx.require(response.status_code == 202, response.text)
        build_id = response.json()["id"]
        deadline = time.monotonic() + ctx.config.build_timeout
        while time.monotonic() < deadline:
            row = ctx.rest.get(f"/api/v1/builds/{build_id}").json()
            if row["status"] in {"succeeded", "failed", "cancelled"}:
                return row
            time.sleep(0.2)
        raise AssertionError(f"build {build_id} timed out")

    def generation():
        response = ctx.rest.get("/api/v1/generations")
        ctx.require(response.status_code == 200, response.text)
        view = response.json()
        return view, next(row for row in view["generations"] if row["current"])

    initial = upload("# Freshness\n\nInitial value.\n")
    full = build("full")
    ctx.require(full["status"] == "succeeded", f"full build failed: {full}")
    view, before = generation()
    ctx.require(
        before["source_commit"] == initial and not view["behind"],
        f"full generation: {view}",
    )
    updated = upload("# Freshness\n\nUpdated value.\n")
    view, current = generation()
    ctx.require(
        updated != initial and view["source_head"] == updated and view["behind"],
        f"expected behind after upload: {view}",
    )
    ctx.require(current == before, "upload changed generation metadata")
    settings = ctx.rest.get("/api/v1/settings").json()
    original_model = settings["embedding_model"]
    response = ctx.rest.put(
        "/api/v1/settings", json={"values": {"embedding_model": "issue15-incompatible"}}
    )
    ctx.require(response.status_code == 200, response.text)
    try:
        failed = build("incremental")
        ctx.require(failed["status"] == "failed", f"expected failed build: {failed}")
        failed_view, failed_generation = generation()
        ctx.require(
            failed_view["behind"] and failed_generation == before,
            f"failed build changed metadata: {failed_view}",
        )
    finally:
        response = ctx.rest.put(
            "/api/v1/settings", json={"values": {"embedding_model": original_model}}
        )
        ctx.require(response.status_code == 200, response.text)
    result = build("incremental")
    ctx.require(result["status"] == "succeeded", f"incremental build failed: {result}")
    view, current = generation()
    ctx.require(
        not view["behind"], f"generation still behind after incremental build: {view}"
    )
    ctx.require(
        current["source_commit"] == updated == view["source_head"],
        f"generation commit is stale: {view}",
    )
    ctx.require(
        current["counts"] == result["counts"],
        f"generation counts are stale: {current}, {result}",
    )
    return {"commit": updated, "build_id": result["id"]}
