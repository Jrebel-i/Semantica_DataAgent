"""Append document structure and searchable chunks to the current generation."""

from .markdown import chunks, doc_ref, table_name
from .stage_contract import StageResult


def write(records, generation, storage):
    from .refs import bounded_ref

    truncations = {}
    docs = [
        {
            **r.data,
            "source_file": r.source_file,
            "source_key": r.source_key or r.source_file,
            "source_files": list(r.source_files),
            "build_id": storage.build_id,
        }
        for r in records
        if r.kind == "document"
    ]
    keys = [(d["category"], d["path"]) for d in docs]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate document path")
    categories = sorted({d["category"] for d in docs})
    rows, parts, links = [], [], []
    for d in docs:
        ref = doc_ref(d["category"], d["path"], truncations)
        index = d["path"] == "index.md"
        rows.append(
            {**d, "ref": ref, "generated": d.get("generated", False), "is_index": index}
        )
        if not index:
            for number, part in enumerate(chunks(d["content"])):
                parts.append(
                    {
                        **part,
                        "section": " / ".join(part["section"]),
                        "ref": bounded_ref(f"{ref}#chunk-{number + 1}", truncations),
                        "doc_id": ref,
                        "category": d["category"],
                        "path": d["path"],
                        "source_file": d["source_file"],
                        "source_key": d["source_key"],
                        "source_files": d["source_files"],
                        "build_id": d["build_id"],
                    }
                )
            match = table_name(d["path"])
            if match:
                links.append({"ref": ref, "database": match[0], "name": match[1]})
    storage.execute(
        "UNWIND $rows AS row CREATE (d:SDAEntity:SDADocument) SET d = row, d.generation = $generation",
        {"generation": generation, "rows": rows},
    )
    storage.execute(
        "UNWIND $rows AS row MATCH (d:SDADocument {generation:$generation, ref:row.doc_id}) CREATE (p:SDAEntity:SDAChunk) SET p = row, p.generation = $generation CREATE (d)-[:HAS_CHUNK]->(p)",
        {"generation": generation, "rows": parts},
    )
    storage.execute(
        "UNWIND $rows AS row MATCH (d:SDADocument {generation:$generation, ref:row.ref}), (t:SDATable {generation:$generation, database:row.database, name:row.name}) MERGE (t)-[:HAS_DOCUMENT]->(d)",
        {"generation": generation, "rows": links},
    )
    for offset in range(0, len(parts), 4):
        batch = parts[offset : offset + 4]
        storage.add_vectors(
            [part["ref"] for part in batch],
            [part["content"] for part in batch],
            [{"scope": "docs", **part} for part in batch],
        )
    return StageResult(
        counts={
            "document": len(docs),
            "chunk": len(parts),
            "category": len(categories),
            **truncations,
        }
    )
