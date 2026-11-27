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


def _catalog_relationship_snapshot():
    tables = [
        {
            "catalog": "hive",
            "database": "shop",
            "name": "orders",
            "ref": "table:hive.shop.orders",
            "fields": [
                {"name": "id", "partition": False},
                {"name": "tenant_id", "partition": False},
                {"name": "customer_id", "partition": False},
                {"name": "dt", "partition": True},
            ],
        },
        {
            "catalog": "hive",
            "database": "shop",
            "name": "customers",
            "ref": "table:hive.shop.customers",
            "fields": [
                {"name": "id", "partition": False},
                {"name": "name", "partition": False},
            ],
        },
        {
            "catalog": "hive",
            "database": "shop",
            "name": "order_items",
            "ref": "table:hive.shop.order_items",
            "fields": [
                {"name": "tenant_id", "partition": False},
                {"name": "order_id", "partition": False},
                {"name": "sku", "partition": False},
            ],
        },
    ]
    relationships = [
        {
            "name": "orders_customer",
            "ref": "relationship:hive.shop.orders:hive.shop.customers:orders_customer",
            "from_table": "hive.shop.orders",
            "to_table": "hive.shop.customers",
            "from_columns": ["customer_id"],
            "to_columns": ["id"],
        },
        {
            "name": "items_order",
            "ref": "relationship:hive.shop.order_items:hive.shop.orders:items_order",
            "from_table": "hive.shop.order_items",
            "to_table": "hive.shop.orders",
            "from_columns": ["tenant_id", "order_id"],
            "to_columns": ["tenant_id", "id"],
        },
    ]
    return Snapshot(tables, [], [], relationships=relationships)


def test_find_join_path_uses_relationships_as_strong_evidence_with_column_pairs():
    data = _catalog_relationship_snapshot()

    forward = join_path(["hive.shop.orders", "hive.shop.customers"], data)
    assert forward["status"] == "found"
    assert forward["strength"] == "strong"
    assert len(forward["steps"]) == 1
    step = forward["steps"][0]
    assert step["from"] == "hive.shop.orders"
    assert step["to"] == "hive.shop.customers"
    assert step["keys"] == ["customer_id=id"]
    assert step["strength"] == "strong"
    assert step["evidence"] == [
        {
            "type": "relationship",
            "strength": "strong",
            "name": "orders_customer",
            "ref": "relationship:hive.shop.orders:hive.shop.customers:orders_customer",
            "from_columns": ["customer_id"],
            "to_columns": ["id"],
        }
    ]

    reverse = join_path(["hive.shop.customers", "hive.shop.orders"], data)
    assert reverse["status"] == "found"
    assert reverse["strength"] == "strong"
    assert reverse["steps"][0]["keys"] == ["id=customer_id"]

    composite = join_path(["hive.shop.order_items", "hive.shop.orders"], data)
    assert composite["status"] == "found"
    assert composite["strength"] == "strong"
    assert composite["steps"][0]["keys"] == ["tenant_id", "order_id=id"]

    multi_hop = join_path(["hive.shop.order_items", "hive.shop.customers"], data)
    assert multi_hop["status"] == "found"
    assert multi_hop["strength"] == "strong"
    assert len(multi_hop["steps"]) == 2

    assert join_path(["shop.orders", "shop.customers"], data)["status"] == "no_path"


def test_load_snapshot_reads_relates_to_edges(monkeypatch):
    from semantica_dataagent import knowledge, sql_validation

    sql_validation._snapshot_cache = None
    monkeypatch.setenv("DATAAGENT_DB_PATH", "/tmp/nonexistent-ignored-by-mock.db")
    monkeypatch.setattr(
        sql_validation, "_build_version", lambda pid: (1, (1, "succeeded"))
    )
    monkeypatch.setattr(
        knowledge,
        "settings",
        lambda project_id=None: {
            "neo4j_uri": "bolt://127.0.0.1:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
            "knowledge_generation": "test_gen",
        },
    )
    monkeypatch.setattr(knowledge, "read_graph", lambda values: object())

    def fake_execute(store, query, params):
        if "HAS_FIELD" in query:
            return [
                {
                    "table": {
                        "catalog": "hive",
                        "database": "shop",
                        "name": "orders",
                        "ref": "table:hive.shop.orders",
                    },
                    "fields": [{"name": "customer_id", "partition": False}],
                },
                {
                    "table": {
                        "catalog": "hive",
                        "database": "shop",
                        "name": "customers",
                        "ref": "table:hive.shop.customers",
                    },
                    "fields": [{"name": "id", "partition": False}],
                },
            ]
        if "DEPENDS_ON" in query:
            return []
        if "RELATES_TO" in query:
            return [
                {
                    "relationship": {
                        "name": "orders_customer",
                        "ref": "relationship:hive.shop.orders:hive.shop.customers:orders_customer",
                        "from_table": "hive.shop.orders",
                        "to_table": "hive.shop.customers",
                        "from_columns": ["customer_id"],
                        "to_columns": ["id"],
                    }
                }
            ]
        return []

    monkeypatch.setattr(knowledge, "execute", fake_execute)
    snap = sql_validation.load_snapshot()
    assert len(snap.relationships) == 1
    path = join_path(["hive.shop.orders", "hive.shop.customers"], snap)
    assert path["status"] == "found"
    assert path["strength"] == "strong"
    assert path["steps"][0]["keys"] == ["customer_id=id"]
    sql_validation._snapshot_cache = None


def test_check_sql_supports_single_and_composite_relationships_with_catalog():
    data = _catalog_relationship_snapshot()

    # Single-column FK with 3-part catalog tables and partition filter
    single = check_sql(
        "SELECT o.id, c.name FROM hive.shop.orders o "
        "JOIN hive.shop.customers c ON o.customer_id = c.id "
        "WHERE o.dt = '2025-01-01'",
        "hive",
        data,
        partition_tables=data.tables,
    )
    assert single["status"] == "pass", single
    assert single["joins"][0]["status"] == "supported"
    assert single["joins"][0]["evidence"] == [
        {
            "type": "relationship",
            "strength": "strong",
            "name": "orders_customer",
            "ref": "relationship:hive.shop.orders:hive.shop.customers:orders_customer",
            "from_columns": ["customer_id"],
            "to_columns": ["id"],
        }
    ]

    # Reversed equality order and unaliased 3-part column qualification
    reversed_unaliased = check_sql(
        "SELECT hive.shop.orders.id FROM hive.shop.orders "
        "JOIN hive.shop.customers "
        "ON hive.shop.customers.id = hive.shop.orders.customer_id "
        "WHERE hive.shop.orders.dt = '2025-01-01'",
        "hive",
        data,
        partition_tables=data.tables,
    )
    assert reversed_unaliased["status"] == "pass", reversed_unaliased
    assert reversed_unaliased["joins"][0]["status"] == "supported"

    # Complete composite key (columns written in reverse table order)
    composite_ok = check_sql(
        "SELECT o.id, i.sku FROM hive.shop.orders o "
        "JOIN hive.shop.order_items i "
        "ON o.id = i.order_id AND i.tenant_id = o.tenant_id "
        "WHERE o.dt = '2025-01-01'",
        "hive",
        data,
        partition_tables=data.tables,
    )
    assert composite_ok["status"] == "pass", composite_ok
    assert composite_ok["joins"][0]["status"] == "supported"
    assert {
        "type": "relationship",
        "strength": "strong",
        "name": "items_order",
        "ref": "relationship:hive.shop.order_items:hive.shop.orders:items_order",
        "from_columns": ["tenant_id", "order_id"],
        "to_columns": ["tenant_id", "id"],
    } in composite_ok["joins"][0]["evidence"]

    # Partial composite key (omits order_id = id) must NOT be supported
    composite_partial = check_sql(
        "SELECT o.id, i.sku FROM hive.shop.orders o "
        "JOIN hive.shop.order_items i "
        "ON i.tenant_id = o.tenant_id "
        "WHERE o.dt = '2025-01-01'",
        "hive",
        data,
        partition_tables=data.tables,
    )
    assert composite_partial["status"] == "issues"
    assert composite_partial["joins"][0]["status"] != "supported"
    assert not any(
        e["type"] == "relationship" for e in composite_partial["joins"][0]["evidence"]
    )

    # Disjunctive ON with composite key must NOT be supported
    composite_or = check_sql(
        "SELECT o.id, i.sku FROM hive.shop.orders o "
        "JOIN hive.shop.order_items i "
        "ON (i.tenant_id = o.tenant_id AND i.order_id = o.id) OR 1 = 1 "
        "WHERE o.dt = '2025-01-01'",
        "hive",
        data,
        partition_tables=data.tables,
    )
    assert composite_or["joins"][0]["status"] != "supported"

    # Wrong column pair on related tables must NOT be supported
    wrong_cols = check_sql(
        "SELECT o.id, c.name FROM hive.shop.orders o "
        "JOIN hive.shop.customers c ON o.id = c.id "
        "WHERE o.dt = '2025-01-01'",
        "hive",
        data,
        partition_tables=data.tables,
    )
    assert wrong_cols["joins"][0]["status"] == "unsupported"
    assert wrong_cols["status"] == "issues"


def test_check_sql_distinguishes_multiple_relationships_between_same_tables():
    tables = [
        {
            "database": "shop",
            "name": "orders",
            "ref": "table:shop.orders",
            "fields": [
                {"name": "id", "partition": False},
                {"name": "billing_id", "partition": False},
                {"name": "shipping_id", "partition": False},
            ],
        },
        {
            "database": "shop",
            "name": "addresses",
            "ref": "table:shop.addresses",
            "fields": [
                {"name": "id", "partition": False},
                {"name": "city", "partition": False},
            ],
        },
    ]
    rels = [
        {
            "name": "billing",
            "ref": "relationship:shop.orders:shop.addresses:billing",
            "from_table": "shop.orders",
            "to_table": "shop.addresses",
            "from_columns": ["billing_id"],
            "to_columns": ["id"],
        },
        {
            "name": "shipping",
            "ref": "relationship:shop.orders:shop.addresses:shipping",
            "from_table": "shop.orders",
            "to_table": "shop.addresses",
            "from_columns": ["shipping_id"],
            "to_columns": ["id"],
        },
    ]
    data = Snapshot(tables, [], [], relationships=rels)
    checked = check_sql(
        "SELECT o.id, a.city FROM shop.orders o JOIN shop.addresses a ON o.shipping_id = a.id",
        "hive",
        data,
    )
    assert checked["status"] == "pass"
    assert [e["name"] for e in checked["joins"][0]["evidence"]] == ["shipping"]


def test_issue_335_public_validation_expands_nested_stars(monkeypatch):
    from semantica_dataagent import sql_validation

    data = _catalog_relationship_snapshot()
    monkeypatch.setattr(sql_validation, "load_snapshot", lambda: data)
    base = "WITH c1 AS (SELECT * FROM hive.shop.customers), c2 AS (SELECT c1.*, COUNT(*) AS cnt FROM c1) "
    result = sql_validation.validate_sql(base + "SELECT c2.id, c2.cnt FROM c2")
    assert result["status"] == "pass", result
    result = sql_validation.validate_sql(base + "SELECT c2.nope FROM c2")
    assert any(
        f["name"] == "c2.nope" and f["status"] == "error" for f in result["fields"]
    ), result
    result = sql_validation.validate_sql("SELECT x.nope FROM (SELECT * FROM mystery) x")
    assert any(
        f["name"] == "x.nope" and f["status"] == "unknown" for f in result["fields"]
    ), result


def test_issue_335_public_validation_requires_each_pair(monkeypatch):
    from semantica_dataagent import sql_validation

    data = _catalog_relationship_snapshot()
    data.documents = [
        {
            "ref": "doc:join",
            "content": "hive.shop.orders.customer_id = hive.shop.customers.id",
        }
    ]
    monkeypatch.setattr(sql_validation, "load_snapshot", lambda: data)
    prefix = "SELECT o.id FROM hive.shop.orders o JOIN hive.shop.customers c ON "
    suffix = " WHERE o.dt = '2025-01-01'"
    good = sql_validation.validate_sql(
        prefix + "o.customer_id = c.id AND o.dt = '2025-01-01'" + suffix
    )
    assert good["status"] == "pass", good
    bad = sql_validation.validate_sql(
        prefix + "o.customer_id = c.id AND o.id = c.name" + suffix
    )
    assert any(i["kind"] == "join" and "o.id" in i["message"] for i in bad["issues"]), (
        bad
    )


def test_issue_335_using_and_composite_pairs(monkeypatch):
    from semantica_dataagent import sql_validation

    data = _catalog_relationship_snapshot()
    monkeypatch.setattr(sql_validation, "load_snapshot", lambda: data)
    sql = (
        "SELECT o.id FROM hive.shop.orders o JOIN hive.shop.order_items i "
        "USING (tenant_id) WHERE o.dt = '2025-01-01'"
    )
    result = sql_validation.validate_sql(sql)
    assert result["joins"][0]["status"] != "supported", result
    sql = (
        "SELECT o.id FROM hive.shop.orders o JOIN hive.shop.order_items i "
        "ON o.tenant_id = i.tenant_id AND o.id = i.order_id "
        "WHERE o.dt = '2025-01-01'"
    )
    result = sql_validation.validate_sql(sql)
    assert result["status"] == "pass", result
    sql = (
        "SELECT o.id FROM hive.shop.orders o JOIN hive.shop.order_items i "
        "ON o.tenant_id = i.tenant_id AND o.id = i.order_id "
        "JOIN hive.shop.customers c USING (id) WHERE o.dt = '2025-01-01'"
    )
    # The left input contains both orders.id and (after adding it) order_items.id.
    data.tables[2]["fields"].append({"name": "id", "partition": False})
    result = sql_validation.validate_sql(sql)
    assert result["joins"][1]["status"] != "supported", result


def test_issue_335_using_single_relationship(monkeypatch):
    from semantica_dataagent import sql_validation

    data = _catalog_relationship_snapshot()
    data.relationships[0]["from_columns"] = ["id"]
    data = Snapshot(data.tables, [], [], relationships=data.relationships)
    monkeypatch.setattr(sql_validation, "load_snapshot", lambda: data)
    result = sql_validation.validate_sql(
        "SELECT o.id FROM hive.shop.orders o JOIN hive.shop.customers c USING (id) WHERE o.dt = '2025-01-01'"
    )
    assert result["status"] == "pass", result


def test_issue_335_document_proves_only_one_composite_pair(monkeypatch):
    from semantica_dataagent import sql_validation

    original = _catalog_relationship_snapshot()
    data = Snapshot(
        original.tables,
        [],
        [
            {
                "ref": "document:join",
                "content": "hive.shop.orders.tenant_id = hive.shop.order_items.tenant_id",
            }
        ],
    )
    monkeypatch.setattr(sql_validation, "load_snapshot", lambda: data)
    result = sql_validation.validate_sql(
        "SELECT o.id FROM hive.shop.orders o JOIN hive.shop.order_items i "
        "ON o.tenant_id = i.tenant_id AND o.id = i.sku "
        "WHERE o.dt = '2025-01-01'"
    )
    assert any(
        i["kind"] == "join" and "o.id = i.sku" in i["message"] for i in result["issues"]
    ), result


def test_issue_335_union_star_does_not_report_known_missing_field(monkeypatch):
    from semantica_dataagent import sql_validation

    data = _catalog_relationship_snapshot()
    monkeypatch.setattr(sql_validation, "load_snapshot", lambda: data)
    result = sql_validation.validate_sql(
        "SELECT x.id FROM (SELECT * FROM hive.shop.customers UNION ALL "
        "SELECT * FROM hive.shop.customers) x"
    )
    assert not any(
        f["name"] == "x.id" and f["status"] == "error" for f in result["fields"]
    ), result


def test_issue_335_unqualified_join_column_is_checked(monkeypatch):
    from semantica_dataagent import sql_validation

    data = _catalog_relationship_snapshot()
    monkeypatch.setattr(sql_validation, "load_snapshot", lambda: data)
    result = sql_validation.validate_sql(
        "SELECT o.id FROM hive.shop.orders o JOIN hive.shop.order_items i "
        "ON o.tenant_id = i.tenant_id AND o.id = i.order_id AND o.customer_id = sku "
        "WHERE o.dt = '2025-01-01'"
    )
    assert any(
        issue["kind"] == "join"
        and "customer_id" in issue["message"]
        and "sku" in issue["message"]
        for issue in result["issues"]
    ), result


def test_issue_335_strong_relationship_needs_no_document_lookup(monkeypatch):
    from semantica_dataagent import sql_validation

    data = _catalog_relationship_snapshot()

    def unavailable(_pairs):
        raise RuntimeError("documents unavailable")

    data.document_loader = unavailable
    monkeypatch.setattr(sql_validation, "load_snapshot", lambda: data)
    result = sql_validation.validate_sql(
        "SELECT o.id FROM hive.shop.orders o JOIN hive.shop.customers c "
        "ON o.customer_id = c.id WHERE o.dt = '2025-01-01'"
    )
    assert result["status"] == "pass", result


def test_issue_335_using_merged_key_on_consecutive_joins(monkeypatch):
    from semantica_dataagent import sql_validation

    tables = [
        {
            "database": "s",
            "name": name,
            "ref": f"table:s.{name}",
            "fields": [{"name": "id", "partition": False}],
        }
        for name in ("a", "b", "c")
    ]
    rels = [
        {
            "name": left + right,
            "from_table": "s." + left,
            "to_table": "s." + right,
            "from_columns": ["id"],
            "to_columns": ["id"],
        }
        for left, right in (("a", "b"), ("a", "c"))
    ]
    data = Snapshot(tables, [], [], relationships=rels)

    def unavailable(_pairs):
        raise RuntimeError("documents unavailable")

    data.document_loader = unavailable
    monkeypatch.setattr(sql_validation, "load_snapshot", lambda: data)
    result = sql_validation.validate_sql(
        "SELECT a.id FROM s.a a JOIN s.b b USING (id) JOIN s.c c USING (id)"
    )
    assert result["status"] == "pass", result


def test_issue_335_duplicate_source_alias_returns_issue(monkeypatch):
    from semantica_dataagent import sql_validation

    data = _catalog_relationship_snapshot()
    monkeypatch.setattr(sql_validation, "load_snapshot", lambda: data)
    from sqlglot.errors import OptimizeError

    try:
        result = sql_validation.validate_sql(
            "SELECT a.id FROM hive.shop.orders a JOIN hive.shop.customers a "
            "ON a.customer_id = a.id WHERE a.dt = '2025-01-01'"
        )
    except OptimizeError:
        result = None
    assert result is not None, "duplicate alias must return a structured issue"
    assert result["status"] == "issues", result
    assert any("a" in issue["message"] for issue in result["issues"]), result


def test_issue_335_cte_column_aliases_after_star_expansion(monkeypatch):
    from semantica_dataagent import sql_validation

    data = _catalog_relationship_snapshot()
    monkeypatch.setattr(sql_validation, "load_snapshot", lambda: data)
    result = sql_validation.validate_sql(
        "WITH c(customer_key, customer_name) AS (SELECT * FROM hive.shop.customers) "
        "SELECT c.customer_key, c.customer_name FROM c"
    )
    assert result["status"] == "pass", result
    missing = sql_validation.validate_sql(
        "WITH c(customer_key, customer_name) AS (SELECT * FROM hive.shop.customers) "
        "SELECT c.id FROM c"
    )
    assert any(
        field["name"] == "c.id" and field["status"] == "error"
        for field in missing["fields"]
    ), missing


def test_issue_335_duplicate_star_projection_is_ambiguous(monkeypatch):
    from semantica_dataagent import sql_validation

    data = _catalog_relationship_snapshot()
    monkeypatch.setattr(sql_validation, "load_snapshot", lambda: data)
    result = sql_validation.validate_sql(
        "SELECT x.id FROM (SELECT o.*, c.* FROM hive.shop.orders o "
        "JOIN hive.shop.customers c ON o.customer_id = c.id "
        "WHERE o.dt = '2025-01-01') x"
    )
    assert any(
        field["name"] == "x.id" and field["status"] == "ambiguous"
        for field in result["fields"]
    ), result
