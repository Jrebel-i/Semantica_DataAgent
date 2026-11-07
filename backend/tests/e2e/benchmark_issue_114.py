"""Measure graph stage writes on test2; credentials come from NEO4J_AUTH.

Example: NEO4J_AUTH=user/password uv run --directory backend python tests/e2e/benchmark_issue_114.py
The generated generation is deleted even if a stage raises an exception.
"""

import argparse
import os
import time
import uuid

from neo4j import GraphDatabase

from semantica_dataagent import document_stage, relationships, table_stage, task_stage
from semantica_dataagent.canonical import Record
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
                    "task_id": f"task{i % 2000}",
                    "evidence": "declared",
                    "parse_status": "success",
                },
                "synthetic",
            )
        )
    schema = {
        f"issue114.t{i:04}": [f"f{j:02}" for j in range(25)] for i in range(args.tables)
    }
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
                        "task_column_lineage",
                        lambda: task_stage.write_column_lineage(
                            records, [], {}, schema, generation, storage
                        ),
                    ),
                    (
                        "relationships",
                        lambda: relationships.reconcile(storage, records, generation),
                    ),
                ]:
                    start = time.monotonic()
                    result = operation()
                    print(name, round(time.monotonic() - start, 3), result, flush=True)
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
