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


def test_partition_issue_lists_table_field_message_and_fix():
    data = snapshot()
    result = check_sql(
        "SELECT o.id FROM shop.orders o",
        "hive",
        data,
        partition_tables=data.tables,
    )

    assert result["status"] == "issues"
    issue = next(item for item in result["issues"] if item["kind"] == "partition")
    assert issue["table"] == "shop.orders"
    assert issue["field"] == "dt"
    assert "缺少分区条件" in issue["message"]
    assert "MAX(dt)" in issue["suggestion"]
    assert "不确定时按快照" in issue["suggestion"]
    assert "业务时间" in issue["suggestion"]


@pytest.mark.parametrize(
    "sql",
    [
        (
            "SELECT o.id FROM shop.orders o "
            "WHERE o.dt = (SELECT MAX(dt) FROM shop.orders)"
        ),
        (
            "SELECT o.id FROM shop.orders o "
            "WHERE o.dt >= '2025-01-01' AND o.dt <= '2025-01-31'"
        ),
    ],
)
def test_partitioned_sql_passes_with_no_issues(sql):
    data = snapshot()
    result = check_sql(sql, "hive", data, partition_tables=data.tables)

    assert result["status"] == "pass", result
    assert result["issues"] == []


def test_existing_problems_are_also_listed_in_issues():
    data = snapshot()
    result = check_sql(
        "SELECT missing FROM shop.ordrs",
        "hive",
        data,
        partition_tables=data.tables,
    )

    assert result["status"] == "issues"
    kinds = {item["kind"] for item in result["issues"]}
    assert {"table", "field"} <= kinds
    table_issue = next(item for item in result["issues"] if item["kind"] == "table")
    assert table_issue["table"] == "shop.ordrs"
    assert table_issue["suggestion"]


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
    assert (
        join_path(["shop.orders", "shop.customers"], snapshot())["status"] == "no_path"
    )


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
    data = Snapshot(data.tables, data.edges, [])
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


def indexed_snapshot():
    tables = [
        {
            "database": "warehouse",
            "name": f"t{i}",
            "ref": f"table:warehouse.t{i}",
            "fields": [{"name": "id"}, {"name": "wide_id"}],
        }
        for i in range(50)
    ]
    tables[0]["fields"].append({"name": "special_id", "ref": "field:a.special_id"})
    tables[1]["fields"].append({"name": "special_id", "ref": "field:b.special_id"})
    return Snapshot(tables, [], [])


def test_generic_keys_are_not_evidence_and_specific_keys_are_weak():
    data = indexed_snapshot()
    generic = check_sql(
        "SELECT a.id FROM warehouse.t2 a JOIN warehouse.t3 b ON a.id = b.id",
        "hive",
        data,
    )
    assert generic["joins"][0]["status"] == "unsupported"
    assert join_path(["warehouse.t2", "warehouse.t3"], data)["status"] == "no_path"
    broad = check_sql(
        "SELECT a.id FROM warehouse.t2 a JOIN warehouse.t3 b ON a.wide_id = b.wide_id",
        "hive",
        data,
    )
    assert broad["joins"][0]["status"] == "unsupported"
    weak = check_sql(
        "SELECT a.id FROM warehouse.t0 a JOIN warehouse.t1 b ON a.special_id = b.special_id",
        "hive",
        data,
    )
    assert weak["joins"][0]["status"] == "weak"
    assert weak["status"] == "issues"
    assert (
        join_path(["warehouse.t0", "warehouse.t1"], data)["steps"][0]["strength"]
        == "weak"
    )


def test_strong_path_preferred_to_weak_direct_path():
    data = indexed_snapshot()
    data = Snapshot(
        data.tables,
        [
            {"upstream": "warehouse.t0", "downstream": "warehouse.t2", "ref": "task:a"},
            {"upstream": "warehouse.t2", "downstream": "warehouse.t1", "ref": "task:b"},
        ],
        [],
    )
    path = join_path(["warehouse.t0", "warehouse.t1"], data)
    assert len(path["steps"]) == 2
    assert all(s["strength"] == "strong" for s in path["steps"])


def test_realistic_snapshot_scale():
    import time

    tables = [
        {
            "database": "warehouse",
            "name": f"t{i}",
            "ref": f"table:warehouse.t{i}",
            "fields": [{"name": f"f{j}"} for j in range(24)] + [{"name": "id"}],
        }
        for i in range(2000)
    ]
    edges = [
        {
            "upstream": f"warehouse.t{i % 1999}",
            "downstream": f"warehouse.t{i % 1999 + 1}",
            "ref": f"task:{i}",
        }
        for i in range(5000)
    ]
    start = time.monotonic()
    data = Snapshot(tables, edges, [])
    assert join_path(["warehouse.t0", "warehouse.t1999"], data)["status"] == "found"
    assert time.monotonic() - start < 2
    start = time.monotonic()
    assert (
        check_sql(
            "SELECT a.id FROM warehouse.t0 a JOIN warehouse.t1 b ON a.id = b.id",
            "hive",
            data,
        )["status"]
        == "pass"
    )
    assert time.monotonic() - start < 2


def test_document_makes_weak_join_strong():
    data = indexed_snapshot()
    data.documents.append(
        {
            "ref": "document:join",
            "content": "warehouse.t0.special_id = warehouse.t1.special_id",
        }
    )
    checked = check_sql(
        "SELECT a.id FROM warehouse.t0 a JOIN warehouse.t1 b ON a.special_id = b.special_id",
        "hive",
        data,
    )
    assert checked["status"] == "pass"
    assert any(
        e["type"] == "document" and e["ref"] == "document:join"
        for e in checked["joins"][0]["evidence"]
    )
    assert join_path(["warehouse.t0", "warehouse.t1"], data)["strength"] == "strong"


def test_key_frequency_threshold_in_small_and_large_generations():
    def generation(count, owners, key):
        return Snapshot(
            [
                {
                    "database": "warehouse",
                    "name": f"t{i}",
                    "ref": f"table:warehouse.t{i}",
                    "fields": [{"name": "id"}]
                    + ([{"name": key}] if i < owners else []),
                }
                for i in range(count)
            ],
            [],
            [],
        )

    small = generation(21, 2, "order_id")
    sql = (
        "SELECT a.id FROM warehouse.t0 a JOIN warehouse.t1 b ON a.order_id = b.order_id"
    )
    assert check_sql(sql, "hive", small)["joins"][0]["status"] == "weak"
    assert join_path(["warehouse.t0", "warehouse.t1"], small)["strength"] == "weak"
    for count, owners, expected in [
        (21, 5, "weak"),
        (21, 6, "unsupported"),
        (2000, 100, "weak"),
        (2000, 101, "unsupported"),
        (2000, 300, "unsupported"),
    ]:
        data = generation(count, owners, "tenant_id")
        checked = check_sql(
            "SELECT a.id FROM warehouse.t0 a JOIN warehouse.t1 b ON a.tenant_id = b.tenant_id",
            "hive",
            data,
        )
        assert checked["joins"][0]["status"] == expected
        generic = check_sql(
            "SELECT a.id FROM warehouse.t0 a JOIN warehouse.t1 b ON a.id = b.id",
            "hive",
            data,
        )
        assert generic["joins"][0]["status"] == "unsupported"
