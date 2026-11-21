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
        (
            "relationships",
            {
                "name": "orders_customer",
                "from_database": "shop",
                "from_table": "orders",
                "from_columns": ["customer_id"],
                "to_database": "shop",
                "to_table": "customers",
                "to_columns": ["id"],
            },
            "from_columns",
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
    from semantica_dataagent.markdown import MarkdownAdapter

    result = CanonicalAdapter().read(root, {})
    docs = MarkdownAdapter().read(root, {})
    counts = Counter(item.kind for item in result.records + docs.records)
    assert counts == {
        "table": 21,
        "field": 62,
        "task": 23,
        "lineage": 22,
        "dimension_value": 10,
        "document": 15,
    }
    assert len(result.hashes) == 5


def test_missing_directory_fails(tmp_path):
    from semantica_dataagent.canonical import CanonicalAdapter

    with pytest.raises(ValueError, match="Source directory"):
        CanonicalAdapter().read(tmp_path / "missing", {})


def test_tables_and_fields_support_optional_catalog(tmp_path: Path):
    from semantica_dataagent.canonical import CanonicalAdapter

    (tmp_path / "tables.json").write_text(
        json.dumps(
            [
                {
                    "catalog": "hive",
                    "database": "shop",
                    "name": "orders",
                    "comment": "订单",
                },
                {"database": "shop", "name": "legacy", "comment": "旧表"},
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "fields.csv").write_text(
        "catalog,database,table,name,type,comment,partition\n"
        "hive,shop,orders,id,string,编号,false\n"
        ",shop,legacy,id,string,编号,false\n",
        encoding="utf-8",
    )
    book = Workbook()
    sheet = book.active
    sheet.append(["catalog", "database", "name", "comment"])
    sheet.append(["internal", "shop", "items", "明细"])
    sheet.append([None, "shop", "bare", "无目录"])
    sub = tmp_path / "extra"
    sub.mkdir()
    book.save(sub / "tables.xlsx")

    result = CanonicalAdapter().read(tmp_path, {})
    tables = {r.data["name"]: r.data for r in result.records if r.kind == "table"}
    fields = {r.data["table"]: r.data for r in result.records if r.kind == "field"}
    assert tables["orders"]["catalog"] == "hive"
    assert "catalog" not in tables["legacy"]
    assert tables["items"]["catalog"] == "internal"
    assert "catalog" not in tables["bare"]
    assert fields["orders"]["catalog"] == "hive"
    assert "catalog" not in fields["legacy"]

    (tmp_path / "tables.json").write_text(
        json.dumps(
            [{"catalog": "", "database": "shop", "name": "orders", "comment": ""}]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="tables.json"):
        CanonicalAdapter().read(tmp_path, {})


def test_canonical_reads_relationships_json_csv_and_xlsx(tmp_path: Path):
    from semantica_dataagent.canonical import CanonicalAdapter

    (tmp_path / "relationships.json").write_text(
        json.dumps(
            [
                {
                    "name": "orders_customer",
                    "from_catalog": "hive",
                    "from_database": "shop",
                    "from_table": "orders",
                    "from_columns": ["customer_id"],
                    "to_catalog": "hive",
                    "to_database": "shop",
                    "to_table": "customers",
                    "to_columns": ["id"],
                }
            ]
        ),
        encoding="utf-8",
    )
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "relationships.csv").write_text(
        "name,from_catalog,from_database,from_table,from_columns,to_catalog,to_database,to_table,to_columns\n"
        'order_items_orders,,shop,order_items,"[""tenant_id"", ""order_id""]",hive,shop,orders,"[""tenant_id"", ""id""]"\n',
        encoding="utf-8",
    )
    xlsx_dir = tmp_path / "xlsx"
    xlsx_dir.mkdir()
    book = Workbook()
    sheet = book.active
    sheet.append(
        [
            "name",
            "from_catalog",
            "from_database",
            "from_table",
            "from_columns",
            "to_catalog",
            "to_database",
            "to_table",
            "to_columns",
        ]
    )
    sheet.append(
        [
            "items_product",
            None,
            "shop",
            "order_items",
            '["product_id"]',
            None,
            "shop",
            "products",
            '["id"]',
        ]
    )
    book.save(xlsx_dir / "relationships.xlsx")

    result = CanonicalAdapter().read(tmp_path, {})
    rels = {r.data["name"]: r.data for r in result.records if r.kind == "relationship"}
    assert rels["orders_customer"]["from_catalog"] == "hive"
    assert rels["order_items_orders"]["from_columns"] == ["tenant_id", "order_id"]
    assert "from_catalog" not in rels["order_items_orders"]
    assert rels["order_items_orders"]["to_catalog"] == "hive"
    assert rels["items_product"]["to_columns"] == ["id"]
    assert "to_catalog" not in rels["items_product"]


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        (
            "relationships.json",
            json.dumps(
                [
                    {
                        "name": "bad",
                        "from_catalog": "",
                        "from_database": "shop",
                        "from_table": "orders",
                        "from_columns": ["customer_id"],
                        "to_database": "shop",
                        "to_table": "customers",
                        "to_columns": ["id"],
                    }
                ]
            ),
        ),
        (
            "relationships.json",
            json.dumps(
                [
                    {
                        "name": "bad",
                        "from_database": "shop",
                        "from_table": "orders",
                        "from_columns": [],
                        "to_database": "shop",
                        "to_table": "customers",
                        "to_columns": ["id"],
                    }
                ]
            ),
        ),
        (
            "relationships.json",
            json.dumps(
                [
                    {
                        "name": "bad",
                        "from_database": "shop",
                        "from_table": "orders",
                        "from_columns": ["customer_id"],
                        "to_database": "shop",
                        "to_table": "customers",
                        "to_columns": ["id"],
                        "extra": True,
                    }
                ]
            ),
        ),
        (
            "relationships.csv",
            (
                "name,from_database,from_table,from_columns,to_database,to_table,to_columns\n"
                "bad,shop,orders,not_json,shop,customers,[id]\n"
            ),
        ),
        (
            "relationships.csv",
            (
                "name,from_database,from_table,from_columns,to_database,to_table,to_columns\n"
                'bad,shop,orders,"""customer_id""",shop,customers,"[""id""]"\n'
            ),
        ),
    ],
)
def test_canonical_rejects_invalid_relationships(
    tmp_path: Path, filename: str, content: str
):
    from semantica_dataagent.canonical import CanonicalAdapter

    (tmp_path / filename).write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=filename):
        CanonicalAdapter().read(tmp_path, {})
