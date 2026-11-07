"""Reconcile relationships after writing or removing source-owned nodes."""

from .stage_contract import delete_batches

RELATIONSHIP_BATCH_SIZE = 2000


def reconcile(storage, records, generation):
    categories = {}
    for record in records:
        if record.kind == "document":
            categories.setdefault(record.data["category"], []).append(record)
    rows = []
    for category, documents in categories.items():
        owner = next(
            (r for r in documents if r.data["path"] == "index.md"), documents[0]
        )
        rows.append(
            {
                "name": category,
                "ref": "category:" + category,
                "source_file": owner.source_file,
                "source_key": owner.source_key or owner.source_file,
                "source_files": list(owner.source_files),
            }
        )
    delete_batches(
        storage.execute,
        "MATCH (c:SDACategory {generation:$generation}) WHERE c.name IN $categories WITH c ORDER BY elementId(c) WITH c.name AS name, collect(c) AS nodes UNWIND nodes[1..] AS duplicate",
        "duplicate",
        {"generation": generation, "categories": list(categories)},
        detach=True,
    )
    delete_batches(
        storage.execute,
        "MATCH (c:SDACategory {generation:$generation}) WHERE NOT c.name IN $categories",
        "c",
        {"generation": generation, "categories": list(categories)},
        detach=True,
    )
    storage.execute(
        "UNWIND $rows AS row MERGE (c:SDAEntity:SDACategory {generation:$generation, name:row.name}) ON CREATE SET c.ref = row.ref, c.source_file = row.source_file, c.source_key = row.source_key, c.source_files = row.source_files, c.build_id = $build_id WITH c, row WHERE c.source_file <> row.source_file OR c.source_key <> row.source_key OR c.source_files <> row.source_files SET c.source_file = row.source_file, c.source_key = row.source_key, c.source_files = row.source_files, c.build_id = $build_id",
        {"generation": generation, "rows": rows, "build_id": storage.build_id},
    )
    relationships = [
        (
            "SDAField",
            "MATCH (t:SDATable {generation:$generation, database:n.database, name:n.table}) MERGE (t)-[:HAS_FIELD]->(n)",
        ),
        (
            "SDADimensionValue",
            "MATCH (f:SDAField {generation:$generation, database:n.database, table:n.table, name:n.field}) MERGE (f)-[:HAS_VALUE]->(n)",
        ),
        (
            "SDADocument",
            "MATCH (c:SDACategory {generation:$generation, name:n.category}) MERGE (c)-[:HAS_DOCUMENT]->(n)",
        ),
    ]
    for label, link_query in relationships:
        nodes = storage.execute(
            f"MATCH (n:{label} {{generation:$generation}}) RETURN elementId(n) AS id",
            {"generation": generation},
        )
        ids = [node["id"] for node in nodes]
        for offset in range(0, len(ids), RELATIONSHIP_BATCH_SIZE):
            storage.execute(
                f"MATCH (n:{label} {{generation:$generation}}) WHERE elementId(n) IN $ids "
                + link_query,
                {
                    "generation": generation,
                    "ids": ids[offset : offset + RELATIONSHIP_BATCH_SIZE],
                },
            )
    import re

    from .markdown import doc_ref, table_name

    tables = {
        (record.data["database"].casefold(), record.data["name"].casefold()): (
            record.data["database"],
            record.data["name"],
        )
        for record in records
        if record.kind == "table"
    }
    links = []
    for record in records:
        if record.kind != "document":
            continue
        path = record.data["path"]
        if path == "index.md":
            continue
        candidates = set(
            re.findall(
                r'(?<![A-Za-z0-9_])(?:[`"]?)([A-Za-z_][A-Za-z0-9_]*)(?:[`"]?)\s*\.\s*(?:[`"]?)([A-Za-z_][A-Za-z0-9_]*)(?:[`"]?)(?![A-Za-z0-9_])',
                record.data["content"],
            )
        )
        match = table_name(path)
        if match:
            candidates.add(match)
        for database, name in candidates:
            actual = tables.get((database.casefold(), name.casefold()))
            if actual:
                links.append(
                    {
                        "ref": doc_ref(record.data["category"], path),
                        "database": actual[0],
                        "name": actual[1],
                    }
                )
    storage.execute(
        "UNWIND $rows AS row MATCH (d:SDADocument {generation:$generation, ref:row.ref}), (t:SDATable {generation:$generation, database:row.database, name:row.name}) MERGE (t)-[:HAS_DOCUMENT]->(d)",
        {"generation": generation, "rows": links},
    )

    lineage = [
        {
            **record.data,
            "source_file": record.source_file,
            "source_key": record.source_key or record.source_file,
            "source_files": list(record.source_files),
        }
        for record in records
        if record.kind == "lineage"
    ]
    storage.execute(
        """UNWIND $rows AS row
        UNWIND [i IN range(0, size(row.upstream) - 1) WHERE substring(row.upstream, i, 1) = '.' | i] AS up_sep
        UNWIND [i IN range(0, size(row.downstream) - 1) WHERE substring(row.downstream, i, 1) = '.' | i] AS down_sep
        MATCH (up:SDATable {generation:$generation, database:substring(row.upstream, 0, up_sep), name:substring(row.upstream, up_sep + 1)})
        MATCH (down:SDATable {generation:$generation, database:substring(row.downstream, 0, down_sep), name:substring(row.downstream, down_sep + 1)})
        MERGE (up)-[r:DEPENDS_ON {task_id: coalesce(row.task_id, ''), upstream: row.upstream, downstream: row.downstream}]->(down)
        ON CREATE SET r.evidence = row.evidence, r.parse_status = row.parse_status, r.source_file = row.source_file, r.source_key = row.source_key, r.source_files = row.source_files, r.build_id = $build_id""",
        {
            "generation": generation,
            "rows": lineage,
            "build_id": storage.build_id,
        },
    )
