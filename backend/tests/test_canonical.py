import json
from pathlib import Path

import pytest
from openpyxl import Workbook


def test_canonical_reads_json_csv_and_xlsx(tmp_path: Path):
    from semantica_dataagent.canonical import CanonicalAdapter

    (tmp_path / "tables.json").write_text(
        json.dumps([{"name": "orders", "database": "shop", "comment": "订单"}]),
        encoding="utf-8",
    )
    (tmp_path / "fields.csv").write_text(
        "table,database,name,type,comment,partition\norders,shop,id,string,编号,false\n",
        encoding="utf-8",
    )
    book = Workbook()
    sheet = book.active
    sheet.append(["name", "sql"])
    sheet.append(["load_orders", "select 1"])
    book.save(tmp_path / "tasks.xlsx")
    result = CanonicalAdapter().read(tmp_path, {})
    assert [record.kind for record in result.records] == ["field", "table", "task"]
    assert len(result.hashes) == 3
    assert result.records[0].data["partition"] is False


def test_invalid_record_is_rejected(tmp_path: Path):
    from semantica_dataagent.canonical import CanonicalAdapter

    (tmp_path / "tables.json").write_text('[{"name": "orders"}]', encoding="utf-8")
    with pytest.raises(ValueError, match="tables.json"):
        CanonicalAdapter().read(tmp_path, {})


@pytest.mark.parametrize(
    ("filename", "record", "missing"),
    [
        (
            "tables",
            {"database": "shop", "name": "orders", "comment": "订单"},
            "database",
        ),
        (
            "fields",
            {
                "database": "shop",
                "table": "orders",
                "name": "id",
                "type": "string",
                "comment": "编号",
                "partition": False,
            },
            "partition",
        ),
        ("tasks", {"name": "load", "sql": "select 1"}, "sql"),
        (
            "lineage",
            {
                "upstream": "a",
                "downstream": "b",
                "evidence": "both",
                "parse_status": "success",
            },
            "evidence",
        ),
        (
            "dimension_values",
            {"database": "shop", "table": "channels", "field": "id", "value": "网页"},
            "value",
        ),
        (
            "documents",
            {"category": "metrics", "path": "sales.md", "content": "销售额"},
            "path",
        ),
    ],
)
def test_all_entity_schemas_validate_records(tmp_path, filename, record, missing):
    from semantica_dataagent.canonical import CanonicalAdapter

    path = tmp_path / (filename + ".json")
    path.write_text(json.dumps([record]), encoding="utf-8")
    assert len(CanonicalAdapter().read(tmp_path, {}).records) == 1
    del record[missing]
    path.write_text(json.dumps([record]), encoding="utf-8")
    with pytest.raises(ValueError, match=filename):
        CanonicalAdapter().read(tmp_path, {})


def test_demo_covers_all_six_entities():
    from collections import Counter

    from semantica_dataagent.canonical import CanonicalAdapter

    root = Path(__file__).resolve().parents[2] / "samples" / "demo"
    result = CanonicalAdapter().read(root, {})
    counts = Counter(item.kind for item in result.records)
    assert counts == {
        "table": 20,
        "field": 60,
        "task": 20,
        "lineage": 20,
        "dimension_value": 2,
        "document": 10,
    }
    assert len(result.hashes) == 6


def test_missing_directory_fails(tmp_path):
    from semantica_dataagent.canonical import CanonicalAdapter

    with pytest.raises(ValueError, match="Source directory"):
        CanonicalAdapter().read(tmp_path / "missing", {})
