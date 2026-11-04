import json
import sys
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from dtb_sample import build_sample
from verify_sample_build import expected_counts

from semantica_dataagent.dtb_source import DtbAdapter
from semantica_dataagent.markdown import MarkdownAdapter, chunks

TASK_COLUMNS = [
    "task_id",
    "task_name",
    "task_type",
    "task_src",
    "task_conf",
    "project_name",
    "upstream_tasks",
    "output_table_name",
]


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def write_tables(path, rows):
    book = Workbook()
    sheet = book.active
    sheet.append(
        [
            "database_name",
            "table_name",
            "column_name",
            "seq_no",
            "column_type",
            "column_comment",
            "output_task_id",
            "comment",
        ]
    )
    for row in rows:
        sheet.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)


def make_raw_root(tmp_path):
    raw = tmp_path / "knowledge_base"
    snapshot = raw / "warehouse/raw/dtb/20260101T000000Z-test"
    write_json(
        snapshot / "任务信息.json",
        {
            "columns": [{"name": name, "type": "string"} for name in TASK_COLUMNS],
            "data": [
                [
                    "1",
                    "t1",
                    "spark sql",
                    "INSERT INTO db.seed SELECT * FROM db.up1",
                    "{}",
                    "p",
                    "2",
                    "db.seed",
                ],
                [
                    "2",
                    "t2",
                    "shell",
                    "insert overwrite table db.up1 select * from db.up2",
                    "{}",
                    "p",
                    "3",
                    "db.up1",
                ],
                [
                    "3",
                    "t3",
                    "merge onself",
                    "",
                    '{"params":{"table":"db.up2"}}',
                    "p",
                    "",
                    "db.up2",
                ],
                [
                    "4",
                    "t4",
                    "spark sql",
                    "INSERT INTO dimensions.dim_x SELECT * FROM db.other",
                    "{}",
                    "p",
                    "",
                    "dimensions.dim_x",
                ],
            ],
        },
    )
    write_tables(
        snapshot / "表信息.xlsx",
        [
            ["db", "seed", "id", "1", "string", "seed id", "1", "seed"],
            ["db", "up1", "id", "1", "string", "up1 id", "2", "up1"],
            ["db", "up2", "id", "1", "string", "up2 id", "3", "up2"],
            ["dimensions", "dim_x", "code", "1", "string", "code", "4", "dim"],
            ["db", "other", "id", "1", "string", "unrelated", "", "other"],
        ],
    )
    write_json(snapshot / "manifest.json", {"version": "test"})
    edges = [
        {
            "source_table": "db.up1",
            "target_table": "db.seed",
            "task_id": 1,
            "evidence_sources": ["sql_lineage"],
        },
        {
            "source_table": "db.up2",
            "target_table": "db.up1",
            "task_id": 2,
            "evidence_sources": ["sql_lineage"],
        },
        {
            "source_table": "dimensions.dim_x",
            "target_table": "db.up1",
            "task_id": 2,
            "evidence_sources": ["upstream_tasks"],
        },
        {
            "source_table": "db.other",
            "target_table": "db.unrelated",
            "task_id": 4,
            "evidence_sources": ["sql_lineage"],
        },
    ]
    write_jsonl(raw / "warehouse/facts/_catalog/lineage_edges.jsonl", edges)
    write_jsonl(
        raw / "warehouse/facts/_catalog/tasks.jsonl",
        [
            {
                "task_id": 1,
                "task_name": "t1",
                "task_type": "spark sql",
                "destinations": ["db.seed"],
                "upstream_tasks": [2],
            },
            {
                "task_id": 2,
                "task_name": "t2",
                "task_type": "shell",
                "destinations": ["db.up1"],
                "upstream_tasks": [3],
            },
            {
                "task_id": 3,
                "task_name": "t3",
                "task_type": "merge onself",
                "destinations": ["db.up2"],
                "upstream_tasks": [],
            },
            {
                "task_id": 4,
                "task_name": "t4",
                "task_type": "spark sql",
                "destinations": ["dimensions.dim_x"],
                "upstream_tasks": [],
            },
        ],
    )
    (raw / "quick_base/air/字典").mkdir(parents=True)
    (raw / "quick_base/air/字典/db.seed.md").write_text(
        "# 字典\n\n说明。\n", encoding="utf-8"
    )
    metric = "# 指标\n\n" + "\n\n".join(
        f"值 {i} " + "这是一段用于触发分块的中文内容。" * 30 for i in range(10)
    )
    (raw / "quick_base/air/指标/metric").mkdir(parents=True)
    (raw / "quick_base/air/指标/metric/variant.md").write_text(metric, encoding="utf-8")
    (raw / "business_base/air").mkdir(parents=True)
    (raw / "business_base/air/biz.md").write_text(
        "# 业务\n\n说明。\n", encoding="utf-8"
    )
    meta = {
        "database": "dimensions",
        "qualified_table": "dimensions.dim_x",
        "table": "dim_x",
        "columns": [{"name": "code"}, {"name": "name"}],
        "record_count": 3,
        "truncated": False,
    }
    write_json(raw / "master_data/raw/dimensions/dim_x/meta.json", meta)
    write_jsonl(
        raw / "master_data/raw/dimensions/dim_x/current/rows.jsonl",
        [
            {"code": "A", "name": "a"},
            {"code": "B", "name": "b"},
            {"code": "C", "name": "c"},
        ],
    )
    return raw


def test_sample_writes_a_buildable_dtb_and_markdown_source(tmp_path):
    raw = make_raw_root(tmp_path)
    output = tmp_path / "sample"
    manifest = build_sample(
        raw,
        output,
        depth=2,
        dimension_row_limit=2,
        metric_char_limit=120,
    )
    assert manifest["counts"] == {
        "tables": 4,
        "fields": 4,
        "tasks": 4,
        "dimension_tables": 1,
        "dimension_rows": 2,
        "documents": 3,
        "chunks": 5,
    }
    assert manifest["selection"]["levels"] == [1, 1, 2]
    assert manifest["embedding"]["vectors"] == 17
    assert expected_counts(output) == {
        "table": 4,
        "field": 4,
        "task": 4,
        "chunk": 5,
        "document": 6,
        "filtered": 0,
    }

    adapter = DtbAdapter()
    result = adapter.read(
        output, {"snapshot_path": "dtb", "dimensions_path": "dimensions"}
    )
    kinds = [record.kind for record in result.records]
    assert kinds.count("table") == 4
    assert kinds.count("field") == 4
    assert kinds.count("task") == 4
    assert kinds.count("dimension_value") > 0

    tasks = json.loads((output / "dtb/任务信息.json").read_text(encoding="utf-8"))
    assert {row[0] for row in tasks["data"]} == {"1", "2", "3", "4"}
    book = load_workbook(output / "dtb/表信息.xlsx", read_only=True)
    try:
        rows = list(book.active.values)
    finally:
        book.close()
    assert {f"{row[0]}.{row[1]}" for row in rows[1:]} == {
        "db.seed",
        "db.up1",
        "db.up2",
        "dimensions.dim_x",
    }

    documents = MarkdownAdapter().read(output, {"path": "knowledge"}).records
    source_docs = [
        record.data for record in documents if not record.data.get("generated")
    ]
    assert len(source_docs) == 3
    assert len([record for record in documents if record.data.get("generated")]) == 3
    metric = next(d for d in source_docs if d["path"].endswith("variant.md"))
    original = make_raw_root(tmp_path / "again")
    original_metric = (original / "quick_base/air/指标/metric/variant.md").read_text(
        encoding="utf-8"
    )
    assert len(chunks(metric["content"])) < len(chunks(original_metric))


def test_sample_rejects_a_budget_that_is_too_small(tmp_path):
    raw = make_raw_root(tmp_path)
    output = tmp_path / "sample"
    with pytest.raises(ValueError, match="above budget"):
        build_sample(raw, output, max_vectors=1)
    assert not output.exists()
