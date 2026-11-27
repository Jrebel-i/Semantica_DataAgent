import json
from collections import Counter

from semantica_dataagent.dtb_snapshot import DtbSnapshotReader


def test_dtb_dimension_records_keep_catalog_and_default_identity(tmp_path):
    for directory, meta in (
        ("hive", {"catalog": "hive", "qualified_table": "hive.shop.orders"}),
        ("internal", {"catalog": "internal", "database": "shop", "table": "orders"}),
        ("qualified", {"qualified_table": "hive.shop.orders", "table": "orders"}),
        (
            "qualified_db",
            {
                "qualified_table": "hive.shop.orders",
                "database": "shop",
                "table": "orders",
            },
        ),
        ("default", {"database": "shop", "table": "orders"}),
    ):
        root = tmp_path / "dimensions" / directory
        (root / "current").mkdir(parents=True)
        (root / "meta.json").write_text(
            json.dumps({**meta, "columns": [{"name": "code"}]})
        )
        (root / "current" / "rows.jsonl").write_text(
            json.dumps({"code": directory}) + "\n"
        )
    records = DtbSnapshotReader()._dimensions(tmp_path, {}, {}, Counter())
    items = {r.data["value"]: r.data for r in records}
    assert (
        items["hive"]["catalog"],
        items["hive"]["database"],
        items["hive"]["table"],
        items["hive"]["row_id"],
    ) == ("hive", "shop", "orders", "hive.shop.orders:1")
    assert items["internal"]["row_id"] == "internal.shop.orders:1"
    assert (items["qualified"]["catalog"], items["qualified"]["database"]) == (
        "hive",
        "shop",
    )
    assert items["qualified_db"]["catalog"] == "hive"
    assert "catalog" not in items["default"]
    assert items["default"]["row_id"] == "shop.orders:1"


def test_dimension_groups_and_incremental_selection_are_catalog_scoped():
    from semantica_dataagent.canonical import Record
    from semantica_dataagent.dimension_stage import prepare
    from semantica_dataagent.knowledge import (
        compute_entity_hashes,
        select_incremental_records,
    )

    records = [
        Record(
            "dimension_value",
            {
                "catalog": catalog,
                "database": "shop",
                "table": "orders",
                "field": field,
                "value": value,
                "row_id": "1",
            },
        )
        for catalog, code, name in (
            ("hive", "A", "Hive"),
            ("internal", "A", "Internal"),
        )
        for field, value in (("order_code", code), ("order_name", name))
    ]
    values, statuses = prepare(records, 10)
    codes = [v for v in values if v["field"] == "order_code"]
    assert {(v.get("catalog"), tuple(v["aliases"]), v["ref"]) for v in codes} == {
        ("hive", ("Hive",), "dimension_value:hive.shop.orders.order_code.A"),
        (
            "internal",
            ("Internal",),
            "dimension_value:internal.shop.orders.order_code.A",
        ),
    }
    assert len(statuses) == 4
    hashes = compute_entity_hashes(records)
    hive_key = next(key for key in hashes if "hive" in key)
    assert {
        r.data["catalog"] for r in select_incremental_records(records, {hive_key})
    } == {"hive"}
    assert len(hashes) == 2


def test_lookup_rejects_ambiguous_two_part_name(monkeypatch):
    from types import SimpleNamespace

    import pytest

    from semantica_dataagent import dimension_api
    from semantica_dataagent.mcp_contract import ToolInvalidInput

    monkeypatch.setattr(dimension_api.knowledge, "settings", dict)
    monkeypatch.setattr(
        dimension_api.project_context,
        "from_values",
        lambda _: SimpleNamespace(generation="g"),
    )
    monkeypatch.setattr(dimension_api.knowledge, "read_graph", lambda _: object())
    monkeypatch.setattr(
        dimension_api.knowledge,
        "execute",
        lambda *args: [
            {"catalog": "hive", "database": "shop", "table": "orders"},
            {"catalog": "internal", "database": "shop", "table": "orders"},
        ],
    )
    with pytest.raises(ToolInvalidInput) as exc:
        dimension_api.lookup_dim_values("shop.orders", "A")
    assert all(
        name in str(exc.value)
        for name in ("422", "hive.shop.orders", "internal.shop.orders")
    )


def test_dimension_relationships_are_catalog_scoped(monkeypatch):
    from types import SimpleNamespace

    from semantica_dataagent import relationships

    for incremental in (False, True):
        edges = set()

        def fake_link(storage, label, where, params, query, generation, edges=edges):
            if label != "SDADimensionValue":
                return
            for field_catalog in ("hive", "internal"):
                for value_catalog in ("hive", "internal"):
                    if (
                        "coalesce(f.catalog, '') = coalesce(n.catalog, '')" not in query
                        or field_catalog == value_catalog
                    ):
                        edges.add((field_catalog, value_catalog))

        monkeypatch.setattr(relationships, "_link_nodes", fake_link)
        monkeypatch.setattr(
            relationships, "delete_batches", lambda *args, **kwargs: None
        )
        storage = SimpleNamespace(
            is_incremental=incremental,
            build_id=1,
            rebuilt_field_tables={
                ("hive", "shop", "orders"),
                ("internal", "shop", "orders"),
            },
            rebuilt_dim_tables=set(),
            execute=lambda *args: [],
        )
        relationships.reconcile(storage, [], "g")
        assert edges == {("hive", "hive"), ("internal", "internal")}


def test_single_catalog_preserves_original_dimension_reference():
    from semantica_dataagent.canonical import Record
    from semantica_dataagent.dimension_stage import prepare
    from semantica_dataagent.knowledge import (
        _resolve_legacy_dimensions,
        compute_entity_hashes,
    )

    records = [
        Record(
            "field",
            {"catalog": "hive", "database": "shop", "table": "orders", "name": "code"},
        ),
        Record(
            "dimension_value",
            {
                "catalog": "hive",
                "database": "shop",
                "table": "orders",
                "field": "code",
                "value": "A",
            },
        ),
    ]
    resolved, _ = _resolve_legacy_dimensions(records)
    values, _ = prepare(resolved, 10)
    assert [v["ref"] for v in values] == ["dimension_value:shop.orders.code.A"]
    assert values[0]["catalog"] == "hive"
    multi, _ = _resolve_legacy_dimensions(
        records
        + [
            Record(
                "field",
                {
                    "catalog": "internal",
                    "database": "shop",
                    "table": "orders",
                    "name": "code",
                },
            )
        ]
    )
    multi_values, _ = prepare(multi, 10)
    assert multi_values[0]["ref"] == "dimension_value:hive.shop.orders.code.A"
    assert (
        compute_entity_hashes(resolved)["dim_table:hive.shop.orders"]
        != compute_entity_hashes(multi)["dim_table:hive.shop.orders"]
    )
