import json
from pathlib import Path

import pytest
from jsonschema import ValidationError, validate

from semantica_dataagent.canonical import SCHEMAS
from semantica_dataagent.dtb_source import DtbAdapter

FIXTURES = Path(__file__).parent / "fixtures" / "dtb_source"
LATEST = "20260202T000000Z-bbbb"


def values(result, kind):
    return [record.data for record in result.records if record.kind == kind]


def test_reads_latest_snapshot_and_reports_unknown_values():
    result = DtbAdapter().read(FIXTURES, {})

    tasks = {item["name"]: item for item in values(result, "task")}
    assert "old_task" not in tasks
    assert len(tasks) == 10
    merge = tasks["air_orders_merge"]
    assert merge["type"] == "merge_onself"
    assert merge["parameters"]["params"]["table"] == "dwd.air_orders_d"
    assert "parameters" not in tasks["air_bad_conf"]

    fields = {
        (item["database"], item["table"], item["name"]): item
        for item in values(result, "field")
    }
    assert fields[("dwd", "air_orders_d", "day_num")]["partition"] is True
    assert fields[("dwd", "air_orders_d", "order_id")]["partition"] is False

    tables = values(result, "table")
    assert {(item["database"], item["name"]) for item in tables} == {
        ("dwd", "air_orders_d"),
        ("dwd", "air_payments_d"),
        ("dwd", "air_tickets_d"),
        ("dimensions", "dim_airport"),
        ("dwd", "air_broken_d"),
        ("dm", "air_report_d"),
        ("dwd", "air_no_id_d"),
        ("dwd", "air_bad_conf_d"),
        ("dwd", "air_bad_seq_d"),
    }
    assert result.stats == {
        "snapshot_directories": 2,
        "task_conf_double_encoded": 1,
        "task_conf_empty": 1,
        "task_conf_unreadable": 1,
        "task_rows_truncated": 1,
        "tasks_without_id": 1,
        "tasks_without_output": 1,
        "upstream_refs_unresolved": 1,
        "table_columns_missing": 1,
        "invalid_seq_no": 1,
        "dimension_tables": 5,
        "dimension_tables_without_meta": 1,
        "dimension_tables_without_rows": 1,
        "dimension_truncated_tables": 1,
        "dimension_rows": 6,
        "dimension_rows_unreadable": 1,
        "dimension_empty_cells": 2,
        "files_hashed": 9,
    }
    assert result.hashes[f"dtb/{LATEST}/manifest.json"]
    assert result.hashes[f"dtb/{LATEST}/任务信息.json"]
    assert result.hashes[f"dtb/{LATEST}/表信息.xlsx"]
    assert result.hashes["dimensions/dim_airport/current/rows.jsonl"]


def test_lineage_uses_declared_upstream_tasks_and_output_tables():
    result = DtbAdapter().read(FIXTURES, {})
    edges = {
        (item["upstream"], item["downstream"], item["task_id"])
        for item in values(result, "lineage")
    }
    assert ("dwd.air_orders_d", "dwd.air_payments_d", "air_payments_livy") in edges
    assert ("dwd.air_payments_d", "dwd.air_tickets_d", "air_tickets_basic") in edges
    assert ("dwd.air_orders_d", "dwd.air_tickets_d", "air_tickets_basic") in edges
    assert ("dwd.air_orders_d", "dwd.air_bad_seq_d", "air_short_row") in edges
    assert all(item["evidence"] == "declared" for item in values(result, "lineage"))
    assert not any(
        item["upstream"] == item["downstream"] for item in values(result, "lineage")
    )


def test_dimension_rows_keep_raw_values_and_report_truncation():
    result = DtbAdapter().read(FIXTURES, {})
    rows = values(result, "dimension_value")
    assert any(
        item["table"] == "dim_airport"
        and item["field"] == "airport_code"
        and item["value"] == " PVG "
        for item in rows
    )
    assert not any(item["value"].strip() == "" for item in rows)
    assert {item["table"] for item in rows} == {
        "dim_airport",
        "dim_truncated",
        "dim_bad_row",
    }
    limited = DtbAdapter().read(
        FIXTURES,
        {"dimension_row_limit": 2, "dimension_tables": ["dim_airport", "missing"]},
    )
    assert limited.stats["dimension_row_limited_tables"] >= 1
    assert limited.stats["dimension_tables_requested_missing"] == 1
    assert limited.stats["dimension_rows"] < result.stats["dimension_rows"]
    none = DtbAdapter().read(FIXTURES, {"dimension_tables": []})
    assert values(none, "dimension_value") == []
    assert none.stats.get("dimension_tables", 0) == 0


@pytest.mark.parametrize(
    ("parameters", "message"),
    [
        ({"latest": False}, "snapshot is required"),
        ({"snapshot": "missing"}, "no task file"),
        ({"snapshot_path": "../outside"}, "relative"),
        ({"dimensions_path": "/absolute"}, "relative"),
        ({"snapshot_path": 1}, "is not of type"),
    ],
)
def test_invalid_parameters_are_rejected(parameters, message):
    error = ValidationError if message == "is not of type" else ValueError
    with pytest.raises(error, match=message):
        DtbAdapter().read(FIXTURES, parameters)


def test_explicit_snapshot_overrides_latest():
    result = DtbAdapter().read(
        FIXTURES, {"snapshot": "20260101T000000Z-aaaa", "snapshot_path": "dtb"}
    )
    assert [item["name"] for item in values(result, "task")] == ["old_task"]
    assert {item["name"] for item in values(result, "table")} == {"old_table_d"}


def test_records_validate_against_canonical_schemas():
    result = DtbAdapter().read(FIXTURES, {})
    schemas = {
        kind: json.loads((SCHEMAS / f"{kind}.schema.json").read_text())
        for kind in {record.kind for record in result.records}
    }
    for record in result.records:
        validate(record.data, schemas[record.kind])


def test_structural_file_errors_are_not_treated_as_empty(tmp_path):
    snapshot = tmp_path / "dtb" / LATEST
    snapshot.mkdir(parents=True)
    (snapshot / "任务信息.json").write_text("{broken", encoding="utf-8")
    (snapshot / "表信息.xlsx").write_bytes(b"")
    with pytest.raises(ValueError, match="Invalid DTB task file"):
        DtbAdapter().read(tmp_path, {})


def test_every_record_carries_source_provenance():
    from semantica_dataagent import knowledge

    result = DtbAdapter().read(FIXTURES, {})
    assert all(record.source_file in result.hashes for record in result.records)
    lineage = [record for record in result.records if record.kind == "lineage"]
    assert lineage
    assert all(set(record.source_files) <= set(result.hashes) for record in lineage)
    records, hashes, commit, stats = knowledge.read_sources(
        {"source_directory": str(FIXTURES)}, [("source", DtbAdapter(), True, {})]
    )
    assert len(records) == len(result.records)
    assert hashes == result.hashes
    assert commit is None
    assert stats["dtb_files_hashed"] == len(result.hashes)
