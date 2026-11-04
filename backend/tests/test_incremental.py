from semantica_dataagent import knowledge


def test_hash_difference_classifies_changed_added_and_deleted():
    compare = getattr(knowledge, "hash_difference", lambda *_: {})
    assert compare(
        {"a": "1", "b": "2", "gone": "3"}, {"a": "1", "b": "4", "new": "5"}
    ) == {"added": {"new"}, "changed": {"b"}, "deleted": {"gone"}}


def test_source_adapters_report_record_provenance(tmp_path):
    import json

    from semantica_dataagent.canonical import CanonicalAdapter
    from semantica_dataagent.markdown import MarkdownAdapter

    (tmp_path / "tables.json").write_text(
        json.dumps([{"database": "shop", "name": "orders", "comment": "x"}])
    )
    root = tmp_path / "knowledge" / "sales"
    root.mkdir(parents=True)
    (root / "sales.md").write_text("# Sales\nhello")
    canonical = CanonicalAdapter().read(tmp_path, {})
    markdown = MarkdownAdapter().read(tmp_path, {})
    assert canonical.records[0].source_file == "tables.json"
    assert (
        next(r for r in markdown.records if r.data["path"] == "sales.md").source_file
        == "knowledge/sales/sales.md"
    )


def test_incremental_plan_selects_only_records_from_changed_or_new_files():
    from semantica_dataagent.canonical import Record

    records = [
        Record("document", {"path": "a"}, "a.md"),
        Record("document", {"path": "b"}, "b.md"),
        Record("document", {"path": "c"}, "c.md"),
    ]
    plan = getattr(knowledge, "incremental_plan", lambda *_: [])
    assert [
        r.data["path"]
        for r in plan(
            records, {"added": {"c.md"}, "changed": {"a.md"}, "deleted": {"deleted.md"}}
        )
    ] == ["a", "c"]


def test_generated_index_hashes_are_not_counted_as_source_files():
    compare = getattr(knowledge, "source_file_difference", lambda *_: {})
    assert compare(
        {"sales.md": "a", "index.md": "draft1"},
        {"sales.md": "b", "index.md": "draft2"},
        {"index.md"},
        {"index.md"},
    ) == {"added": set(), "changed": {"sales.md"}, "deleted": set()}
    assert compare(
        {"index.md": "draft"}, {"index.md": "real"}, {"index.md"}, set()
    ) == {"added": {"index.md"}, "changed": set(), "deleted": set()}


def test_generated_index_tracks_original_files_and_rebuilds_when_they_change(tmp_path):
    from semantica_dataagent.markdown import MarkdownAdapter

    root = tmp_path / "knowledge" / "sales"
    root.mkdir(parents=True)
    (root / "a.md").write_text("# A\ntext")
    (root / "b.md").write_text("# B\ntext")
    result = MarkdownAdapter().read(tmp_path, {})
    index = next(r for r in result.records if r.data.get("generated"))
    assert index.source_file in {"knowledge/sales/a.md", "knowledge/sales/b.md"}
    assert set(index.source_files) == {"knowledge/sales/a.md", "knowledge/sales/b.md"}
    assert index in knowledge.incremental_plan(
        result.records,
        {"added": set(), "changed": {"knowledge/sales/b.md"}, "deleted": set()},
    )


def test_duplicate_entity_across_source_files_is_rejected():
    import pytest

    from semantica_dataagent.canonical import Record

    validate = getattr(knowledge, "validate_records", lambda *_: None)
    records = [
        Record("table", {"database": "shop", "name": "orders"}, "one/tables.json"),
        Record("table", {"database": "shop", "name": "orders"}, "two/tables.json"),
    ]
    with pytest.raises(ValueError, match="Duplicate"):
        validate(records)


def test_empty_category_has_no_synthetic_source(tmp_path):
    from semantica_dataagent.markdown import MarkdownAdapter

    (tmp_path / "knowledge" / "empty").mkdir(parents=True)
    result = MarkdownAdapter().read(tmp_path, {})
    assert result.records == []
    assert result.hashes == {}


def test_source_hash_read_failure_does_not_replace_previous_build(
    tmp_path, monkeypatch
):
    import pytest

    from semantica_dataagent.canonical import CanonicalAdapter

    path = tmp_path / "tables.json"
    path.write_text('[{"database":"prod","name":"t","comment":""}]')
    original = type(path).read_bytes

    def fail_read(self):
        if self == path:
            raise PermissionError("unreadable")
        return original(self)

    monkeypatch.setattr(type(path), "read_bytes", fail_read)
    with pytest.raises(PermissionError):
        CanonicalAdapter().read(tmp_path, {})


def test_derived_records_with_different_keys_do_not_share_identity():
    from semantica_dataagent.canonical import Record

    records = [
        Record("document", {"path": "index.md"}, "a.md", "index.md", ("a.md", "b.md")),
        Record("document", {"path": "other.md"}, "other.md"),
    ]
    assert knowledge.incremental_plan(
        records, {"added": set(), "changed": {"b.md"}, "deleted": set()}
    ) == [records[0]]


def test_records_from_adapter_without_file_hash_are_rejected(tmp_path):
    import pytest

    from semantica_dataagent.canonical import ReadResult, Record

    class Adapter:
        id = "bad"

        def read(self, directory, parameters):
            return ReadResult(
                [Record("table", {"database": "x", "name": "y"}, "../escape")],
                {"../escape": "hash"},
            )

    with pytest.raises(ValueError):
        knowledge.read_sources(
            {"source_directory": str(tmp_path)}, [("source", Adapter(), True, {})]
        )


def test_legacy_build_migration_preserves_existing_rows(tmp_path, monkeypatch):
    import os

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    from semantica_dataagent import cli

    db = tmp_path / "legacy.db"
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(db))
    config = Config()
    config.set_main_option(
        "script_location", os.path.join(os.path.dirname(cli.__file__), "migrations")
    )
    config.set_main_option("sqlalchemy.url", "sqlite:///" + str(db))
    command.upgrade(config, "0004")
    engine = create_engine("sqlite:///" + str(db))
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO builds (status, started_at, counts) VALUES ('succeeded', '2026-01-01', '{}')"
            )
        )
    command.upgrade(config, "head")
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT status, source_hashes, build_type FROM builds")
        ).one()
    assert tuple(row) == ("succeeded", None, None)
