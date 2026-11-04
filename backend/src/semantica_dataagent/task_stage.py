"""Append task nodes and table-level lineage for the current build."""

from urllib.parse import quote

from .plugins import parse_task
from .stage_contract import StageResult


def rank_task_hits(hits):
    """Demote output-less tasks while preserving relevance within each group."""
    return sorted(hits, key=lambda item: not item.get("has_output", False))


def resolve_task(task, declared, parsed):
    declared = set(declared)
    derived = (
        set(parsed.edges)
        if parsed.edges is not None
        else {(read, write) for read in parsed.reads for write in parsed.writes}
    )
    return parsed, [
        (
            up,
            down,
            "both"
            if (up, down) in declared and (up, down) in derived
            else "declared"
            if (up, down) in declared
            else "parsed",
        )
        for up, down in sorted(declared | derived)
    ]


def write(records, generation, storage):
    tasks = [r for r in records if r.kind == "task"]
    names = [r.data["name"] for r in tasks]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate task name")
    declarations = [r.data for r in records if r.kind == "lineage"]
    rows, edges, failures = [], [], []
    for task in tasks:
        name = task.data["name"]
        ref = "task:" + quote(name, safe="")
        declared = [
            (d["upstream"], d["downstream"])
            for d in declarations
            if d.get("task_id") == name
        ]
        result, resolved = resolve_task(task, declared, parse_task(task))
        reads = sorted(set(result.reads) | {u for u, _ in declared})
        writes = sorted(set(result.writes) | {d for _, d in declared})
        rows.append(
            {
                "name": name,
                "ref": ref,
                "sql": task.data["sql"],
                "description": task.data.get("description", ""),
                "parse_status": result.status,
                "reason": result.reason or "",
                "reads": reads,
                "writes": writes,
                "has_output": bool(writes),
            }
        )
        edges.extend(
            {
                "upstream": u,
                "downstream": d,
                "evidence": e,
                "parse_status": result.status,
                "task_id": name,
                "ref": ref,
            }
            for u, d, e in resolved
        )
        if result.status == "failed":
            failures.append(
                {"task_id": name, "reason": result.reason or "Parse failed"}
            )
    # Legacy lineage records can be independent of a task.
    edges.extend(
        {
            "upstream": d["upstream"],
            "downstream": d["downstream"],
            "evidence": d["evidence"],
            "parse_status": d["parse_status"],
            "task_id": "",
            "ref": "",
        }
        for d in declarations
        if not d.get("task_id")
    )
    storage.execute(
        "UNWIND $rows AS row CREATE (t:SDAEntity:SDATask) SET t = row, t.generation = $generation",
        {"rows": rows, "generation": generation},
    )
    storage.execute(
        """UNWIND $rows AS row MATCH (t:SDATask {generation:$generation, ref:row.ref})
    UNWIND row.reads AS name MERGE (n:SDAEntity:SDALineageTable {generation:$generation, name:name}) MERGE (t)-[:READS]->(n)""",
        {"rows": rows, "generation": generation},
    )
    storage.execute(
        """UNWIND $rows AS row MATCH (t:SDATask {generation:$generation, ref:row.ref})
    UNWIND row.writes AS name MERGE (n:SDAEntity:SDALineageTable {generation:$generation, name:name}) MERGE (t)-[:PRODUCES]->(n)""",
        {"rows": rows, "generation": generation},
    )
    storage.execute(
        """UNWIND $rows AS row MERGE (a:SDAEntity:SDALineageTable {generation:$generation, name:row.upstream}) MERGE (b:SDAEntity:SDALineageTable {generation:$generation, name:row.downstream}) CREATE (a)-[r:DEPENDS_ON]->(b) SET r = row""",
        {"rows": edges, "generation": generation},
    )
    storage.execute(
        """UNWIND $rows AS row MATCH (task:SDATask {generation:$generation, ref:row.ref})
        UNWIND row.reads AS name MATCH (table:SDATable {generation:$generation})
        WHERE table.database + '.' + table.name = name MERGE (task)-[:READS]->(table)""",
        {"rows": rows, "generation": generation},
    )
    storage.execute(
        """UNWIND $rows AS row MATCH (task:SDATask {generation:$generation, ref:row.ref})
        UNWIND row.writes AS name MATCH (table:SDATable {generation:$generation})
        WHERE table.database + '.' + table.name = name MERGE (task)-[:PRODUCES]->(table)""",
        {"rows": rows, "generation": generation},
    )
    storage.execute(
        """UNWIND $rows AS row MATCH (a:SDATable {generation:$generation}), (b:SDATable {generation:$generation})
        WHERE a.database + '.' + a.name = row.upstream AND b.database + '.' + b.name = row.downstream
        CREATE (a)-[r:DEPENDS_ON]->(b) SET r = row""",
        {"rows": edges, "generation": generation},
    )
    if rows:
        storage.add_vectors(
            [r["ref"] for r in rows],
            [r["name"] + " " + r["description"] for r in rows],
            [
                {
                    "scope": "tasks",
                    **{
                        k: r[k]
                        for k in (
                            "ref",
                            "name",
                            "description",
                            "has_output",
                            "parse_status",
                        )
                    },
                }
                for r in rows
            ],
        )
    return StageResult(
        counts={"task": len(rows), "lineage": len(edges)}, failures=failures
    )
