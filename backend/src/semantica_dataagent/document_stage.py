"""Append document structure and searchable chunks to the current generation."""

from .markdown import chunks, doc_ref, table_name
from .stage_contract import StageResult


def write(records, generation, storage):
    docs = [r.data for r in records if r.kind == "document"]
    keys = [(d["category"], d["path"]) for d in docs]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate document path")
    categories = sorted({d["category"] for d in docs})
    rows, parts, links = [], [], []
    for d in docs:
        ref = doc_ref(d["category"], d["path"])
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
                        "ref": f"{ref}#chunk-{number + 1}",
                        "doc_id": ref,
                        "category": d["category"],
                        "path": d["path"],
                    }
                )
            match = table_name(d["path"])
            if match:
                links.append({"ref": ref, "database": match[0], "name": match[1]})
    storage.execute(
        "UNWIND $rows AS row CREATE (c:SDAEntity:SDACategory {generation:$generation, name:row.name, ref:row.ref})",
        {
            "generation": generation,
            "rows": [{"name": c, "ref": "category:" + c} for c in categories],
        },
    )
    storage.execute(
        "UNWIND $rows AS row MATCH (c:SDACategory {generation:$generation, name:row.category}) CREATE (d:SDAEntity:SDADocument) SET d = row, d.generation = $generation CREATE (c)-[:HAS_DOCUMENT]->(d)",
        {"generation": generation, "rows": rows},
    )
    storage.execute(
        "UNWIND $rows AS row MATCH (d:SDADocument {generation:$generation, ref:row.doc_id}) CREATE (p:SDAEntity:SDAChunk) SET p = row, p.generation = $generation CREATE (d)-[:HAS_CHUNK]->(p)",
        {"generation": generation, "rows": parts},
    )
    storage.execute(
        "UNWIND $rows AS row MATCH (d:SDADocument {generation:$generation, ref:row.ref}), (t:SDATable {generation:$generation, database:row.database, name:row.name}) CREATE (t)-[:HAS_DOCUMENT]->(d)",
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
        counts={"document": len(docs), "chunk": len(parts), "category": len(categories)}
    )
