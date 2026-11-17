"""Tests for deterministic Apache Ossie export, REST endpoint, CLI, and round-trip (#270)."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import Fernet
from jsonschema import Draft202012Validator

WORKTREE_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = WORKTREE_ROOT / "schemas" / "ossie" / "ossie-schema.json"


def _schema_validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def _make_fake_stores():
    class FakeGraph:
        def __init__(self):
            self.nodes: list[dict] = []
            self.rels: list[dict] = []
            self._next_id = 1

        def _find_node(self, node_id: int) -> dict:
            return next(n for n in self.nodes if n["id"] == node_id)

        def _matches_files(self, props: dict, params: dict) -> bool:
            files = set(params.get("files") or ())
            selected_refs = set(params.get("selected_refs") or ())
            return (
                props.get("source_file") in files
                or props.get("source_key") in files
                or bool(files.intersection(props.get("source_files") or ()))
                or props.get("ref") in selected_refs
                or props.get("doc_id") in selected_refs
            )

        def execute_query(self, query: str, params: dict):
            q = " ".join(query.split())
            if q.startswith(("CREATE INDEX", "CREATE FULLTEXT INDEX", "CALL db.await")):
                return {"success": True, "records": []}
            gen = params.get("generation")

            if (
                "MATCH (t:SDATable {generation:$generation})-[:HAS_DOCUMENT]->(d:SDADocument {generation:$generation})"
                in q
            ):
                database = params.get("database")
                rows = []
                for rel in self.rels:
                    if rel["type"] != "HAS_DOCUMENT":
                        continue
                    t = self._find_node(rel["start"])
                    d = self._find_node(rel["end"])
                    if (
                        "SDATable" not in t["labels"]
                        or "SDADocument" not in d["labels"]
                    ):
                        continue
                    tp, dp = t["props"], d["props"]
                    if tp.get("generation") != gen or dp.get("generation") != gen:
                        continue
                    if database is not None and tp.get("database") != database:
                        continue
                    if dp.get("is_index") is True or dp.get("path") == "index.md":
                        continue
                    rows.append({"table_ref": tp["ref"], "document": dict(dp)})
                rows.sort(
                    key=lambda row: (row["table_ref"], row["document"].get("ref") or "")
                )
                return {"success": True, "records": rows}

            if (
                "MATCH (a:SDATable {generation:$generation})-[r:RELATES_TO]->(b:SDATable {generation:$generation})"
                in q
                and "RETURN a.ref AS from_ref" in q
            ):
                database = params.get("database")
                rows = []
                for rel in self.rels:
                    if rel["type"] != "RELATES_TO":
                        continue
                    a = self._find_node(rel["start"])
                    b = self._find_node(rel["end"])
                    if "SDATable" not in a["labels"] or "SDATable" not in b["labels"]:
                        continue
                    ap, bp = a["props"], b["props"]
                    if ap.get("generation") != gen or bp.get("generation") != gen:
                        continue
                    if database is not None and (
                        ap.get("database") != database or bp.get("database") != database
                    ):
                        continue
                    rows.append(
                        {
                            "from_ref": ap["ref"],
                            "from_catalog": ap.get("catalog") or "",
                            "from_database": ap["database"],
                            "from_name": ap["name"],
                            "to_ref": bp["ref"],
                            "to_catalog": bp.get("catalog") or "",
                            "to_database": bp["database"],
                            "to_name": bp["name"],
                            "relationship": dict(rel["props"]),
                        }
                    )
                return {"success": True, "records": rows}

            if (
                "MATCH (n:SDAEntity {generation:$generation}) WHERE n.source_file IN $files"
                in q
                and "RETURN n.ref AS ref" in q
            ):
                refs = [
                    {"ref": n["props"]["ref"]}
                    for n in self.nodes
                    if "SDAEntity" in n["labels"]
                    and n["props"].get("generation") == gen
                    and self._matches_files(n["props"], params)
                    and n["props"].get("ref")
                ]
                return {"success": True, "records": refs}

            if (
                "MATCH (:SDATable {generation:$generation})-[r:RELATES_TO]->(:SDATable {generation:$generation}) WHERE NOT r.ref IN $refs"
                in q
            ):
                refs = set(params["refs"])
                keep, deleted = [], 0
                for rel in self.rels:
                    if rel["type"] == "RELATES_TO":
                        s, e = (
                            self._find_node(rel["start"]),
                            self._find_node(rel["end"]),
                        )
                        if (
                            s["props"].get("generation") == gen
                            and e["props"].get("generation") == gen
                            and rel["props"].get("ref") not in refs
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

            if "RETURN count(*) AS deleted" in q:
                return {"success": True, "records": [{"deleted": 0}]}

            if "CREATE (n:SDAEntity:SDATable) SET n = row" in q:
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

            if (
                "MERGE (c:SDAEntity:SDACategory {generation:$generation, name:row.name})"
                in q
            ):
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
                        existing["props"].update(
                            {
                                "source_file": row["source_file"],
                                "source_key": row["source_key"],
                                "source_files": row["source_files"],
                                "build_id": params["build_id"],
                            }
                        )
                return {"success": True, "records": []}

            if "RETURN elementId(n) AS id" in q:
                label = (
                    "SDAField"
                    if ":SDAField" in q
                    else "SDADocument"
                    if ":SDADocument" in q
                    else "SDADimensionValue"
                )
                return {
                    "success": True,
                    "records": [
                        {"id": n["id"]}
                        for n in self.nodes
                        if label in n["labels"] and n["props"].get("generation") == gen
                    ],
                }

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

            if "MERGE (a)-[r:RELATES_TO {ref: row.props.ref}]->(b)" in q:
                for row in params["rows"]:
                    a = next(
                        n
                        for n in self.nodes
                        if "SDATable" in n["labels"]
                        and n["props"].get("generation") == gen
                        and n["props"].get("database") == row["from_database"]
                        and n["props"].get("name") == row["from_table_name"]
                        and (n["props"].get("catalog") or "") == row["from_catalog"]
                    )
                    b = next(
                        n
                        for n in self.nodes
                        if "SDATable" in n["labels"]
                        and n["props"].get("generation") == gen
                        and n["props"].get("database") == row["to_database"]
                        and n["props"].get("name") == row["to_table_name"]
                        and (n["props"].get("catalog") or "") == row["to_catalog"]
                    )
                    existing = next(
                        (
                            r
                            for r in self.rels
                            if r["type"] == "RELATES_TO"
                            and r["start"] == a["id"]
                            and r["end"] == b["id"]
                            and r["props"].get("ref") == row["props"]["ref"]
                        ),
                        None,
                    )
                    if existing is None:
                        self.rels.append(
                            {
                                "type": "RELATES_TO",
                                "start": a["id"],
                                "end": b["id"],
                                "props": dict(row["props"]),
                            }
                        )
                    else:
                        existing["props"] = dict(row["props"])
                return {"success": True, "records": []}

            if "OPTIONAL MATCH (t)-[:HAS_FIELD]->(f:SDAField" in q:
                records = []
                names = params.get("names")
                catalog_names = set(params.get("catalog_names") or ())
                database = params.get("database")
                search = params.get("search")
                for t in self.nodes:
                    if not (
                        "SDATable" in t["labels"]
                        and t["props"].get("generation") == gen
                    ):
                        continue
                    tp = t["props"]
                    full_cat = (
                        f"{tp['catalog']}.{tp['database']}.{tp['name']}".lower()
                        if tp.get("catalog")
                        else ""
                    )
                    if names is not None and not (
                        tp["name"] in names
                        or f"{tp['database']}.{tp['name']}" in names
                        or (full_cat and full_cat in catalog_names)
                    ):
                        continue
                    if database is not None and tp["database"] != database:
                        continue
                    if search is not None and search.lower() not in tp["name"].lower():
                        continue
                    fields_for_t = [
                        dict(self._find_node(r["end"])["props"])
                        for r in self.rels
                        if r["type"] == "HAS_FIELD" and r["start"] == t["id"]
                    ]
                    docs_for_t = [
                        {
                            "ref": self._find_node(r["end"])["props"]["ref"],
                            "path": self._find_node(r["end"])["props"]["path"],
                            "category": self._find_node(r["end"])["props"]["category"],
                        }
                        for r in self.rels
                        if r["type"] == "HAS_DOCUMENT" and r["start"] == t["id"]
                    ]
                    if "count(f) AS field_count" in q:
                        records.append(
                            {
                                "table": dict(tp),
                                "field_count": len(fields_for_t),
                                "document_count": len(docs_for_t),
                                "has_partition": any(
                                    f.get("partition") is True for f in fields_for_t
                                ),
                            }
                        )
                    else:
                        records.append(
                            {
                                "table": dict(tp),
                                "fields": fields_for_t,
                                "documents": docs_for_t,
                            }
                        )
                records.sort(
                    key=lambda r: (
                        r["table"].get("catalog") or "",
                        r["table"]["database"],
                        r["table"]["name"],
                    )
                )
                return {"success": True, "records": records}

            if "-[r:RELATES_TO]->(:SDATable" in q:
                records = []
                for rel in self.rels:
                    if rel["type"] == "RELATES_TO":
                        s, e = (
                            self._find_node(rel["start"]),
                            self._find_node(rel["end"]),
                        )
                        if (
                            s["props"].get("generation") == gen
                            and e["props"].get("generation") == gen
                        ):
                            records.append({"relationship": dict(rel["props"])})
                return {"success": True, "records": records}

            if "MATCH (c:SDAChunk {generation:$generation})" in q:
                pairs = params.get("pairs") or []
                return {
                    "success": True,
                    "records": [
                        {"ref": n["props"]["ref"], "content": n["props"]["content"]}
                        for n in self.nodes
                        if "SDAChunk" in n["labels"]
                        and n["props"].get("generation") == gen
                        and any(
                            p[0] in n["props"]["content"].lower()
                            and p[1] in n["props"]["content"].lower()
                            for p in pairs
                        )
                    ],
                }

            return {"success": True, "records": []}

        def close(self):
            pass

    class FakeVectors:
        def __init__(self):
            self.items: dict[str, dict] = {}
            self.collection = type("C", (), {"load": lambda self: None})()
            self.client = type("Cl", (), {"disconnect": lambda self: None})()

        def create_collection(self, *args, **kwargs):
            pass

        def get_collection(self, *args, **kwargs):
            pass

        def add_vectors(self, vectors, ids, metadata):
            for vid, meta in zip(ids, metadata, strict=True):
                self.items[vid] = dict(meta)

        def delete_vectors(self, ids):
            for vid in ids:
                self.items.pop(vid, None)

        def search(self, **kwargs):
            return [[]]

    return FakeGraph(), FakeVectors()


def _wire_fake_stores(monkeypatch: pytest.MonkeyPatch, graph_store, vector_store):
    from semantica_dataagent import knowledge
    from semantica_dataagent.stage_contract import StorageHandles

    @contextmanager
    def fake_storage(values, stats=None):
        yield StorageHandles(
            values["knowledge_generation"],
            graph_store,
            vector_store,
            values,
            _embedding_dimension=4,
        )

    monkeypatch.setattr(knowledge, "build_storage", fake_storage)
    monkeypatch.setattr(knowledge, "incremental_storage", fake_storage)
    monkeypatch.setattr(knowledge, "read_graph", lambda values: graph_store)
    monkeypatch.setattr(knowledge, "read_vectors", lambda values: vector_store)
    monkeypatch.setattr(
        knowledge,
        "embed",
        lambda values, texts, stats=None: [[0.1, 0.2, 0.3, 0.4]] * len(texts),
    )
    monkeypatch.setattr(knowledge, "prune_generations", lambda values: None)


def test_export_ossie_yaml_and_json_pass_schema_with_types_and_extensions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from semantica_dataagent import knowledge
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.ossie_export import export_ossie

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    initialize()

    graph_store, vector_store = _make_fake_stores()
    _wire_fake_stores(monkeypatch, graph_store, vector_store)
    gen = "default"
    monkeypatch.setattr(
        knowledge,
        "settings",
        lambda project_id=None: {
            "knowledge_generation": gen,
            "neo4j_uri": "bolt://127.0.0.1:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
        },
    )

    t_id = 1
    f1_id = 2
    f2_id = 3
    f3_id = 4
    graph_store.nodes.extend(
        [
            {
                "id": t_id,
                "labels": {"SDAEntity", "SDATable"},
                "props": {
                    "generation": gen,
                    "catalog": "dw_cat",
                    "database": "sales",
                    "name": "orders",
                    "comment": "订单明细表",
                    "ref": "table:dw_cat.sales.orders",
                },
            },
            {
                "id": f1_id,
                "labels": {"SDAEntity", "SDAField"},
                "props": {
                    "generation": gen,
                    "catalog": "dw_cat",
                    "database": "sales",
                    "table": "orders",
                    "name": "dt",
                    "type": "varchar(10)",
                    "comment": "分区日期",
                    "partition": True,
                    "ref": "field:dw_cat.sales.orders.dt",
                },
            },
            {
                "id": f2_id,
                "labels": {"SDAEntity", "SDAField"},
                "props": {
                    "generation": gen,
                    "catalog": "dw_cat",
                    "database": "sales",
                    "table": "orders",
                    "name": "amount",
                    "type": "decimal(18,2)",
                    "comment": "订单金额",
                    "partition": False,
                    "ref": "field:dw_cat.sales.orders.amount",
                },
            },
            {
                "id": f3_id,
                "labels": {"SDAEntity", "SDAField"},
                "props": {
                    "generation": gen,
                    "catalog": "dw_cat",
                    "database": "sales",
                    "table": "orders",
                    "name": "payload",
                    "type": "map<string,string>",
                    "comment": "",
                    "partition": False,
                    "ref": "field:dw_cat.sales.orders.payload",
                },
            },
        ]
    )
    graph_store.rels.extend(
        [
            {"type": "HAS_FIELD", "start": t_id, "end": f1_id, "props": {}},
            {"type": "HAS_FIELD", "start": t_id, "end": f2_id, "props": {}},
            {"type": "HAS_FIELD", "start": t_id, "end": f3_id, "props": {}},
        ]
    )

    validator = _schema_validator()
    yaml_text = export_ossie(project_id="default", format="yaml")
    json_text = export_ossie(project_id="default", format="json")

    parsed_yaml = yaml.safe_load(yaml_text)
    parsed_json = json.loads(json_text)
    assert parsed_yaml == parsed_json
    assert list(validator.iter_errors(parsed_yaml)) == []
    assert parsed_yaml["version"] == "0.3.0.dev0"
    assert parsed_yaml["name"] == "default"
    assert "metrics" not in parsed_yaml
    assert len(parsed_yaml["datasets"]) == 1
    ds = parsed_yaml["datasets"][0]
    assert ds["name"] == "orders"
    assert ds["source"] == "dw_cat.sales.orders"
    assert ds["description"] == "订单明细表"
    assert "metrics" not in ds

    fields_by_name = {f["name"]: f for f in ds["fields"]}
    assert fields_by_name["dt"] == {
        "name": "dt",
        "expression": {"sql": "dt"},
        "datatype": "String",
        "description": "分区日期",
        "nonportable_extensions": [
            {
                "vendor_name": "SEMANTICA_DATAAGENT",
                "data": {"partition": True, "raw_type": "varchar(10)"},
            }
        ],
    }
    assert fields_by_name["amount"]["datatype"] == "Decimal"
    assert fields_by_name["amount"]["nonportable_extensions"][0]["data"] == {
        "partition": False,
        "raw_type": "decimal(18,2)",
    }
    assert fields_by_name["payload"]["datatype"] == "Opaque"
    assert "description" not in fields_by_name["payload"]


def test_export_disambiguates_cross_db_and_catalog_filters_db_rels_and_builds_ai_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from semantica_dataagent import knowledge
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.ossie_export import (
        EmptyKnowledgeGenerationError,
        UnsupportedProjectTypeError,
        export_ossie_document,
    )

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    initialize()

    graph_store, vector_store = _make_fake_stores()
    _wire_fake_stores(monkeypatch, graph_store, vector_store)
    gen = "default"
    monkeypatch.setattr(
        knowledge,
        "settings",
        lambda project_id=None: {
            "knowledge_generation": gen,
            "neo4j_uri": "bolt://127.0.0.1:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
        },
    )

    # Deliberately insert in reverse order to verify deterministic sorting.
    tables = [
        (1, "cat_b", "shop", "orders", "table:cat_b.shop.orders"),
        (2, "cat_a", "shop", "orders", "table:cat_a.shop.orders"),
        (3, None, "analytics", "orders", "table:analytics.orders"),
        (4, None, "analytics", "customers", "table:analytics.customers"),
        # Extreme collision: a unique table literally named "analytics__orders"
        (5, None, "staging", "analytics__orders", "table:staging.analytics__orders"),
    ]
    for tid, cat, db_name, name, ref in tables:
        props = {
            "generation": gen,
            "database": db_name,
            "name": name,
            "comment": "",
            "ref": ref,
        }
        if cat:
            props["catalog"] = cat
        graph_store.nodes.append(
            {"id": tid, "labels": {"SDAEntity", "SDATable"}, "props": props}
        )

    fields = [
        (10, "cat_b", "shop", "orders", "cust_id", "bigint", False),
        (11, "cat_a", "shop", "orders", "cust_id", "bigint", False),
        (12, None, "analytics", "orders", "cust_id", "integer", False),
        (13, None, "analytics", "orders", "select", "string", False),
        (14, None, "analytics", "customers", "id", "integer", False),
        (15, None, "staging", "analytics__orders", "row_id", "integer", False),
    ]
    tmap = {(cat or "", db_name, name): tid for tid, cat, db_name, name, _ in tables}
    for fid, cat, db_name, tbl, col, dtype, part in fields:
        props = {
            "generation": gen,
            "database": db_name,
            "table": tbl,
            "name": col,
            "type": dtype,
            "comment": "",
            "partition": part,
            "ref": f"field:{db_name}.{tbl}.{col}",
        }
        if cat:
            props["catalog"] = cat
        graph_store.nodes.append(
            {"id": fid, "labels": {"SDAEntity", "SDAField"}, "props": props}
        )
        graph_store.rels.append(
            {
                "type": "HAS_FIELD",
                "start": tmap[(cat or "", db_name, tbl)],
                "end": fid,
                "props": {},
            }
        )

    # Linked docs: two non-index docs + one index.md + one is_index=True doc
    docs = [
        (
            20,
            "document:biz/zeta.md",
            "biz",
            "zeta.md",
            "# 订单复购指标\n\n口径",
            False,
        ),
        (
            21,
            "document:biz/alpha.md",
            "biz",
            "alpha.md",
            "# 客户主数据说明\n\n细节",
            False,
        ),
        (
            22,
            "document:biz/index.md",
            "biz",
            "index.md",
            "# 索引地图\n\n不应导出",
            True,
        ),
        (
            23,
            "document:biz/custom_idx.md",
            "biz",
            "custom_idx.md",
            "# 目录页\n\n不应导出",
            True,
        ),
    ]
    for did, ref, cat, path, content, is_idx in docs:
        graph_store.nodes.append(
            {
                "id": did,
                "labels": {"SDAEntity", "SDADocument"},
                "props": {
                    "generation": gen,
                    "ref": ref,
                    "category": cat,
                    "path": path,
                    "content": content,
                    "is_index": is_idx,
                },
            }
        )
        # Link all docs to analytics.orders (id=3) in reverse order
        graph_store.rels.append(
            {"type": "HAS_DOCUMENT", "start": 3, "end": did, "props": {}}
        )

    # Foreign key edges: one within analytics, one crossing from cat_a.shop.orders -> analytics.customers
    graph_store.rels.extend(
        [
            {
                "type": "RELATES_TO",
                "start": 3,
                "end": 4,
                "props": {
                    "name": "analytics_orders_to_cust",
                    "ref": "relationship:analytics.orders:analytics.customers:analytics_orders_to_cust",
                    "from_columns": ["cust_id"],
                    "to_columns": ["id"],
                },
            },
            {
                "type": "RELATES_TO",
                "start": 2,
                "end": 4,
                "props": {
                    "name": "shop_a_orders_to_cust",
                    "ref": "relationship:cat_a.shop.orders:analytics.customers:shop_a_orders_to_cust",
                    "from_columns": ["cust_id"],
                    "to_columns": ["id"],
                },
            },
        ]
    )

    validator = _schema_validator()
    full_doc = export_ossie_document("default")
    assert list(validator.iter_errors(full_doc)) == []

    # Check deterministic sort order: (catalog or '', database, name)
    sources = [ds["source"] for ds in full_doc["datasets"]]
    assert sources == [
        "analytics.customers",
        "analytics.orders",
        "staging.analytics__orders",
        "cat_a.shop.orders",
        "cat_b.shop.orders",
    ]
    names = [ds["name"] for ds in full_doc["datasets"]]
    assert len(names) == len(set(names))
    by_source = {ds["source"]: ds for ds in full_doc["datasets"]}
    assert by_source["analytics.customers"]["name"] == "customers"
    assert by_source["cat_a.shop.orders"]["name"] == "cat_a__shop__orders"
    assert by_source["cat_b.shop.orders"]["name"] == "cat_b__shop__orders"

    # Check reserved keyword column "select" is safely quoted for round-trip
    analytics_orders = by_source["analytics.orders"]
    select_field = next(f for f in analytics_orders["fields"] if f["name"] == "select")
    assert select_field["expression"] == {"sql": '"select"'}

    # Check ai_context.instructions has only non-index docs sorted by ref
    instructions = analytics_orders["ai_context"]["instructions"]
    assert "document:biz/alpha.md" in instructions
    assert "客户主数据说明" in instructions
    assert "document:biz/zeta.md" in instructions
    assert "订单复购指标" in instructions
    assert instructions.index("document:biz/alpha.md") < instructions.index(
        "document:biz/zeta.md"
    )
    assert "index.md" not in instructions
    assert "custom_idx.md" not in instructions

    # Full export includes both relationships mapped to disambiguated dataset names
    rels = {r["name"]: r for r in full_doc["relationships"]}
    assert rels["analytics_orders_to_cust"]["from"] == analytics_orders["name"]
    assert rels["analytics_orders_to_cust"]["to"] == "customers"
    assert rels["shop_a_orders_to_cust"]["from"] == "cat_a__shop__orders"
    assert rels["shop_a_orders_to_cust"]["to"] == "customers"

    # Filtered by database="analytics": excludes shop & staging tables AND cross-db relationship!
    filtered_doc = export_ossie_document("default", database="analytics")
    assert list(validator.iter_errors(filtered_doc)) == []
    assert [ds["source"] for ds in filtered_doc["datasets"]] == [
        "analytics.customers",
        "analytics.orders",
    ]
    assert [ds["name"] for ds in filtered_doc["datasets"]] == ["customers", "orders"]
    assert [r["name"] for r in filtered_doc["relationships"]] == [
        "analytics_orders_to_cust"
    ]
    assert filtered_doc["relationships"][0]["from"] == "orders"

    with pytest.raises(EmptyKnowledgeGenerationError):
        export_ossie_document("default", database="missing_db")

    with pytest.raises(UnsupportedProjectTypeError):
        export_ossie_document("default", project_type="document")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    from fastapi.testclient import TestClient

    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    token = initialize()
    with TestClient(app) as test_client:
        yield test_client, token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_rest_export_ossie_permissions_lifecycle_headers_and_formats(
    client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from semantica_dataagent import knowledge
    from semantica_dataagent.db import Build, Project, sessions

    c, admin = client
    graph_store, vector_store = _make_fake_stores()
    _wire_fake_stores(monkeypatch, graph_store, vector_store)

    assert (
        c.put(
            "/api/v1/settings",
            headers=_headers(admin),
            json={
                "values": {
                    "source_directory": str(tmp_path / "default_src"),
                    "neo4j_uri": "bolt://127.0.0.1:7687",
                    "neo4j_user": "neo4j",
                    "neo4j_password": "secret",
                    "milvus_uri": "http://127.0.0.1:19530",
                    "embedding_url": "http://127.0.0.1:8000",
                    "embedding_model": "test-model",
                }
            },
        ).status_code
        == 200
    )

    # Create warehouse project, document project, and empty warehouse project
    wh_proj = c.post(
        "/api/v1/projects",
        headers=_headers(admin),
        json={"name": "Retail Warehouse", "type": "warehouse"},
    ).json()
    wh_id = wh_proj["id"]
    empty_wh = c.post(
        "/api/v1/projects",
        headers=_headers(admin),
        json={"name": "Empty Warehouse", "type": "warehouse"},
    ).json()
    doc_proj = c.post(
        "/api/v1/projects",
        headers=_headers(admin),
        json={"name": "Docs Only", "type": "document"},
    ).json()

    # Create users: project member, project admin, outsider (no projects)
    member = c.post(
        "/api/v1/tokens",
        headers=_headers(admin),
        json={
            "name": "member",
            "role": "user",
            "projects": [{"project_id": wh_id, "role": "member"}],
        },
    ).json()
    proj_admin = c.post(
        "/api/v1/tokens",
        headers=_headers(admin),
        json={
            "name": "proj-admin",
            "role": "user",
            "projects": [{"project_id": wh_id, "role": "admin"}],
        },
    ).json()
    outsider = c.post(
        "/api/v1/tokens",
        headers=_headers(admin),
        json={"name": "outsider", "role": "user", "projects": []},
    ).json()

    # Unauthenticated -> 401
    assert c.get(f"/api/v1/projects/{wh_id}/export/ossie").status_code == 401

    # Empty generation -> 409
    empty_resp = c.get(
        f"/api/v1/projects/{empty_wh['id']}/export/ossie",
        headers=_headers(admin),
    )
    assert empty_resp.status_code == 409, empty_resp.text

    # Document project -> 422
    doc_resp = c.get(
        f"/api/v1/projects/{doc_proj['id']}/export/ossie",
        headers=_headers(admin),
    )
    assert doc_resp.status_code == 422, doc_resp.text

    # Seed and build wh_proj via canonical source
    src_dir = tmp_path / "projects" / wh_id / "source"
    (src_dir / "tables.json").write_text(
        json.dumps(
            [
                {"database": "shop", "name": "orders", "comment": "订单表"},
                {"database": "dim", "name": "users", "comment": "用户表"},
            ]
        ),
        encoding="utf-8",
    )
    (src_dir / "fields.json").write_text(
        json.dumps(
            [
                {
                    "database": "shop",
                    "table": "orders",
                    "name": "user_id",
                    "type": "bigint",
                    "comment": "用户ID",
                    "partition": False,
                },
                {
                    "database": "shop",
                    "table": "orders",
                    "name": "dt",
                    "type": "date",
                    "comment": "日期分区",
                    "partition": True,
                },
                {
                    "database": "dim",
                    "table": "users",
                    "name": "id",
                    "type": "bigint",
                    "comment": "主键",
                    "partition": False,
                },
            ]
        ),
        encoding="utf-8",
    )
    (src_dir / "relationships.json").write_text(
        json.dumps(
            [
                {
                    "name": "orders_to_users",
                    "from_database": "shop",
                    "from_table": "orders",
                    "from_columns": ["user_id"],
                    "to_database": "dim",
                    "to_table": "users",
                    "to_columns": ["id"],
                }
            ]
        ),
        encoding="utf-8",
    )

    with sessions().begin() as db:
        b = Build(
            project_id=wh_id,
            build_type="full",
            status="running",
            started_at="2026-11-18T00:00:00Z",
            counts="{}",
        )
        db.add(b)
        db.flush()
        bid = b.id
    knowledge.run_build(bid)

    # Admin, project admin, and project member all get 200
    for token in (admin, proj_admin["token"], member["token"]):
        resp = c.get(
            f"/api/v1/projects/{wh_id}/export/ossie",
            headers=_headers(token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "application/yaml; charset=utf-8"
        assert (
            resp.headers["content-disposition"]
            == f'attachment; filename="{wh_id}.ossie.yaml"'
        )
        parsed = yaml.safe_load(resp.text)
        assert list(_schema_validator().iter_errors(parsed)) == []
        assert parsed["name"] == wh_id
        assert [ds["source"] for ds in parsed["datasets"]] == [
            "dim.users",
            "shop.orders",
        ]
        assert parsed["relationships"][0]["name"] == "orders_to_users"

    # JSON format + database filter
    json_resp = c.get(
        f"/api/v1/projects/{wh_id}/export/ossie",
        params={"format": "json", "database": "shop"},
        headers=_headers(member["token"]),
    )
    assert json_resp.status_code == 200, json_resp.text
    assert json_resp.headers["content-type"] == "application/json; charset=utf-8"
    assert (
        json_resp.headers["content-disposition"]
        == f'attachment; filename="{wh_id}.ossie.json"'
    )
    json_doc = json_resp.json()
    assert list(_schema_validator().iter_errors(json_doc)) == []
    assert [ds["source"] for ds in json_doc["datasets"]] == ["shop.orders"]
    assert "relationships" not in json_doc

    # Non-matching database filter -> 409
    no_db = c.get(
        f"/api/v1/projects/{wh_id}/export/ossie",
        params={"database": "unknown_db"},
        headers=_headers(admin),
    )
    assert no_db.status_code == 409

    # Invalid format -> 422
    bad_fmt = c.get(
        f"/api/v1/projects/{wh_id}/export/ossie",
        params={"format": "xml"},
        headers=_headers(admin),
    )
    assert bad_fmt.status_code == 422

    # Outsider & nonexistent project -> 404 (identical response, no leakage)
    denied = c.get(
        f"/api/v1/projects/{wh_id}/export/ossie",
        headers=_headers(outsider["token"]),
    )
    missing = c.get(
        "/api/v1/projects/nonexistent/export/ossie",
        headers=_headers(admin),
    )
    assert denied.status_code == missing.status_code == 404
    assert denied.json() == missing.json() == {"detail": "Project not found"}

    # Archived -> 409 项目已归档
    assert (
        c.post(f"/api/v1/projects/{wh_id}/archive", headers=_headers(admin)).status_code
        == 200
    )
    arch_resp = c.get(
        f"/api/v1/projects/{wh_id}/export/ossie",
        headers=_headers(admin),
    )
    assert arch_resp.status_code == 409
    assert arch_resp.json()["detail"] == "项目已归档"

    # Deleting -> 409 项目已归档
    with sessions().begin() as db:
        db.get(Project, wh_id).status = "deleting"
    del_resp = c.get(
        f"/api/v1/projects/{wh_id}/export/ossie",
        headers=_headers(admin),
    )
    assert del_resp.status_code == 409
    assert del_resp.json()["detail"] == "项目已归档"


def test_cli_export_ossie_stdout_atomic_output_help_and_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    import os
    import subprocess
    import sys

    from semantica_dataagent import cli, knowledge

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    cli.initialize()

    # Subprocess --help and unknown argument check via real console script
    cli_bin = str(Path(sys.executable).with_name("semantica-dataagent"))
    help_proc = subprocess.run(
        [cli_bin, "export-ossie", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_proc.returncode == 0, help_proc.stderr
    assert "--project" in help_proc.stdout
    assert "--database" in help_proc.stdout
    assert "--format" in help_proc.stdout
    assert "--output" in help_proc.stdout

    bad_flag_target = tmp_path / "should_not_exist.yaml"
    bad_proc = subprocess.run(
        [
            cli_bin,
            "export-ossie",
            "--unknown-flag",
            "-o",
            str(bad_flag_target),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad_proc.returncode != 0
    assert not bad_flag_target.exists()

    with pytest.raises(SystemExit) as bad_fmt_exc:
        cli.main(["export-ossie", "--format", "toml", "-o", str(bad_flag_target)])
    assert bad_fmt_exc.value.code != 0
    assert not bad_flag_target.exists()

    graph_store, vector_store = _make_fake_stores()
    _wire_fake_stores(monkeypatch, graph_store, vector_store)
    monkeypatch.setattr(
        knowledge,
        "settings",
        lambda project_id=None: {
            "knowledge_generation": "default",
            "neo4j_uri": "bolt://127.0.0.1:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
        },
    )
    graph_store.nodes.extend(
        [
            {
                "id": 1,
                "labels": {"SDAEntity", "SDATable"},
                "props": {
                    "generation": "default",
                    "database": "shop",
                    "name": "items",
                    "comment": "商品表",
                    "ref": "table:shop.items",
                },
            },
            {
                "id": 2,
                "labels": {"SDAEntity", "SDAField"},
                "props": {
                    "generation": "default",
                    "database": "shop",
                    "table": "items",
                    "name": "item_id",
                    "type": "integer",
                    "comment": "商品ID",
                    "partition": False,
                    "ref": "field:shop.items.item_id",
                },
            },
        ]
    )
    graph_store.rels.append({"type": "HAS_FIELD", "start": 1, "end": 2, "props": {}})
    capsys.readouterr()

    # Default stdout YAML
    assert cli.main(["export-ossie"]) == 0
    stdout_yaml = yaml.safe_load(capsys.readouterr().out)
    assert list(_schema_validator().iter_errors(stdout_yaml)) == []
    assert stdout_yaml["datasets"][0]["source"] == "shop.items"

    # Atomic write to -o JSON
    out_dir = tmp_path / "exports"
    out_dir.mkdir()
    out_file = out_dir / "default.ossie.json"
    out_file.write_text("KEEP_SENTINEL", encoding="utf-8")

    assert (
        cli.main(
            [
                "export-ossie",
                "--project",
                "default",
                "--database",
                "shop",
                "--format",
                "json",
                "-o",
                str(out_file),
            ]
        )
        == 0
    )
    exported_json = json.loads(out_file.read_text(encoding="utf-8"))
    assert list(_schema_validator().iter_errors(exported_json)) == []
    assert [p.name for p in out_dir.iterdir()] == ["default.ossie.json"]

    # Export failure (missing database) does not touch existing file or leave temp files
    out_file.write_text("PRESERVE_ON_EXPORT_ERROR", encoding="utf-8")
    assert (
        cli.main(
            [
                "export-ossie",
                "--database",
                "missing_db",
                "-o",
                str(out_file),
            ]
        )
        == 1
    )
    err_msg = capsys.readouterr().err
    assert err_msg.strip()
    assert out_file.read_text(encoding="utf-8") == "PRESERVE_ON_EXPORT_ERROR"
    assert [p.name for p in out_dir.iterdir()] == ["default.ossie.json"]

    # Atomic write failure during os.replace cleans temp file and preserves target
    def boom_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom_replace)
    assert cli.main(["export-ossie", "-o", str(out_file)]) == 1
    assert "disk full" in capsys.readouterr().err
    assert out_file.read_text(encoding="utf-8") == "PRESERVE_ON_EXPORT_ERROR"
    assert [p.name for p in out_dir.iterdir()] == ["default.ossie.json"]


def test_lossless_roundtrip_import_build_export_reimport_build(
    client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import base64

    from semantica_dataagent import knowledge, sql_validation
    from semantica_dataagent.db import Build, MetadataOverride, sessions
    from semantica_dataagent.sql_validation import find_join_path, validate_sql

    c, admin = client
    graph_store, vector_store = _make_fake_stores()
    _wire_fake_stores(monkeypatch, graph_store, vector_store)

    assert (
        c.put(
            "/api/v1/settings",
            headers=_headers(admin),
            json={
                "values": {
                    "source_directory": str(tmp_path / "default_src"),
                    "neo4j_uri": "bolt://127.0.0.1:7687",
                    "neo4j_user": "neo4j",
                    "neo4j_password": "secret",
                    "milvus_uri": "http://127.0.0.1:19530",
                    "embedding_url": "http://127.0.0.1:8000",
                    "embedding_model": "test-model",
                }
            },
        ).status_code
        == 200
    )

    def setup_warehouse(name: str) -> str:
        proj = c.post(
            "/api/v1/projects",
            headers=_headers(admin),
            json={"name": name, "type": "warehouse"},
        ).json()
        pid = proj["id"]
        assert (
            c.put(
                f"/api/v1/projects/{pid}/adapters",
                headers=_headers(admin),
                json={"adapters": [{"id": "ossie", "enabled": True, "parameters": {}}]},
            ).status_code
            == 200
        )
        return pid

    def run_build(pid: str) -> None:
        with sessions().begin() as db:
            b = Build(
                project_id=pid,
                build_type="full",
                status="running",
                started_at="2026-11-18T00:00:00Z",
                counts="{}",
            )
            db.add(b)
            db.flush()
            bid = b.id
        knowledge.run_build(bid)
        sql_validation._snapshot_cache = None
        with sessions()() as db:
            row = db.get(Build, bid)
            assert row.status == "succeeded", row.error

    proj_a = setup_warehouse("Roundtrip Source")
    tpcds_fixture = (
        Path(__file__).resolve().parent
        / "fixtures"
        / "ossie"
        / "tpcds_semantic_model.yaml"
    )
    extra_model = {
        "version": "0.3.0.dev0",
        "name": "roundtrip_extras",
        "datasets": [
            {
                "name": "web_orders",
                "source": "tpcds.public.web_orders",
                "description": "网站订单事实表",
                "fields": [
                    {
                        "name": "tenant_logical",
                        "expression": {"sql": "tenant_id"},
                        "datatype": "String",
                    },
                    {
                        "name": "buyer_logical",
                        "expression": {"sql": "buyer_sk"},
                        "datatype": "Integer",
                    },
                    {
                        "name": "order_dt",
                        "expression": {"sql": "dt"},
                        "datatype": "String",
                        "description": "业务分区日期",
                        "nonportable_extensions": [
                            {
                                "vendor_name": "SEMANTICA_DATAAGENT",
                                "data": {"partition": True, "raw_type": "varchar(16)"},
                            }
                        ],
                    },
                ],
            },
            {
                "name": "buyers",
                "source": "tpcds.public.buyers",
                "description": "买家维表",
                "fields": [
                    {
                        "name": "dim_tenant",
                        "expression": {"sql": "tenant_id"},
                        "datatype": "String",
                    },
                    {
                        "name": "dim_buyer_id",
                        "expression": {"sql": "id"},
                        "datatype": "Integer",
                    },
                ],
            },
        ],
        "relationships": [
            {
                "name": "web_orders_to_buyers",
                "from": "web_orders",
                "to": "buyers",
                "from_columns": ["tenant_logical", "buyer_logical"],
                "to_columns": ["dim_tenant", "dim_buyer_id"],
            }
        ],
    }

    upload_a = c.post(
        "/api/v1/source/upload",
        params={"project": proj_a},
        headers=_headers(admin),
        json={
            "target": "ossie",
            "files": [
                {
                    "path": "tpcds_semantic_model.yaml",
                    "content_base64": base64.b64encode(
                        tpcds_fixture.read_bytes()
                    ).decode(),
                },
                {
                    "path": "extras.json",
                    "content_base64": base64.b64encode(
                        json.dumps(extra_model).encode()
                    ).decode(),
                },
            ],
        },
    )
    assert upload_a.status_code == 200, upload_a.text

    # Add an approved MetadataOverride on a physical field before building Project A
    with sessions().begin() as db:
        db.add(
            MetadataOverride(
                project_id=proj_a,
                target="field:tpcds.public.store_sales.ss_net_profit",
                comment="审定净利润字段说明",
            )
        )

    run_build(proj_a)

    # Export Project A as YAML via REST
    exported_resp = c.get(
        f"/api/v1/projects/{proj_a}/export/ossie",
        headers=_headers(admin),
    )
    assert exported_resp.status_code == 200, exported_resp.text
    exported_bytes = exported_resp.content
    exported_doc = yaml.safe_load(exported_bytes)
    assert list(_schema_validator().iter_errors(exported_doc)) == []

    # Re-import exported YAML into a brand new clean Project B
    proj_b = setup_warehouse("Roundtrip Target")
    upload_b = c.post(
        "/api/v1/source/upload",
        params={"project": proj_b},
        headers=_headers(admin),
        json={
            "target": "ossie",
            "files": [
                {
                    "path": "exported.ossie.yaml",
                    "content_base64": base64.b64encode(exported_bytes).decode(),
                }
            ],
        },
    )
    assert upload_b.status_code == 200, upload_b.text
    run_build(proj_b)

    tables_a = knowledge.describe(project_id=proj_a)
    tables_b = knowledge.describe(project_id=proj_b)

    def table_signature(rows: list[dict]) -> dict:
        sig = {}
        for t in rows:
            key = (t.get("catalog"), t["database"], t["name"])
            sig[key] = {
                f["name"]: {
                    "type": f["type"],
                    "partition": f["partition"],
                    "comment": f["comment"],
                }
                for f in t["fields"]
            }
        return sig

    assert table_signature(tables_a) == table_signature(tables_b)

    # Verify join path and SQL validation behave identically in Project B
    path_b = find_join_path(
        ["tpcds.public.store_sales", "tpcds.public.customer"], project_id=proj_b
    )
    assert path_b["status"] == "found"
    assert path_b["strength"] == "strong"
    assert path_b["steps"][0]["keys"] == ["ss_customer_sk=c_customer_sk"]

    valid_comp = validate_sql(
        "SELECT o.buyer_sk FROM tpcds.public.web_orders o "
        "JOIN tpcds.public.buyers b ON o.tenant_id = b.tenant_id AND o.buyer_sk = b.id "
        "WHERE o.dt = '2026-01-01'",
        "hive",
        project_id=proj_b,
    )
    assert valid_comp["status"] == "pass", valid_comp

    missing_part = validate_sql(
        "SELECT o.buyer_sk FROM tpcds.public.web_orders o",
        "hive",
        project_id=proj_b,
    )
    assert missing_part["status"] == "issues"
    assert any(issue["kind"] == "partition" for issue in missing_part["issues"])


def test_export_rejects_invalid_schema_before_returning_or_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    from semantica_dataagent import cli, knowledge
    from semantica_dataagent.ossie_export import OssieExportError, export_ossie

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    cli.initialize()

    graph_store, vector_store = _make_fake_stores()
    _wire_fake_stores(monkeypatch, graph_store, vector_store)
    monkeypatch.setattr(
        knowledge,
        "settings",
        lambda project_id=None: {
            "knowledge_generation": "default",
            "neo4j_uri": "bolt://127.0.0.1:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
        },
    )
    # Inject a table with empty name -> violates ossie-schema.json minLength: 1
    graph_store.nodes.append(
        {
            "id": 1,
            "labels": {"SDAEntity", "SDATable"},
            "props": {
                "generation": "default",
                "database": "shop",
                "name": "",
                "comment": "",
                "ref": "table:shop.",
            },
        }
    )

    with pytest.raises(OssieExportError, match="schema validation"):
        export_ossie("default")

    target = tmp_path / "existing.yaml"
    target.write_text("UNTOUCHED", encoding="utf-8")
    assert cli.main(["export-ossie", "-o", str(target)]) == 1
    assert "schema validation" in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == "UNTOUCHED"


def test_fresh_default_project_empty_409_and_cli_rejects_missing_or_doc_project(
    client, tmp_path: Path, capsys
):
    from semantica_dataagent import cli

    c, admin = client
    # Fresh default project before any settings/build returns 409
    default_empty = c.get(
        "/api/v1/projects/default/export/ossie",
        headers=_headers(admin),
    )
    assert default_empty.status_code == 409, default_empty.text

    # Document project created via REST
    doc_id = c.post(
        "/api/v1/projects",
        headers=_headers(admin),
        json={"name": "CLI Doc Proj", "type": "document"},
    ).json()["id"]
    out_file = tmp_path / "never_created.yaml"

    capsys.readouterr()
    assert (
        cli.main(["export-ossie", "--project", "missing_proj", "-o", str(out_file)])
        == 1
    )
    assert capsys.readouterr().err.strip()
    assert not out_file.exists()

    assert cli.main(["export-ossie", "--project", doc_id, "-o", str(out_file)]) == 1
    assert "数仓" in capsys.readouterr().err
    assert not out_file.exists()
