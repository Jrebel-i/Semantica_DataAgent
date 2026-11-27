"""Override corrections deep-merge table and field attributes (#336).

The check creates an isolated warehouse project, uploads canonical table/field
metadata, then approves a baseline override followed by a patch that updates
only one table attribute and one field attribute.  The approval path must keep
the untouched attributes in YAML and in the incrementally built REST records.
"""

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


def _upload(ctx, project_id):
    tables = [
        {
            "database": "issue336",
            "name": "orders",
            "comment": "原始订单表",
        }
    ]
    fields = [
        {
            "database": "issue336",
            "table": "orders",
            "name": "amount",
            "type": "decimal(18,2)",
            "comment": "原始金额",
            "partition": False,
        }
    ]
    response = ctx.rest.post(
        "/api/v1/source/upload",
        params={"project": project_id},
        json={
            "target": "canonical",
            "files": [
                {
                    "path": "tables.json",
                    "content_base64": base64.b64encode(
                        json.dumps(tables).encode()
                    ).decode(),
                },
                {
                    "path": "fields.json",
                    "content_base64": base64.b64encode(
                        json.dumps(fields).encode()
                    ).decode(),
                },
            ],
        },
    )
    ctx.require(response.status_code == 200, response.text[:300])


def _propose(ctx, project_id, patch):
    response = ctx.rest.post(
        "/api/v1/corrections",
        params={"project": project_id},
        json={
            "target": "table:issue336.orders",
            "reason": "issue 336 深合并验证",
            "structured_patch": patch,
        },
    )
    ctx.require(response.status_code == 201, response.text[:300])
    proposal = response.json()
    ctx.require(proposal["status"] == "pending", f"提案未待审: {proposal}")
    approved = ctx.rest.post(
        f"/api/v1/corrections/{proposal['id']}/approve",
        params={"project": project_id},
    )
    ctx.require(approved.status_code == 200, approved.text[:300])
    ctx.require(approved.json()["status"] == "approved", approved.text[:300])
    return proposal


def check_issue_336_deep_merge_approval_and_incremental_build(ctx):
    import yaml

    suffix = ctx.generation[-8:]
    created = ctx.rest.post(
        "/api/v1/projects",
        json={"name": f"Issue 336 {suffix}", "type": "warehouse"},
    )
    ctx.require(created.status_code == 201, created.text[:300])
    project_id = created.json()["id"]

    _upload(ctx, project_id)
    _wait_idle(ctx, project_id)
    full = ctx.rest.post(
        "/api/v1/builds",
        params={"project": project_id},
        json={"build_type": "full"},
    )
    ctx.require(full.status_code == 202, full.text[:300])
    _wait_build(ctx, project_id, full.json()["id"])

    baseline_increments = _incremental_count(ctx, project_id)
    seed = _propose(
        ctx,
        project_id,
        {
            "version": 1,
            "tables": {
                "issue336.orders": {
                    "database": "issue336",
                    "name": "orders",
                    "comment": "基础表说明",
                    "synonyms": ["表同义词"],
                    "instructions": "基础表约束",
                }
            },
            "fields": {
                "issue336.orders.amount": {
                    "database": "issue336",
                    "table": "orders",
                    "name": "amount",
                    "comment": "基础字段说明",
                    "synonyms": ["字段同义词"],
                    "instructions": "基础字段约束",
                }
            },
        },
    )
    _wait_incremental(ctx, project_id, baseline_increments + 1)

    patch = _propose(
        ctx,
        project_id,
        {
            "version": 1,
            "tables": {
                "issue336.orders": {
                    "database": "issue336",
                    "name": "orders",
                    "comment": "只改表说明",
                }
            },
            "fields": {
                "issue336.orders.amount": {
                    "database": "issue336",
                    "table": "orders",
                    "name": "amount",
                    "instructions": "只改字段约束",
                }
            },
        },
    )

    source = ctx.work_dir / "projects" / project_id / "source"
    written = yaml.safe_load(
        (source / "overrides" / "semantic_overrides.yaml").read_text(encoding="utf-8")
    )
    ctx.require(
        written["tables"]["issue336.orders"]
        == {
            "database": "issue336",
            "name": "orders",
            "comment": "只改表说明",
            "synonyms": ["表同义词"],
            "instructions": "基础表约束",
        },
        f"YAML 表条目丢失属性: {written['tables']['issue336.orders']}",
    )
    ctx.require(
        written["fields"]["issue336.orders.amount"]
        == {
            "database": "issue336",
            "table": "orders",
            "name": "amount",
            "comment": "基础字段说明",
            "synonyms": ["字段同义词"],
            "instructions": "只改字段约束",
        },
        f"YAML 字段条目丢失属性: {written['fields']['issue336.orders.amount']}",
    )

    _wait_incremental(ctx, project_id, baseline_increments + 2)
    response = ctx.rest.get(
        "/api/v1/tables",
        params={"project": project_id, "names": "issue336.orders"},
    )
    ctx.require(response.status_code == 200, response.text[:300])
    rows = response.json()
    ctx.require(rows, "增量构建后未找到 issue336.orders")
    table = rows[0]
    ctx.require(table.get("comment") == "只改表说明", f"表说明丢失: {table}")
    ctx.require(table.get("synonyms") == ["表同义词"], f"表同义词丢失: {table}")
    ctx.require(table.get("instructions") == "基础表约束", f"表约束丢失: {table}")
    amount = next(
        (field for field in table.get("fields", []) if field.get("name") == "amount"),
        None,
    )
    ctx.require(amount is not None, f"表记录缺少 amount 字段: {table}")
    ctx.require(amount.get("comment") == "基础字段说明", f"字段说明丢失: {amount}")
    ctx.require(amount.get("synonyms") == ["字段同义词"], f"字段同义词丢失: {amount}")
    ctx.require(
        amount.get("instructions") == "只改字段约束",
        f"字段约束丢失: {amount}",
    )
    return {
        "project_id": project_id,
        "seed_proposal_id": seed["id"],
        "deep_merge_proposal_id": patch["id"],
    }
