import sys
from pathlib import Path

from semantica_dataagent.canonical import Record
from semantica_dataagent.filters import DEFAULT_FILTERS, filter_records


def test_installed_entry_point_is_discovered(tmp_path, monkeypatch):
    # A dist-info directory is the installed metadata read by importlib.metadata.
    fixture = Path(__file__).parent / "fixtures" / "third_party"
    monkeypatch.syspath_prepend(str(fixture))
    info = tmp_path / "dataagent_test_plugin-1.0.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Name: dataagent-test-plugin\nVersion: 1.0.0\n")
    (info / "entry_points.txt").write_text(
        "[semantica_dataagent.source_adapters]\nthird_party = third_party:Adapter\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    from semantica_dataagent.plugins import discover

    assert discover()["source", "third_party"].display_name == "Third party"
    assert discover()["source", "canonical"].id == "canonical"
    sys.modules.pop("third_party", None)


def test_filter_defaults_and_custom_patterns():
    records = [
        Record("table", {"database": db, "name": "t"}) for db in ("tmp", "test", "prod")
    ]
    records += [
        Record("task", {"name": "daily_load"}),
        Record("task", {"name": "adhoc"}),
    ]
    kept, count = filter_records(records, DEFAULT_FILTERS)
    assert count == 2
    assert [r.data.get("database") for r in kept[:1]] == ["prod"]
    rules = {**DEFAULT_FILTERS, "database_exclude": [], "task_include": ["daily*"]}
    kept, count = filter_records(records, rules)
    assert count == 1
    assert [r.data.get("database") for r in kept[:3]] == ["tmp", "test", "prod"]
