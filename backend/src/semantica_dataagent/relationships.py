"""Reconcile relationships after writing or removing source-owned nodes."""

import re
from collections import defaultdict

from .markdown import doc_ref, table_name
from .stage_contract import delete_batches

RELATIONSHIP_BATCH_SIZE = 2000

_IDENT = r'(?:[`"]?[A-Za-z_][A-Za-z0-9_]*[`"]?)'
_TABLE_MENTION_RE = re.compile(
    rf'(?<![A-Za-z0-9_`"]){_IDENT}(?:\s*\.\s*{_IDENT})+(?![A-Za-z0-9_`"])'
)


def link_table_documents(execute, generation, rows):
    """Merge HAS_DOCUMENT edges between SDATable and SDADocument nodes."""
    return execute(
        "UNWIND $rows AS row MATCH (d:SDADocument {generation:$generation, ref:row.ref}), (t:SDATable {generation:$generation, database:row.database, name:row.name}) WHERE coalesce(t.catalog, '') = coalesce(row.catalog, '') MERGE (t)-[:HAS_DOCUMENT]->(d)",
        {"generation": generation, "rows": rows},
    )


def _reconcile_document_table_links(storage, records, generation):
    """Link documents to tables mentioned in document text or dictionary filenames."""
    tables = {
        (
            (record.data.get("catalog") or "").casefold(),
            record.data["database"].casefold(),
            record.data["name"].casefold(),
        ): (
            record.data.get("catalog") or "",
            record.data["database"],
            record.data["name"],
        )
        for record in records
        if record.kind == "table"
    }
    links = []
    seen_links = set()
    for record in records:
        if record.kind != "document":
            continue
        path = record.data["path"]
        if path == "index.md":
            continue
        candidates = []
        for chain in _TABLE_MENTION_RE.findall(record.data["content"]):
            parts = [re.sub(r'[`"\s]', "", part) for part in chain.split(".")]
            if len(parts) >= 3:
                three = (
                    parts[0].casefold(),
                    parts[1].casefold(),
                    parts[2].casefold(),
                )
                if three not in tables:
                    continue
                candidates.append(three)
            elif len(parts) == 2:
                candidates.append(("", parts[0].casefold(), parts[1].casefold()))
        match = table_name(path)
        if match:
            cat, db, tbl = match if len(match) == 3 else ("", *match)
            candidates.append((cat.casefold(), db.casefold(), tbl.casefold()))
        ref = doc_ref(record.data["category"], path)
        for key in candidates:
            actual = tables.get(key)
            if actual and (ref, *actual) not in seen_links:
                seen_links.add((ref, *actual))
                links.append(
                    {
                        "ref": ref,
                        "catalog": actual[0],
                        "database": actual[1],
                        "name": actual[2],
                    }
                )
    link_table_documents(storage.execute, generation, links)


def _reconcile_foreign_keys(storage, records, generation):
    """Validate and reconcile explicit RELATES_TO edges against full records."""
    from .knowledge import reference

    table_set = {
        (r.data.get("catalog") or "", r.data["database"], r.data["name"])
        for r in records
        if r.kind == "table"
    }
    table_fields = defaultdict(set)
    for r in records:
        if r.kind == "field":
            d = r.data
            table_fields[(d.get("catalog") or "", d["database"], d["table"])].add(
                d["name"]
            )
    rel_rows = []
    seen_rel_refs = set()
    for record in records:
        if record.kind != "relationship":
            continue
        d = record.data
        from_catalog = d.get("from_catalog") or ""
        to_catalog = d.get("to_catalog") or ""
        from_key = (from_catalog, d["from_database"], d["from_table"])
        to_key = (to_catalog, d["to_database"], d["to_table"])
        from_cols = d.get("from_columns") or []
        to_cols = d.get("to_columns") or []
        if not from_cols or len(from_cols) != len(to_cols):
            raise ValueError(
                f"Relationship {d['name']} must have equal non-empty from_columns and to_columns"
            )
        if from_key not in table_set:
            raise ValueError(f"Relationship {d['name']} refers to missing source table")
        if to_key not in table_set:
            raise ValueError(f"Relationship {d['name']} refers to missing target table")
        missing_from = [c for c in from_cols if c not in table_fields[from_key]]
        if missing_from:
            raise ValueError(
                f"Relationship {d['name']} refers to missing source columns: {', '.join(missing_from)}"
            )
        missing_to = [c for c in to_cols if c not in table_fields[to_key]]
        if missing_to:
            raise ValueError(
                f"Relationship {d['name']} refers to missing target columns: {', '.join(missing_to)}"
            )
        rel_ref = reference("relationship", d)
        if rel_ref in seen_rel_refs:
            raise ValueError("Duplicate relationship reference")
        seen_rel_refs.add(rel_ref)
        from_full = ".".join(p for p in from_key if p)
        to_full = ".".join(p for p in to_key if p)
        rel_rows.append(
            {
                "from_catalog": from_catalog,
                "from_database": d["from_database"],
                "from_table_name": d["from_table"],
                "to_catalog": to_catalog,
                "to_database": d["to_database"],
                "to_table_name": d["to_table"],
                "props": {
                    "name": d["name"],
                    "ref": rel_ref,
                    "from_table": from_full,
                    "to_table": to_full,
                    "from_columns": list(from_cols),
                    "to_columns": list(to_cols),
                    "source_file": record.source_file,
                    "source_key": record.source_key or record.source_file,
                    "source_files": list(record.source_files),
                    "build_id": storage.build_id,
                },
            }
        )
    delete_batches(
        storage.execute,
        "MATCH (:SDATable {generation:$generation})-[r:RELATES_TO]->(:SDATable {generation:$generation}) WHERE NOT r.ref IN $refs",
        "r",
        {"generation": generation, "refs": [row["props"]["ref"] for row in rel_rows]},
    )
    storage.execute(
        """UNWIND $rows AS row
        MATCH (a:SDATable {generation:$generation, database:row.from_database, name:row.from_table_name})
        WHERE coalesce(a.catalog, '') = row.from_catalog
        MATCH (b:SDATable {generation:$generation, database:row.to_database, name:row.to_table_name})
        WHERE coalesce(b.catalog, '') = row.to_catalog
        MERGE (a)-[r:RELATES_TO {ref: row.props.ref}]->(b)
        ON CREATE SET r = row.props
        WITH r, row
        WHERE r.name <> row.props.name
           OR r.from_table <> row.props.from_table
           OR r.to_table <> row.props.to_table
           OR r.from_columns <> row.props.from_columns
           OR r.to_columns <> row.props.to_columns
           OR coalesce(r.source_file, '') <> coalesce(row.props.source_file, '')
           OR coalesce(r.source_key, '') <> coalesce(row.props.source_key, '')
           OR coalesce(r.source_files, []) <> row.props.source_files
        SET r = row.props""",
        {"generation": generation, "rows": rel_rows},
    )


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
            "MATCH (t:SDATable {generation:$generation, database:n.database, name:n.table}) WHERE coalesce(t.catalog, '') = coalesce(n.catalog, '') MERGE (t)-[:HAS_FIELD]->(n)",
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
    _reconcile_document_table_links(storage, records, generation)

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
    _reconcile_foreign_keys(storage, records, generation)
