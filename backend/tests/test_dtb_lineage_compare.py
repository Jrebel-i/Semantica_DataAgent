import json
import sys
from pathlib import Path

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from dtb_lineage_compare import compare, render_markdown


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def write_tables(path):
    book = Workbook()
    sheet = book.active
    sheet.append(["database_name", "table_name", "column_name"])
    for database, table in (
        ("db", "orders"),
        ("db", "order_summary"),
        ("db", "shell_orders"),
        ("db", "shell_summary"),
    ):
        sheet.append([database, table, "id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)


def make_raw_root(tmp_path):
    raw = tmp_path / "knowledge_base"
    snapshot = raw / "warehouse/raw/dtb/20260101T000000Z-test"
    write_json(
        snapshot / "任务信息.json",
        {
            "columns": [
                {"name": "task_id"},
                {"name": "task_name"},
                {"name": "task_type"},
                {"name": "task_src"},
                {"name": "output_table_name"},
            ],
            "data": [
                [
                    "1",
                    "load_out",
                    "spark sql",
                    "INSERT INTO db.order_summary SELECT * FROM db.orders AS source",
                    "db.order_summary",
                ],
                [
                    "2",
                    "load_shell",
                    "shell",
                    "insert overwrite table db.shell_summary select * from db.shell_orders",
                    "db.shell_summary",
                ],
            ],
        },
    )
    write_tables(snapshot / "表信息.xlsx")
    facts = [
        {
            "source_table": "DB.ORDERS",
            "target_table": "db.order_summary",
            "task_id": 1,
            "evidence_sources": ["sql_lineage"],
        },
        {
            "source_table": "db.shell_orders",
            "target_table": "db.shell_summary",
            "task_id": 2,
            "evidence_sources": ["sql_lineage", "upstream_tasks"],
        },
        {
            "source_table": "db.miss",
            "target_table": "db.order_summary",
            "task_id": 1,
            "evidence_sources": ["sql_lineage"],
        },
    ]
    write_jsonl(raw / "warehouse/facts/_catalog/lineage_edges.jsonl", facts)
    write_jsonl(
        raw / "warehouse/facts/_catalog/tasks.jsonl",
        [
            {
                "task_id": 1,
                "task_name": "load_out",
                "task_type": "spark sql",
                "destinations": ["db.order_summary"],
                "upstream_tasks": [],
            },
            {
                "task_id": 2,
                "task_name": "load_shell",
                "task_type": "shell",
                "destinations": ["db.shell_summary"],
                "upstream_tasks": [],
            },
        ],
    )
    return raw


def test_compare_reports_recall_precision_and_differences(tmp_path):
    raw = make_raw_root(tmp_path)
    report = compare(raw)
    assert report["overall"] == {
        "parsed": 2,
        "facts": 3,
        "intersection": 2,
        "missing": 1,
        "extra": 0,
        "recall": 0.6667,
        "precision": 1.0,
        "f1": 0.8,
    }
    assert report["by_type"]["spark"]["parsed"] == 1
    assert report["by_type"]["spark"]["facts"] == 2
    assert report["by_type"]["spark"]["recall"] == 0.5
    assert report["by_type"]["shell"]["recall"] == 1.0
    assert report["by_type"]["merge_onself"]["parsed"] == 0
    assert report["missing_examples"][0]["upstream"] == "db.miss"
    assert report["extra_examples"] == []
    markdown = render_markdown(report)
    assert "## Overall" in markdown
    assert "db.miss" in markdown
    assert "db.orders AS source" not in markdown


def test_compare_drops_self_edges_and_unknown_edges(tmp_path):
    raw = make_raw_root(tmp_path / "self")
    facts = raw / "warehouse/facts/_catalog/lineage_edges.jsonl"
    rows = [
        json.loads(line)
        for line in facts.read_text(encoding="utf-8").splitlines()
        if line
    ]
    rows.append(
        {
            "source_table": "db.order_summary",
            "target_table": "db.order_summary",
            "task_id": 1,
            "evidence_sources": ["sql_lineage"],
        }
    )
    facts.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    report = compare(raw)
    assert report["overall"]["facts"] == 3
    assert report["overall"]["extra"] == 0
