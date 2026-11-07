"""End-to-end checks for building vectors from deduplicated text (#118)."""

from __future__ import annotations

import re

COUNT = re.compile(
    r"(embedding_inputs|embedding_unique|embedding_cache_hits|embedding_cache_misses)=(\d+)"
)


def _stage_counts(build):
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
        if not {"embedding_inputs", "embedding_unique"} <= set(counts):
            raise AssertionError(
                f"阶段 {entry.get('stage')} 的 Completed 日志缺少去重计数：{message}"
            )
        stages[entry["stage"]] = counts
    if "table" not in stages:
        raise AssertionError(f"没有表阶段的 Completed 日志：{sorted(stages)}")
    return stages


def check_embedding_dedupe_counts(ctx):
    response = ctx.rest.get(f"/api/v1/builds/{ctx.build_id}")
    ctx.require(response.status_code == 200, f"构建详情 HTTP {response.status_code}")
    build = response.json()
    ctx.require(build.get("status") == "succeeded", f"构建状态：{build.get('status')}")
    stages = _stage_counts(build)
    table = stages["table"]
    ctx.require(
        table["embedding_inputs"] > table["embedding_unique"],
        "演示源包含重复文本，表阶段应去重："
        f"inputs={table['embedding_inputs']} unique={table['embedding_unique']}",
    )

    counts = build.get("counts") or {}
    total_inputs = sum(item["embedding_inputs"] for item in stages.values())
    total_unique = sum(item["embedding_unique"] for item in stages.values())
    ctx.require(
        counts.get("embedding_inputs") == total_inputs,
        f"构建 embedding_inputs 与阶段之和不符：{counts} != {total_inputs}",
    )
    ctx.require(
        counts.get("embedding_unique") == total_unique,
        f"构建 embedding_unique 与阶段之和不符：{counts} != {total_unique}",
    )
    ctx.require(
        total_inputs > total_unique,
        f"全构建应存在重复文本：inputs={total_inputs} unique={total_unique}",
    )

    hits = ctx.mcp_call(
        "search_knowledge",
        {"query": ctx.config.search_query, "scope": "docs", "top_k": 5},
    )
    ctx.require(isinstance(hits, list) and hits, "去重后文档检索没有结果")
    return {
        "table_inputs": table["embedding_inputs"],
        "table_unique": table["embedding_unique"],
        "total_inputs": total_inputs,
        "total_unique": total_unique,
        "stages": sorted(stages),
        "doc_hits": len(hits),
    }
