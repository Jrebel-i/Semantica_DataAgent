"""References written into entity indexes stay within Neo4j key limits."""

from semantica_dataagent.canonical import Record
from semantica_dataagent.knowledge import reference
from semantica_dataagent.markdown import doc_ref
from semantica_dataagent.refs import MAX_REF_BYTES, bounded_ref


def test_bounded_ref_is_stable_and_collision_resistant():
    short = "table:shop.orders"
    assert bounded_ref(short) == short
    first = "document:cat/" + "a" * 5000
    second = "document:cat/" + "b" * 5000
    assert len(bounded_ref(first).encode()) <= MAX_REF_BYTES
    assert len(bounded_ref(second).encode()) <= MAX_REF_BYTES
    assert bounded_ref(first) == bounded_ref(first)
    assert bounded_ref(first) != bounded_ref(second)
    assert bounded_ref(first).startswith("document:cat/")
    counts = {}
    bounded_ref(first, counts)
    bounded_ref(first, counts)
    assert counts == {"ref_truncated": 2}


def test_table_field_and_document_references_are_bounded():
    table = reference(
        "table",
        {"database": "db", "name": "名" * 2000},
    )
    field = reference(
        "field",
        {"database": "db", "table": "t", "name": "字" * 2000},
    )
    document = doc_ref("知识类" * 500, "path/" + "文" * 2000)
    assert all(
        len(value.encode()) <= MAX_REF_BYTES for value in (table, field, document)
    )


def test_stages_record_truncated_references():
    from semantica_dataagent import document_stage, table_stage

    class Storage:
        build_id = 1
        all_records = None
        is_incremental = False

        def __init__(self):
            self.rows = []

        def execute(self, query, parameters):
            if "UNWIND $rows AS row" in query:
                self.rows.extend(parameters["rows"])
            return []

        def add_vectors(self, *args):
            pass

    storage = Storage()
    records = [
        Record(
            "table",
            {"database": "db", "name": "表" * 2000, "comment": "long name"},
            "tables.json",
        ),
        Record(
            "field",
            {
                "database": "db",
                "table": "表" * 2000,
                "name": "字段" * 1000,
                "type": "string",
                "comment": "",
                "partition": False,
            },
            "fields.json",
        ),
    ]
    result = table_stage.write(records, "test", storage)
    assert result.counts["ref_truncated"] == 2
    assert all(len(row["ref"].encode()) <= MAX_REF_BYTES for row in storage.rows)
    assert {row["ref"].split(":", 1)[0] for row in storage.rows} == {"table", "field"}

    storage = Storage()
    result = document_stage.write(
        [
            Record(
                "document",
                {
                    "category": "类" * 500,
                    "path": "文档/" + "文" * 1000 + ".md",
                    "content": "# 标题\n\n正文",
                },
                "source.md",
            )
        ],
        "test",
        storage,
    )
    assert result.counts["ref_truncated"] >= 1
    assert all(len(row["ref"].encode()) <= MAX_REF_BYTES for row in storage.rows)
