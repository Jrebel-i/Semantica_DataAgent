"""Exercise correction approval, rejection, metadata persistence and gaps."""

import json
import subprocess
import time


def check_corrections_and_gaps(ctx):
    def wait(build_id):
        deadline = time.monotonic() + ctx.config.build_timeout
        while time.monotonic() < deadline:
            response = ctx.rest.get(f"/api/v1/builds/{build_id}")
            response.raise_for_status()
            build = response.json()
            if build["status"] in {"succeeded", "failed", "cancelled"}:
                ctx.require(build["status"] == "succeeded", f"build failed: {build}")
                return build
            time.sleep(0.2)
        raise AssertionError(f"build {build_id} timed out")

    def post(path, body=None):
        response = ctx.rest.post(path, json=body)
        ctx.require(
            response.status_code == 200,
            f"{path}: {response.status_code} {response.text}",
        )
        return response.json()

    def propose(target, patch):
        return ctx.mcp_call(
            "propose_correction",
            {"target": target, "patch": patch, "reason": "issue 22 verification"},
        )["proposal_id"]

    def head():
        return subprocess.check_output(
            ["git", "-C", str(ctx.source_dir), "rev-parse", "HEAD"], text=True
        ).strip()

    target = "document:metrics/sales.md"
    path = ctx.source_dir / "knowledge/metrics/sales.md"
    old = path.read_text()
    new = old + "\nissue22quokkafreshness\n"
    # Initialize source history through the supported upload endpoint.
    import base64

    response = ctx.rest.post(
        "/api/v1/source/upload",
        json={
            "target": "knowledge/metrics",
            "files": [
                {
                    "path": "sales.md",
                    "content_base64": base64.b64encode(old.encode()).decode(),
                }
            ],
        },
    )
    ctx.require(response.status_code == 200, response.text)
    before = head()
    proposal = propose(target, new)
    detail = ctx.rest.get(f"/api/v1/corrections/{proposal}")
    ctx.require(
        detail.status_code == 200
        and "+issue22quokkafreshness" in detail.json()["diff"],
        f"missing diff: {detail.text}",
    )
    post(f"/api/v1/corrections/{proposal}/approve")
    ctx.require(
        head() != before and path.read_text() == new, "approval did not commit content"
    )
    builds = ctx.rest.get("/api/v1/builds").json()
    incremental = next(
        (b for b in builds if b.get("build_type") == "incremental"), None
    )
    ctx.require(incremental is not None, "approval did not queue incremental build")
    wait(incremental["id"])
    search = json.dumps(
        ctx.mcp_call(
            "search_knowledge",
            {"query": "issue22quokkafreshness", "scope": "docs", "top_k": 50},
        ),
        ensure_ascii=False,
    )
    ctx.require(
        "issue22quokkafreshness" in search, "approved content missing from search"
    )
    rejected = propose(target, new + "\nissue22rejected\n")
    previous = head()
    post(f"/api/v1/corrections/{rejected}/reject")
    ctx.require(
        head() == previous and path.read_text() == new, "rejection changed source"
    )
    field = propose("field:shop.orders.id", "issue22 corrected field comment")
    post(f"/api/v1/corrections/{field}/approve")
    response = ctx.rest.post("/api/v1/builds", json={"build_type": "full"})
    ctx.require(response.status_code == 202, response.text)
    wait(response.json()["id"])
    described = json.dumps(
        ctx.mcp_call("describe_tables", {"names": ["shop.orders"]}), ensure_ascii=False
    )
    ctx.require(
        "issue22 corrected field comment" in described,
        "field override lost after full import",
    )
    user = ctx.rest.post(
        "/api/v1/tokens", json={"name": "issue22 reader", "role": "user"}
    )
    ctx.require(user.status_code == 201, user.text)
    denied = ctx.rest.post(
        f"/api/v1/corrections/{field}/approve",
        headers={"Authorization": "Bearer " + user.json()["token"]},
    )
    ctx.require(
        denied.status_code == 403, f"user approval returned {denied.status_code}"
    )
    ctx.mcp_call(
        "report_knowledge_gap",
        {"question": "issue22 question", "missing": "issue22 missing definition"},
    )
    gaps = ctx.rest.get("/api/v1/knowledge-gaps")
    ctx.require(
        gaps.status_code == 200
        and any(g["missing"] == "issue22 missing definition" for g in gaps.json()),
        "knowledge gap missing",
    )
    return {"proposal_id": proposal, "rejected_id": rejected, "field_id": field}
