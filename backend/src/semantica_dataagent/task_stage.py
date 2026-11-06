"""Append task nodes and table-level lineage for the current build."""

from urllib.parse import quote

from .plugins import parse_task
from .stage_contract import StageResult, delete_batches


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


def _provenance(record, build_id):
    return {
        "source_file": record.source_file,
        "source_key": record.source_key or record.source_file,
        "source_files": list(record.source_files),
        "build_id": build_id,
    }


def _edge_provenance(task, declarations, build_id):
    files = {task.source_file, *task.source_files}
    for declaration in declarations:
        files.add(declaration.source_file)
        files.update(declaration.source_files)
    return {
        "source_file": task.source_file,
        "source_key": task.source_key or task.source_file,
        "source_files": sorted(files),
        "build_id": build_id,
    }


def write(records, generation, storage):
    all_records = storage.all_records if storage.all_records is not None else records
    all_tasks = [r for r in all_records if r.kind == "task"]
    all_declarations = [r for r in all_records if r.kind == "lineage"]
    schema = {}
    for record in all_records:
        if record.kind == "field":
            field = record.data
            schema.setdefault(f"{field['database']}.{field['table']}", []).append(
                field["name"]
            )
    parsed_tasks = {
        task.data["name"]: parse_task(task, schema=schema) for task in all_tasks
    }
    rebuild_all = not storage.is_incremental or any(
        record.kind == "lineage" for record in records
    )
    if rebuild_all:
        tasks = all_tasks
        delete_batches(
            storage.execute,
            "MATCH (t:SDATask {generation:$generation})",
            "t",
            {"generation": generation},
            detach=True,
        )
        delete_batches(
            storage.execute,
            "MATCH (:SDALineageTable {generation:$generation})-[r:DEPENDS_ON]->(:SDALineageTable {generation:$generation})",
            "r",
            {"generation": generation},
            detach=False,
        )
        delete_batches(
            storage.execute,
            "MATCH (:SDATable {generation:$generation})-[r:DEPENDS_ON]->(:SDATable {generation:$generation})",
            "r",
            {"generation": generation},
            detach=False,
        )
    else:
        tasks = [r for r in records if r.kind == "task"]
        names = [r.data["name"] for r in tasks]
        delete_batches(
            storage.execute,
            "MATCH (:SDALineageTable {generation:$generation})-[r:DEPENDS_ON]->(:SDALineageTable {generation:$generation}) WHERE r.task_id IN $names",
            "r",
            {"generation": generation, "names": names},
            detach=False,
        )
        delete_batches(
            storage.execute,
            "MATCH (:SDATable {generation:$generation})-[r:DEPENDS_ON]->(:SDATable {generation:$generation}) WHERE r.task_id IN $names",
            "r",
            {"generation": generation, "names": names},
            detach=False,
        )
    names = [r.data["name"] for r in tasks]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate task name")
    declarations = {}
    for record in all_declarations:
        task_id = record.data.get("task_id")
        if task_id:
            pair = (record.data["upstream"], record.data["downstream"])
            declarations.setdefault(task_id, {}).setdefault(pair, []).append(record)
    rows, edges, failures = [], [], []
    for task in tasks:
        name = task.data["name"]
        ref = "task:" + quote(name, safe="")
        declared = list(declarations.get(name, {}))
        result, resolved = resolve_task(task, declared, parsed_tasks[name])
        reads = sorted(set(result.reads) | {u for u, _ in declared})
        writes = sorted(set(result.writes) | {d for _, d in declared})
        rows.append(
            {
                "name": name,
                "type": task.data.get("type", ""),
                "ref": ref,
                "sql": task.data["sql"],
                "description": task.data.get("description", ""),
                "parse_status": result.status,
                "reason": result.reason or "",
                "reads": reads,
                "writes": writes,
                "has_output": bool(writes),
                **_provenance(task, storage.build_id),
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
                **_edge_provenance(
                    task, declarations.get(name, {}).get((u, d), []), storage.build_id
                ),
            }
            for u, d, e in resolved
        )
        if result.status == "failed":
            failures.append(
                {"task_id": name, "reason": result.reason or "Parse failed"}
            )
    # Legacy lineage records can be independent of a task, or refer to a task
    # that is not present in this source snapshot.
    if rebuild_all:
        known = {record.data["name"] for record in all_tasks}
        edges.extend(
            {
                "upstream": record.data["upstream"],
                "downstream": record.data["downstream"],
                "evidence": record.data["evidence"],
                "parse_status": record.data["parse_status"],
                "task_id": record.data.get("task_id", ""),
                "ref": (
                    "task:" + quote(record.data["task_id"], safe="")
                    if record.data.get("task_id")
                    else ""
                ),
                **_provenance(record, storage.build_id),
            }
            for record in all_declarations
            if record.data.get("task_id") not in known
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
    lineage_nodes = {}
    for edge in edges:
        for name in (edge["upstream"], edge["downstream"]):
            node = lineage_nodes.setdefault(
                name,
                {
                    "name": name,
                    "source_file": edge["source_file"],
                    "source_key": edge["source_key"],
                    "source_files": set(),
                },
            )
            node["source_files"].update(edge["source_files"])
    storage.execute(
        """UNWIND $rows AS row MERGE (n:SDAEntity:SDALineageTable {generation:$generation, name:row.name}) SET n.source_file = row.source_file, n.source_key = row.source_key, n.source_files = row.source_files, n.build_id = $build_id""",
        {
            "rows": [
                {**row, "source_files": sorted(row["source_files"])}
                for row in lineage_nodes.values()
            ],
            "generation": generation,
            "build_id": storage.build_id,
        },
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
    column_count, column_failures = write_column_lineage(
        all_records, all_tasks, parsed_tasks, schema, generation, storage
    )
    failures.extend(column_failures)
    return StageResult(
        counts={
            "task": len(rows),
            "lineage": len(edges),
            "column_lineage": column_count,
        },
        failures=failures,
    )


def write_column_lineage(records, tasks, parsed_tasks, schema, generation, storage):
    """Reconcile field edges against the complete source snapshot on every build."""
    declared = {}
    for record in records:
        if record.kind != "column_lineage":
            continue
        data = record.data
        key = (
            data["upstream_table"],
            data["upstream_field"],
            data["downstream_table"],
            data["downstream_field"],
        )
        declared.setdefault(data["task_id"], {})[key] = record
    rows, failures = [], []
    for task in tasks:
        name = task.data["name"]
        result = parsed_tasks[name]
        parsed = set(result.column_edges or [])
        declared_for_task = declared.pop(name, {})
        if result.status == "unsupported" or (
            result.status == "success" and result.column_status != "success"
        ):
            failures.append(
                {
                    "task_id": name,
                    "reason": result.column_reason
                    or result.reason
                    or "Column lineage unavailable",
                }
            )
        for edge in sorted(parsed | declared_for_task.keys()):
            upstream, source, downstream, target = edge
            if source not in schema.get(upstream, []) or target not in schema.get(
                downstream, []
            ):
                failures.append(
                    {
                        "task_id": name,
                        "reason": f"Field metadata missing for {upstream}.{source} or {downstream}.{target}",
                    }
                )
                continue
            declaration = declared_for_task.get(edge)
            rows.append(
                {
                    "upstream_table": upstream,
                    "upstream_field": source,
                    "downstream_table": downstream,
                    "downstream_field": target,
                    "task_id": name,
                    "evidence": "both"
                    if declaration and edge in parsed
                    else "declared"
                    if declaration
                    else "parsed",
                    "parse_status": result.column_status,
                    **_edge_provenance(
                        task, [declaration] if declaration else [], storage.build_id
                    ),
                }
            )
    # Orphan declarations are retained if both fields exist.
    for items in declared.values():
        for edge, record in items.items():
            upstream, source, downstream, target = edge
            if source in schema.get(upstream, []) and target in schema.get(
                downstream, []
            ):
                rows.append({**record.data, **_provenance(record, storage.build_id)})
    delete_batches(
        storage.execute,
        "MATCH (:SDAField {generation:$generation})-[r:FIELD_DEPENDS_ON]->(:SDAField {generation:$generation})",
        "r",
        {"generation": generation},
        detach=False,
    )
    storage.execute(
        """UNWIND $rows AS row
        MATCH (a:SDAField {generation:$generation, name:row.upstream_field}),
              (b:SDAField {generation:$generation, name:row.downstream_field})
        WHERE a.database + '.' + a.table = row.upstream_table
          AND b.database + '.' + b.table = row.downstream_table
        CREATE (a)-[r:FIELD_DEPENDS_ON]->(b) SET r = row""",
        {"rows": rows, "generation": generation},
    )
    return len(rows), failures
