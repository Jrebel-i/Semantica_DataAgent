import json
from collections import Counter

from semantica_dataagent.dtb_snapshot import DtbSnapshotReader


def test_dtb_dimension_records_keep_catalog_and_default_identity(tmp_path):
    for directory, meta in (
        ("hive", {"catalog": "hive", "qualified_table": "hive.shop.orders"}),
        ("internal", {"catalog": "internal", "database": "shop", "table": "orders"}),
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
    assert {(v["catalog"], tuple(v["aliases"]), v["ref"]) for v in codes} == {
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
