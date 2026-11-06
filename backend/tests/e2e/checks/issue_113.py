"""Check that internal field-lineage errors degrade to a task-level failure."""


def check_internal_error_degrades(ctx):
    ctx.require(ctx.build["status"] == "succeeded", f"构建失败：{ctx.build}")
    failures = ctx.build.get("parse_failures") or []
    bad = [item for item in failures if item.get("task_id") == "bad_union"]
    ctx.require(len(bad) == 1, f"异常任务未记入 parse_failures：{failures}")
    reason = bad[0].get("reason", "")
    ctx.require(
        reason.startswith("column_lineage_internal_error: "),
        f"失败原因类别错误：{reason!r}",
    )
    ctx.require("IndexError" in reason, f"失败原因缺少异常类型：{reason!r}")
    ctx.require(
        "SELECT" not in reason and "src." not in reason,
        f"失败原因泄露 SQL 原文：{reason!r}",
    )
    counts = ctx.build.get("counts") or {}
    ctx.require(
        counts.get("column_failures") == 1,
        f"字段级失败任务数错误：{counts!r}",
    )
    response = ctx.rest.get(f"/api/v1/builds/{ctx.build_id}")
    ctx.require(response.status_code == 200, response.text)
    task_logs = [
        entry["message"]
        for entry in response.json()["logs"]
        if entry["stage"] == "task"
    ]
    ctx.require(
        any("column_failures=1" in message for message in task_logs),
        f"task 阶段日志未汇总字段级失败任务数：{task_logs}",
    )

    for table in ("dst.orders", "dst.summary"):
        response = ctx.rest.get(
            "/api/v1/column-lineage",
            params={"table": table, "column": "order_id"},
        )
        ctx.require(response.status_code == 200, response.text)
        upstream = response.json()["upstream"]
        ctx.require(
            any(
                edge.get("upstream_table") == "src.orders"
                and edge.get("upstream_field") == "id"
                and edge.get("task_id") in ("good_orders", "good_summary")
                for edge in upstream
            ),
            f"{table} 缺少正常任务的字段血缘：{upstream}",
        )

    response = ctx.rest.get(
        "/api/v1/column-lineage", params={"table": "dst.merged", "column": "order_id"}
    )
    ctx.require(response.status_code == 200, response.text)
    ctx.require(
        response.json()["upstream"] == [],
        f"异常任务不应写入字段血缘：{response.json()}",
    )

    response = ctx.rest.get("/api/v1/table-lineage", params={"table": "dst.merged"})
    ctx.require(response.status_code == 200, response.text)
    table_edges = {
        (edge["upstream"], edge["downstream"], edge["task_id"])
        for edge in response.json()["upstream"]
    }
    ctx.require(
        table_edges
        == {
            ("src.orders", "dst.merged", "bad_union"),
            ("src.extra", "dst.merged", "bad_union"),
        },
        f"异常任务的表级血缘未保留：{table_edges}",
    )
    return {"parse_failures": len(failures), "table_edges": len(table_edges)}
