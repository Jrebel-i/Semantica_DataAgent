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
