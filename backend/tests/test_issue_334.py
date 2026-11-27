import json
from pathlib import Path

import pytest

from semantica_dataagent import sql_validation
from semantica_dataagent.sql_validation import Snapshot


@pytest.fixture
def validate(monkeypatch):
    root = Path(__file__).resolve().parents[2] / "samples" / "demo"
    tables = json.loads((root / "tables.json").read_text())
    fields = json.loads((root / "fields.json").read_text())
    snapshot = Snapshot(
        [
            {
                **t,
                "ref": f"table:{t['database']}.{t['name']}",
                "fields": [
                    f
                    for f in fields
                    if f["database"] == t["database"] and f["table"] == t["name"]
                ],
            }
            for t in tables
        ],
        [],
        [],
    )
    monkeypatch.setattr(sql_validation, "load_snapshot", lambda: snapshot)
    return lambda sql: sql_validation.validate_sql(sql, dialect="doris")


@pytest.mark.parametrize(
    "predicate",
    [
        "'2025-01-01' <= dt",
        "CURRENT_DATE() <= dt",
        "'2025-01-31' >= dt",
        "dt >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)",
        "dt = CURRENT_DATE()",
        "dt <= '2025-01-31'",
        "dt = (SELECT MAX(dt) FROM shop.orders)",
    ],
)
def test_partition_constants(validate, predicate):
    result = validate(f"SELECT id FROM shop.orders WHERE {predicate}")
    assert result["status"] == "pass", result["issues"]


@pytest.mark.parametrize(
    "predicate",
    [
        "dt = id",
        "CURRENT_DATE() >= dt",
        "NOW() > dt",
        "DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY) >= dt",
        "dt >= DATE_SUB(id, INTERVAL 1 DAY)",
        "dt = DATE_ADD(id, INTERVAL 1 DAY)",
        "id <= dt",
        "(dt = '2025-01-01' OR id = 1)",
        "dt = (SELECT MAX(dt) FROM shop.customers)",
        "dt IN (SELECT dt FROM shop.customers)",
        "dt <= CURRENT_DATE()",
        "dt < NOW()",
        "dt <= DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)",
        "dt = DATE_FORMAT(id, '%Y-%m-%d')",
    ],
)
def test_invalid_partition_expressions(validate, predicate):
    result = validate(f"SELECT id FROM shop.orders WHERE {predicate}")
    assert any(
        i["kind"] == "partition" and i["table"] == "shop.orders"
        for i in result["issues"]
    ), result


@pytest.mark.parametrize(
    "join, on, where, missing",
    [
        ("LEFT", "c.dt = '2025-01-01'", "o.dt = '2025-01-01'", set()),
        ("RIGHT", "o.dt = '2025-01-01'", "c.dt = '2025-01-01'", set()),
        (
            "FULL",
            "o.dt = '2025-01-01' AND c.dt = '2025-01-01'",
            "1 = 1",
            {"shop.orders", "shop.customers"},
        ),
        (
            "LEFT",
            "o.dt = '2025-01-01' AND c.dt = '2025-01-01'",
            "1 = 1",
            {"shop.orders"},
        ),
        (
            "RIGHT",
            "o.dt = '2025-01-01' AND c.dt = '2025-01-01'",
            "1 = 1",
            {"shop.customers"},
        ),
        ("INNER", "o.dt = '2025-01-01' AND c.dt = '2025-01-01'", "1 = 1", set()),
    ],
)
def test_join_partition_sides(validate, join, on, where, missing):
    sql = f"SELECT o.id FROM shop.orders o {join} JOIN shop.customers c ON o.id = c.id AND {on} WHERE {where}"
    result = validate(sql)
    issues = [i for i in result["issues"] if i["kind"] == "partition"]
    assert {i["table"] for i in issues} == missing, result
    if join == "FULL":
        assert all(
            "FULL JOIN" in i["suggestion"] and "WHERE" in i["suggestion"]
            for i in issues
        )


@pytest.mark.parametrize(
    "join, missing, expected",
    [
        ("LEFT", "shop.customers", "右侧"),
        ("RIGHT", "shop.orders", "左侧"),
    ],
)
def test_join_suggestion(validate, join, missing, expected):
    sql = (
        f"SELECT o.id FROM shop.orders o {join} JOIN shop.customers c ON o.id = c.id WHERE "
        + ("o.dt = '2025-01-01'" if join == "LEFT" else "c.dt = '2025-01-01'")
    )
    result = validate(sql)
    issue = next(
        i
        for i in result["issues"]
        if i["kind"] == "partition" and i["table"] == missing
    )
    assert (
        join + " JOIN" in issue["suggestion"]
        and expected in issue["suggestion"]
        and "ON" in issue["suggestion"]
    )


def test_left_join_on_and_where_have_different_results():
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.executescript(
        "CREATE TABLE a(id INTEGER); CREATE TABLE b(id INTEGER, dt TEXT); INSERT INTO a VALUES (1),(2); INSERT INTO b VALUES (1,'2025-01-01');"
    )
    base = "SELECT a.id, b.id FROM a LEFT JOIN b ON a.id = b.id"
    assert db.execute(base + " AND b.dt = '2025-01-01'").fetchall() == [
        (1, 1),
        (2, None),
    ]
    assert db.execute(base + " WHERE b.dt = '2025-01-01'").fetchall() == [(1, 1)]


def test_chain_join_uses_each_node(validate):
    sql = (
        "SELECT o.id FROM shop.orders o LEFT JOIN shop.customers c "
        "ON o.id = c.id AND c.dt = '2025-01-01' "
        "LEFT JOIN shop.products p ON p.id = o.id AND p.dt = '2025-01-01' "
        "WHERE o.dt = '2025-01-01'"
    )
    result = validate(sql)
    assert not [i for i in result["issues"] if i["kind"] == "partition"], result


def test_propose_query_uses_partition_validation(validate, monkeypatch):
    from semantica_dataagent import mcp_context, query_api
    from semantica_dataagent.mcp_contract import ToolInvalidInput

    monkeypatch.setattr(query_api, "_project_type", lambda _: "data")
    monkeypatch.setattr(mcp_context, "current_requester", lambda: object())

    async def plan(*args, **kwargs):
        return {"accepted": True}

    monkeypatch.setattr(query_api, "_create_plan", plan)
    import asyncio

    assert asyncio.run(
        query_api.propose_query("SELECT id FROM shop.orders WHERE dt = CURRENT_DATE()")
    ) == {"accepted": True}
    with pytest.raises(ToolInvalidInput):
        asyncio.run(
            query_api.propose_query(
                "SELECT id FROM shop.orders WHERE dt <= CURRENT_DATE()"
            )
        )
