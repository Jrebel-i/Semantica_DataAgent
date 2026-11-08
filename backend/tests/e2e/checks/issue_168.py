"""Issue #168 end-to-end checks: embedding concurrency 8 is accepted and usable.

The Kubernetes pool and the batch size are static infrastructure properties
checked by ``tests/test_embedding_deployment.py``. The REST-observable part of
this ticket is the recommended runtime setting: the documented range must be
enforced and a build must succeed with ``embedding_concurrency=8``.
"""

from __future__ import annotations

import time

RECOMMENDED = "8"
OUT_OF_RANGE = "33"


def _wait_for_build(ctx, build_id):
    deadline = time.monotonic() + ctx.config.build_timeout
    detail = None
    while time.monotonic() < deadline:
        response = ctx.rest.get(f"/api/v1/builds/{build_id}")
        ctx.require(
            response.status_code == 200, f"构建详情 HTTP {response.status_code}"
        )
        detail = response.json()
        if detail.get("status") != "running":
            return detail
        time.sleep(ctx.config.poll_interval)
    raise AssertionError(f"构建 {build_id} 在 {ctx.config.build_timeout}s 后仍在运行")


def check_recommended_embedding_concurrency_is_accepted(ctx):
    response = ctx.rest.get("/api/v1/settings")
    ctx.require(response.status_code == 200, f"读取设置 HTTP {response.status_code}")
    original = response.json().get("embedding_concurrency")

    try:
        response = ctx.rest.put(
            "/api/v1/settings",
            json={"values": {"embedding_concurrency": OUT_OF_RANGE}},
        )
        ctx.require(
            response.status_code == 422,
            f"embedding_concurrency={OUT_OF_RANGE} 应被拒绝，实际 HTTP {response.status_code}",
        )

        response = ctx.rest.put(
            "/api/v1/settings",
            json={"values": {"embedding_concurrency": RECOMMENDED}},
        )
        ctx.require(
            response.status_code == 200,
            f"保存 embedding_concurrency={RECOMMENDED} HTTP {response.status_code}: "
            f"{response.text[:300]}",
        )
        saved = ctx.rest.get("/api/v1/settings").json().get("embedding_concurrency")
        ctx.require(
            saved == RECOMMENDED,
            f"设置回读为 {saved!r}，期望 {RECOMMENDED!r}",
        )

        response = ctx.rest.post("/api/v1/builds")
        ctx.require(response.status_code == 202, response.text[:500])
        build = _wait_for_build(ctx, response.json()["id"])
        ctx.require(
            build.get("status") == "succeeded",
            f"并发 8 的构建未成功：{build.get('status')} {build.get('error')}",
        )

        hits = ctx.mcp_call(
            "search_knowledge",
            {"query": ctx.config.search_query, "scope": "docs", "top_k": 3},
        )
        ctx.require(
            isinstance(hits, list) and hits,
            f"并发 8 的构建后 search_knowledge 无结果：{hits!r}",
        )
        return {
            "embedding_concurrency": RECOMMENDED,
            "build_id": build.get("id"),
            "hits": len(hits),
        }
    finally:
        restore = {"values": {"embedding_concurrency": original or ""}}
        ctx.rest.put("/api/v1/settings", json=restore)
