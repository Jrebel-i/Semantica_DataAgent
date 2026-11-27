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
