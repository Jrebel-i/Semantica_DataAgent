"""Seam 2 tests for entity-level incremental builds, cross-puller fast-fail, and fallbacks."""

from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import Fernet


def _setup_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from semantica_dataagent import sql_validation
    from semantica_dataagent.cli import initialize

    sql_validation._snapshot_cache = None
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    return initialize()


def _make_ossie_model(
    model_name: str,
    table_specs: list[tuple[str, str, str]],
    *,
    relationships: list[dict] | None = None,
) -> dict:
    datasets = []
    for db_name, tbl_name, desc in table_specs:
        datasets.append(
            {
                "name": tbl_name,
                "source": f"{db_name}.{tbl_name}",
                "description": desc,
                "fields": [
                    {
                        "name": "id",
                        "expression": {"sql": "id"},
                        "datatype": "Integer",
                        "description": f"{tbl_name}主键",
                    },
                    {
                        "name": "amount",
                        "expression": {"sql": "amount"},
                        "datatype": "Decimal",
                        "description": f"{tbl_name}金额",
                    },
                ],
            }
        )
    doc: dict = {
        "version": "0.3.0.dev0",
        "name": model_name,
        "datasets": datasets,
    }
    if relationships:
        doc["relationships"] = relationships
    return doc


class _InMemoryGraph:
    """In-memory Neo4j stand-in that tracks queries, node build_ids, and Cypher deletes."""

    def __init__(self):
        self.nodes: list[dict] = []
        self.rels: list[dict] = []
        self.queries: list[tuple[str, dict]] = []
        self._next_id = 1
        self.fail_on_delete = False
        self.fail_on_table_create = False

    def _find_node(self, node_id: int) -> dict:
        return next(n for n in self.nodes if n["id"] == node_id)

    def _matches_incremental(self, node: dict, params: dict, query: str) -> bool:
        props = node["props"]
        files = set(params.get("files") or ())
        selected_refs = set(params.get("selected_refs") or ())
        touched_tables = {
            (t.get("catalog") or "", t.get("database"), t.get("table"))
            for t in (params.get("touched_tables") or ())
        }
        dim_tables = {
            (t.get("catalog") or "", t.get("database"), t.get("table"))
            for t in (params.get("tables") or ())
        }
        if (
            props.get("source_file") in files
            or props.get("source_key") in files
            or bool(files.intersection(props.get("source_files") or ()))
            or props.get("ref") in selected_refs
            or props.get("doc_id") in selected_refs
        ):
            return True
        if "SDAField" in node["labels"] and (
            (props.get("catalog") or "", props.get("database"), props.get("table"))
            in touched_tables
        ):
            return True
        return "SDADimensionValue" in node["labels"] and any(
            db == props.get("database")
            and table == props.get("table")
            and (
                "coalesce(t.catalog, '') = coalesce(n.catalog, '')"
                not in query.partition("n:SDADimensionValue AND any")[2]
                or catalog == (props.get("catalog") or "")
            )
            for catalog, db, table in dim_tables
        )

    def execute_query(self, query: str, params: dict):
        q = " ".join(query.split())
        self.queries.append((q, copy.deepcopy(params)))
        if q.startswith(("CREATE INDEX", "CREATE FULLTEXT INDEX", "CALL db.")):
            return {"success": True, "records": []}
        gen = params.get("generation")

        if (
            "MATCH (t:SDATable {generation:$generation})-[:HAS_FIELD]->(f:SDAField {generation:$generation}) WHERE t.ref IN $table_refs RETURN f.ref AS ref"
            in q
        ):
            table_refs = set(params.get("table_refs") or ())
            table_ids = {
                n["id"]
                for n in self.nodes
                if "SDATable" in n["labels"]
                and n["props"].get("generation") == gen
                and n["props"].get("ref") in table_refs
            }
            field_ids = {
                r["end"]
                for r in self.rels
                if r["type"] == "HAS_FIELD" and r["start"] in table_ids
            }
            return {
                "success": True,
                "records": [
                    {"ref": n["props"]["ref"]}
                    for n in self.nodes
                    if n["id"] in field_ids
                    and "SDAField" in n["labels"]
                    and n["props"].get("generation") == gen
                    and n["props"].get("ref")
                ],
            }

        if (
            "MATCH (f:SDAField {generation:$generation})" in q
            and "RETURN f.ref AS ref" in q
        ):
            touched_tables = {
                (t.get("catalog") or "", t.get("database"), t.get("table"))
                for t in (params.get("tables") or ())
            }
            return {
                "success": True,
                "records": [
                    {"ref": n["props"]["ref"]}
                    for n in self.nodes
                    if "SDAField" in n["labels"]
                    and n["props"].get("generation") == gen
                    and (
                        n["props"].get("catalog") or "",
                        n["props"].get("database"),
                        n["props"].get("table"),
                    )
                    in touched_tables
                    and n["props"].get("ref")
                ],
            }

        if (
            "MATCH (n:SDAEntity {generation:$generation}) WHERE n.source_file IN $files"
            in q
            and "RETURN n.ref AS ref" in q
        ):
            return {
                "success": True,
                "records": [
                    {"ref": n["props"]["ref"]}
                    for n in self.nodes
                    if "SDAEntity" in n["labels"]
                    and n["props"].get("generation") == gen
                    and self._matches_incremental(n, params, q)
                    and n["props"].get("ref")
                ],
            }

        if (
            "MATCH (n:SDAEntity {generation:$generation}) WHERE n.source_file IN $files"
            in q
        ):
            if self.fail_on_delete:
                raise RuntimeError("Simulated incremental graph delete failure")
            victims = {
                n["id"]
                for n in self.nodes
                if "SDAEntity" in n["labels"]
                and n["props"].get("generation") == gen
                and self._matches_incremental(n, params, q)
            }
            if "DELETE batch_relationship" in q:
                keep = [
                    r
                    for r in self.rels
                    if r["start"] not in victims and r["end"] not in victims
                ]
                deleted = len(self.rels) - len(keep)
                self.rels = keep
                return {"success": True, "records": [{"deleted": deleted}]}
            if "DETACH DELETE n" in q:
                self.rels = [
                    r
                    for r in self.rels
                    if r["start"] not in victims and r["end"] not in victims
                ]
                keep = [n for n in self.nodes if n["id"] not in victims]
                deleted = len(self.nodes) - len(keep)
                self.nodes = keep
                return {"success": True, "records": [{"deleted": deleted}]}

        if (
            "MATCH (:SDATable {generation:$generation})-[r:DEPENDS_ON]->(:SDATable {generation:$generation}) WHERE r.task_id IN $task_names"
            in q
        ):
            names = set(params.get("task_names") or ())
            keep, deleted = [], 0
            for rel in self.rels:
                if rel["type"] == "DEPENDS_ON" and rel["props"].get("task_id") in names:
                    s = self._find_node(rel["start"])
                    e = self._find_node(rel["end"])
                    if (
                        "SDATable" in s["labels"]
                        and "SDATable" in e["labels"]
                        and s["props"].get("generation") == gen
                        and e["props"].get("generation") == gen
                    ):
                        deleted += 1
                        continue
                keep.append(rel)
            self.rels = keep
            return {"success": True, "records": [{"deleted": deleted}]}

        if (
            "MATCH (:SDALineageTable {generation:$generation})-[r:DEPENDS_ON]->(:SDALineageTable {generation:$generation}) WHERE r.task_id IN"
            in q
        ):
            names = set(params.get("names") or params.get("task_names") or ())
            keep, deleted = [], 0
            for rel in self.rels:
                if rel["type"] == "DEPENDS_ON" and rel["props"].get("task_id") in names:
                    s = self._find_node(rel["start"])
                    e = self._find_node(rel["end"])
                    if (
                        "SDALineageTable" in s["labels"]
                        and "SDALineageTable" in e["labels"]
                        and s["props"].get("generation") == gen
                        and e["props"].get("generation") == gen
                    ):
                        deleted += 1
                        continue
                keep.append(rel)
            self.rels = keep
            return {"success": True, "records": [{"deleted": deleted}]}

        if (
            "MATCH (:SDAField {generation:$generation})-[r:FIELD_DEPENDS_ON]->(:SDAField {generation:$generation})"
            in q
        ):
            names = set(params.get("task_names") or ())
            has_filter = "WHERE r.task_id IN" in q
            keep, deleted = [], 0
            for rel in self.rels:
                if rel["type"] == "FIELD_DEPENDS_ON" and (
                    not has_filter or rel["props"].get("task_id") in names
                ):
                    s = self._find_node(rel["start"])
                    e = self._find_node(rel["end"])
                    if (
                        "SDAField" in s["labels"]
                        and "SDAField" in e["labels"]
                        and s["props"].get("generation") == gen
                        and e["props"].get("generation") == gen
                    ):
                        deleted += 1
                        continue
                keep.append(rel)
            self.rels = keep
            return {"success": True, "records": [{"deleted": deleted}]}

        if (
            "MATCH (n:SDAEntity {generation:$generation}) MATCH (n)-[batch_relationship]-()"
            in q
            or "MATCH (n:SDAEntity {generation:$generation}) WITH n LIMIT $batch_size DETACH DELETE n"
            in q
        ):
            victims = {
                n["id"] for n in self.nodes if n["props"].get("generation") == gen
            }
            self.rels = [
                r
                for r in self.rels
                if r["start"] not in victims and r["end"] not in victims
            ]
            keep = [n for n in self.nodes if n["id"] not in victims]
            deleted = len(self.nodes) - len(keep)
            self.nodes = keep
            return {"success": True, "records": [{"deleted": deleted}]}

        if "MATCH (c:SDACategory {generation:$generation})" in q and "DELETE" in q:
            cats = set(params.get("categories") or ())
            if "NOT c.name IN $categories" in q:
                victims = {
                    n["id"]
                    for n in self.nodes
                    if "SDACategory" in n["labels"]
                    and n["props"].get("generation") == gen
                    and n["props"].get("name") not in cats
                }
                if "DELETE batch_relationship" in q:
                    keep = [
                        r
                        for r in self.rels
                        if r["start"] not in victims and r["end"] not in victims
                    ]
                    deleted = len(self.rels) - len(keep)
                    self.rels = keep
                    return {"success": True, "records": [{"deleted": deleted}]}
                if "DETACH DELETE c" in q:
                    self.rels = [
                        r
                        for r in self.rels
                        if r["start"] not in victims and r["end"] not in victims
                    ]
                    keep = [n for n in self.nodes if n["id"] not in victims]
                    deleted = len(self.nodes) - len(keep)
                    self.nodes = keep
                    return {"success": True, "records": [{"deleted": deleted}]}
            return {"success": True, "records": [{"deleted": 0}]}

        if "RETURN count(*) AS deleted" in q:
            return {"success": True, "records": [{"deleted": 0}]}

        if "CREATE (n:SDAEntity:SDATable) SET n = row" in q:
            if self.fail_on_table_create:
                raise RuntimeError("Simulated incremental table write failure")
            for row in params["rows"]:
                self.nodes.append(
                    {
                        "id": self._next_id,
                        "labels": {"SDAEntity", "SDATable"},
                        "props": {**row, "generation": gen},
                    }
                )
                self._next_id += 1
            return {"success": True, "records": []}

        if "CREATE (f:SDAEntity:SDAField) SET f = row" in q:
            for row in params["rows"]:
                self.nodes.append(
                    {
                        "id": self._next_id,
                        "labels": {"SDAEntity", "SDAField"},
                        "props": {**row, "generation": gen},
                    }
                )
                self._next_id += 1
            return {"success": True, "records": []}

        if "CREATE (t:SDAEntity:SDATask) SET t = row" in q:
            for row in params["rows"]:
                self.nodes.append(
                    {
                        "id": self._next_id,
                        "labels": {"SDAEntity", "SDATask"},
                        "props": {**row, "generation": gen},
                    }
                )
                self._next_id += 1
            return {"success": True, "records": []}

        if "CREATE (d:SDAEntity:SDADocument) SET d = row" in q:
            for row in params["rows"]:
                self.nodes.append(
                    {
                        "id": self._next_id,
                        "labels": {"SDAEntity", "SDADocument"},
                        "props": {**row, "generation": gen},
                    }
                )
                self._next_id += 1
            return {"success": True, "records": []}

        if "CREATE (p:SDAEntity:SDAChunk) SET p = row" in q:
            for row in params["rows"]:
                doc = next(
                    n
                    for n in self.nodes
                    if "SDADocument" in n["labels"]
                    and n["props"].get("generation") == gen
                    and n["props"].get("ref") == row["doc_id"]
                )
                chunk = {
                    "id": self._next_id,
                    "labels": {"SDAEntity", "SDAChunk"},
                    "props": {**row, "generation": gen},
                }
                self._next_id += 1
                self.nodes.append(chunk)
                self.rels.append(
                    {
                        "type": "HAS_CHUNK",
                        "start": doc["id"],
                        "end": chunk["id"],
                        "props": {},
                    }
                )
            return {"success": True, "records": []}

        if "MERGE (t)-[:READS]->(n)" in q or "MERGE (t)-[:PRODUCES]->(n)" in q:
            rel_type = "READS" if ":READS" in q else "PRODUCES"
            field = "reads" if rel_type == "READS" else "writes"
            for row in params["rows"]:
                task_node = next(
                    n
                    for n in self.nodes
                    if "SDATask" in n["labels"]
                    and n["props"].get("generation") == gen
                    and n["props"].get("ref") == row["ref"]
                )
                for tbl_name in row[field]:
                    lt = next(
                        (
                            n
                            for n in self.nodes
                            if "SDALineageTable" in n["labels"]
                            and n["props"].get("generation") == gen
                            and n["props"].get("name") == tbl_name
                        ),
                        None,
                    )
                    if lt is None:
                        lt = {
                            "id": self._next_id,
                            "labels": {"SDAEntity", "SDALineageTable"},
                            "props": {"generation": gen, "name": tbl_name},
                        }
                        self._next_id += 1
                        self.nodes.append(lt)
                    if not any(
                        r["type"] == rel_type
                        and r["start"] == task_node["id"]
                        and r["end"] == lt["id"]
                        for r in self.rels
                    ):
                        self.rels.append(
                            {
                                "type": rel_type,
                                "start": task_node["id"],
                                "end": lt["id"],
                                "props": {},
                            }
                        )
            return {"success": True, "records": []}

        if "MATCH (a:SDALineageTable" in q or "MERGE (a:SDAEntity:SDALineageTable" in q:
            for row in params["rows"]:
                nodes_by_name = {}
                for name in (row["upstream"], row["downstream"]):
                    existing = next(
                        (
                            n
                            for n in self.nodes
                            if "SDALineageTable" in n["labels"]
                            and n["props"].get("generation") == gen
                            and n["props"].get("name") == name
                        ),
                        None,
                    )
                    if existing is None:
                        existing = {
                            "id": self._next_id,
                            "labels": {"SDAEntity", "SDALineageTable"},
                            "props": {"generation": gen, "name": name},
                        }
                        self._next_id += 1
                        self.nodes.append(existing)
                    nodes_by_name[name] = existing
                self.rels.append(
                    {
                        "type": "DEPENDS_ON",
                        "start": nodes_by_name[row["upstream"]]["id"],
                        "end": nodes_by_name[row["downstream"]]["id"],
                        "props": dict(row),
                    }
                )
            return {"success": True, "records": []}

        if "MATCH (task:SDATask" in q and (
            "MERGE (task)-[:READS]->(table)" in q
            or "MERGE (task)-[:PRODUCES]->(table)" in q
        ):
            rel_type = "READS" if ":READS" in q else "PRODUCES"
            field = "reads" if rel_type == "READS" else "writes"
            for row in params["rows"]:
                task_node = next(
                    (
                        n
                        for n in self.nodes
                        if "SDATask" in n["labels"]
                        and n["props"].get("generation") == gen
                        and n["props"].get("ref") == row["ref"]
                    ),
                    None,
                )
                if task_node is None:
                    continue
                for target_name in row[field]:
                    if "." not in target_name:
                        continue
                    db_name, tbl_name = target_name.split(".", 1)
                    tbl = next(
                        (
                            n
                            for n in self.nodes
                            if "SDATable" in n["labels"]
                            and n["props"].get("generation") == gen
                            and n["props"].get("database") == db_name
                            and n["props"].get("name") == tbl_name
                        ),
                        None,
                    )
                    if tbl is not None and not any(
                        r["type"] == rel_type
                        and r["start"] == task_node["id"]
                        and r["end"] == tbl["id"]
                        for r in self.rels
                    ):
                        self.rels.append(
                            {
                                "type": rel_type,
                                "start": task_node["id"],
                                "end": tbl["id"],
                                "props": {},
                            }
                        )
            return {"success": True, "records": []}

        if "MATCH (a:SDATable" in q and "DEPENDS_ON" in q:
            for row in params["rows"]:
                if "." not in row["upstream"] or "." not in row["downstream"]:
                    continue
                up_db, up_tbl = row["upstream"].split(".", 1)
                dn_db, dn_tbl = row["downstream"].split(".", 1)
                a = next(
                    (
                        n
                        for n in self.nodes
                        if "SDATable" in n["labels"]
                        and n["props"].get("generation") == gen
                        and n["props"].get("database") == up_db
                        and n["props"].get("name") == up_tbl
                    ),
                    None,
                )
                b = next(
                    (
                        n
                        for n in self.nodes
                        if "SDATable" in n["labels"]
                        and n["props"].get("generation") == gen
                        and n["props"].get("database") == dn_db
                        and n["props"].get("name") == dn_tbl
                    ),
                    None,
                )
                if a is not None and b is not None:
                    self.rels.append(
                        {
                            "type": "DEPENDS_ON",
                            "start": a["id"],
                            "end": b["id"],
                            "props": dict(row),
                        }
                    )
            return {"success": True, "records": []}

        if "FIELD_DEPENDS_ON" in q and "CREATE (a)-[r:FIELD_DEPENDS_ON]->(b)" in q:
            for row in params["rows"]:
                if (
                    "." not in row["upstream_table"]
                    or "." not in row["downstream_table"]
                ):
                    continue
                up_db, up_tbl = row["upstream_table"].split(".", 1)
                dn_db, dn_tbl = row["downstream_table"].split(".", 1)
                a = next(
                    (
                        n
                        for n in self.nodes
                        if "SDAField" in n["labels"]
                        and n["props"].get("generation") == gen
                        and n["props"].get("database") == up_db
                        and n["props"].get("table") == up_tbl
                        and n["props"].get("name") == row["upstream_field"]
                    ),
                    None,
                )
                b = next(
                    (
                        n
                        for n in self.nodes
                        if "SDAField" in n["labels"]
                        and n["props"].get("generation") == gen
                        and n["props"].get("database") == dn_db
                        and n["props"].get("table") == dn_tbl
                        and n["props"].get("name") == row["downstream_field"]
                    ),
                    None,
                )
                if a is not None and b is not None:
                    self.rels.append(
                        {
                            "type": "FIELD_DEPENDS_ON",
                            "start": a["id"],
                            "end": b["id"],
                            "props": dict(row),
                        }
                    )
            return {"success": True, "records": []}

        if "MERGE (c:SDAEntity:SDACategory" in q:
            for row in params["rows"]:
                existing = next(
                    (
                        n
                        for n in self.nodes
                        if "SDACategory" in n["labels"]
                        and n["props"].get("generation") == gen
                        and n["props"].get("name") == row["name"]
                    ),
                    None,
                )
                if existing is None:
                    self.nodes.append(
                        {
                            "id": self._next_id,
                            "labels": {"SDAEntity", "SDACategory"},
                            "props": {
                                "generation": gen,
                                "build_id": params["build_id"],
                                **row,
                            },
                        }
                    )
                    self._next_id += 1
                else:
                    existing["props"].update(row)
                    existing["props"]["build_id"] = params["build_id"]
            return {"success": True, "records": []}

        if "RETURN elementId(n) AS id" in q:
            if ":SDAField" in q:
                tables = {
                    (t.get("catalog") or "", t.get("database"), t.get("table"))
                    for t in (params.get("tables") or ())
                }
                build_id = params.get("build_id")
                return {
                    "success": True,
                    "records": [
                        {"id": n["id"]}
                        for n in self.nodes
                        if "SDAField" in n["labels"]
                        and n["props"].get("generation") == gen
                        and (
                            not tables
                            or (
                                build_id is not None
                                and n["props"].get("build_id") == build_id
                            )
                            or (
                                n["props"].get("catalog") or "",
                                n["props"].get("database"),
                                n["props"].get("table"),
                            )
                            in tables
                        )
                    ],
                }
            if ":SDADocument" in q:
                doc_refs = set(params.get("doc_refs") or ())
                build_id = params.get("build_id")
                return {
                    "success": True,
                    "records": [
                        {"id": n["id"]}
                        for n in self.nodes
                        if "SDADocument" in n["labels"]
                        and n["props"].get("generation") == gen
                        and (
                            not doc_refs
                            or (
                                build_id is not None
                                and n["props"].get("build_id") == build_id
                            )
                            or n["props"].get("ref") in doc_refs
                        )
                    ],
                }
            return {"success": True, "records": []}

        if "MERGE (t)-[:HAS_FIELD]->(n)" in q:
            ids = set(params["ids"])
            for f in self.nodes:
                if f["id"] not in ids:
                    continue
                for t in self.nodes:
                    if (
                        "SDATable" in t["labels"]
                        and t["props"].get("generation") == gen
                        and t["props"].get("database") == f["props"].get("database")
                        and t["props"].get("name") == f["props"].get("table")
                        and (t["props"].get("catalog") or "")
                        == (f["props"].get("catalog") or "")
                        and not any(
                            r["type"] == "HAS_FIELD"
                            and r["start"] == t["id"]
                            and r["end"] == f["id"]
                            for r in self.rels
                        )
                    ):
                        self.rels.append(
                            {
                                "type": "HAS_FIELD",
                                "start": t["id"],
                                "end": f["id"],
                                "props": {},
                            }
                        )
            return {"success": True, "records": []}

        if "MERGE (c)-[:HAS_DOCUMENT]->(n)" in q:
            ids = set(params["ids"])
            for d in self.nodes:
                if d["id"] not in ids:
                    continue
                for c in self.nodes:
                    if (
                        "SDACategory" in c["labels"]
                        and c["props"].get("generation") == gen
                        and c["props"].get("name") == d["props"].get("category")
                        and not any(
                            r["type"] == "HAS_DOCUMENT"
                            and r["start"] == c["id"]
                            and r["end"] == d["id"]
                            for r in self.rels
                        )
                    ):
                        self.rels.append(
                            {
                                "type": "HAS_DOCUMENT",
                                "start": c["id"],
                                "end": d["id"],
                                "props": {},
                            }
                        )
            return {"success": True, "records": []}

        if "MERGE (t)-[:HAS_DOCUMENT]->(d)" in q:
            for row in params["rows"]:
                d = next(
                    (
                        n
                        for n in self.nodes
                        if "SDADocument" in n["labels"]
                        and n["props"].get("generation") == gen
                        and n["props"].get("ref") == row["ref"]
                    ),
                    None,
                )
                t = next(
                    (
                        n
                        for n in self.nodes
                        if "SDATable" in n["labels"]
                        and n["props"].get("generation") == gen
                        and n["props"].get("database") == row["database"]
                        and n["props"].get("name") == row["name"]
                        and (n["props"].get("catalog") or "")
                        == (row.get("catalog") or "")
                    ),
                    None,
                )
                if (
                    d is not None
                    and t is not None
                    and not any(
                        r["type"] == "HAS_DOCUMENT"
                        and r["start"] == t["id"]
                        and r["end"] == d["id"]
                        for r in self.rels
                    )
                ):
                    self.rels.append(
                        {
                            "type": "HAS_DOCUMENT",
                            "start": t["id"],
                            "end": d["id"],
                            "props": {},
                        }
                    )
            return {"success": True, "records": []}

        if (
            "MATCH (t:SDATask {generation:$generation, name:$name}) RETURN properties(t) AS task"
            in q
        ):
            return {
                "success": True,
                "records": [
                    {"task": dict(n["props"])}
                    for n in self.nodes
                    if "SDATask" in n["labels"]
                    and n["props"].get("generation") == gen
                    and n["props"].get("name") == params["name"]
                ],
            }

        if "MATCH (start:SDALineageTable" in q and "DEPENDS_ON" in q:
            start_node = next(
                (
                    n
                    for n in self.nodes
                    if "SDALineageTable" in n["labels"]
                    and n["props"].get("generation") == gen
                    and n["props"].get("name") == params["table"]
                ),
                None,
            )
            if start_node is None:
                return {"success": True, "records": []}
            depth = 2 if "*1..2" in q else 3 if "*1..3" in q else 1
            go_up = "<-[" in q
            frontier = {start_node["id"]}
            visited_edges: list[dict] = []
            seen_keys = set()
            for _ in range(depth):
                next_frontier = set()
                for r in self.rels:
                    if r["type"] != "DEPENDS_ON":
                        continue
                    if go_up and r["end"] in frontier:
                        key = (
                            r["props"].get("task_id"),
                            r["props"].get("upstream"),
                            r["props"].get("downstream"),
                        )
                        if key not in seen_keys:
                            seen_keys.add(key)
                            visited_edges.append({"edge": dict(r["props"])})
                        next_frontier.add(r["start"])
                    elif not go_up and r["start"] in frontier:
                        key = (
                            r["props"].get("task_id"),
                            r["props"].get("upstream"),
                            r["props"].get("downstream"),
                        )
                        if key not in seen_keys:
                            seen_keys.add(key)
                            visited_edges.append({"edge": dict(r["props"])})
                        next_frontier.add(r["end"])
                frontier = next_frontier
            return {"success": True, "records": visited_edges}

        if "MATCH (start:SDAField" in q and "FIELD_DEPENDS_ON" in q:
            start_field = next(
                (
                    n
                    for n in self.nodes
                    if "SDAField" in n["labels"]
                    and n["props"].get("generation") == gen
                    and n["props"].get("name") == params["column"]
                    and f"{n['props'].get('database')}.{n['props'].get('table')}"
                    == params["table"]
                ),
                None,
            )
            if start_field is None:
                return {"success": True, "records": []}
            depth = 2 if "*1..2" in q else 3 if "*1..3" in q else 1
            go_up = "<-[" in q
            frontier = {start_field["id"]}
            visited_edges = []
            seen_keys = set()
            for _ in range(depth):
                next_frontier = set()
                for r in self.rels:
                    if r["type"] != "FIELD_DEPENDS_ON":
                        continue
                    if go_up and r["end"] in frontier:
                        key = (
                            r["props"].get("task_id"),
                            r["props"].get("upstream_table"),
                            r["props"].get("upstream_field"),
                            r["props"].get("downstream_table"),
                            r["props"].get("downstream_field"),
                        )
                        if key not in seen_keys:
                            seen_keys.add(key)
                            task_node = next(
                                (
                                    n
                                    for n in self.nodes
                                    if "SDATask" in n["labels"]
                                    and n["props"].get("generation") == gen
                                    and n["props"].get("name")
                                    == r["props"].get("task_id")
                                ),
                                None,
                            )
                            visited_edges.append(
                                {
                                    "edge": dict(r["props"]),
                                    "task_type": task_node["props"].get("type")
                                    if task_node
                                    else None,
                                }
                            )
                        next_frontier.add(r["start"])
                frontier = next_frontier
            return {"success": True, "records": visited_edges}

        if "OPTIONAL MATCH (t)-[:HAS_FIELD]->(f:SDAField" in q:
            records = []
            for t in self.nodes:
                if "SDATable" in t["labels"] and t["props"].get("generation") == gen:
                    fields_for_t = [
                        dict(self._find_node(rel["end"])["props"])
                        for rel in self.rels
                        if rel["type"] == "HAS_FIELD" and rel["start"] == t["id"]
                    ]
                    docs_for_t = [
                        {
                            "ref": self._find_node(rel["end"])["props"]["ref"],
                            "path": self._find_node(rel["end"])["props"]["path"],
                            "category": self._find_node(rel["end"])["props"][
                                "category"
                            ],
                            "generated": bool(
                                self._find_node(rel["end"])["props"].get(
                                    "generated", False
                                )
                            ),
                        }
                        for rel in self.rels
                        if rel["type"] == "HAS_DOCUMENT" and rel["start"] == t["id"]
                    ]
                    records.append(
                        {
                            "table": dict(t["props"]),
                            "fields": fields_for_t,
                            "documents": docs_for_t,
                            "out_rels": [],
                            "in_rels": [],
                        }
                    )
            return {"success": True, "records": records}

        return {"success": True, "records": []}

    def close(self):
        pass


class _InMemoryVectors:
    def __init__(self):
        self.items: dict[str, dict] = {}
        self.deleted_calls: list[list[str]] = []
        self.added_calls: list[list[str]] = []
        self.collection = type("C", (), {"load": lambda self: None})()
        self.client = type("Cl", (), {"disconnect": lambda self: None})()
        self.fail_on_delete = False
        self.fail_on_add = False

    def create_collection(self, *args, **kwargs):
        pass

    def get_collection(self, *args, **kwargs):
        pass

    def add_vectors(self, vectors, ids, metadata):
        if self.fail_on_add:
            raise RuntimeError("Simulated Milvus add failure")
        self.added_calls.append(list(ids))
        for vid, meta in zip(ids, metadata, strict=True):
            self.items[vid] = dict(meta)

    def delete_vectors(self, ids):
        if self.fail_on_delete:
            raise RuntimeError("Simulated Milvus delete failure")
        self.deleted_calls.append(list(ids))
        for vid in ids:
            self.items.pop(vid, None)


def _wire_stores(
    monkeypatch: pytest.MonkeyPatch,
    source_dir: Path,
    graph_store: _InMemoryGraph,
    vector_store: _InMemoryVectors,
    *,
    extra_settings: dict | None = None,
    embed_calls: list[list[str]] | None = None,
):
    from semantica_dataagent import knowledge
    from semantica_dataagent.canonical import CanonicalAdapter
    from semantica_dataagent.markdown import MarkdownAdapter
    from semantica_dataagent.ossie_source import OssieAdapter
    from semantica_dataagent.sql_tasks import SQLTaskParser
    from semantica_dataagent.stage_contract import StorageHandles

    base_settings = {
        "source_directory": str(source_dir),
        "knowledge_generation": "gen_init",
        "neo4j_uri": "bolt://127.0.0.1:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        "milvus_uri": "http://127.0.0.1:19530",
        "embedding_url": "http://127.0.0.1:8000",
        "embedding_model": "test-model",
        "embedding_cache_max_mb": "0",
        **(extra_settings or {}),
    }

    @contextmanager
    def fake_storage(values, stats=None):
        yield StorageHandles(
            values["knowledge_generation"],
            graph_store,
            vector_store,
            values,
            _embedding_dimension=4,
        )

    def fake_embed(values, texts, stats=None):
        if embed_calls is not None:
            embed_calls.append(list(texts))
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    monkeypatch.setattr(
        knowledge, "settings", lambda project_id=None: dict(base_settings)
    )
    monkeypatch.setattr(knowledge, "build_storage", fake_storage)
    monkeypatch.setattr(knowledge, "incremental_storage", fake_storage)
    monkeypatch.setattr(knowledge, "read_graph", lambda values: graph_store)
    monkeypatch.setattr(knowledge, "read_vectors", lambda values: vector_store)
    monkeypatch.setattr(knowledge, "embed", fake_embed)
    monkeypatch.setattr(knowledge, "prune_generations", lambda values: None)
    monkeypatch.setattr(
        knowledge,
        "configured",
        lambda project_id=None, project_type=None: [
            ("source", CanonicalAdapter(), True, {}),
            ("source", MarkdownAdapter(), True, {}),
            ("source", OssieAdapter(), True, {}),
            ("parser", SQLTaskParser(), True, {}),
        ],
    )
    return base_settings


def _run_build(base_settings: dict, build_type: str):
    from semantica_dataagent import knowledge, sql_validation
    from semantica_dataagent.db import Build, GenerationPointer, sessions

    with sessions().begin() as db:
        row = Build(
            project_id="default",
            build_type=build_type,
            status="running",
            started_at="2026-11-23T00:00:00Z",
            counts="{}",
        )
        db.add(row)
        db.flush()
        bid = row.id
    knowledge.run_build(bid)
    sql_validation._snapshot_cache = None
    with sessions()() as db:
        row = db.get(Build, bid)
        pointer = db.get(GenerationPointer, 1)
        if pointer is not None:
            base_settings["knowledge_generation"] = pointer.generation
        return {
            "id": row.id,
            "status": row.status,
            "build_type": row.build_type,
            "fallback_reason": row.fallback_reason,
            "generation": row.generation,
            "error": row.error,
            "counts": json.loads(row.counts),
            "entity_hashes": json.loads(row.entity_hashes)
            if row.entity_hashes
            else None,
            "stale_overrides": json.loads(row.stale_overrides or "[]"),
            "logs": json.loads(row.logs or "[]"),
        }


def test_entity_incremental_2_of_20_tables_in_single_ossie_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _setup_db(tmp_path, monkeypatch)
    source = tmp_path / "source"
    ossie_dir = source / "ossie" / "inst_a"
    ossie_dir.mkdir(parents=True)

    specs = [("shop", f"t{i:02d}", f"表{i:02d}说明") for i in range(1, 21)]
    model_path = ossie_dir / "warehouse.yaml"
    model_path.write_text(
        yaml.safe_dump(_make_ossie_model("inst_a_model", specs), allow_unicode=True),
        encoding="utf-8",
    )

    graph_store = _InMemoryGraph()
    vector_store = _InMemoryVectors()
    embed_calls: list[list[str]] = []
    base_settings = _wire_stores(
        monkeypatch, source, graph_store, vector_store, embed_calls=embed_calls
    )

    full = _run_build(base_settings, "full")
    assert full["status"] == "succeeded", full["error"]
    assert full["counts"]["table"] == 20
    assert full["counts"]["field"] == 40
    assert full["entity_hashes"] is not None
    assert sum(1 for k in full["entity_hashes"] if k.startswith("table:")) == 20

    # Edit ONLY 2 of the 20 tables inside the single Ossie YAML
    embed_calls.clear()
    graph_store.queries.clear()
    vector_store.added_calls.clear()
    vector_store.deleted_calls.clear()

    edited_specs = [
        (db, name, f"{desc}已更新" if name in {"t01", "t02"} else desc)
        for db, name, desc in specs
    ]
    model_path.write_text(
        yaml.safe_dump(
            _make_ossie_model("inst_a_model", edited_specs), allow_unicode=True
        ),
        encoding="utf-8",
    )

    inc = _run_build(base_settings, "incremental")
    assert inc["status"] == "succeeded", inc["error"]
    assert inc["build_type"] == "incremental"
    assert inc["fallback_reason"] is None
    assert inc["counts"]["table"] == 2
    assert inc["counts"]["field"] == 4

    # Only the 2 changed tables + their 4 fields (plus affected derived doc) were deleted & re-embedded
    embedded_texts = [t for batch in embed_calls for t in batch]
    assert "t01 表01说明已更新" in embedded_texts
    assert "t02 表02说明已更新" in embedded_texts
    for i in range(3, 21):
        assert f"t{i:02d} 表{i:02d}说明" not in embedded_texts

    added_ids = {vid for batch in vector_store.added_calls for vid in batch}
    deleted_ids = {vid for batch in vector_store.deleted_calls for vid in batch}
    expected_table_and_field_ids = {
        "table:shop.t01",
        "table:shop.t02",
        "field:shop.t01.id",
        "field:shop.t01.amount",
        "field:shop.t02.id",
        "field:shop.t02.amount",
    }
    assert expected_table_and_field_ids <= added_ids
    assert expected_table_and_field_ids <= deleted_ids
    for i in range(3, 21):
        assert f"table:shop.t{i:02d}" not in added_ids
        assert f"table:shop.t{i:02d}" not in deleted_ids
        assert f"field:shop.t{i:02d}.id" not in added_ids
        assert f"field:shop.t{i:02d}.id" not in deleted_ids

    # Untouched 18 tables in Neo4j kept their original full-build build_id
    gen = base_settings["knowledge_generation"]
    table_nodes = {
        n["props"]["name"]: n["props"]["build_id"]
        for n in graph_store.nodes
        if "SDATable" in n["labels"] and n["props"].get("generation") == gen
    }
    assert len(table_nodes) == 20
    assert table_nodes["t01"] == inc["id"]
    assert table_nodes["t02"] == inc["id"]
    for i in range(3, 21):
        assert table_nodes[f"t{i:02d}"] == full["id"]

    # Local incremental reconcile scoped SDAField and SDADimensionValue queries ONLY to t01 and t02
    field_reconcile_queries = [
        params
        for q, params in graph_store.queries
        if "RETURN elementId(n) AS id" in q and ":SDAField" in q
    ]
    assert len(field_reconcile_queries) == 1
    assert {t["table"] for t in field_reconcile_queries[0]["tables"]} == {"t01", "t02"}

    dim_reconcile_queries = [
        params
        for q, params in graph_store.queries
        if "RETURN elementId(n) AS id" in q and ":SDADimensionValue" in q
    ]
    assert len(dim_reconcile_queries) == 1
    assert {t["table"] for t in dim_reconcile_queries[0]["tables"]} == {"t01", "t02"}

    doc_reconcile_queries = [
        params
        for q, params in graph_store.queries
        if "RETURN elementId(n) AS id" in q and ":SDADocument" in q
    ]
    assert len(doc_reconcile_queries) == 1
    assert doc_reconcile_queries[0]["doc_refs"] == ["document:ossie/inst_a_model.md"]
    # 18 untouched table hashes stayed identical
    unchanged_keys = [f"table:shop.t{i:02d}" for i in range(3, 21)]
    for key in unchanged_keys:
        assert inc["entity_hashes"][key] == full["entity_hashes"][key]
    assert (
        inc["entity_hashes"]["table:shop.t01"]
        != full["entity_hashes"]["table:shop.t01"]
    )
    assert (
        inc["entity_hashes"]["table:shop.t02"]
        != full["entity_hashes"]["table:shop.t02"]
    )


def test_pulls_tasks_json_alongside_ossie_populates_tasks_and_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from semantica_dataagent.task_api import get_lineage, read_task_sql

    _setup_db(tmp_path, monkeypatch)
    source = tmp_path / "source"
    (source / "ossie" / "inst_a").mkdir(parents=True)
    (source / "pulls" / "inst_a").mkdir(parents=True)

    (source / "ossie" / "inst_a" / "model.yaml").write_text(
        yaml.safe_dump(
            _make_ossie_model(
                "orders_pipeline",
                [
                    ("raw", "orders", "原始订单"),
                    ("dwd", "orders_clean", "清洗订单"),
                    ("dm", "orders_agg", "汇总订单"),
                ],
            ),
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (source / "pulls" / "inst_a" / "tasks.json").write_text(
        json.dumps(
            [
                {
                    "name": "etl_clean",
                    "type": "hive",
                    "sql": "INSERT OVERWRITE TABLE dwd.orders_clean SELECT amount, id FROM raw.orders",
                    "description": "清洗订单任务",
                },
                {
                    "name": "etl_agg",
                    "type": "hive",
                    "sql": "INSERT OVERWRITE TABLE dm.orders_agg SELECT amount, id FROM dwd.orders_clean",
                    "description": "汇总订单任务",
                },
            ]
        ),
        encoding="utf-8",
    )

    graph_store = _InMemoryGraph()
    vector_store = _InMemoryVectors()
    base_settings = _wire_stores(monkeypatch, source, graph_store, vector_store)

    full = _run_build(base_settings, "full")
    assert full["status"] == "succeeded", full["error"]
    assert "task:etl_clean" in full["entity_hashes"]
    assert "task:etl_agg" in full["entity_hashes"]

    task_info = read_task_sql("etl_clean")
    assert "INSERT OVERWRITE TABLE dwd.orders_clean" in task_info["sql"]

    up_lineage = get_lineage("dm.orders_agg", direction="up", depth=2)
    edges = {(item["upstream"], item["downstream"]) for item in up_lineage}
    assert edges == {
        ("dwd.orders_clean", "dm.orders_agg"),
        ("raw.orders", "dwd.orders_clean"),
    }

    col_lineage = get_lineage("dm.orders_agg", direction="up", depth=2, column="amount")
    assert any(
        edge["upstream_table"] == "raw.orders" and edge["upstream_field"] == "amount"
        for edge in col_lineage
    )

    # Incremental update of only etl_agg preserves etl_clean build_id
    (source / "pulls" / "inst_a" / "tasks.json").write_text(
        json.dumps(
            [
                {
                    "name": "etl_clean",
                    "type": "hive",
                    "sql": "INSERT OVERWRITE TABLE dwd.orders_clean SELECT amount, id FROM raw.orders",
                    "description": "清洗订单任务",
                },
                {
                    "name": "etl_agg",
                    "type": "hive",
                    "sql": "INSERT OVERWRITE TABLE dm.orders_agg SELECT amount, id FROM dwd.orders_clean",
                    "description": "汇总订单任务v2",
                },
            ]
        ),
        encoding="utf-8",
    )
    graph_store.queries.clear()
    inc = _run_build(base_settings, "incremental")
    assert inc["status"] == "succeeded", inc["error"]
    assert inc["counts"]["task"] == 1
    assert read_task_sql("etl_clean")["build_id"] == full["id"]
    assert read_task_sql("etl_agg")["build_id"] == inc["id"]
    assert not any("RETURN elementId(n) AS id" in q for q, _ in graph_store.queries)

    # Incremental deletion of etl_agg removes its table & field lineage while preserving etl_clean
    (source / "pulls" / "inst_a" / "tasks.json").write_text(
        json.dumps(
            [
                {
                    "name": "etl_clean",
                    "type": "hive",
                    "sql": "INSERT OVERWRITE TABLE dwd.orders_clean SELECT amount, id FROM raw.orders",
                    "description": "清洗订单任务",
                }
            ]
        ),
        encoding="utf-8",
    )
    inc_del = _run_build(base_settings, "incremental")
    assert inc_del["status"] == "succeeded", inc_del["error"]
    assert inc_del["counts"]["task"] == 0
    assert get_lineage("dm.orders_agg", direction="up", depth=2) == []
    assert get_lineage("dm.orders_agg", direction="up", depth=2, column="amount") == []
    remaining_edges = {
        (item["upstream"], item["downstream"])
        for item in get_lineage("dwd.orders_clean", direction="up", depth=1)
    }
    assert remaining_edges == {("raw.orders", "dwd.orders_clean")}
    assert any(
        edge["upstream_table"] == "raw.orders" and edge["upstream_field"] == "amount"
        for edge in get_lineage(
            "dwd.orders_clean", direction="up", depth=1, column="amount"
        )
    )
    assert "task:etl_agg" not in vector_store.items
    assert "task:etl_clean" in vector_store.items


def test_embedding_failure_during_incremental_leaves_live_generation_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from semantica_dataagent import knowledge

    _setup_db(tmp_path, monkeypatch)
    source = tmp_path / "source"
    (source / "ossie" / "inst_a").mkdir(parents=True)
    model_path = source / "ossie" / "inst_a" / "model.yaml"
    specs = [("shop", f"t{i:02d}", f"表{i:02d}") for i in range(1, 6)]
    model_path.write_text(
        yaml.safe_dump(_make_ossie_model("m", specs), allow_unicode=True),
        encoding="utf-8",
    )

    graph_store = _InMemoryGraph()
    vector_store = _InMemoryVectors()
    base_settings = _wire_stores(monkeypatch, source, graph_store, vector_store)

    full = _run_build(base_settings, "full")
    assert full["status"] == "succeeded", full["error"]

    nodes_before = copy.deepcopy(graph_store.nodes)
    rels_before = copy.deepcopy(graph_store.rels)
    vectors_before = copy.deepcopy(vector_store.items)
    gen_before = base_settings["knowledge_generation"]

    # Edit 1 table and fail embedding
    model_path.write_text(
        yaml.safe_dump(
            _make_ossie_model("m", [("shop", "t01", "表01故障"), *specs[1:]]),
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    graph_store.queries.clear()
    vector_store.deleted_calls.clear()
    vector_store.added_calls.clear()

    def failing_embed(values, texts, stats=None):
        raise RuntimeError("Embedding gateway down")

    monkeypatch.setattr(knowledge, "embed", failing_embed)
    failed = _run_build(base_settings, "incremental")
    assert failed["status"] == "failed"
    assert "Embedding gateway down" in (failed["error"] or "")

    # Zero Neo4j deletes, zero Milvus deletes, 100% intact live generation
    assert not any("DETACH DELETE" in q for q, _ in graph_store.queries)
    assert vector_store.deleted_calls == []
    assert vector_store.added_calls == []
    assert graph_store.nodes == nodes_before
    assert graph_store.rels == rels_before
    assert vector_store.items == vectors_before
    assert base_settings["knowledge_generation"] == gen_before


def test_threshold_exceeded_and_missing_setting_default_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _setup_db(tmp_path, monkeypatch)
    source = tmp_path / "source"
    (source / "ossie" / "inst_a").mkdir(parents=True)
    model_path = source / "ossie" / "inst_a" / "model.yaml"
    specs = [("shop", f"t{i:02d}", f"表{i:02d}") for i in range(1, 11)]
    model_path.write_text(
        yaml.safe_dump(_make_ossie_model("m10", specs), allow_unicode=True),
        encoding="utf-8",
    )

    graph_store = _InMemoryGraph()
    vector_store = _InMemoryVectors()
    # Explicitly do NOT set incremental_fallback_threshold -> tests default 0.20 guard
    monkeypatch.delenv("DATAAGENT_INCREMENTAL_FALLBACK_THRESHOLD", raising=False)
    monkeypatch.delenv("INCREMENTAL_FALLBACK_THRESHOLD", raising=False)
    base_settings = _wire_stores(monkeypatch, source, graph_store, vector_store)

    full = _run_build(base_settings, "full")
    assert full["status"] == "succeeded", full["error"]
    gen_1 = base_settings["knowledge_generation"]

    # Edit 3 of 10 tables (30% > default 20%) -> triggers full new-generation switch
    edited = [
        (db, name, f"{desc}_v2" if name in {"t01", "t02", "t03"} else desc)
        for db, name, desc in specs
    ]
    model_path.write_text(
        yaml.safe_dump(_make_ossie_model("m10", edited), allow_unicode=True),
        encoding="utf-8",
    )

    fallback = _run_build(base_settings, "incremental")
    assert fallback["status"] == "succeeded", fallback["error"]
    assert fallback["build_type"] == "full"
    assert fallback["fallback_reason"] == "threshold_exceeded"
    gen_2 = base_settings["knowledge_generation"]
    assert gen_2 != gen_1
    assert fallback["generation"] == gen_2
    gen2_tables = [
        n
        for n in graph_store.nodes
        if "SDATable" in n["labels"] and n["props"].get("generation") == gen_2
    ]
    assert len(gen2_tables) == 10

    # Explicit threshold 0.50 keeps 3/10 (30%) as incremental
    base_settings["incremental_fallback_threshold"] = "0.50"
    edited_2 = [
        (db, name, f"{desc}_v3" if name in {"t01", "t02", "t03"} else desc)
        for db, name, desc in specs
    ]
    model_path.write_text(
        yaml.safe_dump(_make_ossie_model("m10", edited_2), allow_unicode=True),
        encoding="utf-8",
    )
    inc = _run_build(base_settings, "incremental")
    assert inc["status"] == "succeeded", inc["error"]
    assert inc["build_type"] == "incremental"
    assert inc["fallback_reason"] is None
    assert inc["counts"]["table"] == 3


def test_incremental_storage_failure_triggers_full_new_generation_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _setup_db(tmp_path, monkeypatch)
    source = tmp_path / "source"
    (source / "ossie" / "inst_a").mkdir(parents=True)
    model_path = source / "ossie" / "inst_a" / "model.yaml"
    specs = [("shop", f"t{i:02d}", f"表{i:02d}") for i in range(1, 6)]
    model_path.write_text(
        yaml.safe_dump(_make_ossie_model("m5", specs), allow_unicode=True),
        encoding="utf-8",
    )

    graph_store = _InMemoryGraph()
    vector_store = _InMemoryVectors()
    base_settings = _wire_stores(monkeypatch, source, graph_store, vector_store)

    full = _run_build(base_settings, "full")
    assert full["status"] == "succeeded", full["error"]
    gen_1 = base_settings["knowledge_generation"]

    # Edit 1 of 5 tables (20% <= 20%) so it enters incremental storage mutation
    model_path.write_text(
        yaml.safe_dump(
            _make_ossie_model("m5", [("shop", "t01", "表01更新"), *specs[1:]]),
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    # Fail Milvus delete during incremental mutation -> fallback full build uses build_storage (no delete_vectors)
    vector_store.fail_on_delete = True
    fallback = _run_build(base_settings, "incremental")
    vector_store.fail_on_delete = False

    assert fallback["status"] == "succeeded", fallback["error"]
    assert fallback["build_type"] == "full"
    assert fallback["fallback_reason"] == "incremental_failed"
    gen_2 = base_settings["knowledge_generation"]
    assert gen_2 != gen_1
    gen2_tables = {
        n["props"]["name"]
        for n in graph_store.nodes
        if "SDATable" in n["labels"] and n["props"].get("generation") == gen_2
    }
    assert gen2_tables == {"t01", "t02", "t03", "t04", "t05"}


def test_no_previous_full_config_changed_and_previous_full_failed_auto_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from semantica_dataagent.db import Build, sessions

    _setup_db(tmp_path, monkeypatch)
    source = tmp_path / "source"
    (source / "ossie" / "inst_a").mkdir(parents=True)
    (source / "ossie" / "inst_a" / "model.yaml").write_text(
        yaml.safe_dump(
            _make_ossie_model("m1", [("shop", "orders", "订单")]), allow_unicode=True
        ),
        encoding="utf-8",
    )
    graph_store = _InMemoryGraph()
    vector_store = _InMemoryVectors()
    base_settings = _wire_stores(monkeypatch, source, graph_store, vector_store)

    # 1. Incremental requested with zero prior builds -> no_previous_full
    first = _run_build(base_settings, "incremental")
    assert first["status"] == "succeeded", first["error"]
    assert first["build_type"] == "full"
    assert first["fallback_reason"] == "no_previous_full"

    # 2. Change config -> config_changed
    base_settings["dimension_value_limit"] = "42"
    second = _run_build(base_settings, "incremental")
    assert second["status"] == "succeeded", second["error"]
    assert second["build_type"] == "full"
    assert second["fallback_reason"] == "config_changed"
    assert second["generation"] != first["generation"]

    # 3. Failed full build on current generation -> previous_full_failed
    with sessions().begin() as db:
        db.add(
            Build(
                project_id="default",
                build_type="full",
                status="failed",
                generation=base_settings["knowledge_generation"],
                started_at="2026-11-23T00:01:00Z",
                counts="{}",
                affected_files=json.dumps({"full_started": True}),
            )
        )
    third = _run_build(base_settings, "incremental")
    assert third["status"] == "succeeded", third["error"]
    assert third["build_type"] == "full"
    assert third["fallback_reason"] == "previous_full_failed"
    assert third["generation"] != second["generation"]


def test_cross_instance_duplicate_physical_table_and_task_fast_fail(tmp_path: Path):
    from semantica_dataagent import knowledge
    from semantica_dataagent.canonical import CanonicalAdapter
    from semantica_dataagent.ossie_source import OssieAdapter

    # 1. Cross-puller Ossie physical table collision -> fast fail naming both instances & table
    src1 = tmp_path / "src_ossie"
    (src1 / "ossie" / "inst_a").mkdir(parents=True)
    (src1 / "ossie" / "inst_b").mkdir(parents=True)
    (src1 / "ossie" / "inst_a" / "m.yaml").write_text(
        yaml.safe_dump(
            _make_ossie_model("model_a", [("shop", "orders", "A订单")]),
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (src1 / "ossie" / "inst_b" / "m.yaml").write_text(
        yaml.safe_dump(
            _make_ossie_model("model_b", [("shop", "orders", "B订单")]),
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as exc_info:
        OssieAdapter().read(src1, {})
    msg = str(exc_info.value)
    assert "inst_a" in msg and "inst_b" in msg and "shop.orders" in msg

    # Same-instance multi-file merge still succeeds
    src_same = tmp_path / "src_same_inst"
    (src_same / "ossie" / "inst_a").mkdir(parents=True)
    (src_same / "ossie" / "inst_a" / "m1.yaml").write_text(
        yaml.safe_dump(
            _make_ossie_model("model_a1", [("shop", "orders", "订单1")]),
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (src_same / "ossie" / "inst_a" / "m2.yaml").write_text(
        yaml.safe_dump(
            _make_ossie_model("model_a2", [("shop", "orders", "订单2")]),
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    merged = OssieAdapter().read(src_same, {})
    assert len([r for r in merged.records if r.kind == "table"]) == 1

    # 2. Cross-instance duplicate task in pulls/<inst>/tasks.json -> fast fail naming both instances
    src2 = tmp_path / "src_tasks"
    (src2 / "pulls" / "inst_alpha").mkdir(parents=True)
    (src2 / "pulls" / "inst_beta").mkdir(parents=True)
    task_payload = [{"name": "daily_orders", "type": "hive", "sql": "SELECT 1"}]
    (src2 / "pulls" / "inst_alpha" / "tasks.json").write_text(
        json.dumps(task_payload), encoding="utf-8"
    )
    (src2 / "pulls" / "inst_beta" / "tasks.json").write_text(
        json.dumps(task_payload), encoding="utf-8"
    )
    read_res = CanonicalAdapter().read(src2, {})
    with pytest.raises(ValueError) as task_exc:
        knowledge.validate_records(read_res.records)
    task_msg = str(task_exc.value)
    assert "Duplicate task reference" in task_msg
    assert "daily_orders" in task_msg
    assert "inst_alpha" in task_msg and "inst_beta" in task_msg


def test_upstream_table_deletion_skips_stale_override_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from semantica_dataagent import knowledge

    _setup_db(tmp_path, monkeypatch)
    source = tmp_path / "source"
    (source / "ossie" / "inst_a").mkdir(parents=True)
    (source / "overrides").mkdir(parents=True)
    model_path = source / "ossie" / "inst_a" / "model.yaml"
    model_path.write_text(
        yaml.safe_dump(
            _make_ossie_model(
                "shop_model",
                [("shop", "orders", "订单表"), ("shop", "users", "用户表")],
            ),
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (source / "overrides" / "semantic_overrides.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "tables": {
                    "shop.orders": {
                        "database": "shop",
                        "name": "orders",
                        "comment": "审定订单注释",
                    },
                    "shop.users": {
                        "database": "shop",
                        "name": "users",
                        "comment": "审定用户注释",
                    },
                },
                "fields": {
                    "shop.users.amount": {
                        "database": "shop",
                        "table": "users",
                        "name": "amount",
                        "comment": "用户金额",
                    }
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    graph_store = _InMemoryGraph()
    vector_store = _InMemoryVectors()
    base_settings = _wire_stores(monkeypatch, source, graph_store, vector_store)

    full = _run_build(base_settings, "full")
    assert full["status"] == "succeeded", full["error"]
    assert full["stale_overrides"] == []

    # Upstream deletes shop.users
    model_path.write_text(
        yaml.safe_dump(
            _make_ossie_model("shop_model", [("shop", "orders", "订单表")]),
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    inc = _run_build(base_settings, "incremental")
    assert inc["status"] == "succeeded", inc["error"]
    stale_keys = {item["key"] for item in inc["stale_overrides"]}
    assert {"shop.users", "shop.users.amount"} <= stale_keys
    assert inc["counts"]["stale_overrides"] == len(inc["stale_overrides"])
    assert any(entry["stage"] == "overrides" for entry in inc["logs"])

    described = knowledge.describe()
    assert [t["name"] for t in described] == ["orders"]
    assert described[0]["comment"] == "审定订单注释"

    gen = base_settings["knowledge_generation"]
    users_fields = [
        n
        for n in graph_store.nodes
        if "SDAField" in n["labels"]
        and n["props"].get("generation") == gen
        and n["props"].get("database") == "shop"
        and n["props"].get("table") == "users"
    ]
    assert users_fields == []
    assert "table:shop.users" not in vector_store.items
    assert not any(k.startswith("field:shop.users.") for k in vector_store.items)


def test_compute_entity_hashes_and_legacy_build_without_entity_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from semantica_dataagent import knowledge
    from semantica_dataagent.canonical import Record
    from semantica_dataagent.db import Build, sessions

    # Unit check for compute_entity_hashes across canonical kinds and field ordering
    r1 = [
        Record(
            "table", {"database": "shop", "name": "orders", "comment": "x"}, "a.yaml"
        ),
        Record(
            "field",
            {
                "database": "shop",
                "table": "orders",
                "name": "b",
                "type": "int",
                "comment": "",
                "partition": False,
            },
            "a.yaml",
        ),
        Record(
            "field",
            {
                "database": "shop",
                "table": "orders",
                "name": "a",
                "type": "int",
                "comment": "",
                "partition": False,
            },
            "a.yaml",
        ),
        Record("task", {"name": "t1", "type": "hive", "sql": "SELECT 1"}, "tasks.json"),
        Record(
            "dimension_value",
            {"database": "shop", "table": "orders", "field": "a", "value": "1"},
            "dim.json",
        ),
        Record(
            "document",
            {"category": "rules", "path": "doc.md", "content": "# hi"},
            "knowledge/rules/doc.md",
        ),
        Record(
            "relationship",
            {
                "name": "fk",
                "from_database": "shop",
                "from_table": "orders",
                "from_columns": ["a"],
                "to_database": "shop",
                "to_table": "orders",
                "to_columns": ["b"],
            },
            "rels.json",
        ),
        Record(
            "lineage",
            {
                "task_id": "t1",
                "upstream": "raw.a",
                "downstream": "shop.orders",
                "evidence": "declared",
                "parse_status": "success",
            },
            "lin.json",
        ),
    ]
    r2 = [r1[0], r1[2], r1[1], *r1[3:]]  # swapped field order
    h1 = knowledge.compute_entity_hashes(r1)
    h2 = knowledge.compute_entity_hashes(r2)
    assert h1 == h2
    assert {
        "table:shop.orders",
        "task:t1",
        "dim_table:shop.orders",
        "document:rules/doc.md",
        "relationship:shop.orders:shop.orders:fk",
        "lineage:t1:raw.a:shop.orders",
    } <= set(h1)

    # Graceful legacy build fallback when previous.entity_hashes is None
    _setup_db(tmp_path, monkeypatch)
    source = tmp_path / "source"
    (source / "ossie" / "inst_a").mkdir(parents=True)
    model_path = source / "ossie" / "inst_a" / "model.yaml"
    model_path.write_text(
        yaml.safe_dump(
            _make_ossie_model("legacy_m", [("shop", "orders", "订单v1")]),
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    graph_store = _InMemoryGraph()
    vector_store = _InMemoryVectors()
    base_settings = _wire_stores(monkeypatch, source, graph_store, vector_store)

    full = _run_build(base_settings, "full")
    assert full["status"] == "succeeded"
    # Wipe entity_hashes on the prior full build to simulate a pre-M4 legacy build row
    with sessions().begin() as db:
        db.get(Build, full["id"]).entity_hashes = None

    model_path.write_text(
        yaml.safe_dump(
            _make_ossie_model("legacy_m", [("shop", "orders", "订单v2")]),
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    inc = _run_build(base_settings, "incremental")
    assert inc["status"] == "succeeded", inc["error"]
    assert inc["build_type"] == "incremental"
    assert inc["entity_hashes"] is not None


def test_incremental_dimension_delete_keeps_other_catalog(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    (source / "tables.json").write_text(
        json.dumps([{"database": "shop", "name": "orders", "comment": "orders"}])
    )
    dimensions = [
        {
            "catalog": c,
            "database": "shop",
            "table": "orders",
            "field": "code",
            "value": c,
        }
        for c in ("hive", "internal")
    ]
    path = source / "dimension_values.json"
    path.write_text(json.dumps(dimensions))
    graph, vectors = _InMemoryGraph(), _InMemoryVectors()
    settings = _wire_stores(monkeypatch, source, graph, vectors)
    full = _run_build(settings, "full")
    assert full["status"] == "succeeded", full
    for idx, row in enumerate(dimensions, 10000):
        graph.nodes.append(
            {
                "id": idx,
                "labels": {"SDAEntity", "SDADimensionValue"},
                "props": {
                    **row,
                    "generation": settings["knowledge_generation"],
                    "ref": "dimension_value:" + row["catalog"],
                    "build_id": full["id"],
                },
            }
        )
    dimensions[0]["value"] = "changed"
    path.write_text(json.dumps(dimensions))
    result = _run_build(settings, "incremental")
    assert result["status"] == "succeeded" and result["build_type"] == "incremental", (
        result
    )
    assert any(
        n["props"].get("ref") == "dimension_value:internal"
        and n["props"].get("build_id") == full["id"]
        for n in graph.nodes
    )
