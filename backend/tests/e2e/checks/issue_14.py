"""Exercise uploads, incremental freshness, deletion and build queue via the API."""

import base64
import json
import time


def check_source_incremental_and_queue(ctx):
    def upload(files, replace=False):
        response = ctx.rest.post(
            "/api/v1/source/upload",
            json={
                "target": "knowledge/issue14",
                "files": [
                    {
                        "path": name,
                        "content_base64": base64.b64encode(text.encode()).decode(),
                    }
                    for name, text in files.items()
                ],
                "replace": replace,
            },
        )
        response.raise_for_status()
        commit = response.json()["commit"]
        ctx.require(len(commit) == 40, f"upload did not return a commit: {commit}")
        return commit

    def start(kind):
        response = ctx.rest.post("/api/v1/builds", json={"build_type": kind})
        response.raise_for_status()
        return response.json()

    def wait(record):
        deadline = time.monotonic() + ctx.config.build_timeout
        while time.monotonic() < deadline:
            response = ctx.rest.get(f"/api/v1/builds/{record['id']}")
            response.raise_for_status()
            record = response.json()
            if record["status"] in {"succeeded", "failed", "cancelled"}:
                ctx.require(record["status"] == "succeeded", f"build failed: {record}")
                return record
            time.sleep(0.2)
        raise AssertionError(f"build timed out: {record}")

    def search(term):
        return json.dumps(
            ctx.mcp_call(
                "search_knowledge", {"query": term, "scope": "docs", "top_k": 50}
            ),
            ensure_ascii=False,
        )

    old = "zinnamonissuefourteen"
    new = "quokkadyneissuefourteen"
    other = "tamarindissuefourteen"
    first = upload(
        {
            "index.md": "# Index\n\nA and B.\n",
            "a.md": f"# A\n\n{old} metric.\n",
            "b.md": f"# B\n\n{other} metric.\n",
        }
    )
    wait(start("full"))
    ctx.require(
        old in search(old) and other in search(other),
        "initial documents missing from search",
    )
    second = upload({"a.md": f"# A\n\n{new} metric.\n"})
    ctx.require(first != second, "second upload did not create a new commit")
    changed = wait(start("incremental"))
    ctx.require(
        changed["counts"].get("changed") == 1,
        f"expected one changed file: {changed['counts']}",
    )
    ctx.require(
        changed["counts"].get("document") == 1,
        f"expected one rebuilt document: {changed['counts']}",
    )
    ctx.require(new in search(new), "new content missing from search")
    ctx.require(old not in search(old), "old content remains in search")
    ctx.require(other in search(other), "unchanged document disappeared")
    upload(
        {"index.md": "# Index\n\nA and B.\n", "a.md": f"# A\n\n{new} metric.\n"},
        replace=True,
    )
    one = start("incremental")
    two = start("incremental")
    ctx.require(two["status"] == "queued", f"second build did not queue: {two}")
    deleted = wait(one)
    last = wait(two)
    ctx.require(
        deleted["counts"].get("deleted") == 1,
        f"expected one deleted file: {deleted['counts']}",
    )
    ctx.require(other not in search(other), "deleted document remains in search")
    ctx.require(
        last["started_at"] >= deleted["finished_at"],
        "queued build started before its predecessor finished",
    )
    return {
        "upload_commit": second,
        "changed_build": changed["id"],
        "deleted_build": deleted["id"],
        "queued_build": last["id"],
    }
