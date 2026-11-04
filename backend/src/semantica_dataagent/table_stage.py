"""Write table and field knowledge for one generation."""


def write(records, generation, storage):
    from .knowledge import reference
    from .stage_contract import StageResult

    tables = [
        dict(r.data, ref=reference(r.kind, r.data))
        for r in records
        if r.kind == "table"
    ]
    fields = [
        dict(r.data, ref=reference(r.kind, r.data))
        for r in records
        if r.kind == "field"
    ]
    table_keys = {(table["database"], table["name"]) for table in tables}
    refs = [item["ref"] for item in tables + fields]
    if len(set(refs)) != len(refs):
        raise ValueError("Duplicate table or field reference")
    if any((field["database"], field["table"]) not in table_keys for field in fields):
        raise ValueError("A field refers to a missing table")
    if not tables:
        raise ValueError("Source has no tables")
    storage.execute(
        "UNWIND $rows AS row CREATE (n:SDAEntity:SDATable) SET n = row, n.generation = $generation",
        {"rows": tables, "generation": generation},
    )
    storage.execute(
        "UNWIND $rows AS row MATCH (t:SDATable {generation:$generation, database:row.database, name:row.table}) CREATE (f:SDAEntity:SDAField) SET f = row, f.generation = $generation CREATE (t)-[:HAS_FIELD]->(f)",
        {"rows": fields, "generation": generation},
    )
    entities = [("table", x) for x in tables] + [("field", x) for x in fields]
    storage.add_vectors(
        [x["ref"] for _, x in entities],
        [f"{x['name']} {x['comment']}" for _, x in entities],
        [{"scope": kind + "s", **x} for kind, x in entities],
    )
    return StageResult(counts={"table": len(tables), "field": len(fields)})
