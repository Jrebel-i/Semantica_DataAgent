"""Write table and field knowledge for one generation."""


def apply_overrides(records):
    """Overlay approved comments on imported records without changing the source."""
    from dataclasses import replace

    from .db import MetadataOverride, sessions
    from .knowledge import reference

    with sessions()() as db:
        overrides = {
            row.target: row.comment for row in db.query(MetadataOverride).all()
        }
    result = []
    for record in records:
        key = (
            reference(record.kind, record.data)
            if record.kind in {"table", "field"}
            else None
        )
        if key in overrides:
            record = replace(record, data={**record.data, "comment": overrides[key]})
        result.append(record)
    return result


def write(records, generation, storage):
    from .knowledge import reference
    from .stage_contract import StageResult

    truncations = {}
    storage.execute(
        "CREATE FULLTEXT INDEX sda_knowledge IF NOT EXISTS FOR (n:SDATable|SDAField|SDATask|SDAChunk) ON EACH [n.name, n.comment, n.description, n.content] OPTIONS {indexConfig: {`fulltext.analyzer`: 'cjk'}}",
        {},
    )
    storage.execute("CALL db.awaitIndex('sda_knowledge', 120)", {})
    tables = [
        dict(
            r.data,
            ref=reference(r.kind, r.data, truncations),
            source_file=r.source_file,
            build_id=storage.build_id,
        )
        for r in records
        if r.kind == "table"
    ]
    fields = [
        dict(
            r.data,
            ref=reference(r.kind, r.data, truncations),
            source_file=r.source_file,
            build_id=storage.build_id,
        )
        for r in records
        if r.kind == "field"
    ]
    all_records = storage.all_records if storage.all_records is not None else records
    table_keys = {
        (r.data["database"], r.data["name"]) for r in all_records if r.kind == "table"
    }
    refs = [item["ref"] for item in tables + fields]
    if len(set(refs)) != len(refs):
        raise ValueError("Duplicate table or field reference")
    if not storage.is_incremental and any(
        (field["database"], field["table"]) not in table_keys for field in fields
    ):
        raise ValueError("A field refers to a missing table")
    if not storage.is_incremental and not table_keys:
        raise ValueError("Source has no tables")
    storage.execute(
        "UNWIND $rows AS row CREATE (n:SDAEntity:SDATable) SET n = row, n.generation = $generation",
        {"rows": tables, "generation": generation},
    )
    storage.execute(
        "UNWIND $rows AS row CREATE (f:SDAEntity:SDAField) SET f = row, f.generation = $generation",
        {"rows": fields, "generation": generation},
    )
    entities = [("table", x) for x in tables] + [("field", x) for x in fields]
    storage.add_vectors(
        [x["ref"] for _, x in entities],
        [f"{x['name']} {x['comment']}" for _, x in entities],
        [{"scope": kind + "s", **x} for kind, x in entities],
    )
    return StageResult(
        counts={"table": len(tables), "field": len(fields), **truncations}
    )
