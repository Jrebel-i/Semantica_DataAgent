"""Measure graph stage writes on test2; credentials come from NEO4J_AUTH.

Example: NEO4J_AUTH=user/password uv run --directory backend python tests/e2e/benchmark_issue_114.py
The generated generation is deleted even if a stage raises an exception.
"""

import argparse
import os
import time
import uuid
from collections import Counter

from neo4j import GraphDatabase

from semantica_dataagent import document_stage, relationships, table_stage, task_stage
from semantica_dataagent.canonical import Record
from semantica_dataagent.sql_tasks import SQLTaskParser
from semantica_dataagent.stage_contract import delete_batches


class Storage:
    build_id = 114
    is_incremental = False

    def __init__(self, session, records):
        self.session = session
        self.all_records = records

    def execute(self, query, parameters):
        rows = parameters.get("rows")
        if rows is not None and "UNWIND $rows AS row" in query and len(rows) > 2000:
            result = []
            for start in range(0, len(rows), 2000):
                result.extend(
                    self.execute(
                        query, {**parameters, "rows": rows[start : start + 2000]}
                    )
                )
            return result
        return [dict(row) for row in self.session.run(query, parameters)]

    def add_vectors(self, ids, texts, metadata):
        pass  # Isolate graph costs; embedding and Milvus are not measured.


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tables", type=int, default=2000)
    parser.add_argument("--edges", type=int, default=100000)
    parser.add_argument("--documents", type=int, default=4000)
    parser.add_argument("--tasks", type=int, default=2000)
    args = parser.parse_args()
    generation = "issue114_" + uuid.uuid4().hex[:12]
    print("generation", generation, flush=True)
    records = []
    for i in range(args.tables):
        table = f"t{i:04}"
        records.append(
            Record(
                "table",
                {"database": "issue114", "name": table, "comment": ""},
                "synthetic",
            )
        )
        for j in range(25):
            records.append(
                Record(
                    "field",
                    {
                        "database": "issue114",
                        "table": table,
                        "name": f"f{j:02}",
                        "comment": "",
                    },
                    "synthetic",
                )
            )
    for i in range(args.documents):
        records.append(
            Record(
                "document",
                {
                    "category": "issue114",
                    "path": f"doc{i}.md",
                    "content": "synthetic document",
                },
                "synthetic",
            )
        )
    for i in range(args.tasks):
        up, down = i % args.tables, (i + 1) % args.tables
        records.append(
            Record(
                "task",
                {
                    "name": f"task{i}",
                    "type": "sql",
                    "sql": f"INSERT INTO issue114.t{down:04} (f00) SELECT f00 FROM issue114.t{up:04}",
                },
                "synthetic",
            )
        )
        records.append(
            Record(
                "lineage",
                {
                    "upstream": f"issue114.t{up:04}",
                    "downstream": f"issue114.t{down:04}",
                    "task_id": f"task{i}",
                    "evidence": "declared",
                    "parse_status": "success",
                },
                "synthetic",
            )
        )
    for i in range(args.edges):
        up, down = i % args.tables, (i + 1 + i // (args.tables * 25)) % args.tables
        records.append(
            Record(
                "column_lineage",
                {
                    "upstream_table": f"issue114.t{up:04}",
                    "upstream_field": f"f{i % 25:02}",
                    "downstream_table": f"issue114.t{down:04}",
                    "downstream_field": f"f{(i // args.tables) % 25:02}",
                    "task_id": f"orphan{i % 2000}",
                    "evidence": "declared",
                    "parse_status": "success",
                },
                "synthetic",
            )
        )
    # Use the real SQL parser without consulting a user's plugin configuration.
    parser_plugin = SQLTaskParser()
    task_stage.parse_task = lambda task, schema: parser_plugin.parse(
        task, {"schema": schema}
    )
    driver = GraphDatabase.driver(
        "bolt://172.16.242.124:31621",
        auth=tuple(os.environ["NEO4J_AUTH"].split("/", 1)),
    )
    try:
        with driver.session() as session:
            storage = Storage(session, records)
            try:
                for name, operation in [
                    ("table", lambda: table_stage.write(records, generation, storage)),
                    (
                        "document",
                        lambda: document_stage.write(records, generation, storage),
                    ),
                    (
                        "task",
                        lambda: task_stage.write(records, generation, storage),
                    ),
                    (
                        "relationships",
                        lambda: relationships.reconcile(storage, records, generation),
                    ),
                ]:
                    start = time.monotonic()
                    result = operation()
                    print(name, round(time.monotonic() - start, 3), result, flush=True)
                counts = session.run(
                    """MATCH (n:SDAEntity {generation:$generation})
                    RETURN sum(CASE WHEN n:SDATable THEN 1 ELSE 0 END) AS tables,
                           sum(CASE WHEN n:SDAField THEN 1 ELSE 0 END) AS fields,
                           sum(CASE WHEN n:SDATask THEN 1 ELSE 0 END) AS tasks,
                           sum(CASE WHEN n:SDAChunk THEN 1 ELSE 0 END) AS chunks""",
                    generation=generation,
                ).single()
                assert dict(counts) == {
                    "tables": args.tables,
                    "fields": args.tables * 25,
                    "tasks": args.tasks,
                    "chunks": args.documents,
                }, dict(counts)
                edge_counts = session.run(
                    """MATCH (a:SDAField {generation:$generation})-[r:FIELD_DEPENDS_ON]->(b:SDAField {generation:$generation})
                    RETURN count(r) AS total, sum(CASE WHEN r.upstream_table = a.database + '.' + a.table AND r.upstream_field = a.name AND r.downstream_table = b.database + '.' + b.table AND r.downstream_field = b.name THEN 0 ELSE 1 END) AS invalid""",
                    generation=generation,
                ).single()
                table_edges = session.run(
                    """MATCH (a:SDATable {generation:$generation})-[r:DEPENDS_ON]->(b:SDATable {generation:$generation})
                    RETURN count(r) AS total, coalesce(sum(CASE WHEN r.upstream = a.database + '.' + a.name AND r.downstream = b.database + '.' + b.name THEN 0 ELSE 1 END), 0) AS invalid""",
                    generation=generation,
                ).single()
                assert (
                    table_edges["total"] == args.tasks and table_edges["invalid"] == 0
                ), dict(table_edges)
                print(
                    "verified",
                    dict(counts),
                    dict(edge_counts),
                    dict(table_edges),
                    flush=True,
                )
                assert (
                    edge_counts["total"] == args.edges + args.tasks
                    and edge_counts["invalid"] == 0
                )
                keys = (
                    "upstream_table",
                    "upstream_field",
                    "downstream_table",
                    "downstream_field",
                    "task_id",
                    "evidence",
                    "parse_status",
                )
                expected = Counter(
                    tuple(record.data[key] for key in keys)
                    for record in records
                    if record.kind == "column_lineage"
                )
                actual = Counter(
                    tuple(row[key] for key in keys)
                    for row in session.run(
                        """MATCH (:SDAField {generation:$generation})-[r:FIELD_DEPENDS_ON]->(:SDAField {generation:$generation}) WHERE r.task_id STARTS WITH 'orphan' RETURN r.upstream_table AS upstream_table, r.upstream_field AS upstream_field, r.downstream_table AS downstream_table, r.downstream_field AS downstream_field, r.task_id AS task_id, r.evidence AS evidence, r.parse_status AS parse_status""",
                        generation=generation,
                    )
                )
                assert actual == expected, "字段来源的内容或重复数量不同"
                print("field content verified", sum(actual.values()), flush=True)
            finally:
                delete_batches(
                    storage.execute,
                    "MATCH (n:SDAEntity {generation:$generation})",
                    "n",
                    {"generation": generation},
                    detach=True,
                )
                print("cleaned", generation, flush=True)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
