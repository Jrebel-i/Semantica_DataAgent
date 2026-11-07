"""End-to-end checks for the cross-build embedding cache (#119)."""

from __future__ import annotations

import re
import time

from pymilvus import MilvusClient

COUNT = re.compile(
    r"(embedding_inputs|embedding_unique|embedding_cache_hits|embedding_cache_misses)=(\d+)"
)
REQUIRED = {
    "embedding_inputs",
    "embedding_unique",
    "embedding_cache_hits",
    "embedding_cache_misses",
}


def _build(ctx, build_id):
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


def _vectors(ctx):
    collection = "sda_" + ctx.generation
    client = MilvusClient(uri=ctx.config.milvus_uri, timeout=30)
    try:
        client.load_collection(collection)
        iterator = client.query_iterator(
            collection_name=collection,
            batch_size=1000,
            filter='id != ""',
            output_fields=["id", "vector"],
        )
        vectors = {}
        try:
            while True:
                rows = iterator.next()
                if not rows:
                    break
                for row in rows:
                    vectors[row["id"]] = [float(value) for value in row["vector"]]
        finally:
            iterator.close()
    finally:
        client.close()
    return vectors


def _stage_counts(ctx, build):
    stages = {}
    for entry in build.get("logs") or []:
        message = entry.get("message", "")
        if (
            entry.get("stage") == "relationships"
            or entry.get("level") != "info"
            or not message.startswith("Completed")
        ):
            continue
        counts = {name: int(value) for name, value in COUNT.findall(message)}
        missing = REQUIRED - set(counts)
        ctx.require(
            not missing,
            f"阶段 {entry.get('stage')} 的 Completed 日志缺少计数 {sorted(missing)}：{message}",
        )
        stages[entry["stage"]] = counts
    ctx.require("table" in stages, f"没有表阶段的 Completed 日志：{sorted(stages)}")
    return stages


def check_embedding_cache_across_full_builds(ctx):
    first = _build(ctx, ctx.build_id)
    ctx.require(
        first.get("status") == "succeeded", f"第一次构建状态：{first.get('status')}"
    )
    first_counts = first.get("counts") or {}
    for key in REQUIRED:
        ctx.require(
            key in first_counts, f"第一次构建 counts 缺少 {key}：{first_counts}"
        )
    first_unique = first_counts.get("embedding_unique", 0)
    ctx.require(first_unique > 0, f"演示源应产生唯一 embedding 文本：{first_counts}")
    ctx.require(
        first_counts.get("embedding_cache_hits", 0)
        + first_counts.get("embedding_cache_misses", 0)
        == first_unique,
        f"首次构建的缓存命中与未命中之和不等于唯一文本数：{first_counts}",
    )
    ctx.require(
        first_counts.get("embedding_cache_misses", 0) > 0,
        f"首次构建应有未命中：{first_counts}",
    )

    cache_file = ctx.runtime.db_path.parent / "embedding-cache.sqlite"
    ctx.require(cache_file.is_file(), f"缓存文件不存在：{cache_file}")
    first_vectors = _vectors(ctx)
    ctx.require(first_vectors, "第一次构建没有写入向量")

    response = ctx.rest.post("/api/v1/builds")
    ctx.require(response.status_code == 202, f"第二次构建 HTTP {response.status_code}")
    second_id = response.json()["id"]
    second = _build(ctx, second_id)
    ctx.require(
        second.get("status") == "succeeded", f"第二次构建状态：{second.get('status')}"
    )
    second_counts = second.get("counts") or {}
    ctx.require(
        second_counts.get("embedding_cache_misses") == 0,
        f"第二次构建不应未命中缓存：{second_counts}",
    )
    ctx.require(
        second_counts.get("embedding_requests") == 0,
        f"第二次构建不应发出 embedding 请求：{second_counts}",
    )
    ctx.require(
        second_counts.get("embedding_cache_hits") == first_unique,
        f"第二次构建命中数与首次唯一文本数不符：{second_counts} != {first_unique}",
    )

    stages = _stage_counts(ctx, second)
    for stage, counts in stages.items():
        ctx.require(
            counts["embedding_cache_hits"] + counts["embedding_cache_misses"]
            == counts["embedding_unique"],
            f"阶段 {stage} 的命中与未命中之和不等于唯一文本数：{counts}",
        )
        ctx.require(
            counts["embedding_cache_misses"] == 0,
            f"阶段 {stage} 第二次构建仍有未命中：{counts}",
        )

    second_vectors = _vectors(ctx)
    ctx.require(
        set(first_vectors) == set(second_vectors),
        "两次构建的向量 id 集合不同："
        f"首次 {len(first_vectors)} 个，第二次 {len(second_vectors)} 个",
    )
    for identifier, vector in first_vectors.items():
        ctx.require(
            second_vectors[identifier] == vector,
            f"向量与第一次不一致：id={identifier}",
        )

    return {
        "first_requests": first_counts.get("embedding_requests"),
        "first_unique": first_unique,
        "first_misses": first_counts.get("embedding_cache_misses"),
        "second_requests": second_counts.get("embedding_requests"),
        "second_hits": second_counts.get("embedding_cache_hits"),
        "second_misses": second_counts.get("embedding_cache_misses"),
        "vectors": len(first_vectors),
        "stages": sorted(stages),
    }
