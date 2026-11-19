"""第 5～9 期端到端验收检查集（供 backend/tests/e2e/harness.py --checks 加载）。"""

from __future__ import annotations

import base64
import json
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
ACCEPTANCE_ROOT = REPO_ROOT / "eval" / "phase-5-9-acceptance"
OSSIE_SAMPLE_DIR = ACCEPTANCE_ROOT / "ossie-sample"


def _mcp_value(session: Any, name: str, arguments: dict[str, Any] | None = None) -> Any:
    result = session.call_tool(name, arguments or {})
    if getattr(result, "isError", False):
        body = getattr(result, "structuredContent", None) or {}
        raise AssertionError(
            f"MCP 工具 {name} 失败: {body.get('code')}: {body.get('message')}"
        )
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured.get("result", structured)
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return None


def _create_project(ctx: Any, name: str, project_type: str) -> dict[str, Any]:
    resp = ctx.rest.post(
        "/api/v1/projects",
        json={"name": name, "type": project_type},
    )
    ctx.require(
        resp.status_code == 201,
        f"创建项目 {name} ({project_type}) 失败: {resp.status_code} {resp.text[:300]}",
    )
    return resp.json()


def _cleanup_project(ctx: Any, project_id: str, project_name: str) -> None:
    try:
        arch = ctx.rest.post(f"/api/v1/projects/{project_id}/archive")
        if arch.status_code not in (200, 404):
            return
        if arch.status_code == 200:
            ctx.rest.post(
                f"/api/v1/projects/{project_id}/delete",
                json={"name": project_name},
            )
    except Exception as exc:  # noqa: BLE001 - best-effort cleanup in finally
        if getattr(ctx, "logger", None) is not None:
            ctx.logger.log(f"cleanup {project_id} failed: {exc}")


def _upload_files(
    ctx: Any,
    project_id: str,
    target: str,
    files: dict[str, bytes],
) -> None:
    resp = ctx.rest.post(
        "/api/v1/source/upload",
        params={"project": project_id},
        json={
            "target": target,
            "files": [
                {"path": rel, "content_base64": base64.b64encode(raw).decode()}
                for rel, raw in files.items()
            ],
        },
    )
    ctx.require(
        resp.status_code == 200,
        f"上传 {project_id}:{target} 失败: {resp.status_code} {resp.text[:300]}",
    )


def _trigger_and_wait_build(
    ctx: Any, project_id: str, build_type: str = "full"
) -> dict[str, Any]:
    resp = ctx.rest.post(
        "/api/v1/builds",
        params={"project": project_id},
        json={"build_type": build_type},
    )
    ctx.require(
        resp.status_code == 202,
        f"触发项目 {project_id} 构建失败: {resp.status_code} {resp.text[:300]}",
    )
    build_id = resp.json()["id"]
    deadline = time.monotonic() + ctx.config.build_timeout
    while time.monotonic() < deadline:
        detail_resp = ctx.rest.get(
            f"/api/v1/builds/{build_id}",
            params={"project": project_id},
        )
        ctx.require(
            detail_resp.status_code == 200,
            f"查询构建 {build_id} 失败: {detail_resp.status_code}",
        )
        detail = detail_resp.json()
        if detail.get("status") in {"succeeded", "failed", "cancelled"}:
            ctx.require(
                detail["status"] == "succeeded",
                f"项目 {project_id} 构建未成功: {detail.get('status')} {detail.get('error')}",
            )
            return detail
        time.sleep(ctx.config.poll_interval)
    raise AssertionError(f"项目 {project_id} 构建 {build_id} 超时")


def check_phase5_ossie_import_and_export(ctx: Any) -> dict[str, Any]:
    """第 5 期：Ossie YAML 导入三路分流、外键强连接推导与双格式导出 Schema 校验。"""
    from semantica_dataagent.ossie_source import validate_ossie_schema

    suffix = uuid.uuid4().hex[:8]
    proj_name = f"p5ossie{suffix}"
    proj = _create_project(ctx, proj_name, "warehouse")
    pid = proj["id"]
    try:
        enable = ctx.rest.put(
            f"/api/v1/projects/{pid}/adapters",
            json={
                "adapters": [
                    {
                        "id": "ossie",
                        "enabled": True,
                        "parameters": {
                            "path": "ossie",
                            "category": "ossie",
                            "strip_catalog": False,
                        },
                    },
                    {
                        "id": "markdown",
                        "enabled": True,
                        "parameters": {"path": "knowledge"},
                    },
                ]
            },
        )
        ctx.require(enable.status_code == 200, f"启用 ossie 适配器失败: {enable.text}")

        _upload_files(
            ctx,
            pid,
            "ossie",
            {
                "demo_commerce.yaml": (
                    OSSIE_SAMPLE_DIR / "ossie" / "demo_commerce.yaml"
                ).read_bytes()
            },
        )
        _upload_files(
            ctx,
            pid,
            "knowledge/metrics",
            {
                "gmv.md": (
                    OSSIE_SAMPLE_DIR / "knowledge" / "metrics" / "gmv.md"
                ).read_bytes()
            },
        )
        _upload_files(
            ctx,
            pid,
            "overrides",
            {
                "semantic_overrides.yaml": (
                    OSSIE_SAMPLE_DIR / "overrides" / "semantic_overrides.yaml"
                ).read_bytes()
            },
        )

        build = _trigger_and_wait_build(ctx, pid, "full")
        counts = build.get("counts") or {}
        ctx.require(counts.get("table", 0) >= 2, f"期望至少 2 张表，实际: {counts}")

        session = ctx.open_mcp(ctx.token, project=pid)
        tables = _mcp_value(
            session,
            "describe_tables",
            {"names": ["hive_prod.dwd.dwd_orders", "hive_prod.dim.dim_city"]},
        )
        orders_info = next(
            (t for t in tables if t.get("name") == "dwd_orders"),
            None,
        )
        ctx.require(orders_info is not None, f"未找到 dwd_orders: {tables}")
        dt_field = next(
            (f for f in orders_info.get("fields", []) if f.get("name") == "dt"),
            None,
        )
        ctx.require(
            dt_field is not None and dt_field.get("partition") is True,
            f"Ossie 分区扩展属性未生效: {dt_field}",
        )

        join_res = _mcp_value(
            session,
            "find_join_path",
            {"tables": ["hive_prod.dwd.dwd_orders", "hive_prod.dim.dim_city"]},
        )
        ctx.require(join_res.get("status") == "found", f"未找到外键路径: {join_res}")
        steps = join_res.get("steps") or []
        ctx.require(
            steps and steps[0].get("strength") == "strong",
            f"期望外键连接 strength=strong，实际: {join_res}",
        )

        sql_res = _mcp_value(
            session,
            "validate_sql",
            {
                "sql": (
                    "SELECT c.region_name, SUM(o.pay_amount_cny - o.refund_amount_cny) "
                    "FROM hive_prod.dwd.dwd_orders o "
                    "JOIN hive_prod.dim.dim_city c ON o.city_code = c.city_code "
                    "WHERE o.dt = '2026-11-18' GROUP BY c.region_name"
                ),
                "dialect": "hive",
            },
        )
        ctx.require(sql_res.get("status") == "pass", f"validate_sql 未通过: {sql_res}")

        yaml_resp = ctx.rest.get(f"/api/v1/projects/{pid}/export/ossie?format=yaml")
        ctx.require(yaml_resp.status_code == 200, f"YAML 导出失败: {yaml_resp.text}")
        yaml_doc = yaml.safe_load(yaml_resp.text)
        validate_ossie_schema(yaml_doc, context="export_ossie_yaml")

        json_resp = ctx.rest.get(f"/api/v1/projects/{pid}/export/ossie?format=json")
        ctx.require(json_resp.status_code == 200, f"JSON 导出失败: {json_resp.text}")
        json_doc = json.loads(json_resp.text)
        validate_ossie_schema(json_doc, context="export_ossie_json")

        return {
            "project_id": pid,
            "datasets": len(json_doc.get("datasets", [])),
            "relationships": len(json_doc.get("relationships", [])),
            "join_strength": steps[0].get("strength"),
        }
    finally:
        _cleanup_project(ctx, pid, proj_name)


def check_phase6_dtb_to_ossie_conversion(ctx: Any) -> dict[str, Any]:
    """第 6 期：convert-dtb-ossie 等值 JOIN 外键、主键推导与混合维度落盘验证。"""
    from openpyxl import Workbook

    from semantica_dataagent.cli import main as cli_main
    from semantica_dataagent.ossie_source import validate_ossie_schema

    work = Path(ctx.work_dir) / f"phase6-dtb-{uuid.uuid4().hex[:6]}"
    snap = work / "src" / "dtb" / "2026-11-18"
    snap.mkdir(parents=True, exist_ok=True)

    headers = [
        "table_id",
        "table_name",
        "database_name",
        "comment",
        "output_task_id",
        "output_task_name",
        "column_name",
        "seq_no",
        "column_type",
        "column_comment",
        "column_business_comment",
    ]
    rows = [
        [
            1,
            "dwd_orders",
            "dwd",
            "订单宽表",
            "10",
            "etl_orders",
            "order_id",
            1,
            "bigint",
            "订单ID",
            "",
        ],
        [
            1,
            "dwd_orders",
            "dwd",
            "订单宽表",
            "10",
            "etl_orders",
            "city_code",
            2,
            "string",
            "城市编码",
            "",
        ],
        [
            1,
            "dwd_orders",
            "dwd",
            "订单宽表",
            "10",
            "etl_orders",
            "channel_id",
            3,
            "string",
            "渠道ID",
            "",
        ],
        [
            1,
            "dwd_orders",
            "dwd",
            "订单宽表",
            "10",
            "etl_orders",
            "amount",
            4,
            "decimal(18,2)",
            "实付金额",
            "",
        ],
        [
            1,
            "dwd_orders",
            "dwd",
            "订单宽表",
            "10",
            "etl_orders",
            "dt",
            10001,
            "string",
            "分区日期",
            "",
        ],
        [
            2,
            "dim_city",
            "dimensions",
            "城市维表",
            "",
            "",
            "city_code",
            1,
            "string",
            "城市编码",
            "",
        ],
        [
            2,
            "dim_city",
            "dimensions",
            "城市维表",
            "",
            "",
            "city_name",
            2,
            "string",
            "城市名",
            "",
        ],
        [
            3,
            "dim_channel",
            "dimensions",
            "渠道维表",
            "",
            "",
            "channel_id",
            1,
            "string",
            "渠道ID",
            "",
        ],
        [
            3,
            "dim_channel",
            "dimensions",
            "渠道维表",
            "",
            "",
            "channel_name",
            2,
            "string",
            "渠道名",
            "",
        ],
    ]
    book = Workbook()
    book.active.append(headers)
    for row in rows:
        book.active.append(row)
    book.save(snap / "表信息.xlsx")
    book.close()

    task_cols = [
        "task_id",
        "task_name",
        "task_type",
        "task_src",
        "task_conf",
        "project_name",
        "upstream_tasks",
        "output_table_name",
    ]
    task_sql = (
        "INSERT OVERWRITE TABLE dwd.dwd_orders PARTITION(dt='${dt}') "
        "SELECT o.order_id, o.city_code, o.channel_id, SUM(o.amount) AS amount "
        "FROM ods.raw_orders o "
        "GROUP BY o.order_id, o.city_code, o.channel_id"
    )
    join_sql = (
        "SELECT o.order_id, c.city_name, ch.channel_name, o.amount "
        "FROM dwd.dwd_orders o "
        "JOIN dimensions.dim_city c ON o.city_code = c.city_code "
        "JOIN dimensions.dim_channel ch ON o.channel_id = ch.channel_id "
        "WHERE o.dt = '2026-11-18'"
    )
    (snap / "任务信息.json").write_text(
        json.dumps(
            {
                "columns": [{"name": col} for col in task_cols],
                "data": [
                    [
                        "10",
                        "etl_orders",
                        "spark",
                        task_sql,
                        None,
                        "commerce",
                        "",
                        "dwd.dwd_orders",
                    ],
                    [
                        "11",
                        "report_orders",
                        "spark",
                        join_sql,
                        None,
                        "commerce",
                        "10",
                        "",
                    ],
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # Low-cardinality dim_channel (4 rows <= limit 20) & high-cardinality dim_city (25 rows > limit 20)
    dim_root = work / "src" / "dimensions"
    for tbl, cols, data_rows in [
        (
            "dim_channel",
            ["channel_id", "channel_name"],
            [
                {"channel_id": "app", "channel_name": "移动 App", "row_id": "1"},
                {"channel_id": "web", "channel_name": "官网 Web", "row_id": "2"},
                {"channel_id": "mini", "channel_name": "微信小程序", "row_id": "3"},
                {"channel_id": "offline", "channel_name": "线下门店", "row_id": "4"},
            ],
        ),
        (
            "dim_city",
            ["city_code", "city_name"],
            [
                {
                    "city_code": f"C{i:02d}",
                    "city_name": f"城市_{i:02d}",
                    "row_id": str(i),
                }
                for i in range(1, 26)
            ],
        ),
    ]:
        tdir = dim_root / tbl
        (tdir / "current").mkdir(parents=True, exist_ok=True)
        (tdir / "meta.json").write_text(
            json.dumps(
                {
                    "database": "dimensions",
                    "table": tbl,
                    "qualified_table": f"dimensions.{tbl}",
                    "columns": [{"name": col} for col in cols],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (tdir / "current" / "rows.jsonl").write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in data_rows),
            encoding="utf-8",
        )

    out_dir = work / "out"
    code = cli_main(
        [
            "convert-dtb-ossie",
            "--source-dir",
            str(work / "src"),
            "--output-dir",
            str(out_dir),
            "--low-cardinality-limit",
            "20",
            "--sample-limit",
            "5",
        ]
    )
    ctx.require(code == 0, f"convert-dtb-ossie 返回非零退出码: {code}")

    model_path = out_dir / "ossie" / "dtb_semantic_model.yaml"
    dim_path = out_dir / "dimension_values.json"
    ctx.require(model_path.is_file(), f"缺少输出 Ossie 文件: {model_path}")
    ctx.require(dim_path.is_file(), f"高基数维度未导出至 {dim_path}")

    doc = yaml.safe_load(model_path.read_text(encoding="utf-8"))
    validate_ossie_schema(doc, context="convert_dtb_ossie")
    dim_rows = json.loads(dim_path.read_text(encoding="utf-8"))
    ctx.require(
        isinstance(dim_rows, list) and len(dim_rows) >= 25,
        f"期望至少 25 条高基数维度记录，实际: {len(dim_rows)}",
    )
    ctx.require(
        all("row_id" in row for row in dim_rows),
        "dimension_values.json 缺少同行 row_id 标识",
    )

    by_source = {ds["source"]: ds for ds in doc.get("datasets", [])}
    ctx.require(
        "dwd.dwd_orders" in by_source, f"缺少 dwd.dwd_orders: {list(by_source)}"
    )
    ctx.require(
        len(doc.get("relationships", [])) >= 2,
        f"期望从任务 SQL 提取至少 2 条外键关系，实际: {doc.get('relationships')}",
    )
    return {
        "datasets": len(doc.get("datasets", [])),
        "relationships": len(doc.get("relationships", [])),
        "high_cardinality_rows": len(dim_rows),
    }


def check_phase7_project_isolation_and_lifecycle(ctx: Any) -> dict[str, Any]:
    """第 7 期：文档型与数仓型项目能力边界、归档冻结 409 与恢复、永久删除。"""
    from semantica_dataagent.project_types import DOCUMENT_TOOL_NAMES

    suffix = uuid.uuid4().hex[:8]
    doc_name = f"p7doc{suffix}"
    doc_proj = _create_project(ctx, doc_name, "document")
    doc_id = doc_proj["id"]
    try:
        ossie_resp = ctx.rest.get(f"/api/v1/projects/{doc_id}/export/ossie")
        ctx.require(
            ossie_resp.status_code == 422,
            f"文档型项目导出 Ossie 期望 422，实际 {ossie_resp.status_code}",
        )
        disc_resp = ctx.rest.get(f"/api/v1/projects/{doc_id}/discovery-config")
        ctx.require(
            disc_resp.status_code == 422,
            f"文档型项目获取发现配置期望 422，实际 {disc_resp.status_code}",
        )

        session = ctx.open_mcp(ctx.token, project=doc_id)
        caps = _mcp_value(session, "get_capabilities", {})
        tool_set = set(caps.get("tools", []))
        ctx.require(
            tool_set == set(DOCUMENT_TOOL_NAMES),
            f"文档型项目工具集不符: {sorted(tool_set)} != {sorted(DOCUMENT_TOOL_NAMES)}",
        )
        ctx.require(
            "propose_query" not in tool_set and "describe_tables" not in tool_set,
            f"文档型项目不得暴露 SQL/取数工具: {sorted(tool_set)}",
        )

        arch = ctx.rest.post(f"/api/v1/projects/{doc_id}/archive")
        ctx.require(
            arch.status_code == 200 and arch.json()["status"] == "archived", arch.text
        )
        frozen_put = ctx.rest.put(
            f"/api/v1/projects/{doc_id}/adapters",
            json={"adapters": [{"id": "markdown", "enabled": True, "parameters": {}}]},
        )
        ctx.require(
            frozen_put.status_code == 409,
            f"归档项目写配置期望 409，实际 {frozen_put.status_code}",
        )

        restored = ctx.rest.post(f"/api/v1/projects/{doc_id}/restore")
        ctx.require(
            restored.status_code == 200 and restored.json()["status"] == "active",
            restored.text,
        )
        return {"doc_project": doc_id, "document_tools": len(tool_set)}
    finally:
        _cleanup_project(ctx, doc_id, doc_name)


def check_phase8_overrides_eval_and_evolution_apis(ctx: Any) -> dict[str, Any]:
    """第 8 期：语义覆盖层校验、评测题集 dev/holdout、演化配置与纠错草稿 L1 门禁。"""
    from semantica_dataagent.semantic_overrides import (
        load_overrides_file,
        run_l1_gate,
        validate_override_document,
    )

    ovr_doc, _ = load_overrides_file(OSSIE_SAMPLE_DIR)
    ovr_errs = validate_override_document(ovr_doc)
    ctx.require(not ovr_errs, f"示例 semantic_overrides.yaml 校验失败: {ovr_errs}")
    gate = run_l1_gate(
        target="table:hive_prod.dwd.dwd_orders",
        patch="",
        reason="验收测试语义覆盖层",
        structured_patch=ovr_doc,
        target_kind="override",
        project_id="default",
    )
    ctx.require(gate.passed, f"示例覆盖层未通过 L1 校验: {gate.errors}")

    draft_resp = ctx.rest.post(
        "/api/v1/corrections/draft",
        json={
            "target": f"table:{ctx.expect_table}",
            "category": "caliber",
            "description": "验收草稿：按 dt 分区筛选订单并去重计数。",
        },
    )
    ctx.require(draft_resp.status_code == 200, f"生成纠错草稿失败: {draft_resp.text}")
    draft_data = draft_resp.json()
    ctx.require(
        draft_data.get("gate_report", {}).get("passed") is True,
        f"合法草稿 L1 校验未通过: {draft_data}",
    )

    suffix = uuid.uuid4().hex[:8]
    proj_name = f"p8evo{suffix}"
    proj = _create_project(ctx, proj_name, "warehouse")
    pid = proj["id"]
    try:
        evo_get = ctx.rest.get(f"/api/v1/projects/{pid}/evolution-config")
        ctx.require(evo_get.status_code == 200, evo_get.text)
        evo_put = ctx.rest.put(
            f"/api/v1/projects/{pid}/evolution-config",
            json={"max_rounds": 4, "max_eval_cases": 15},
        )
        ctx.require(
            evo_put.status_code == 200 and evo_put.json()["max_rounds"] == 4,
            evo_put.text,
        )

        pol_put = ctx.rest.put(
            f"/api/v1/projects/{pid}/correction-policy",
            json={"correction_require_review": True, "evolution_require_review": False},
        )
        ctx.require(
            pol_put.status_code == 200
            and pol_put.json()["evolution_require_review"] is False,
            pol_put.text,
        )

        q_dev = ctx.rest.post(
            "/api/v1/eval-questions",
            params={"project": pid},
            json={
                "question": "统计每日有效订单数",
                "caliber": "按 dt 分组统计去重 order_id",
                "reference_sql": "SELECT dt, COUNT(DISTINCT order_id) FROM hive_prod.dwd.dwd_orders WHERE dt = '2026-11-18' GROUP BY dt",
                "dialect": "hive",
                "split": "dev",
                "answer_confirmed": True,
            },
        )
        ctx.require(q_dev.status_code == 201, f"创建 dev 评测题失败: {q_dev.text}")
        q_holdout = ctx.rest.post(
            "/api/v1/eval-questions",
            params={"project": pid},
            json={
                "question": "统计各城市净销售额",
                "caliber": "实付减退款按城市汇总",
                "reference_sql": "SELECT city_code, SUM(pay_amount_cny - refund_amount_cny) FROM hive_prod.dwd.dwd_orders WHERE dt = '2026-11-18' GROUP BY city_code",
                "dialect": "hive",
                "split": "holdout",
                "answer_confirmed": True,
            },
        )
        ctx.require(
            q_holdout.status_code == 201, f"创建 holdout 评测题失败: {q_holdout.text}"
        )

        listed = ctx.rest.get("/api/v1/eval-questions", params={"project": pid})
        ctx.require(listed.status_code == 200 and len(listed.json()) == 2, listed.text)
        for qid in (q_dev.json()["id"], q_holdout.json()["id"]):
            d_resp = ctx.rest.delete(
                f"/api/v1/eval-questions/{qid}", params={"project": pid}
            )
            ctx.require(d_resp.status_code == 204, f"删除评测题失败: {d_resp.text}")

        return {"project_id": pid, "draft_target": draft_data.get("target")}
    finally:
        _cleanup_project(ctx, pid, proj_name)


def check_phase9_discovery_config_and_guardrails(ctx: Any) -> dict[str, Any]:
    """第 9 期：发现配置双预设与防注入校验、未配 DTB 令牌拦截、discovery 来源防伪造。"""
    suffix = uuid.uuid4().hex[:8]
    proj_name = f"p9disc{suffix}"
    proj = _create_project(ctx, proj_name, "warehouse")
    pid = proj["id"]
    try:
        cfg_resp = ctx.rest.get(f"/api/v1/projects/{pid}/discovery-config")
        ctx.require(cfg_resp.status_code == 200, cfg_resp.text)
        cfg = cfg_resp.json()
        ctx.require(
            set(cfg.get("presets", {})) == {"doris_audit_log", "dtb_tracking_table"},
            f"发现配置缺少预设模板: {cfg}",
        )

        switched = ctx.rest.put(
            f"/api/v1/projects/{pid}/discovery-config",
            json={
                "preset": "dtb_tracking_table",
                "source_table": "mysql.data_tool_box_emr.query_history",
                "sql_column": "query_sql",
                "status_column": "status",
                "time_column": "create_time",
                "success_statuses": ["SUCCESS", "FINISHED"],
            },
        )
        ctx.require(
            switched.status_code == 200
            and switched.json()["preset"] == "dtb_tracking_table",
            f"切换发现预设失败: {switched.text}",
        )

        bad_ident = ctx.rest.put(
            f"/api/v1/projects/{pid}/discovery-config",
            json={"source_table": "__internal_schema.audit_log; DROP TABLE x"},
        )
        ctx.require(
            bad_ident.status_code == 422,
            f"非法 source_table 期望返回 422，实际 {bad_ident.status_code}",
        )

        dtb_state = ctx.rest.get("/api/v1/me/dtb-token")
        ctx.require(dtb_state.status_code == 200, dtb_state.text)
        if not dtb_state.json().get("configured"):
            missing_tok = ctx.rest.post(
                "/api/v1/discovery/runs",
                params={"project": pid},
                json={"mine_query_history": True, "probe_relationships": True},
            )
            ctx.require(
                missing_tok.status_code == 400,
                f"未配置个人 DTB 令牌时期望返回 400，实际 {missing_tok.status_code}: {missing_tok.text}",
            )

        spoof = ctx.rest.post(
            "/api/v1/corrections",
            params={"project": pid},
            json={
                "target": "table:hive_prod.dwd.dwd_orders",
                "patch": "{}",
                "reason": "attempt spoof discovery source",
                "source": "discovery",
            },
        )
        ctx.require(
            spoof.status_code == 422,
            f"外部请求伪造 source='discovery' 期望返回 422，实际 {spoof.status_code}",
        )
        return {"project_id": pid, "presets": sorted(cfg.get("presets", {}))}
    finally:
        _cleanup_project(ctx, pid, proj_name)
