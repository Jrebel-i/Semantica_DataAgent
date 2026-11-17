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


def test_graph_row_writes_are_split_without_losing_rows(monkeypatch):
    from semantica_dataagent.stage_contract import StorageHandles

    calls = []

    def execute(graph, query, parameters):
        calls.append(parameters["rows"])
        return []

    monkeypatch.setattr(knowledge, "execute", execute)
    monkeypatch.setattr(
        "semantica_dataagent.stage_contract.WRITE_BATCH_SIZE", 2, raising=False
    )
    storage = StorageHandles("test", object(), object(), {})
    storage.execute(
        "UNWIND $rows AS row CREATE (n) SET n = row",
        {"rows": [{"n": i} for i in range(5)]},
    )
    assert calls == [[{"n": 0}, {"n": 1}], [{"n": 2}, {"n": 3}], [{"n": 4}]]


def test_reconcile_limits_relationship_writes(monkeypatch):
    from semantica_dataagent import relationships

    class Storage:
        build_id = 1

        def __init__(self):
            self.linked = []

        def execute(self, query, params):
            if "RETURN count(*) AS deleted" in query:
                return [{"deleted": 0}]
            if "RETURN elementId(n) AS id" in query:
                return (
                    [{"id": str(i)} for i in range(5)]
                    if ":SDADimensionValue" in query
                    else []
                )
            if "HAS_VALUE" in query:
                self.linked.extend(params["ids"])
                assert len(params["ids"]) <= 2
            return []

    monkeypatch.setattr(relationships, "RELATIONSHIP_BATCH_SIZE", 2, raising=False)
    storage = Storage()
    relationships.reconcile(storage, [], "generation")
    assert storage.linked == ["0", "1", "2", "3", "4"]


def test_deletions_are_limited_and_keep_filter_parameters(monkeypatch):
    from semantica_dataagent import stage_contract

    remaining = list(range(5))
    batches = []

    def execute(query, params):
        assert params["files"] == ["changed.json"]
        if "DELETE batch_relationship" in query:
            batches.append("relationships")
            return [{"deleted": 0}]
        assert batches and batches[0] == "relationships"
        assert "WITH n LIMIT $batch_size DETACH DELETE n" in query
        removed = remaining[: params["batch_size"]]
        del remaining[: params["batch_size"]]
        batches.append(removed)
        return [{"deleted": len(removed)}]

    monkeypatch.setattr(stage_contract, "DELETE_BATCH_SIZE", 2, raising=False)
    stage_contract.delete_batches(
        execute,
        "MATCH (n:SDAEntity) WHERE n.source_file IN $files",
        "n",
        {"files": ["changed.json"]},
        detach=True,
    )
    assert batches == ["relationships", [0, 1], [2, 3], [4], []]


def test_filter_and_validate_records_for_relationships():
    import pytest

    from semantica_dataagent.canonical import Record
    from semantica_dataagent.filters import DEFAULT_FILTERS, filter_records

    kept_rel = Record(
        "relationship",
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
        },
        "a/relationships.json",
    )
    excluded_from = Record(
        "relationship",
        {
            "name": "tmp_rel",
            "from_database": "tmp",
            "from_table": "stage",
            "from_columns": ["id"],
            "to_database": "shop",
            "to_table": "customers",
            "to_columns": ["id"],
        },
        "a/relationships.json",
    )
    excluded_to = Record(
        "relationship",
        {
            "name": "test_rel",
            "from_database": "shop",
            "from_table": "orders",
            "from_columns": ["id"],
            "to_database": "test",
            "to_table": "customers",
            "to_columns": ["id"],
        },
        "a/relationships.json",
    )
    kept, removed = filter_records(
        [kept_rel, excluded_from, excluded_to], DEFAULT_FILTERS
    )
    assert kept == [kept_rel]
    assert removed == 2

    kept_inc, removed_inc = filter_records(
        [kept_rel, excluded_to],
        {
            "database_include": ["shop"],
            "database_exclude": [],
            "task_include": [],
            "task_exclude": [],
        },
    )
    assert kept_inc == [kept_rel]
    assert removed_inc == 1

    # Same name on different table pair is allowed
    other_rel = Record(
        "relationship",
        {
            "name": "orders_customer",
            "from_catalog": "hive",
            "from_database": "shop",
            "from_table": "invoices",
            "from_columns": ["customer_id"],
            "to_catalog": "hive",
            "to_database": "shop",
            "to_table": "customers",
            "to_columns": ["id"],
        },
        "b/relationships.json",
    )
    knowledge.validate_records([kept_rel, other_rel])

    # Unequal column counts rejected
    mismatched = Record(
        "relationship",
        {
            "name": "bad_cols",
            "from_database": "shop",
            "from_table": "orders",
            "from_columns": ["a", "b"],
            "to_database": "shop",
            "to_table": "customers",
            "to_columns": ["id"],
        },
        "a/relationships.json",
    )
    with pytest.raises(ValueError, match="column"):
        knowledge.validate_records([mismatched])

    # Duplicate relationship rejected
    dup_rel = Record(
        "relationship",
        dict(kept_rel.data),
        "b/relationships.json",
    )
    with pytest.raises(ValueError, match="Duplicate"):
        knowledge.validate_records([kept_rel, dup_rel])


def test_reconcile_validates_and_writes_relates_to_edges():
    import pytest

    from semantica_dataagent.canonical import Record
    from semantica_dataagent.relationships import reconcile

    class Storage:
        build_id = 42

        def __init__(self):
            self.queries = []
            self.rel_rows = []

        def execute(self, query, params):
            self.queries.append((query, params))
            if "RETURN count(*) AS deleted" in query:
                return [{"deleted": 0}]
            if "RETURN elementId(n) AS id" in query:
                return []
            if "RELATES_TO" in query and "UNWIND $rows AS row" in query:
                self.rel_rows.extend(params["rows"])
            return []

    tables = [
        Record(
            "table",
            {"catalog": "hive", "database": "shop", "name": "orders", "comment": ""},
            "tables.json",
        ),
        Record(
            "table",
            {"catalog": "hive", "database": "shop", "name": "customers", "comment": ""},
            "tables.json",
        ),
        Record(
            "table",
            {
                "catalog": "internal",
                "database": "shop",
                "name": "orders",
                "comment": "",
            },
            "tables.json",
        ),
    ]
    fields = [
        Record(
            "field",
            {
                "catalog": "hive",
                "database": "shop",
                "table": "orders",
                "name": "customer_id",
                "type": "string",
                "comment": "",
                "partition": False,
            },
            "fields.json",
        ),
        Record(
            "field",
            {
                "catalog": "hive",
                "database": "shop",
                "table": "customers",
                "name": "id",
                "type": "string",
                "comment": "",
                "partition": False,
            },
            "fields.json",
        ),
        Record(
            "field",
            {
                "catalog": "internal",
                "database": "shop",
                "table": "orders",
                "name": "only_internal",
                "type": "string",
                "comment": "",
                "partition": False,
            },
            "fields.json",
        ),
    ]
    valid_rel = Record(
        "relationship",
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
        },
        "a/relationships.json",
        source_key="a/relationships.json",
        source_files=("a/relationships.json", "a/extra.yaml"),
    )
    storage = Storage()
    reconcile(storage, [*tables, *fields, valid_rel], "gen1")

    assert len(storage.rel_rows) == 1
    row = storage.rel_rows[0]
    props = row["props"]
    assert props["name"] == "orders_customer"
    assert props["ref"].startswith("relationship:")
    assert props["from_table"] == "hive.shop.orders"
    assert props["to_table"] == "hive.shop.customers"
    assert props["from_columns"] == ["customer_id"]
    assert props["to_columns"] == ["id"]
    assert props["source_file"] == "a/relationships.json"
    assert props["source_key"] == "a/relationships.json"
    assert props["source_files"] == ["a/relationships.json", "a/extra.yaml"]
    assert props["build_id"] == 42

    # Missing source table, target table, source column, target column, or wrong catalog
    bad_cases = [
        {
            **valid_rel.data,
            "from_table": "missing",
        },
        {
            **valid_rel.data,
            "to_table": "missing",
        },
        {
            **valid_rel.data,
            "from_columns": ["missing_col"],
        },
        {
            **valid_rel.data,
            "to_columns": ["missing_col"],
        },
        {
            **valid_rel.data,
            "from_catalog": "internal",
            "from_columns": ["customer_id"],
        },
        {
            **valid_rel.data,
            "from_columns": ["customer_id", "customer_id"],
        },
    ]
    for bad in bad_cases:
        with pytest.raises(ValueError):
            reconcile(
                Storage(),
                [*tables, *fields, Record("relationship", bad, "bad.json")],
                "gen1",
            )


def test_run_build_full_and_incremental_relates_to_with_in_memory_stores(
    tmp_path, monkeypatch
):
    import json
    from contextlib import contextmanager

    from cryptography.fernet import Fernet

    from semantica_dataagent import knowledge, sql_validation
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.db import Build, GenerationPointer, sessions
    from semantica_dataagent.sql_validation import find_join_path, validate_sql

    sql_validation._snapshot_cache = None

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    initialize()

    source = tmp_path / "source"
    source.mkdir()

    class InMemoryGraph:
        def __init__(self):
            self.nodes = []  # list of {"id": int, "labels": set[str], "props": dict}
            self.rels = []  # list of {"type": str, "start": int, "end": int, "props": dict}
            self._next_id = 1

        def _node_matches(self, node, labels, filter_props):
            if not labels <= node["labels"]:
                return False
            return all(node["props"].get(k) == v for k, v in filter_props.items())

        def execute_query(self, query, params):
            q = " ".join(query.split())
            if q.startswith(("CREATE INDEX", "CREATE FULLTEXT INDEX", "CALL db.")):
                return {"success": True, "records": []}
            # Incremental ref lookup
            if (
                "MATCH (n:SDAEntity {generation:$generation}) WHERE n.source_file IN $files"
                in q
                and "RETURN n.ref AS ref" in q
            ):
                files = set(params["files"])
                refs = [
                    {"ref": n["props"]["ref"]}
                    for n in self.nodes
                    if "SDAEntity" in n["labels"]
                    and n["props"].get("generation") == params["generation"]
                    and (
                        n["props"].get("source_file") in files
                        or n["props"].get("source_key") in files
                        or bool(
                            files.intersection(n["props"].get("source_files") or ())
                        )
                    )
                    and n["props"].get("ref")
                ]
                return {"success": True, "records": refs}
            # Incremental RELATES_TO cleanup by source_file/source_key/source_files
            if (
                "MATCH (:SDATable {generation:$generation})-[r:RELATES_TO]->(:SDATable {generation:$generation}) WHERE r.source_file IN $files"
                in q
            ):
                files = set(params["files"])
                keep, deleted = [], 0
                for rel in self.rels:
                    if rel["type"] == "RELATES_TO":
                        start = next(n for n in self.nodes if n["id"] == rel["start"])
                        end = next(n for n in self.nodes if n["id"] == rel["end"])
                        p = rel["props"]
                        if (
                            start["props"].get("generation") == params["generation"]
                            and end["props"].get("generation") == params["generation"]
                            and (
                                p.get("source_file") in files
                                or p.get("source_key") in files
                                or bool(files.intersection(p.get("source_files") or ()))
                            )
                        ):
                            deleted += 1
                            continue
                    keep.append(rel)
                self.rels = keep
                return {"success": True, "records": [{"deleted": deleted}]}
            # Reconcile stale RELATES_TO cleanup
            if (
                "MATCH (:SDATable {generation:$generation})-[r:RELATES_TO]->(:SDATable {generation:$generation}) WHERE NOT r.ref IN $refs"
                in q
            ):
                refs = set(params["refs"])
                keep, deleted = [], 0
                for rel in self.rels:
                    if rel["type"] == "RELATES_TO":
                        start = next(n for n in self.nodes if n["id"] == rel["start"])
                        end = next(n for n in self.nodes if n["id"] == rel["end"])
                        if (
                            start["props"].get("generation") == params["generation"]
                            and end["props"].get("generation") == params["generation"]
                            and rel["props"].get("ref") not in refs
                        ):
                            deleted += 1
                            continue
                    keep.append(rel)
                self.rels = keep
                return {"success": True, "records": [{"deleted": deleted}]}
            # Incremental DEPENDS_ON cleanup
            if "-[r:DEPENDS_ON]->" in q and "RETURN count(*) AS deleted" in q:
                return {"success": True, "records": [{"deleted": 0}]}
            # Incremental SDAEntity delete_batches
            if (
                "MATCH (n:SDAEntity {generation:$generation}) WHERE n.source_file IN $files"
                in q
            ):
                if "DELETE batch_relationship" in q:
                    files = set(params["files"])
                    victims = {
                        n["id"]
                        for n in self.nodes
                        if "SDAEntity" in n["labels"]
                        and n["props"].get("generation") == params["generation"]
                        and (
                            n["props"].get("source_file") in files
                            or n["props"].get("source_key") in files
                            or bool(
                                files.intersection(n["props"].get("source_files") or ())
                            )
                        )
                    }
                    keep = [
                        r
                        for r in self.rels
                        if r["start"] not in victims and r["end"] not in victims
                    ]
                    deleted = len(self.rels) - len(keep)
                    self.rels = keep
                    return {"success": True, "records": [{"deleted": deleted}]}
                if "DETACH DELETE n" in q:
                    files = set(params["files"])
                    victims = {
                        n["id"]
                        for n in self.nodes
                        if "SDAEntity" in n["labels"]
                        and n["props"].get("generation") == params["generation"]
                        and (
                            n["props"].get("source_file") in files
                            or n["props"].get("source_key") in files
                            or bool(
                                files.intersection(n["props"].get("source_files") or ())
                            )
                        )
                    }
                    self.rels = [
                        r
                        for r in self.rels
                        if r["start"] not in victims and r["end"] not in victims
                    ]
                    keep = [n for n in self.nodes if n["id"] not in victims]
                    deleted = len(self.nodes) - len(keep)
                    self.nodes = keep
                    return {"success": True, "records": [{"deleted": deleted}]}
            # Full generation delete_batches
            if (
                "MATCH (n:SDAEntity {generation:$generation}) MATCH (n)-[batch_relationship]-()"
                in q
            ):
                victims = {
                    n["id"]
                    for n in self.nodes
                    if n["props"].get("generation") == params["generation"]
                }
                keep = [
                    r
                    for r in self.rels
                    if r["start"] not in victims and r["end"] not in victims
                ]
                deleted = len(self.rels) - len(keep)
                self.rels = keep
                return {"success": True, "records": [{"deleted": deleted}]}
            if (
                "MATCH (n:SDAEntity {generation:$generation}) WITH n LIMIT $batch_size DETACH DELETE n"
                in q
            ):
                victims = {
                    n["id"]
                    for n in self.nodes
                    if n["props"].get("generation") == params["generation"]
                }
                self.rels = [
                    r
                    for r in self.rels
                    if r["start"] not in victims and r["end"] not in victims
                ]
                keep = [n for n in self.nodes if n["id"] not in victims]
                deleted = len(self.nodes) - len(keep)
                self.nodes = keep
                return {"success": True, "records": [{"deleted": deleted}]}
            if "RETURN count(*) AS deleted" in q:
                return {"success": True, "records": [{"deleted": 0}]}
            # Create SDATable / SDAField
            if "CREATE (n:SDAEntity:SDATable) SET n = row" in q:
                for row in params["rows"]:
                    self.nodes.append(
                        {
                            "id": self._next_id,
                            "labels": {"SDAEntity", "SDATable"},
                            "props": {**row, "generation": params["generation"]},
                        }
                    )
                    self._next_id += 1
                return {"success": True, "records": []}
            if "CREATE (f:SDAEntity:SDAField) SET f = row" in q:
                for row in params["rows"]:
                    self.nodes.append(
                        {
                            "id": self._next_id,
                            "labels": {"SDAEntity", "SDAField"},
                            "props": {**row, "generation": params["generation"]},
                        }
                    )
                    self._next_id += 1
                return {"success": True, "records": []}
            # Reconcile elementId queries
            if "RETURN elementId(n) AS id" in q:
                if ":SDAField" in q:
                    return {
                        "success": True,
                        "records": [
                            {"id": n["id"]}
                            for n in self.nodes
                            if "SDAField" in n["labels"]
                            and n["props"].get("generation") == params["generation"]
                        ],
                    }
                return {"success": True, "records": []}
            # HAS_FIELD link
            if "MERGE (t)-[:HAS_FIELD]->(n)" in q:
                ids = set(params["ids"])
                for f in self.nodes:
                    if f["id"] not in ids:
                        continue
                    for t in self.nodes:
                        if (
                            "SDATable" in t["labels"]
                            and t["props"].get("generation") == params["generation"]
                            and t["props"].get("database") == f["props"].get("database")
                            and t["props"].get("name") == f["props"].get("table")
                            and (t["props"].get("catalog") or "")
                            == (f["props"].get("catalog") or "")
                            and not any(
                                r["type"] == "HAS_FIELD"
                                and r["start"] == t["id"]
                                and r["end"] == f["id"]
                                for r in self.rels
                            )
                        ):
                            self.rels.append(
                                {
                                    "type": "HAS_FIELD",
                                    "start": t["id"],
                                    "end": f["id"],
                                    "props": {},
                                }
                            )
                return {"success": True, "records": []}
            # RELATES_TO merge
            if "MERGE (a)-[r:RELATES_TO {ref: row.props.ref}]->(b)" in q:
                for row in params["rows"]:
                    a = next(
                        n
                        for n in self.nodes
                        if "SDATable" in n["labels"]
                        and n["props"].get("generation") == params["generation"]
                        and n["props"].get("database") == row["from_database"]
                        and n["props"].get("name") == row["from_table_name"]
                        and (n["props"].get("catalog") or "") == row["from_catalog"]
                    )
                    b = next(
                        n
                        for n in self.nodes
                        if "SDATable" in n["labels"]
                        and n["props"].get("generation") == params["generation"]
                        and n["props"].get("database") == row["to_database"]
                        and n["props"].get("name") == row["to_table_name"]
                        and (n["props"].get("catalog") or "") == row["to_catalog"]
                    )
                    existing = next(
                        (
                            r
                            for r in self.rels
                            if r["type"] == "RELATES_TO"
                            and r["start"] == a["id"]
                            and r["end"] == b["id"]
                            and r["props"].get("ref") == row["props"]["ref"]
                        ),
                        None,
                    )
                    if existing is None:
                        self.rels.append(
                            {
                                "type": "RELATES_TO",
                                "start": a["id"],
                                "end": b["id"],
                                "props": dict(row["props"]),
                            }
                        )
                    else:
                        p = existing["props"]
                        np = row["props"]
                        if (
                            p.get("name") != np.get("name")
                            or p.get("from_table") != np.get("from_table")
                            or p.get("to_table") != np.get("to_table")
                            or p.get("from_columns") != np.get("from_columns")
                            or p.get("to_columns") != np.get("to_columns")
                            or (p.get("source_file") or "")
                            != (np.get("source_file") or "")
                            or (p.get("source_key") or "")
                            != (np.get("source_key") or "")
                            or (p.get("source_files") or [])
                            != (np.get("source_files") or [])
                        ):
                            existing["props"] = dict(np)
                return {"success": True, "records": []}
            # Snapshot queries
            if "OPTIONAL MATCH (t)-[:HAS_FIELD]->(f:SDAField" in q:
                records = []
                for t in self.nodes:
                    if (
                        "SDATable" in t["labels"]
                        and t["props"].get("generation") == params["generation"]
                    ):
                        fields_for_t = [
                            next(
                                n["props"] for n in self.nodes if n["id"] == rel["end"]
                            )
                            for rel in self.rels
                            if rel["type"] == "HAS_FIELD" and rel["start"] == t["id"]
                        ]
                        records.append(
                            {"table": dict(t["props"]), "fields": fields_for_t}
                        )
                return {"success": True, "records": records}
            if "-[r:DEPENDS_ON]->(:SDALineageTable" in q:
                return {"success": True, "records": []}
            if "-[r:RELATES_TO]->(:SDATable" in q:
                records = []
                for rel in self.rels:
                    if rel["type"] == "RELATES_TO":
                        s = next(n for n in self.nodes if n["id"] == rel["start"])
                        e = next(n for n in self.nodes if n["id"] == rel["end"])
                        if (
                            s["props"].get("generation") == params["generation"]
                            and e["props"].get("generation") == params["generation"]
                        ):
                            records.append({"relationship": dict(rel["props"])})
                return {"success": True, "records": records}
            return {"success": True, "records": []}

        def close(self):
            pass

    class InMemoryVectors:
        def __init__(self):
            self.items = {}
            self.collection = type("C", (), {"load": lambda self: None})()
            self.client = type("Cl", (), {"disconnect": lambda self: None})()

        def create_collection(self, *args, **kwargs):
            pass

        def get_collection(self, *args, **kwargs):
            pass

        def add_vectors(self, vectors, ids, metadata):
            for vid, meta in zip(ids, metadata, strict=True):
                self.items[vid] = meta

        def delete_vectors(self, ids):
            for vid in ids:
                self.items.pop(vid, None)

    graph_store = InMemoryGraph()
    vector_store = InMemoryVectors()

    @contextmanager
    def fake_build_storage(values, stats=None):
        from semantica_dataagent.stage_contract import StorageHandles

        yield StorageHandles(
            values["knowledge_generation"],
            graph_store,
            vector_store,
            values,
            _embedding_dimension=4,
        )

    @contextmanager
    def fake_incremental_storage(values, stats=None):
        from semantica_dataagent.stage_contract import StorageHandles

        yield StorageHandles(
            values["knowledge_generation"],
            graph_store,
            vector_store,
            values,
            _embedding_dimension=4,
        )

    base_settings = {
        "source_directory": str(source),
        "knowledge_generation": "gen_init",
        "neo4j_uri": "bolt://127.0.0.1:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        "milvus_uri": "http://127.0.0.1:19530",
        "embedding_url": "http://127.0.0.1:8000",
        "embedding_model": "test-model",
    }
    monkeypatch.setattr(
        knowledge, "settings", lambda project_id=None: dict(base_settings)
    )
    monkeypatch.setattr(knowledge, "build_storage", fake_build_storage)
    monkeypatch.setattr(knowledge, "incremental_storage", fake_incremental_storage)
    monkeypatch.setattr(knowledge, "read_graph", lambda values: graph_store)
    monkeypatch.setattr(knowledge, "read_vectors", lambda values: vector_store)
    monkeypatch.setattr(
        knowledge,
        "embed",
        lambda values, texts, stats=None: [[0.1, 0.2, 0.3, 0.4]] * len(texts),
    )
    monkeypatch.setattr(knowledge, "prune_generations", lambda values: None)

    # Set up multi-file source directory
    (source / "tables.json").write_text(
        json.dumps(
            [
                {
                    "catalog": "hive",
                    "database": "shop",
                    "name": "orders",
                    "comment": "订单",
                },
                {
                    "catalog": "hive",
                    "database": "shop",
                    "name": "customers",
                    "comment": "客户",
                },
                {
                    "catalog": "hive",
                    "database": "shop",
                    "name": "order_items",
                    "comment": "明细",
                },
            ]
        ),
        encoding="utf-8",
    )
    (source / "fields.json").write_text(
        json.dumps(
            [
                {
                    "catalog": "hive",
                    "database": "shop",
                    "table": "orders",
                    "name": "id",
                    "type": "string",
                    "comment": "",
                    "partition": False,
                },
                {
                    "catalog": "hive",
                    "database": "shop",
                    "table": "orders",
                    "name": "tenant_id",
                    "type": "string",
                    "comment": "",
                    "partition": False,
                },
                {
                    "catalog": "hive",
                    "database": "shop",
                    "table": "orders",
                    "name": "cust_key",
                    "type": "string",
                    "comment": "",
                    "partition": False,
                },
                {
                    "catalog": "hive",
                    "database": "shop",
                    "table": "orders",
                    "name": "dt",
                    "type": "string",
                    "comment": "",
                    "partition": True,
                },
                {
                    "catalog": "hive",
                    "database": "shop",
                    "table": "customers",
                    "name": "cid",
                    "type": "string",
                    "comment": "",
                    "partition": False,
                },
                {
                    "catalog": "hive",
                    "database": "shop",
                    "table": "order_items",
                    "name": "tenant_id",
                    "type": "string",
                    "comment": "",
                    "partition": False,
                },
                {
                    "catalog": "hive",
                    "database": "shop",
                    "table": "order_items",
                    "name": "order_id",
                    "type": "string",
                    "comment": "",
                    "partition": False,
                },
            ]
        ),
        encoding="utf-8",
    )
    dir_a = source / "a"
    dir_b = source / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    (dir_a / "relationships.json").write_text(
        json.dumps(
            [
                {
                    "name": "orders_customer",
                    "from_catalog": "hive",
                    "from_database": "shop",
                    "from_table": "orders",
                    "from_columns": ["cust_key"],
                    "to_catalog": "hive",
                    "to_database": "shop",
                    "to_table": "customers",
                    "to_columns": ["cid"],
                }
            ]
        ),
        encoding="utf-8",
    )
    (dir_b / "relationships.json").write_text(
        json.dumps(
            [
                {
                    "name": "items_orders",
                    "from_catalog": "hive",
                    "from_database": "shop",
                    "from_table": "order_items",
                    "from_columns": ["tenant_id", "order_id"],
                    "to_catalog": "hive",
                    "to_database": "shop",
                    "to_table": "orders",
                    "to_columns": ["tenant_id", "id"],
                }
            ]
        ),
        encoding="utf-8",
    )

    def run_one(build_type):
        with sessions().begin() as db:
            row = Build(
                project_id="default",
                build_type=build_type,
                status="running",
                started_at="2026-11-18T00:00:00Z",
                counts="{}",
            )
            db.add(row)
            db.flush()
            bid = row.id
        knowledge.run_build(bid)
        with sessions()() as db:
            row = db.get(Build, bid)
            pointer = db.get(GenerationPointer, 1)
            if pointer:
                base_settings["knowledge_generation"] = pointer.generation
            return row.id, row.status, row.error, json.loads(row.counts)

    full_id, status, err, counts = run_one("full")
    assert status == "succeeded", err
    assert counts["relationship"] == 2

    def current_rels():
        return {
            rel["props"]["name"]: dict(rel["props"])
            for rel in graph_store.rels
            if rel["type"] == "RELATES_TO"
        }

    rels_v1 = current_rels()
    assert set(rels_v1) == {"orders_customer", "items_orders"}
    assert rels_v1["orders_customer"]["build_id"] == full_id
    assert rels_v1["items_orders"]["build_id"] == full_id

    # End-to-end SQL validation & join path after full build
    path_v1 = find_join_path(["hive.shop.orders", "hive.shop.customers"])
    assert path_v1["status"] == "found"
    assert path_v1["strength"] == "strong"
    assert path_v1["steps"][0]["keys"] == ["cust_key=cid"]

    ok_sql = validate_sql(
        "SELECT o.id FROM hive.shop.orders o "
        "JOIN hive.shop.customers c ON o.cust_key = c.cid "
        "JOIN hive.shop.order_items i ON o.tenant_id = i.tenant_id AND o.id = i.order_id "
        "WHERE o.dt = '2025-01-01'",
        "hive",
    )
    assert ok_sql["status"] == "pass", ok_sql
    assert all(j["status"] == "supported" for j in ok_sql["joins"])

    partial_sql = validate_sql(
        "SELECT o.id FROM hive.shop.orders o "
        "JOIN hive.shop.order_items i ON o.tenant_id = i.tenant_id "
        "WHERE o.dt = '2025-01-01'",
        "hive",
    )
    assert partial_sql["status"] == "issues"
    assert partial_sql["joins"][0]["status"] != "supported"

    unqualified_sql = validate_sql(
        "SELECT o.id FROM shop.orders o JOIN shop.customers c ON o.cust_key = c.cid WHERE o.dt = '2025-01-01'",
        "hive",
    )
    assert unqualified_sql["status"] == "issues"
    assert any(
        issue["kind"] == "table" and "目录" in issue["message"]
        for issue in unqualified_sql["issues"]
    )

    # Incremental build modifying dir_a only keeps dir_b build_id untouched
    (dir_a / "relationships.json").write_text(
        json.dumps(
            [
                {
                    "name": "orders_customer_v2",
                    "from_catalog": "hive",
                    "from_database": "shop",
                    "from_table": "orders",
                    "from_columns": ["cust_key"],
                    "to_catalog": "hive",
                    "to_database": "shop",
                    "to_table": "customers",
                    "to_columns": ["cid"],
                }
            ]
        ),
        encoding="utf-8",
    )
    inc1_id, status, err, _ = run_one("incremental")
    assert status == "succeeded", err
    rels_v2 = current_rels()
    assert set(rels_v2) == {"orders_customer_v2", "items_orders"}
    assert rels_v2["orders_customer_v2"]["build_id"] == inc1_id
    assert rels_v2["items_orders"]["build_id"] == full_id

    # Incremental build deleting dir_a/relationships.json cleans up its RELATES_TO edge
    (dir_a / "relationships.json").unlink()
    _, status, err, _ = run_one("incremental")
    assert status == "succeeded", err
    rels_v3 = current_rels()
    assert set(rels_v3) == {"items_orders"}
    assert rels_v3["items_orders"]["build_id"] == full_id
    assert (
        find_join_path(["hive.shop.orders", "hive.shop.customers"])["status"]
        == "no_path"
    )
    assert (
        validate_sql(
            "SELECT o.id FROM hive.shop.orders o JOIN hive.shop.customers c ON o.cust_key = c.cid WHERE o.dt = '2025-01-01'",
            "hive",
        )["joins"][0]["status"]
        == "unsupported"
    )

    # Incremental build updating tables.json recreates attached RELATES_TO edge
    (source / "tables.json").write_text(
        json.dumps(
            [
                {
                    "catalog": "hive",
                    "database": "shop",
                    "name": "orders",
                    "comment": "订单已更新",
                },
                {
                    "catalog": "hive",
                    "database": "shop",
                    "name": "customers",
                    "comment": "客户",
                },
                {
                    "catalog": "hive",
                    "database": "shop",
                    "name": "order_items",
                    "comment": "明细",
                },
            ]
        ),
        encoding="utf-8",
    )
    inc3_id, status, err, _ = run_one("incremental")
    assert status == "succeeded", err
    rels_v4 = current_rels()
    assert set(rels_v4) == {"items_orders"}
    assert rels_v4["items_orders"]["build_id"] == inc3_id

    # Incremental build removing a referenced field fails reconcile cleanly
    fields_data = json.loads((source / "fields.json").read_text())
    (source / "fields.json").write_text(
        json.dumps(
            [
                f
                for f in fields_data
                if not (f["table"] == "order_items" and f["name"] == "order_id")
            ]
        ),
        encoding="utf-8",
    )
    _, status, err, _ = run_one("incremental")
    assert status == "failed"
    assert "order_id" in (err or "")
    sql_validation._snapshot_cache = None


def test_incremental_relates_to_cleanup_matches_source_files(tmp_path, monkeypatch):
    import hashlib
    from contextlib import contextmanager

    from cryptography.fernet import Fernet

    from semantica_dataagent.canonical import ReadResult, Record
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.db import Build, GenerationPointer, sessions

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    initialize()
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.yaml").write_text("v1", encoding="utf-8")
    (source / "b.yaml").write_text("v1", encoding="utf-8")

    rel_edges = []

    class FakeGraph:
        def execute_query(self, query, params):
            q = " ".join(query.split())
            if "RETURN count(*) AS deleted" in q:
                if "RELATES_TO" in q and "IN $files" in q:
                    files = set(params["files"])
                    before = len(rel_edges)
                    rel_edges[:] = [
                        e
                        for e in rel_edges
                        if not (
                            e.get("source_file") in files
                            or e.get("source_key") in files
                            or bool(files.intersection(e.get("source_files") or ()))
                        )
                    ]
                    return {
                        "success": True,
                        "records": [{"deleted": before - len(rel_edges)}],
                    }
                if "RELATES_TO" in q and "NOT r.ref IN $refs" in q:
                    refs = set(params["refs"])
                    before = len(rel_edges)
                    rel_edges[:] = [e for e in rel_edges if e.get("ref") in refs]
                    return {
                        "success": True,
                        "records": [{"deleted": before - len(rel_edges)}],
                    }
                return {"success": True, "records": [{"deleted": 0}]}
            if "MERGE (a)-[r:RELATES_TO" in q:
                for row in params["rows"]:
                    if not any(e["ref"] == row["props"]["ref"] for e in rel_edges):
                        rel_edges.append(dict(row["props"]))
            return {"success": True, "records": []}

        def close(self):
            pass

    class FakeVectors:
        collection = type("C", (), {"load": lambda self: None})()
        client = type("Cl", (), {"disconnect": lambda self: None})()

        def create_collection(self, *args, **kwargs):
            pass

        def get_collection(self, *args, **kwargs):
            pass

        def add_vectors(self, *args, **kwargs):
            pass

        def delete_vectors(self, *args, **kwargs):
            pass

    @contextmanager
    def fake_storage(values, stats=None):
        from semantica_dataagent.stage_contract import StorageHandles

        yield StorageHandles(
            values["knowledge_generation"],
            FakeGraph(),
            FakeVectors(),
            values,
            _embedding_dimension=4,
        )

    base_settings = {
        "source_directory": str(source),
        "knowledge_generation": "gen_init",
        "neo4j_uri": "bolt://127.0.0.1:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        "milvus_uri": "http://127.0.0.1:19530",
        "embedding_url": "http://127.0.0.1:8000",
        "embedding_model": "test-model",
    }

    class MultiFileAdapter:
        id = "multi"
        version = "1.0.0"
        project_types = ("warehouse",)

        def read(self, directory, parameters):
            a_hash = hashlib.sha256((directory / "a.yaml").read_bytes()).hexdigest()
            b_hash = hashlib.sha256((directory / "b.yaml").read_bytes()).hexdigest()
            b_text = (directory / "b.yaml").read_text(encoding="utf-8")
            recs = [
                Record(
                    "table",
                    {"database": "shop", "name": "orders", "comment": ""},
                    "a.yaml",
                ),
                Record(
                    "table",
                    {"database": "shop", "name": "customers", "comment": ""},
                    "a.yaml",
                ),
                Record(
                    "field",
                    {
                        "database": "shop",
                        "table": "orders",
                        "name": "cid",
                        "type": "string",
                        "comment": "",
                        "partition": False,
                    },
                    "a.yaml",
                ),
                Record(
                    "field",
                    {
                        "database": "shop",
                        "table": "customers",
                        "name": "id",
                        "type": "string",
                        "comment": "",
                        "partition": False,
                    },
                    "a.yaml",
                ),
            ]
            if b_text == "v1":
                recs.append(
                    Record(
                        "relationship",
                        {
                            "name": "orders_customer",
                            "from_database": "shop",
                            "from_table": "orders",
                            "from_columns": ["cid"],
                            "to_database": "shop",
                            "to_table": "customers",
                            "to_columns": ["id"],
                        },
                        "a.yaml",
                        source_key="a.yaml",
                        source_files=("a.yaml", "b.yaml"),
                    )
                )
            return ReadResult(recs, {"a.yaml": a_hash, "b.yaml": b_hash})

    monkeypatch.setattr(
        knowledge, "settings", lambda project_id=None: dict(base_settings)
    )
    monkeypatch.setattr(
        knowledge,
        "configured",
        lambda project_id=None, project_type=None: [
            ("source", MultiFileAdapter(), True, {})
        ],
    )
    monkeypatch.setattr(knowledge, "build_storage", fake_storage)
    monkeypatch.setattr(knowledge, "incremental_storage", fake_storage)
    monkeypatch.setattr(knowledge, "read_graph", lambda values: FakeGraph())
    monkeypatch.setattr(knowledge, "read_vectors", lambda values: FakeVectors())
    monkeypatch.setattr(
        knowledge,
        "embed",
        lambda values, texts, stats=None: [[0.1, 0.2, 0.3, 0.4]] * len(texts),
    )
    monkeypatch.setattr(knowledge, "prune_generations", lambda values: None)

    def run_one(build_type):
        with sessions().begin() as db:
            row = Build(
                project_id="default",
                build_type=build_type,
                status="running",
                started_at="2026-11-18T00:00:00Z",
                counts="{}",
            )
            db.add(row)
            db.flush()
            bid = row.id
        knowledge.run_build(bid)
        with sessions()() as db:
            row = db.get(Build, bid)
            pointer = db.get(GenerationPointer, 1)
            if pointer:
                base_settings["knowledge_generation"] = pointer.generation
            return row.id, row.status, row.error

    first_id, status, err = run_one("full")
    assert status == "succeeded", err
    assert len(rel_edges) == 1 and rel_edges[0]["build_id"] == first_id

    # Changing b.yaml (only in source_files, not source_file) removes the RELATES_TO edge
    (source / "b.yaml").write_text("v2", encoding="utf-8")
    _, status, err = run_one("incremental")
    assert status == "succeeded", err
    assert rel_edges == []
