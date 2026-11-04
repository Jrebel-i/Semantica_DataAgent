"""Write table and field knowledge for one generation."""


def write(records, generation, values):
    from pymilvus import utility

    from .knowledge import collection_name, embed, execute, graph, milvus, reference

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
    store = graph(values)
    try:
        execute(
            store,
            "MATCH (n:SDAEntity {generation:$generation}) WHERE n:SDATable OR n:SDAField DETACH DELETE n",
            {"generation": generation},
        )
        execute(
            store,
            "UNWIND $rows AS row CREATE (n:SDAEntity:SDATable) SET n = row, n.generation = $generation",
            {"rows": tables, "generation": generation},
        )
        execute(
            store,
            "UNWIND $rows AS row MATCH (t:SDATable {generation:$generation, database:row.database, name:row.table}) CREATE (f:SDAEntity:SDAField) SET f = row, f.generation = $generation CREATE (t)-[:HAS_FIELD]->(f)",
            {"rows": fields, "generation": generation},
        )
    finally:
        store.close()
    entities = [("table", x) for x in tables] + [("field", x) for x in fields]
    texts = [f"{x['name']} {x['comment']}" for _, x in entities]
    vectors = []
    for offset in range(0, len(texts), 32):
        vectors.extend(embed(values, texts[offset : offset + 32]))
    vectors_store = milvus(values)
    try:
        name = collection_name(values)
        if utility.has_collection(name):
            utility.drop_collection(name)
        vectors_store.create_collection(name, len(vectors[0]), metric_type="COSINE")
        vectors_store.add_vectors(
            vectors,
            ids=[x["ref"] for _, x in entities],
            metadata=[
                {"generation": generation, "scope": kind + "s", **x}
                for kind, x in entities
            ],
        )
        vectors_store.collection.load()
    finally:
        vectors_store.client.disconnect()
    return {"table": len(tables), "field": len(fields)}
