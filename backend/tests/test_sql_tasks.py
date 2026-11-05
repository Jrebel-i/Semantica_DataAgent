from semantica_dataagent.canonical import Record
from semantica_dataagent.sql_tasks import SQLTaskParser


def test_sql_parser_resolves_temporary_views_and_ctes():
    parser = SQLTaskParser()
    for dialect in ("hive", "spark", "doris"):
        task = Record(
            "task",
            {
                "name": "load",
                "type": dialect,
                "sql": "CREATE TEMPORARY VIEW v AS SELECT * FROM raw.orders; INSERT OVERWRITE TABLE shop.orders PARTITION (dt='2025-01-01') WITH c AS (SELECT * FROM v) SELECT * FROM c JOIN raw.customers ON 1=1",
            },
        )
        result = parser.parse(task, {})
        assert result.status == "success"
        assert set(result.reads) == {"raw.orders", "raw.customers"}
        assert result.writes == ["shop.orders"]


def test_failed_sql_and_declared_edges():
    from semantica_dataagent.task_stage import resolve_task

    task = Record(
        "task", {"name": "broken", "type": "hive", "sql": "INSERT INTO x SELECT FROM"}
    )
    result, edges = resolve_task(
        task, [("raw.a", "shop.b")], SQLTaskParser().parse(task, {})
    )
    assert result.status == "failed"
    assert edges == [("raw.a", "shop.b", "declared")]


def test_evidence_is_merged():
    from semantica_dataagent.task_stage import resolve_task

    task = Record(
        "task", {"name": "load", "sql": "INSERT INTO shop.b SELECT * FROM raw.a"}
    )
    parsed = SQLTaskParser().parse(task, {})
    _, edges = resolve_task(task, [("raw.a", "shop.b"), ("raw.c", "shop.b")], parsed)
    assert set(edges) == {("raw.a", "shop.b", "both"), ("raw.c", "shop.b", "declared")}


def test_independent_statements_do_not_create_cross_dependencies():
    from semantica_dataagent.task_stage import resolve_task

    task = Record(
        "task",
        {
            "name": "two",
            "type": "hive",
            "sql": "INSERT INTO shop.a SELECT * FROM raw.a; INSERT INTO shop.b SELECT * FROM raw.b",
        },
    )
    parsed = SQLTaskParser().parse(task, {})
    _, edges = resolve_task(task, [], parsed)
    assert set(edges) == {("raw.a", "shop.a", "parsed"), ("raw.b", "shop.b", "parsed")}


def test_unmatched_task_type_uses_declared_edges(monkeypatch):
    from semantica_dataagent import plugins
    from semantica_dataagent.task_stage import resolve_task

    monkeypatch.setattr(
        plugins, "configured", lambda: [("parser", SQLTaskParser(), True, {})]
    )
    task = Record("task", {"name": "external", "type": "shell", "sql": "echo ignored"})
    result, edges = resolve_task(task, [("raw.a", "shop.b")], plugins.parse_task(task))
    assert result.status == "unsupported"
    assert edges == [("raw.a", "shop.b", "declared")]


def test_source_table_names_exclude_join_aliases():
    parser = SQLTaskParser()
    task = Record(
        "task",
        {
            "name": "aliased",
            "type": "hive",
            "sql": "INSERT INTO shop.orders SELECT * FROM raw.orders AS o JOIN raw.customers c ON 1=1",
        },
    )
    result = parser.parse(task, {})
    assert result.status == "success"
    assert set(result.reads) == {"raw.orders", "raw.customers"}
    assert set(result.edges) == {
        ("raw.orders", "shop.orders"),
        ("raw.customers", "shop.orders"),
    }
