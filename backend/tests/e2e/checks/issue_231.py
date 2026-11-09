"""#231: the process-local database layer still serves published builds after the
fork-safety change (a forked child must build its own engine)."""

from __future__ import annotations


def check_build_publication_over_rest(ctx):
    response = ctx.rest.get(f"/api/v1/builds/{ctx.build_id}")
    ctx.require(response.status_code == 200, response.text)
    build = response.json()
    ctx.require(build["status"] == "succeeded", build)
    ctx.require(build.get("finished_at"), "published build has no finished_at")
    projects = ctx.rest.get("/api/v1/projects")
    ctx.require(projects.status_code == 200, projects.text)
    ctx.require(projects.json(), "no project is visible over REST")
    return {
        "build_status": build["status"],
        "generation": build.get("generation"),
        "projects": len(projects.json()),
    }
