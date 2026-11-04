import json
from pathlib import Path

import pytest

from semantica_dataagent.sql_validation import Snapshot, check_sql, join_path


def snapshot():
    root = Path(__file__).resolve().parents[2] / "samples" / "demo"
    tables = json.loads((root / "tables.json").read_text())
    fields = json.loads((root / "fields.json").read_text())
    return Snapshot(
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


@pytest.mark.parametrize("dialect", ["hive", "spark", "doris"])
def test_valid_sql_dialects(dialect):
    assert (
        check_sql("SELECT o.id FROM shop.orders o", dialect, snapshot())["status"]
        == "pass"
    )


def test_invalid_and_suggestions():
    assert check_sql("SELECT FROM", "hive", snapshot())["syntax"]["status"] == "error"
    result = check_sql("SELECT missing FROM shop.ordrs", "hive", snapshot())
    assert result["tables"][0]["status"] == "error"
    assert "shop.orders" in result["tables"][0]["suggestions"]
    assert (
        check_sql("SELECT missing FROM shop.orders", "hive", snapshot())["fields"][0][
            "status"
        ]
        == "error"
    )


def test_join_and_unknown():
    result = check_sql(
        "SELECT o.id FROM shop.orders o JOIN shop.customers c ON o.created_at = c.id",
        "hive",
        snapshot(),
    )
    assert result["joins"][0]["status"] == "unsupported"
    result = check_sql("SELECT x.id FROM mystery x", "hive", snapshot())
    assert result["fields"][0]["status"] == "unknown"
    assert join_path(["shop.orders", "shop.customers"], snapshot())["status"] == "found"


def test_derived_columns_and_failed_lineage():
    data = snapshot()
    data.edges.append(
        {
            "upstream": "shop.orders",
            "downstream": "shop.customers",
            "parse_status": "failed",
            "evidence": "declared",
            "ref": "task:failed",
        }
    )
    result = check_sql(
        "WITH x AS (SELECT id AS order_id FROM shop.orders) SELECT x.order_id FROM x",
        "hive",
        data,
    )
    assert result["status"] == "pass", result
    result = check_sql(
        "SELECT o.id FROM shop.orders o JOIN shop.customers c ON o.id = c.created_at",
        "hive",
        data,
    )
    assert result["joins"][0]["evidence"][0]["parse_status"] == "failed"
    assert result["joins"][0]["evidence"][0]["ref"] == "task:failed"


def test_no_path_between_known_unrelated_tables():
    data = snapshot()
    data.tables.append(
        {
            "database": "shop",
            "name": "isolated",
            "ref": "table:shop.isolated",
            "fields": [],
        }
    )
    assert join_path(["shop.orders", "shop.isolated"], data)["status"] == "no_path"


def test_disjunctive_join_is_not_approved():
    result = check_sql(
        "SELECT o.id FROM shop.orders o JOIN shop.customers c ON o.id = c.id OR 1 = 1",
        "hive",
        snapshot(),
    )
    assert result["joins"][0]["status"] != "supported"
