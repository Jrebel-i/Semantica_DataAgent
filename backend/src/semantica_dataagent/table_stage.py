"""Write table and field knowledge for one generation."""


def apply_overrides(records, project_id="default"):
    """Overlay approved comments and structured semantic overrides onto records."""
    from .semantic_overrides import apply_overrides_detailed

    return apply_overrides_detailed(records, project_id=project_id).records


def vector_text(item: dict) -> str:
    """Build the embedding text for one table or field dictionary."""
    parts = [
        str(item.get("name") or ""),
        str(item.get("comment") or ""),
        " ".join(str(s) for s in (item.get("synonyms") or ()) if s),
        str(item.get("instructions") or ""),
    ]
    return " ".join(part for part in parts if part).strip()


def vector_texts(records) -> list[str]:
    """Return embedding texts for all table and field records in order."""
    return [vector_text(r.data) for r in records if r.kind in {"table", "field"}]


def write(records, generation, storage):
    from . import project_context
    from .knowledge import reference
    from .stage_contract import StageResult

    truncations = {}
    index = project_context.index_name("knowledge")
    storage.execute(
        "CREATE FULLTEXT INDEX __KNOWLEDGE_INDEX__ IF NOT EXISTS FOR (n:SDATable|SDAField|SDATask|SDAChunk) ON EACH [n.name, n.comment, n.description, n.content] OPTIONS {indexConfig: {`fulltext.analyzer`: 'cjk'}}".replace(
            "__KNOWLEDGE_INDEX__", index
        ),
        {},
    )
    storage.execute(f"CALL db.awaitIndex('{index}', 120)", {})
    tables = [
        dict(
            r.data,
            ref=reference(r.kind, r.data, truncations),
            source_file=r.source_file,
            source_key=r.source_key or r.source_file,
            source_files=list(r.source_files),
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
            source_key=r.source_key or r.source_file,
            source_files=list(r.source_files),
            build_id=storage.build_id,
        )
        for r in records
        if r.kind == "field"
    ]
    all_records = storage.all_records if storage.all_records is not None else records
    table_keys = {
        (r.data.get("catalog") or "", r.data["database"], r.data["name"])
        for r in all_records
        if r.kind == "table"
    }
    refs = [item["ref"] for item in tables + fields]
    if len(set(refs)) != len(refs):
        raise ValueError("Duplicate table or field reference")
    if not storage.is_incremental and any(
        (field.get("catalog") or "", field["database"], field["table"])
        not in table_keys
        for field in fields
    ):
        raise ValueError("A field refers to a missing table")
    if (
        not storage.is_incremental
        and not table_keys
        and storage._settings.get("_project_type") != "document"
    ):
        raise ValueError("Source has no tables")
    storage.execute(
        "UNWIND $rows AS row CREATE (n:SDAEntity:SDATable) SET n = row, n.generation = $generation",
        {"rows": tables, "generation": generation},
    )
    storage.execute(
        "UNWIND $rows AS row CREATE (f:SDAEntity:SDAField) SET f = row, f.generation = $generation",
        {"rows": fields, "generation": generation},
    )
    storage.rebuilt_table_keys.update(
        (t.get("catalog") or "", t["database"], t["name"]) for t in tables
    )
    storage.rebuilt_field_tables.update(
        (f.get("catalog") or "", f["database"], f["table"]) for f in fields
    )
    entities = [("table", x) for x in tables] + [("field", x) for x in fields]

    storage.add_vectors(
        [x["ref"] for _, x in entities],
        [vector_text(x) for _, x in entities],
        [{"scope": kind + "s", **x} for kind, x in entities],
    )
    return StageResult(
        counts={"table": len(tables), "field": len(fields), **truncations}
    )
