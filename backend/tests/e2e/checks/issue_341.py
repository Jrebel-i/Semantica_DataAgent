"""Approval rejects multi-catalog ambiguity (#341)."""

from __future__ import annotations

import base64
import json
import time


def _wait_build(ctx, project_id, build_id):
    deadline = time.monotonic() + ctx.config.build_timeout
    while time.monotonic() < deadline:
        response = ctx.rest.get(
            f"/api/v1/builds/{build_id}", params={"project": project_id}
        )
        ctx.require(
            response.status_code == 200,
            f"构建 {build_id} HTTP {response.status_code}: {response.text[:300]}",
        )
        build = response.json()
        if build.get("status") in {"succeeded", "failed", "cancelled"}:
            ctx.require(
                build["status"] == "succeeded",
                f"构建 {build_id} 失败: {build.get('error')}",
            )
            return build
        time.sleep(ctx.config.poll_interval)
    raise AssertionError(f"构建 {build_id} 超时")


def _wait_idle(ctx, project_id):
    deadline = time.monotonic() + ctx.config.build_timeout
    while time.monotonic() < deadline:
        rows = ctx.rest.get("/api/v1/builds", params={"project": project_id}).json()
        if rows and all(row.get("status") not in {"queued", "running"} for row in rows):
            ctx.require(
                all(row.get("status") == "succeeded" for row in rows),
                f"初始构建失败: {rows}",
            )
            return rows
        time.sleep(ctx.config.poll_interval)
    raise AssertionError(f"项目 {project_id} 的初始构建超时")


def _incremental_count(ctx, project_id):
    rows = ctx.rest.get("/api/v1/builds", params={"project": project_id}).json()
    return len([row for row in rows if row.get("build_type") == "incremental"])


def _wait_incremental(ctx, project_id, minimum):
    deadline = time.monotonic() + ctx.config.build_timeout
    while time.monotonic() < deadline:
        rows = ctx.rest.get("/api/v1/builds", params={"project": project_id}).json()
        increments = [row for row in rows if row.get("build_type") == "incremental"]
        if len(increments) >= minimum and all(
            row.get("status") not in {"queued", "running"} for row in increments
        ):
            ctx.require(
                all(row.get("status") == "succeeded" for row in increments),
                f"增量构建失败: {increments}",
            )
            return increments
        time.sleep(ctx.config.poll_interval)
    raise AssertionError(f"项目 {project_id} 未出现第 {minimum} 个成功的增量构建")


def check_issue_341_ambiguous_approval(ctx):
    import yaml

    created = ctx.rest.post(
        "/api/v1/projects", json={"name": f"i341 {ctx.generation}", "type": "warehouse"}
    )
    ctx.require(created.status_code == 201, created.text)
    project = created.json()["id"]
    tables = [
        {"catalog": cat, "database": "shop", "name": "orders", "comment": "original"}
        for cat in ("hive", "internal")
    ]
    fields = [
        {
            "catalog": cat,
            "database": "shop",
            "table": "orders",
            "name": "id",
            "type": "bigint",
            "comment": "id",
            "partition": False,
        }
        for cat in ("hive", "internal")
    ]
    uploaded = ctx.rest.post(
        "/api/v1/source/upload",
        params={"project": project},
        json={
            "target": "canonical",
            "files": [
                {
                    "path": name + ".json",
                    "content_base64": base64.b64encode(
                        json.dumps(data).encode()
                    ).decode(),
                }
                for name, data in (("tables", tables), ("fields", fields))
            ],
        },
    )
    ctx.require(uploaded.status_code == 200, uploaded.text)
    _wait_idle(ctx, project)
    full = ctx.rest.post(
        "/api/v1/builds", params={"project": project}, json={"build_type": "full"}
    )
    ctx.require(full.status_code == 202, full.text)
    _wait_build(ctx, project, full.json()["id"])
    seed = {
        "version": 1,
        "tables": {
            "hive.shop.orders": {
                "catalog": "hive",
                "database": "shop",
                "name": "orders",
                "comment": "seed",
            }
        },
    }
    proposal = ctx.rest.post(
        "/api/v1/corrections",
        params={"project": project},
        json={
            "target": "table:hive.shop.orders",
            "reason": "i341 verification",
            "structured_patch": seed,
        },
    )
    ctx.require(proposal.status_code == 201, proposal.text)
    pid = proposal.json()["id"]
    rel = {
        "name": "r",
        "from_catalog": "hive",
        "from_database": "shop",
        "from_table": "orders",
        "from_columns": ["id"],
        "to_catalog": "hive",
        "to_database": "shop",
        "to_table": "orders",
        "to_columns": ["id"],
    }
    from_rel = {k: v for k, v in rel.items() if k != "from_catalog"}
    to_rel = {k: v for k, v in rel.items() if k != "to_catalog"}
    cases = [
        {
            "tables": {
                "shop.orders": {"database": "shop", "name": "orders", "comment": "x"}
            }
        },
        {
            "fields": {
                "shop.orders.id": {
                    "database": "shop",
                    "table": "orders",
                    "name": "id",
                    "comment": "x",
                }
            }
        },
        {"relationships": {"r": from_rel}},
        {"relationships": {"r": to_rel}},
        {
            "metrics": {
                "r": {"name": "r", "expression": "SUM(id)", "tables": ["shop.orders"]}
            }
        },
    ]
    for case in cases:
        response = ctx.rest.post(
            f"/api/v1/corrections/{pid}/approve",
            params={"project": project},
            json={"patch": yaml.safe_dump({"version": 1, **case})},
        )
        ctx.require(
            response.status_code == 422
            and "hive.shop.orders" in response.text
            and "internal.shop.orders" in response.text,
            f"{case}: {response.status_code} {response.text}",
        )
    increments = _incremental_count(ctx, project)
    approved = ctx.rest.post(
        f"/api/v1/corrections/{pid}/approve", params={"project": project}
    )
    ctx.require(approved.status_code == 200, approved.text)
    _wait_incremental(ctx, project, increments + 1)
    response = ctx.rest.get(
        "/api/v1/tables", params={"project": project, "names": "shop.orders"}
    )
    ctx.require(response.status_code == 200, response.text)
    rows = {row.get("catalog"): row for row in response.json()}
    ctx.require(
        rows.get("hive", {}).get("comment") == "seed", f"hive override missing: {rows}"
    )
    ctx.require(
        rows.get("internal", {}).get("comment") == "original",
        f"internal override changed: {rows}",
    )
    return {"project": project, "cases": len(cases)}
