"""Tests for DTB to Apache Ossie conversion and CLI command (#277)."""

from __future__ import annotations

from semantica_dataagent.canonical import Record


def _task(task_type: str, sql: str, parameters: dict | None = None, name: str = "t"):
    data: dict = {"name": name, "type": task_type, "sql": sql}
    if parameters is not None:
        data["parameters"] = parameters
    return Record("task", data)


def test_extract_task_sql_statements_expands_shell_and_preserves_sql_dialects():
    from semantica_dataagent.dtb_tasks import DtbTaskParser, extract_task_sql_statements

    livy = r"""#!/bin/sh
src_db="ods"
src_tbl="orders_raw"
dst_db="dwd"
dst_tbl="orders_d"
tmp_view="CREATE TEMPORARY VIEW v_orders USING org.apache.spark.sql.jdbc OPTIONS (dbtable '\`ods\`.\`orders_raw\`')"
load_sql="SET hive.exec.dynamic.partition=true"
ins_sql="INSERT OVERWRITE TABLE ${dst_db}.${dst_tbl} PARTITION(dt='${day_num}') SELECT o.id, o.cid FROM v_orders o"
submit_sql_and_wait "${tmp_view}"
submit_sql_and_wait "${load_sql}"
submit_sql_and_wait "${ins_sql}"
"""
    beeline = """#!/bin/sh
dim_db="dimensions"
beeline -u jdbc:hive2://host:10000 -e "SELECT a.id FROM ${dim_db}.dim_city a WHERE a.dt = '${bizdate}'"
"""

    livy_stmts = extract_task_sql_statements(_task("shell", livy))
    assert len(livy_stmts) == 1
    sql_text, dialect = livy_stmts[0]
    assert dialect == "spark"
    assert "dwd.orders_d" in sql_text
    assert "ods.orders_raw" in sql_text
    assert "SELECT" in sql_text.upper()

    beeline_stmts = extract_task_sql_statements(_task("shell", beeline))
    assert len(beeline_stmts) == 1
    assert "dimensions.dim_city" in beeline_stmts[0][0]
    assert "${bizdate}" not in beeline_stmts[0][0]

    assert extract_task_sql_statements(
        _task("spark", "INSERT INTO dwd.a SELECT id FROM ods.b WHERE dt = '${dt}'")
    ) == [("INSERT INTO dwd.a SELECT id FROM ods.b WHERE dt = '0'", "spark")]
    assert extract_task_sql_statements(_task("sql", "SELECT 1 FROM dwd.a")) == [
        ("SELECT 1 FROM dwd.a", "hive")
    ]
    assert (
        extract_task_sql_statements(
            _task("merge_onself", "", {"params": {"table": "dwd.a"}})
        )
        == []
    )

    # Existing DtbTaskParser behavior stays unchanged.
    parsed = DtbTaskParser().parse(_task("shell", livy), {})
    assert parsed.status == "success"
    assert parsed.edges == [("ods.orders_raw", "dwd.orders_d")]


def _write_dtb_snapshot(
    root,
    *,
    table_rows: list[list],
    task_rows: list[list],
    task_columns: list[str] | None = None,
    table_headers: list[str] | None = None,
    dimensions: dict[str, tuple[list[str], list[dict]]] | None = None,
):
    import json

    from openpyxl import Workbook

    snap = root / "dtb" / "2026-11-18"
    snap.mkdir(parents=True, exist_ok=True)
    headers = table_headers or [
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
    book = Workbook()
    book.active.append(headers)
    for row in table_rows:
        book.active.append(row)
    book.save(snap / "表信息.xlsx")
    book.close()

    tcols = task_columns or [
        "task_id",
        "task_name",
        "task_type",
        "task_src",
        "task_conf",
        "project_name",
        "upstream_tasks",
        "output_table_name",
    ]
    (snap / "任务信息.json").write_text(
        json.dumps(
            {
                "columns": [{"name": name} for name in tcols],
                "data": task_rows,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    if dimensions:
        dim_root = root / "dimensions"
        for tbl_name, (col_names, rows) in dimensions.items():
            tbl_dir = dim_root / tbl_name
            (tbl_dir / "current").mkdir(parents=True, exist_ok=True)
            (tbl_dir / "meta.json").write_text(
                json.dumps(
                    {
                        "database": "dimensions",
                        "table": tbl_name,
                        "qualified_table": f"dimensions.{tbl_name}",
                        "columns": [{"name": col} for col in col_names],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (tbl_dir / "current" / "rows.jsonl").write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                encoding="utf-8",
            )


def _table_row(
    tid: int,
    db: str,
    tbl: str,
    col: str,
    ctype: str,
    seq: int = 1,
    comment: str = "",
    col_comment: str = "",
    output_task_id: str = "",
):
    return [tid, tbl, db, comment, output_task_id, "", col, seq, ctype, col_comment, ""]


def test_convert_dtb_to_ossie_extracts_fk_pk_partitions_and_handles_bad_inputs(
    tmp_path,
):
    import pytest

    from semantica_dataagent.dtb_ossie import convert_dtb_to_ossie
    from semantica_dataagent.ossie_export import validate_ossie_document

    table_rows = [
        # ods.raw_orders
        _table_row(
            1, "ods", "raw_orders", "order_id", "bigint", 1, "原始订单", "订单ID"
        ),
        _table_row(
            1, "ods", "raw_orders", "buyer_id", "bigint", 2, "原始订单", "买家ID"
        ),
        _table_row(
            1, "ods", "raw_orders", "tenant_id", "string", 3, "原始订单", "租户ID"
        ),
        _table_row(
            1, "ods", "raw_orders", "amount", "decimal(10,2)", 4, "原始订单", "金额"
        ),
        _table_row(
            1, "ods", "raw_orders", "create_date", "string", 5, "原始订单", "下单日期"
        ),
        _table_row(
            1, "ods", "raw_orders", "dt", "string", 10001, "原始订单", "分区日期"
        ),
        # ods.raw_users & dwd.users (same short name test)
        _table_row(2, "ods", "users", "id", "bigint", 1, "ODS用户", "用户ID"),
        _table_row(2, "ods", "users", "tenant_id", "string", 2, "ODS用户", "租户"),
        _table_row(2, "ods", "users", "alt_id", "bigint", 3, "ODS用户", "备用ID"),
        _table_row(2, "ods", "users", "dt", "string", 10001, "ODS用户", "分区日期"),
        _table_row(
            3,
            "dwd",
            "users",
            "uid",
            "bigint",
            1,
            "DWD用户",
            "用户主键",
            output_task_id="2",
        ),
        _table_row(3, "dwd", "users", "ts", "timestamp", 2, "DWD用户", "更新时间"),
        _table_row(3, "dwd", "users", "dt", "string", 10001, "DWD用户", "分区日期"),
        # dimensions.dim_buyer
        _table_row(
            4, "dimensions", "dim_buyer", "tenant_id", "string", 1, "买家维表", "租户ID"
        ),
        _table_row(
            4, "dimensions", "dim_buyer", "buyer_key", "bigint", 2, "买家维表", "买家键"
        ),
        _table_row(
            4,
            "dimensions",
            "dim_buyer",
            "buyer_name",
            "string",
            3,
            "买家维表",
            "买家名称",
        ),
        _table_row(
            4, "dimensions", "dim_buyer", "dt", "string", 10001, "买家维表", "分区日期"
        ),
        # dwd.order_summary (GROUP BY PK + snapshot OVERWRITE)
        _table_row(
            5,
            "dwd",
            "order_summary",
            "order_id",
            "bigint",
            1,
            "订单汇总",
            "订单ID",
            output_task_id="1",
        ),
        _table_row(
            5,
            "dwd",
            "order_summary",
            "total_amount",
            "decimal(12,2)",
            2,
            "订单汇总",
            "总额",
        ),
        _table_row(
            5, "dwd", "order_summary", "dt", "string", 10001, "订单汇总", "分区日期"
        ),
        # dwd.event_log (INSERT INTO incremental)
        _table_row(
            6,
            "dwd",
            "event_log",
            "event_id",
            "bigint",
            1,
            "事件明细",
            "事件ID",
            output_task_id="3",
        ),
        _table_row(
            6, "dwd", "event_log", "dt", "string", 10001, "事件明细", "分区日期"
        ),
        # dwd.merge_fact (merge_onself)
        _table_row(
            7,
            "dwd",
            "merge_fact",
            "item_id",
            "bigint",
            1,
            "合并表",
            "明细ID",
            output_task_id="4",
        ),
        _table_row(7, "dwd", "merge_fact", "dt", "string", 10001, "合并表", "分区日期"),
        # dwd.conflict_tbl (conflicting PK tasks)
        _table_row(
            8,
            "dwd",
            "conflict_tbl",
            "a",
            "bigint",
            1,
            "冲突表",
            "列A",
            output_task_id="5",
        ),
        _table_row(8, "dwd", "conflict_tbl", "b", "bigint", 2, "冲突表", "列B"),
        _table_row(8, "dwd", "conflict_tbl", "cnt", "bigint", 3, "冲突表", "计数"),
        # dwd.bad_rollup (ROLLUP should NOT infer PK)
        _table_row(9, "dwd", "bad_rollup", "a", "bigint", 1, "上卷表", "列A"),
        _table_row(9, "dwd", "bad_rollup", "cnt", "bigint", 2, "上卷表", "计数"),
    ]

    task_rows = [
        # 1: GROUP BY PK + composite FK + pure dt=dt join stripped + OR/CAST skipped + snapshot overwrite
        [
            "1",
            "t_order_summary",
            "spark sql",
            (
                "INSERT OVERWRITE TABLE dwd.order_summary PARTITION(dt='0') "
                "SELECT o.order_id, SUM(o.amount) AS total_amount "
                "FROM ods.raw_orders o "
                "JOIN dimensions.dim_buyer b ON o.tenant_id = b.tenant_id AND o.buyer_id = b.buyer_key AND o.dt = b.dt "
                "LEFT JOIN ods.users u ON o.buyer_id = u.id OR o.buyer_id = u.alt_id "
                "CROSS JOIN dwd.users du "
                "GROUP BY o.order_id"
            ),
            None,
            "订单主题",
            "",
            "dwd.order_summary",
        ],
        # 2: ROW_NUMBER() = 1 subquery dedup + non-bare CAST join skipped + pure partition join skipped
        [
            "2",
            "t_dwd_users",
            "spark",
            (
                "INSERT OVERWRITE TABLE dwd.users PARTITION(dt='0') "
                "SELECT uid, ts FROM ("
                "  SELECT u.id AS uid, u.dt AS ts, "
                "         ROW_NUMBER() OVER (PARTITION BY u.id ORDER BY u.dt DESC) AS rn "
                "  FROM ods.users u "
                "  JOIN ods.raw_orders o ON CAST(u.id AS STRING) = CAST(o.buyer_id AS STRING) AND u.dt = o.dt "
                "  WHERE u.dt = '${bizdate}'"
                ") s WHERE s.rn = 1"
            ),
            None,
            "用户主题",
            "1",
            "dwd.users",
        ],
        # 3: INSERT INTO incremental + single FK reversed order
        [
            "3",
            "t_event_log",
            "spark sql",
            (
                "INSERT INTO TABLE dwd.event_log "
                "SELECT o.order_id AS event_id, o.dt "
                "FROM dwd.users du "
                "JOIN ods.raw_orders o ON du.uid = o.buyer_id"
            ),
            None,
            "日志主题",
            "",
            "dwd.event_log",
        ],
        # 4: merge_onself
        [
            "4",
            "t_merge_fact",
            "merge onself",
            "",
            '{"params": {"table": "dwd.merge_fact"}}',
            "合并主题",
            "",
            "dwd.merge_fact",
        ],
        # 5 & 6: conflicting GROUP BY on conflict_tbl + ROLLUP + broken SQL
        [
            "5",
            "t_conflict_a",
            "spark",
            "INSERT INTO dwd.conflict_tbl SELECT o.order_id AS a, MAX(o.buyer_id) AS b, COUNT(*) AS cnt FROM ods.raw_orders o GROUP BY o.order_id",
            None,
            "冲突",
            "",
            "dwd.conflict_tbl",
        ],
        [
            "6",
            "t_conflict_b",
            "spark",
            "INSERT INTO dwd.conflict_tbl (a, b, cnt) SELECT MAX(total_amount) AS a, order_id AS b, COUNT(*) AS cnt FROM dwd.order_summary GROUP BY order_id",
            None,
            "冲突",
            "",
            "dwd.conflict_tbl",
        ],
        [
            "7",
            "t_rollup",
            "spark",
            "INSERT OVERWRITE TABLE dwd.bad_rollup SELECT order_id AS a, COUNT(*) AS cnt FROM ods.raw_orders GROUP BY order_id WITH ROLLUP",
            None,
            "上卷",
            "",
            "dwd.bad_rollup",
        ],
        [
            "8",
            "t_broken_sql",
            "spark",
            "INSERT INTO dwd.event_log SELECT FROM WHERE BROKEN(((",
            None,
            "坏SQL",
            "",
            "dwd.event_log",
        ],
    ]

    src = tmp_path / "source"
    _write_dtb_snapshot(src, table_rows=table_rows, task_rows=task_rows)

    result = convert_dtb_to_ossie(src, model_name="topic_domain")
    doc = result.document
    validate_ossie_document(doc)
    assert doc["name"] == "topic_domain"
    assert result.stats["tasks_sql_unparseable"] == 1

    by_source = {ds["source"]: ds for ds in doc["datasets"]}
    assert by_source["ods.users"]["name"] == "ods__users"
    assert by_source["dwd.users"]["name"] == "dwd__users"

    # Primary key assertions
    assert set(by_source["dwd.order_summary"]["primary_key"]) == {"dt", "order_id"}
    assert set(by_source["dwd.users"]["primary_key"]) == {"dt", "uid"}
    assert "primary_key" not in by_source["dwd.conflict_tbl"]
    assert "primary_key" not in by_source["dwd.bad_rollup"]

    # Relationships assertions: composite FK to dim_buyer + single FK ods.raw_orders -> dwd.users
    rels = doc.get("relationships") or []
    rel_tuples = {
        (
            r["from"],
            r["to"],
            tuple(r["from_columns"]),
            tuple(r["to_columns"]),
        )
        for r in rels
    }
    assert (
        "raw_orders",
        "dim_buyer",
        ("buyer_id", "tenant_id"),
        ("buyer_key", "tenant_id"),
    ) in rel_tuples
    assert (
        "raw_orders",
        "dwd__users",
        ("buyer_id",),
        ("uid",),
    ) in rel_tuples
    # No OR, CROSS, CAST, or dt=dt relationships
    assert not any(
        (r["from"] == "raw_orders" and r["to"] == "ods__users")
        or (r["from"] == "ods__users" and r["to"] == "raw_orders")
        for r in rels
    )
    assert not any("dt" in r["from_columns"] for r in rels)

    # Partition strategy & upstream summary assertions
    order_inst = by_source["dwd.order_summary"]["ai_context"]["instructions"]
    assert "全量快照分区（INSERT OVERWRITE" in order_inst
    assert "直接上游表：" in order_inst
    assert "ods.raw_orders" in order_inst

    dwd_user_inst = by_source["dwd.users"]["ai_context"]["instructions"]
    assert "INSERT OVERWRITE 分区覆盖" in dwd_user_inst

    event_inst = by_source["dwd.event_log"]["ai_context"]["instructions"]
    assert "增量追加分区（INSERT INTO" in event_inst

    merge_inst = by_source["dwd.merge_fact"]["ai_context"]["instructions"]
    assert "merge_onself 合并更新" in merge_inst

    # Parameter & bad-input guards
    with pytest.raises(ValueError, match="model_name"):
        convert_dtb_to_ossie(src, model_name="../bad")
    with pytest.raises(ValueError, match="database"):
        convert_dtb_to_ossie(src, database="nonexistent_db")

    bad_dir = tmp_path / "bad_dtb"
    _write_dtb_snapshot(
        bad_dir,
        table_rows=[["1", "t", "", "c", "", "", "", 1, "string", "", ""]],
        task_rows=[],
    )
    with pytest.raises(ValueError, match="Invalid DTB metadata"):
        convert_dtb_to_ossie(bad_dir)


def test_convert_dtb_hybrid_dimensions_and_atomic_write(tmp_path):
    import json
    from pathlib import Path

    from jsonschema import validate

    from semantica_dataagent.dtb_ossie import (
        convert_dtb_to_ossie,
        write_dtb_ossie_output,
    )

    dim_schema = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "schemas"
            / "v2"
            / "dimension_value.schema.json"
        ).read_text(encoding="utf-8")
    )

    table_rows = [
        _table_row(
            1,
            "dimensions",
            "dim_region",
            "region_code",
            "string",
            1,
            "区域维表",
            "区域代码",
        ),
        _table_row(
            1,
            "dimensions",
            "dim_region",
            "region_name",
            "string",
            2,
            "区域维表",
            "区域名称",
        ),
        _table_row(
            2,
            "dimensions",
            "dim_city",
            "city_code",
            "string",
            1,
            "城市维表",
            "城市代码",
        ),
        _table_row(
            2,
            "dimensions",
            "dim_city",
            "city_name",
            "string",
            2,
            "城市维表",
            "城市名称",
        ),
    ]

    # dim_region: low cardinality (3 distinct codes <= 5), preserves trailing space 'HK '
    region_rows = [
        {"region_code": "HK ", "region_name": "中国香港"},
        {"region_code": "HK ", "region_name": "中国香港"},
        {"region_code": "SH", "region_name": "上海"},
        {"region_code": "BJ", "region_name": "北京"},
    ]
    # dim_city: high cardinality (8 distinct cities > low_cardinality_limit=5)
    city_rows = [
        {"city_code": f"C{i:02d}", "city_name": f"城市_{i:02d}"} for i in range(1, 9)
    ]

    src = tmp_path / "source"
    _write_dtb_snapshot(
        src,
        table_rows=table_rows,
        task_rows=[],
        dimensions={
            "dim_region": (["region_code", "region_name"], region_rows),
            "dim_city": (["city_code", "city_name"], city_rows),
        },
    )

    res = convert_dtb_to_ossie(
        src,
        model_name="dims_hybrid",
        low_cardinality_limit=5,
        sample_limit=3,
    )
    by_ds = {ds["source"]: ds for ds in res.document["datasets"]}

    region_fields = {f["name"]: f for f in by_ds["dimensions.dim_region"]["fields"]}
    code_inst = region_fields["region_code"]["ai_context"]["instructions"]
    assert "全部取值（共 3 个）" in code_inst
    assert "'HK '" in code_inst
    assert "'SH'" in code_inst and "'BJ'" in code_inst

    city_fields = {f["name"]: f for f in by_ds["dimensions.dim_city"]["fields"]}
    city_code_inst = city_fields["city_code"]["ai_context"]["instructions"]
    assert "示例取值（共 8 个去重值，抽样前 3 个）" in city_code_inst

    # Only dim_city (both city_code and city_name with row_id!) is written to dimension_values
    assert len(res.dimension_values) == 16
    assert {item["table"] for item in res.dimension_values} == {"dim_city"}
    assert {item["field"] for item in res.dimension_values} == {
        "city_code",
        "city_name",
    }
    assert all(item.get("row_id") for item in res.dimension_values)
    for item in res.dimension_values:
        validate(item, dim_schema)

    assert res.stats["low_cardinality_fields"] == 2
    assert res.stats["high_cardinality_fields"] == 2
    assert res.stats["high_cardinality_tables"] == 1
    assert res.stats["dimension_values"] == 16

    out_dir = tmp_path / "out"
    written = write_dtb_ossie_output(
        res, out_dir, ossie_path="ossie", model_name="dims_hybrid"
    )
    assert set(written) == {"ossie", "dimension_values"}
    assert (out_dir / "ossie" / "dims_hybrid.yaml").is_file()
    written_dims = json.loads(
        (out_dir / "dimension_values.json").read_text(encoding="utf-8")
    )
    assert len(written_dims) == 16


def _make_fake_stores():
    class FakeGraph:
        def __init__(self):
            self.nodes: list[dict] = []
            self.rels: list[dict] = []
            self._next_id = 1

        def connect(self):
            pass

        def _find_node(self, node_id: int) -> dict:
            return next(n for n in self.nodes if n["id"] == node_id)

        def execute_query(self, query: str, parameters: dict | None = None):
            params = parameters or {}
            gen = params.get("generation")
            q = " ".join(query.split())

            if q.startswith(("CREATE INDEX", "CREATE FULLTEXT", "CALL db.awaitIndex")):
                return {"success": True, "records": []}
            if "RETURN count(*) AS deleted" in q:
                return {"success": True, "records": [{"deleted": 0}]}
            if (
                "MATCH (n:SDAEntity {generation:$generation})" in q
                and "DETACH DELETE n" in q
            ):
                victims = {
                    n["id"]
                    for n in self.nodes
                    if "SDAEntity" in n["labels"]
                    and n["props"].get("generation") == gen
                }
                self.rels = [
                    r
                    for r in self.rels
                    if r["start"] not in victims and r["end"] not in victims
                ]
                self.nodes = [n for n in self.nodes if n["id"] not in victims]
                return {"success": True, "records": [{"deleted": len(victims)}]}

            if "CREATE (n:SDAEntity:SDATable) SET n = row" in q:
                for row in params["rows"]:
                    self.nodes.append(
                        {
                            "id": self._next_id,
                            "labels": {"SDAEntity", "SDATable"},
                            "props": {**row, "generation": gen},
                        }
                    )
                    self._next_id += 1
                return {"success": True, "records": []}

            if "CREATE (f:SDAEntity:SDAField) SET f = row" in q:
                for row in params["rows"]:
                    self.nodes.append(
                        {
                            "id": self._next_id,
                            "labels": {"SDAEntity", "SDAField"},
                            "props": {**row, "generation": gen},
                        }
                    )
                    self._next_id += 1
                return {"success": True, "records": []}

            if "CREATE (v:SDAEntity:SDADimensionValue) SET v = row" in q:
                for row in params["rows"]:
                    self.nodes.append(
                        {
                            "id": self._next_id,
                            "labels": {"SDAEntity", "SDADimensionValue"},
                            "props": {**row, "generation": gen},
                        }
                    )
                    self._next_id += 1
                return {"success": True, "records": []}

            if "SET f.dimension_count = row.count" in q:
                for row in params["rows"]:
                    for f in self.nodes:
                        if (
                            "SDAField" in f["labels"]
                            and f["props"].get("generation") == gen
                            and f["props"].get("database") == row["database"]
                            and f["props"].get("table") == row["table"]
                            and f["props"].get("name") == row["field"]
                        ):
                            f["props"]["dimension_count"] = row["count"]
                            f["props"]["dimension_too_many"] = row["too_many"]
                            f["props"]["dimension_samples"] = row["samples"]
                return {"success": True, "records": []}

            if "CREATE (d:SDAEntity:SDADocument) SET d = row" in q:
                for row in params["rows"]:
                    self.nodes.append(
                        {
                            "id": self._next_id,
                            "labels": {"SDAEntity", "SDADocument"},
                            "props": {**row, "generation": gen},
                        }
                    )
                    self._next_id += 1
                return {"success": True, "records": []}

            if "CREATE (p:SDAEntity:SDAChunk) SET p = row" in q:
                for row in params["rows"]:
                    doc = next(
                        n
                        for n in self.nodes
                        if "SDADocument" in n["labels"]
                        and n["props"].get("generation") == gen
                        and n["props"].get("ref") == row["doc_id"]
                    )
                    chunk = {
                        "id": self._next_id,
                        "labels": {"SDAEntity", "SDAChunk"},
                        "props": {**row, "generation": gen},
                    }
                    self._next_id += 1
                    self.nodes.append(chunk)
                    self.rels.append(
                        {
                            "type": "HAS_CHUNK",
                            "start": doc["id"],
                            "end": chunk["id"],
                            "props": {},
                        }
                    )
                return {"success": True, "records": []}

            if "MERGE (c:SDAEntity:SDACategory" in q:
                for row in params["rows"]:
                    if not any(
                        "SDACategory" in n["labels"]
                        and n["props"].get("generation") == gen
                        and n["props"].get("name") == row["name"]
                        for n in self.nodes
                    ):
                        self.nodes.append(
                            {
                                "id": self._next_id,
                                "labels": {"SDAEntity", "SDACategory"},
                                "props": {
                                    "generation": gen,
                                    "build_id": params["build_id"],
                                    **row,
                                },
                            }
                        )
                        self._next_id += 1
                return {"success": True, "records": []}

            if "RETURN elementId(n) AS id" in q:
                label = (
                    "SDAField"
                    if ":SDAField" in q
                    else "SDADocument"
                    if ":SDADocument" in q
                    else "SDADimensionValue"
                )
                return {
                    "success": True,
                    "records": [
                        {"id": n["id"]}
                        for n in self.nodes
                        if label in n["labels"] and n["props"].get("generation") == gen
                    ],
                }

            if "MERGE (t)-[:HAS_FIELD]->(n)" in q:
                ids = set(params["ids"])
                for f in self.nodes:
                    if f["id"] not in ids:
                        continue
                    for t in self.nodes:
                        if (
                            "SDATable" in t["labels"]
                            and t["props"].get("generation") == gen
                            and t["props"].get("database") == f["props"].get("database")
                            and t["props"].get("name") == f["props"].get("table")
                            and (t["props"].get("catalog") or "")
                            == (f["props"].get("catalog") or "")
                        ):
                            self.rels.append(
                                {
                                    "type": "HAS_FIELD",
                                    "start": t["id"],
                                    "end": f["id"],
                                    "props": {},
                                }
                            )
                return {"success": True, "records": []}

            if "MERGE (t)-[:HAS_DOCUMENT]->(d)" in q:
                for row in params["rows"]:
                    d = next(
                        (
                            n
                            for n in self.nodes
                            if "SDADocument" in n["labels"]
                            and n["props"].get("generation") == gen
                            and n["props"].get("ref") == row["ref"]
                        ),
                        None,
                    )
                    t = next(
                        (
                            n
                            for n in self.nodes
                            if "SDATable" in n["labels"]
                            and n["props"].get("generation") == gen
                            and n["props"].get("database") == row["database"]
                            and n["props"].get("name") == row["name"]
                            and (n["props"].get("catalog") or "")
                            == (row.get("catalog") or "")
                        ),
                        None,
                    )
                    if d is not None and t is not None:
                        self.rels.append(
                            {
                                "type": "HAS_DOCUMENT",
                                "start": t["id"],
                                "end": d["id"],
                                "props": {},
                            }
                        )
                return {"success": True, "records": []}

            if "MERGE (a)-[r:RELATES_TO {ref: row.props.ref}]->(b)" in q:
                for row in params["rows"]:
                    a = next(
                        n
                        for n in self.nodes
                        if "SDATable" in n["labels"]
                        and n["props"].get("generation") == gen
                        and n["props"].get("database") == row["from_database"]
                        and n["props"].get("name") == row["from_table_name"]
                        and (n["props"].get("catalog") or "") == row["from_catalog"]
                    )
                    b = next(
                        n
                        for n in self.nodes
                        if "SDATable" in n["labels"]
                        and n["props"].get("generation") == gen
                        and n["props"].get("database") == row["to_database"]
                        and n["props"].get("name") == row["to_table_name"]
                        and (n["props"].get("catalog") or "") == row["to_catalog"]
                    )
                    self.rels.append(
                        {
                            "type": "RELATES_TO",
                            "start": a["id"],
                            "end": b["id"],
                            "props": dict(row["props"]),
                        }
                    )
                return {"success": True, "records": []}

            if "RETURN DISTINCT n.ref AS ref" in q:
                refs = set(params["refs"])
                return {
                    "success": True,
                    "records": [
                        {"ref": n["props"]["ref"]}
                        for n in self.nodes
                        if "SDAEntity" in n["labels"]
                        and n["props"].get("generation") == gen
                        and n["props"].get("ref") in refs
                    ],
                }

            if "OPTIONAL MATCH (t)-[:HAS_FIELD]->(f:SDAField" in q:
                names = params.get("names")
                database = params.get("database")
                records = []
                for t in self.nodes:
                    if not (
                        "SDATable" in t["labels"]
                        and t["props"].get("generation") == gen
                    ):
                        continue
                    tp = t["props"]
                    if names is not None and not (
                        tp["name"] in names or f"{tp['database']}.{tp['name']}" in names
                    ):
                        continue
                    if database is not None and tp["database"] != database:
                        continue
                    fields_for_t = [
                        dict(self._find_node(r["end"])["props"])
                        for r in self.rels
                        if r["type"] == "HAS_FIELD" and r["start"] == t["id"]
                    ]
                    docs_for_t = [
                        {
                            "ref": self._find_node(r["end"])["props"]["ref"],
                            "path": self._find_node(r["end"])["props"]["path"],
                            "category": self._find_node(r["end"])["props"]["category"],
                        }
                        for r in self.rels
                        if r["type"] == "HAS_DOCUMENT" and r["start"] == t["id"]
                    ]
                    records.append(
                        {
                            "table": dict(tp),
                            "fields": fields_for_t,
                            "documents": docs_for_t,
                        }
                    )
                records.sort(key=lambda r: (r["table"]["database"], r["table"]["name"]))
                return {"success": True, "records": records}

            if "-[r:RELATES_TO]->(:SDATable" in q:
                records = []
                for rel in self.rels:
                    if rel["type"] == "RELATES_TO":
                        s, e = (
                            self._find_node(rel["start"]),
                            self._find_node(rel["end"]),
                        )
                        if (
                            s["props"].get("generation") == gen
                            and e["props"].get("generation") == gen
                        ):
                            records.append({"relationship": dict(rel["props"])})
                return {"success": True, "records": records}

            if "MATCH (v:SDADimensionValue {generation:$generation})" in q:
                tbl = params["table"]
                field = params.get("field")
                kw = params["keyword"].lower()
                rows = []
                for n in self.nodes:
                    p = n["props"]
                    if (
                        "SDADimensionValue" not in n["labels"]
                        or p.get("generation") != gen
                    ):
                        continue
                    if (
                        p.get("table") != tbl
                        and f"{p.get('database')}.{p.get('table')}" != tbl
                    ):
                        continue
                    if field is not None and p.get("field") != field:
                        continue
                    if p.get("normalized", "").lower() == kw or any(
                        a.lower() == kw for a in p.get("aliases", ())
                    ):
                        rows.append({"item": dict(p)})
                return {"success": True, "records": rows}

            if "CREATE (t:SDAEntity:SDATask) SET t = row" in q:
                for row in params["rows"]:
                    self.nodes.append(
                        {
                            "id": self._next_id,
                            "labels": {"SDAEntity", "SDATask"},
                            "props": {**row, "generation": gen},
                        }
                    )
                    self._next_id += 1
                return {"success": True, "records": []}

            if "CREATE (a)-[r:DEPENDS_ON]->(b) SET r = row" in q:
                for row in params["rows"]:
                    if "SDALineageTable" in q:
                        a = next(
                            (
                                n
                                for n in self.nodes
                                if "SDALineageTable" in n["labels"]
                                and n["props"].get("generation") == gen
                                and n["props"].get("name") == row["upstream"]
                            ),
                            None,
                        )
                        if a is None:
                            a = {
                                "id": self._next_id,
                                "labels": {"SDAEntity", "SDALineageTable"},
                                "props": {"generation": gen, "name": row["upstream"]},
                            }
                            self._next_id += 1
                            self.nodes.append(a)
                        b = next(
                            (
                                n
                                for n in self.nodes
                                if "SDALineageTable" in n["labels"]
                                and n["props"].get("generation") == gen
                                and n["props"].get("name") == row["downstream"]
                            ),
                            None,
                        )
                        if b is None:
                            b = {
                                "id": self._next_id,
                                "labels": {"SDAEntity", "SDALineageTable"},
                                "props": {
                                    "generation": gen,
                                    "name": row["downstream"],
                                },
                            }
                            self._next_id += 1
                            self.nodes.append(b)
                        self.rels.append(
                            {
                                "type": "DEPENDS_ON",
                                "start": a["id"],
                                "end": b["id"],
                                "props": dict(row),
                            }
                        )
                return {"success": True, "records": []}

            if "CREATE (a)-[r:FIELD_DEPENDS_ON]->(b) SET r = row" in q:
                for row in params["rows"]:
                    up_db, up_tbl = row["upstream_table"].split(".", 1)
                    dn_db, dn_tbl = row["downstream_table"].split(".", 1)
                    a = next(
                        (
                            n
                            for n in self.nodes
                            if "SDAField" in n["labels"]
                            and n["props"].get("generation") == gen
                            and n["props"].get("database") == up_db
                            and n["props"].get("table") == up_tbl
                            and n["props"].get("name") == row["upstream_field"]
                        ),
                        None,
                    )
                    b = next(
                        (
                            n
                            for n in self.nodes
                            if "SDAField" in n["labels"]
                            and n["props"].get("generation") == gen
                            and n["props"].get("database") == dn_db
                            and n["props"].get("table") == dn_tbl
                            and n["props"].get("name") == row["downstream_field"]
                        ),
                        None,
                    )
                    if a is not None and b is not None:
                        self.rels.append(
                            {
                                "type": "FIELD_DEPENDS_ON",
                                "start": a["id"],
                                "end": b["id"],
                                "props": dict(row),
                            }
                        )
                return {"success": True, "records": []}

            if (
                "MATCH (t:SDATask {generation:$generation, name:$name}) RETURN properties(t) AS task"
                in q
            ):
                return {
                    "success": True,
                    "records": [
                        {"task": dict(n["props"])}
                        for n in self.nodes
                        if "SDATask" in n["labels"]
                        and n["props"].get("generation") == gen
                        and n["props"].get("name") == params["name"]
                    ],
                }

            if (
                "MATCH (start:SDALineageTable {generation:$generation, name:$table})"
                in q
            ):
                start = next(
                    (
                        n
                        for n in self.nodes
                        if "SDALineageTable" in n["labels"]
                        and n["props"].get("generation") == gen
                        and n["props"].get("name") == params["table"]
                    ),
                    None,
                )
                if start is None:
                    return {"success": True, "records": []}
                is_up = "<-[:DEPENDS_ON" in q
                visited = {start["id"]}
                frontier = [start["id"]]
                edges = []
                for _ in range(3):
                    next_frontier = []
                    for nid in frontier:
                        for rel in self.rels:
                            if rel["type"] != "DEPENDS_ON":
                                continue
                            if is_up and rel["end"] == nid:
                                edges.append({"edge": dict(rel["props"])})
                                if rel["start"] not in visited:
                                    visited.add(rel["start"])
                                    next_frontier.append(rel["start"])
                            elif not is_up and rel["start"] == nid:
                                edges.append({"edge": dict(rel["props"])})
                                if rel["end"] not in visited:
                                    visited.add(rel["end"])
                                    next_frontier.append(rel["end"])
                    frontier = next_frontier
                return {"success": True, "records": edges}

            if "MATCH (c:SDAChunk {generation:$generation})" in q:
                pairs = params.get("pairs") or []
                return {
                    "success": True,
                    "records": [
                        {"ref": n["props"]["ref"], "content": n["props"]["content"]}
                        for n in self.nodes
                        if "SDAChunk" in n["labels"]
                        and n["props"].get("generation") == gen
                        and any(
                            p[0] in n["props"]["content"].lower()
                            and p[1] in n["props"]["content"].lower()
                            for p in pairs
                        )
                    ],
                }
            return {"success": True, "records": []}

        def close(self):
            pass

    class FakeVectors:
        def __init__(self):
            self.items: dict[str, dict] = {}
            self.collection = type("C", (), {"load": lambda self: None})()
            self.client = type("Cl", (), {"disconnect": lambda self: None})()

        def create_collection(self, *args, **kwargs):
            pass

        def get_collection(self, *args, **kwargs):
            pass

        def add_vectors(self, vectors, ids, metadata):
            for vid, meta in zip(ids, metadata, strict=True):
                self.items[vid] = dict(meta)

        def delete_vectors(self, ids):
            for vid in ids:
                self.items.pop(vid, None)

        def search(self, **kwargs):
            return [[]]

    return FakeGraph(), FakeVectors()


def test_convert_dtb_ossie_cli_and_full_build_round_trip(tmp_path, monkeypatch, capsys):
    from contextlib import contextmanager

    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient

    from semantica_dataagent import knowledge, sql_validation
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.cli import main as cli_main
    from semantica_dataagent.db import Build, sessions
    from semantica_dataagent.dimension_api import lookup_dim_values
    from semantica_dataagent.sql_validation import find_join_path, validate_sql
    from semantica_dataagent.stage_contract import StorageHandles

    raw_src = tmp_path / "raw_dtb"
    table_rows = [
        _table_row(
            1,
            "dwd",
            "fact_sales",
            "sale_id",
            "bigint",
            1,
            "销售事实表",
            "销售ID",
            output_task_id="1",
        ),
        _table_row(
            1, "dwd", "fact_sales", "city_key", "string", 2, "销售事实表", "城市编码"
        ),
        _table_row(
            1, "dwd", "fact_sales", "region_code", "string", 3, "销售事实表", "区域编码"
        ),
        _table_row(
            1, "dwd", "fact_sales", "amount", "decimal(12,2)", 4, "销售事实表", "销售额"
        ),
        _table_row(1, "dwd", "fact_sales", "dt", "string", 10001, "销售事实表", "分区"),
        _table_row(
            2,
            "dimensions",
            "dim_city",
            "city_code",
            "string",
            1,
            "城市维表",
            "城市编码",
        ),
        _table_row(
            2,
            "dimensions",
            "dim_city",
            "city_name",
            "string",
            2,
            "城市维表",
            "城市名称",
        ),
        _table_row(
            3,
            "dimensions",
            "dim_region",
            "region_code",
            "string",
            1,
            "区域维表",
            "区域编码",
        ),
        _table_row(
            3,
            "dimensions",
            "dim_region",
            "region_name",
            "string",
            2,
            "区域维表",
            "区域名称",
        ),
    ]
    task_rows = [
        [
            "1",
            "t_sales",
            "spark sql",
            (
                "INSERT OVERWRITE TABLE dwd.fact_sales PARTITION(dt='0') "
                "SELECT f.sale_id, f.city_key, f.region_code, f.amount "
                "FROM ods.raw_sales f "
                "JOIN dimensions.dim_city c ON f.city_key = c.city_code "
                "JOIN dimensions.dim_region r ON f.region_code = r.region_code"
            ),
            None,
            "销售域",
            "",
            "dwd.fact_sales",
        ],
        # Include self-loop join between dwd.fact_sales and dim_city directly inside SQL
        [
            "2",
            "t_report",
            "spark sql",
            (
                "SELECT f.sale_id, c.city_name, r.region_name "
                "FROM dwd.fact_sales f "
                "JOIN dimensions.dim_city c ON f.city_key = c.city_code "
                "JOIN dimensions.dim_region r ON f.region_code = r.region_code"
            ),
            None,
            "销售域",
            "",
            "",
        ],
    ]
    city_rows = [
        {"city_code": f"C{i:02d}", "city_name": f"城市_{i:02d}"} for i in range(1, 9)
    ]
    region_rows = [
        {"region_code": "HK ", "region_name": "中国香港"},
        {"region_code": "SH", "region_name": "上海"},
    ]
    _write_dtb_snapshot(
        raw_src,
        table_rows=table_rows,
        task_rows=task_rows,
        dimensions={
            "dim_city": (["city_code", "city_name"], city_rows),
            "dim_region": (["region_code", "region_name"], region_rows),
        },
    )

    # Reject unknown format/invalid path without touching files
    assert (
        cli_main(
            [
                "convert-dtb-ossie",
                "--source-dir",
                str(raw_src),
                "--output-dir",
                str(tmp_path / "bad_out"),
                "--model-name",
                "../bad",
            ]
        )
        == 1
    )

    project_src = tmp_path / "ossie_project_source"
    exit_code = cli_main(
        [
            "convert-dtb-ossie",
            "--source-dir",
            str(raw_src),
            "--output-dir",
            str(project_src),
            "--model-name",
            "sales_domain",
            "--low-cardinality-limit",
            "5",
            "--sample-limit",
            "3",
        ]
    )
    assert exit_code == 0, capsys.readouterr()
    assert (project_src / "ossie" / "sales_domain.yaml").is_file()
    assert (project_src / "dimension_values.json").is_file()
    assert (project_src / "tasks.json").is_file()

    # Build the converted directory with ossie + canonical + markdown (dtb disabled)
    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    from semantica_dataagent.app import app

    token = initialize()
    sql_validation._snapshot_cache = None
    graph_store, vector_store = _make_fake_stores()

    @contextmanager
    def fake_storage(values, stats=None):
        yield StorageHandles(
            values["knowledge_generation"],
            graph_store,
            vector_store,
            values,
            _embedding_dimension=4,
        )

    monkeypatch.setattr(knowledge, "build_storage", fake_storage)
    monkeypatch.setattr(knowledge, "read_graph", lambda values: graph_store)
    monkeypatch.setattr(knowledge, "read_vectors", lambda values: vector_store)
    monkeypatch.setattr(
        knowledge,
        "embed",
        lambda values, texts, stats=None: [[0.1, 0.2, 0.3, 0.4]] * len(texts),
    )
    monkeypatch.setattr(knowledge, "prune_generations", lambda values: None)

    with TestClient(app) as c:
        headers = {"Authorization": f"Bearer {token}"}
        assert (
            c.put(
                "/api/v1/settings",
                headers=headers,
                json={
                    "values": {
                        "source_directory": str(project_src),
                        "neo4j_uri": "bolt://127.0.0.1:7687",
                        "neo4j_user": "neo4j",
                        "neo4j_password": "secret",
                        "milvus_uri": "http://127.0.0.1:19530",
                        "embedding_url": "http://127.0.0.1:8000",
                        "embedding_model": "test-model",
                    }
                },
            ).status_code
            == 200
        )
        assert (
            c.put(
                "/api/v1/projects/default/adapters",
                headers=headers,
                json={
                    "adapters": [
                        {"id": "ossie", "enabled": True, "parameters": {}},
                        {"id": "canonical", "enabled": True, "parameters": {}},
                        {"id": "markdown", "enabled": True, "parameters": {}},
                        {"id": "dtb", "enabled": False, "parameters": {}},
                    ]
                },
            ).status_code
            == 200
        )

        with sessions().begin() as db:
            row = Build(
                project_id="default",
                build_type="full",
                status="running",
                started_at="2026-11-18T00:00:00Z",
                counts="{}",
            )
            db.add(row)
            db.flush()
            bid = row.id
        knowledge.run_build(bid)
        sql_validation._snapshot_cache = None

    from semantica_dataagent.task_api import get_lineage, read_task_sql

    with sessions()() as db:
        build_row = db.get(Build, bid)
        assert build_row.status == "succeeded", build_row.error

    # Verify SDATask, read_task_sql, and table lineage survive convert-dtb-ossie round-trip
    sales_task = read_task_sql("t_sales")
    assert "dwd.fact_sales" in sales_task["sql"]
    assert sales_task["type"] == "spark"
    up_lineage = get_lineage("dwd.fact_sales", "up", depth=1)
    assert {edge["upstream"] for edge in up_lineage} >= {
        "ods.raw_sales",
        "dimensions.dim_city",
        "dimensions.dim_region",
    }

    # Verify describe_tables shows partition mark + verbatim 'HK ' enum note
    described = {f"{t['database']}.{t['name']}": t for t in knowledge.describe()}
    assert set(described) == {
        "dwd.fact_sales",
        "dimensions.dim_city",
        "dimensions.dim_region",
    }
    region_fields = {f["name"]: f for f in described["dimensions.dim_region"]["fields"]}
    assert "'HK '" in region_fields["region_code"]["comment"]
    sales_fields = {f["name"]: f for f in described["dwd.fact_sales"]["fields"]}
    assert sales_fields["dt"]["partition"] is True

    # Verify strong join path & SQL validation using extracted FK relationships
    join_res = find_join_path(["dwd.fact_sales", "dimensions.dim_city"])
    assert join_res["status"] == "found"
    assert join_res["steps"][0]["strength"] == "strong"
    assert join_res["steps"][0]["evidence"][0]["type"] == "relationship"

    sql_res = validate_sql(
        "SELECT SUM(f.amount) FROM dwd.fact_sales f "
        "JOIN dimensions.dim_city c ON f.city_key = c.city_code "
        "WHERE f.dt = '2026-11-18' AND c.city_code = 'C03'",
        "doris",
    )
    assert sql_res["status"] == "pass", sql_res

    # Verify high-cardinality alias lookup via preserved row_id, and NO nodes for dim_region
    city_hits = lookup_dim_values("dim_city", "城市_03", field="city_code")
    assert len(city_hits) == 1
    assert city_hits[0]["value"] == "C03"
    assert lookup_dim_values("dim_region", "HK", field="region_code") == []


def test_convert_dtb_ossie_qualify_shell_dedup_and_cli_help(tmp_path):
    import subprocess
    import sys

    import pytest

    from semantica_dataagent.dtb_ossie import (
        convert_dtb_to_ossie,
        write_dtb_ossie_output,
    )

    table_rows = [
        _table_row(
            1, "dwd", "dedup_orders", "tenant_id", "string", 1, "去重订单", "租户"
        ),
        _table_row(
            1, "dwd", "dedup_orders", "order_id", "bigint", 2, "去重订单", "订单ID"
        ),
        _table_row(
            1, "dwd", "dedup_orders", "amt", "decimal(10,2)", 3, "去重订单", "金额"
        ),
        _table_row(
            2, "dimensions", "dim_org", "tenant_id", "string", 1, "组织维表", "租户"
        ),
        _table_row(
            2, "dimensions", "dim_org", "org_id", "bigint", 2, "组织维表", "组织ID"
        ),
        _table_row(
            2, "dimensions", "dim_org", "org_name", "string", 3, "组织维表", "组织名称"
        ),
    ]
    shell_task = r"""#!/bin/sh
dst="dwd.dedup_orders"
sql_q="INSERT OVERWRITE TABLE ${dst} SELECT o.tenant_id, o.order_id, o.amt FROM ods.raw_o o JOIN dimensions.dim_org d ON o.tenant_id = d.tenant_id AND o.order_id = d.org_id QUALIFY ROW_NUMBER() OVER (PARTITION BY o.tenant_id, o.order_id ORDER BY o.amt DESC) = 1"
submit_sql_and_wait "${sql_q}"
"""
    task_rows = [
        [
            "1",
            "t_shell_qualify",
            "shell",
            shell_task,
            None,
            "订单",
            "",
            "dwd.dedup_orders",
        ],
        # Reversed column order in AND and reversed FROM/JOIN order -> must deduplicate to 1 relationship
        [
            "2",
            "t_reversed_join",
            "spark",
            (
                "SELECT d.org_name, o.amt FROM dimensions.dim_org d "
                "JOIN dwd.dedup_orders o ON d.org_id = o.order_id AND d.tenant_id = o.tenant_id"
            ),
            None,
            "订单",
            "",
            "",
        ],
    ]
    src = tmp_path / "src"
    _write_dtb_snapshot(
        src,
        table_rows=table_rows,
        task_rows=task_rows,
        dimensions={
            "dim_org": (
                ["tenant_id", "org_id", "org_name"],
                [{"tenant_id": "T1", "org_id": "10", "org_name": "总部"}],
            )
        },
    )

    res = convert_dtb_to_ossie(
        src, model_name="qualify_model", low_cardinality_limit=10
    )
    by_source = {ds["source"]: ds for ds in res.document["datasets"]}
    assert set(by_source["dwd.dedup_orders"]["primary_key"]) == {
        "tenant_id",
        "order_id",
    }
    assert len(res.document["relationships"]) == 1
    assert res.dimension_values == []

    # Low-cardinality only output must NOT create dimension_values.json, while writing tasks.json
    out_dir = tmp_path / "low_only_out"
    written = write_dtb_ossie_output(res, out_dir, model_name="qualify_model")
    assert set(written) == {"ossie", "tasks"}
    assert not (out_dir / "dimension_values.json").exists()
    assert (out_dir / "tasks.json").is_file()

    with pytest.raises(ValueError):
        write_dtb_ossie_output(res, out_dir, ossie_path="../escape")
    with pytest.raises(ValueError):
        write_dtb_ossie_output(res, out_dir, dimension_filename=".hidden.json")
    with pytest.raises(ValueError):
        write_dtb_ossie_output(res, out_dir, tasks_filename=".hidden_tasks.json")

    from pathlib import Path

    cli_bin = str(Path(sys.executable).with_name("semantica-dataagent"))
    help_proc = subprocess.run(
        [cli_bin, "convert-dtb-ossie", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_proc.returncode == 0, help_proc.stderr
    assert "--low-cardinality-limit" in help_proc.stdout

    unknown_proc = subprocess.run(
        [cli_bin, "convert-dtb-ossie", "--unknown-flag"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert unknown_proc.returncode != 0


def test_convert_dtb_to_ossie_emits_canonical_tasks_and_prefixes(tmp_path):
    import json
    from pathlib import Path

    import pytest
    from jsonschema import validate

    from semantica_dataagent.dtb_ossie import (
        convert_dtb_to_ossie,
        write_dtb_ossie_output,
    )
    from semantica_dataagent.ossie_export import validate_ossie_document
    from semantica_dataagent.ossie_source import OssieAdapter

    task_schema = json.loads(
        (
            Path(__file__).resolve().parents[2] / "schemas" / "v2" / "task.schema.json"
        ).read_text(encoding="utf-8")
    )
    dim_schema = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "schemas"
            / "v2"
            / "dimension_value.schema.json"
        ).read_text(encoding="utf-8")
    )

    table_rows = [
        _table_row(
            1, "ods", "raw_orders", "order_id", "bigint", 1, "原始订单", "订单ID"
        ),
        _table_row(
            1, "ods", "raw_orders", "city_id", "string", 2, "原始订单", "城市ID"
        ),
        _table_row(
            2,
            "dwd",
            "fact_orders",
            "order_id",
            "bigint",
            1,
            "事实订单",
            "订单ID",
            output_task_id="1",
        ),
        _table_row(
            2, "dwd", "fact_orders", "city_id", "string", 2, "事实订单", "城市ID"
        ),
        _table_row(
            3, "dimensions", "dim_city", "city_id", "string", 1, "城市维表", "城市ID"
        ),
        _table_row(
            3,
            "dimensions",
            "dim_city",
            "city_name",
            "string",
            2,
            "城市维表",
            "城市名称",
        ),
    ]
    task_rows = [
        [
            "1",
            "load_fact_orders",
            "spark sql",
            "INSERT OVERWRITE TABLE dwd.fact_orders SELECT r.order_id, r.city_id FROM ods.raw_orders r JOIN dimensions.dim_city c ON r.city_id = c.city_id",
            None,
            "订单域",
            "",
            "dwd.fact_orders",
        ],
        [
            "2",
            "merge_dim_city",
            "merge onself",
            "",
            '{"params": {"table": "dimensions.dim_city"}}',
            "维度域",
            "",
            "dimensions.dim_city",
        ],
    ]
    city_rows = [{"city_id": f"C{i}", "city_name": f"City_{i}"} for i in range(1, 7)]

    src = tmp_path / "src"
    _write_dtb_snapshot(
        src,
        table_rows=table_rows,
        task_rows=task_rows,
        dimensions={"dim_city": (["city_id", "city_name"], city_rows)},
    )

    res = convert_dtb_to_ossie(
        src,
        model_name="prefixed_model",
        low_cardinality_limit=3,
        sample_limit=2,
        catalog_prefix="lake_prod",
        task_namespace="team.orders",
    )
    validate_ossie_document(res.document)

    by_ds = {ds["source"]: ds for ds in res.document["datasets"]}
    assert {
        "lake_prod.ods.raw_orders",
        "lake_prod.dwd.fact_orders",
        "lake_prod.dimensions.dim_city",
    } == set(by_ds)
    assert (
        "lake_prod.ods.raw_orders"
        in by_ds["lake_prod.dwd.fact_orders"]["ai_context"]["instructions"]
    )

    assert len(res.tasks) == 2
    for task in res.tasks:
        validate(task, task_schema)
    by_task = {t["name"]: t for t in res.tasks}
    assert set(by_task) == {
        "team.orders:load_fact_orders",
        "team.orders:merge_dim_city",
    }
    assert by_task["team.orders:load_fact_orders"]["type"] == "spark"
    assert by_task["team.orders:load_fact_orders"]["description"] == "订单域"
    assert by_task["team.orders:merge_dim_city"]["parameters"] == {
        "params": {"table": "dimensions.dim_city"}
    }

    assert res.dimension_values
    for item in res.dimension_values:
        validate(item, dim_schema)
        assert item["row_id"].startswith("lake_prod.dimensions.dim_city:")

    out_dir = tmp_path / "out"
    written = write_dtb_ossie_output(res, out_dir, model_name="prefixed_model")
    assert set(written) == {"ossie", "dimension_values", "tasks"}
    read_back = OssieAdapter().read(out_dir, {"path": "ossie"})
    table_catalogs = {
        r.data.get("catalog") for r in read_back.records if r.kind == "table"
    }
    assert table_catalogs == {"lake_prod"}

    with pytest.raises(ValueError, match="catalog_prefix"):
        convert_dtb_to_ossie(src, catalog_prefix="bad-catalog")
    with pytest.raises(ValueError, match="task_namespace"):
        convert_dtb_to_ossie(src, task_namespace="bad namespace")
