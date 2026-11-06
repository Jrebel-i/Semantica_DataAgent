"""Column lineage must survive SQL extraction and projection resolution."""

from semantica_dataagent.canonical import Record
from semantica_dataagent.dtb_tasks import DtbTaskParser
from semantica_dataagent.sql_tasks import SQLTaskParser

SCHEMA = {
    "src.orders": ["id", "amount"],
    "dst.orders": ["order_id", "total"],
}


def edges(result):
    return set(getattr(result, "column_edges", ()))


def test_spark_subquery_alias_and_static_partition():
    task = Record(
        "task",
        {
            "name": "load",
            "type": "spark",
            "sql": """
        INSERT OVERWRITE TABLE dst.orders PARTITION (dt='2025-01-01')
        SELECT s.id AS order_id, s.amount * 2 AS total
        FROM (SELECT * FROM src.orders) s
    """,
        },
    )
    result = SQLTaskParser().parse(task, {"schema": SCHEMA})
    assert edges(result) == {
        ("src.orders", "id", "dst.orders", "order_id"),
        ("src.orders", "amount", "dst.orders", "total"),
    }


def test_shell_livy_expands_variables_before_resolving_columns():
    task = Record(
        "task",
        {
            "name": "shell",
            "type": "shell",
            "sql": """
source="src.orders"
query="INSERT INTO dst.orders SELECT id AS order_id, amount AS total FROM ${source}"
submit_sql_and_wait "$query"
""",
        },
    )
    result = DtbTaskParser().parse(task, {"schema": SCHEMA})
    assert edges(result) == {
        ("src.orders", "id", "dst.orders", "order_id"),
        ("src.orders", "amount", "dst.orders", "total"),
    }


def test_merge_onself_resolves_each_field_to_itself():
    task = Record(
        "task",
        {
            "name": "merge",
            "type": "merge_onself",
            "sql": "",
            "parameters": {"table": "dst.orders"},
        },
    )
    result = DtbTaskParser().parse(task, {"schema": SCHEMA})
    assert edges(result) == {
        ("dst.orders", "order_id", "dst.orders", "order_id"),
        ("dst.orders", "total", "dst.orders", "total"),
    }


def test_shell_resolves_jdbc_view_columns_to_physical_source():
    script = """
create_temp_view="CREATE TEMPORARY VIEW v USING org.apache.spark.sql.jdbc OPTIONS (dbtable 'src.orders')"
query="INSERT INTO dst.orders SELECT id AS order_id, amount AS total FROM v"
submit_sql_and_wait "$create_temp_view"
submit_sql_and_wait "$query"
"""
    result = DtbTaskParser().parse(
        Record("task", {"name": "jdbc", "type": "shell", "sql": script}),
        {"schema": SCHEMA},
    )
    assert edges(result) == {
        ("src.orders", "id", "dst.orders", "order_id"),
        ("src.orders", "amount", "dst.orders", "total"),
    }


def test_sql_temporary_view_retains_field_lineage():
    task = Record(
        "task",
        {
            "name": "view",
            "type": "spark",
            "sql": """
        CREATE TEMPORARY VIEW v AS SELECT id, amount FROM src.orders;
        INSERT INTO dst.orders SELECT id AS order_id, amount AS total FROM v
    """,
        },
    )
    result = SQLTaskParser().parse(task, {"schema": SCHEMA})
    assert edges(result) == {
        ("src.orders", "id", "dst.orders", "order_id"),
        ("src.orders", "amount", "dst.orders", "total"),
    }


def test_field_lineage_query_returns_upstream_task_and_evidence(monkeypatch):
    from semantica_dataagent import task_api

    edge = {
        "upstream_table": "src.orders",
        "upstream_field": "id",
        "downstream_table": "dst.orders",
        "downstream_field": "order_id",
        "task_id": "load",
        "task_type": "spark",
        "evidence": "parsed",
        "parse_status": "success",
    }
    monkeypatch.setattr(
        task_api.knowledge, "settings", lambda: {"knowledge_generation": "test"}
    )
    monkeypatch.setattr(task_api.knowledge, "read_graph", lambda _: object())
    monkeypatch.setattr(task_api.knowledge, "execute", lambda *args: [{"edge": edge}])
    result = task_api.get_lineage("dst.orders", column="order_id")
    assert result, "字段血缘应包含上游字段"
    assert result[0]["upstream_field"] == "id"
    assert result[0]["task_id"] == "load"
    assert result[0]["evidence"] == "parsed"
