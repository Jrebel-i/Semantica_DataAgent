"""Isolated 候选知识代 build, GenerationPointer isolation, cache reuse, and orphan cleanup (#295)."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from semantica_dataagent import (
    knowledge,
    project_context,
    project_deletion,
    sql_validation,
)
from semantica_dataagent.db import (
    Build,
    KnowledgeGeneration,
    generation_pointer,
    sessions,
)
from semantica_dataagent.embedding_cache import open_embedding_cache
from semantica_dataagent.stage_contract import StorageHandles


class _MultiCollectionFakeGraph:
    def __init__(self):
        self.nodes: list[dict] = []
        self.rels: list[dict] = []
        self._next_id = 1

    def _find_node(self, node_id: int) -> dict:
        return next(n for n in self.nodes if n["id"] == node_id)

    def execute_query(self, query: str, params: dict) -> dict:
        q = " ".join(query.split())
        gen = params.get("generation")

        if q.startswith(("CREATE INDEX", "CREATE FULLTEXT INDEX", "CALL db.")):
            return {"success": True, "records": []}
        if "WHERE n.generation STARTS WITH $prefix" in q:
            prefix = params["prefix"]
            victims = {
                n["id"]
                for n in self.nodes
                if str(n["props"].get("generation") or "").startswith(prefix)
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
            "MATCH (n:SDAEntity {generation:$generation}) MATCH (n)-[batch_relationship]-()"
            in q
        ):
            victims = {
                n["id"] for n in self.nodes if n["props"].get("generation") == gen
            }
            keep = [
                r
                for r in self.rels
                if r["start"] not in victims and r["end"] not in victims
            ]
            deleted = len(self.rels) - len(keep)
            self.rels = keep
            return {"success": True, "records": [{"deleted": deleted}]}
        if (
            "MATCH (n:SDAEntity {generation:$generation}) WITH n LIMIT $batch_size DETACH DELETE n"
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
                self.nodes.append(chunk)
                self._next_id += 1
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
                                "name": row["name"],
                                "ref": row["ref"],
                                "source_file": row["source_file"],
                                "source_key": row["source_key"],
                                "source_files": list(row["source_files"]),
                                "build_id": params["build_id"],
                            },
                        }
                    )
                    self._next_id += 1
            return {"success": True, "records": []}
        if "RETURN elementId(n) AS id" in q:
            label = "SDAField" if ":SDAField" in q else "SDADocument"
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
        if "OPTIONAL MATCH (t)-[:HAS_FIELD]->(f:SDAField" in q:
            records = []
            for t in self.nodes:
                if not (
                    "SDATable" in t["labels"] and t["props"].get("generation") == gen
                ):
                    continue
                tp = t["props"]
                fields_for_t = [
                    dict(self._find_node(r["end"])["props"])
                    for r in self.rels
                    if r["type"] == "HAS_FIELD" and r["start"] == t["id"]
                ]
                records.append(
                    {
                        "table": dict(tp),
                        "fields": fields_for_t,
                        "documents": [],
                        "out_rels": [],
                        "in_rels": [],
                    }
                )
            return {"success": True, "records": records}
        if "db.index.fulltext.queryNodes" in q:
            scopes = set(params["scopes"])
            q_lower = params["query"].lower()
            rows = []
            for n in self.nodes:
                p = n["props"]
                if p.get("generation") != gen:
                    continue
                in_scope = (
                    ("SDATable" in n["labels"] and "tables" in scopes)
                    or ("SDAField" in n["labels"] and "fields" in scopes)
                    or ("SDAChunk" in n["labels"] and "docs" in scopes)
                )
                if not in_scope:
                    continue
                hay = " ".join(
                    str(p.get(k) or "")
                    for k in (
                        "name",
                        "comment",
                        "description",
                        "content",
                        "text",
                        "title",
                    )
                ).lower()
                if any(tok and tok in hay for tok in q_lower.split()):
                    rows.append({"source": "fulltext", "item": dict(p)})
            return {"success": True, "records": rows}
        return {"success": True, "records": []}

    def close(self):
        pass


class _MultiCollectionFakeMilvus:
    def __init__(self):
        self.collections: dict[str, dict[str, dict]] = {}

    def reader_for(self, name: str):
        items = self.collections.setdefault(name, {})

        class _Reader:
            def __init__(self, col_name: str, store_items: dict[str, dict]):
                self.col_name = col_name
                self.items = store_items
                self.collection = type("C", (), {"load": lambda s: None})()
                self.client = type("Cl", (), {"disconnect": lambda s: None})()

            def create_collection(self, collection_name, *args, **kwargs):
                self.items.clear()

            def add_vectors(self, vectors, ids, metadata):
                for vid, meta in zip(ids, metadata, strict=True):
                    self.items[vid] = dict(meta)

            def delete_vectors(self, ids):
                for vid in ids:
                    self.items.pop(vid, None)

            def search(self, **kwargs):
                flt = kwargs.get("filter") or ""
                hits = []
                for meta in self.items.values():
                    scope = meta.get("scope", "")
                    if f'"{scope}"' not in flt:
                        continue
                    hits.append({"entity": {"metadata": dict(meta)}})
                return [hits]

            def close(self):
                pass

        return _Reader(name, items)

    def client(self):
        collections = self.collections

        class _Client:
            def list_collections(self):
                return list(collections.keys())

            def has_collection(self, name: str) -> bool:
                return name in collections

            def drop_collection(self, name: str) -> None:
                collections.pop(name, None)

            def load_collection(self, name: str) -> None:
                pass

            def search(self, collection_name: str, **kwargs):
                flt = kwargs.get("filter") or ""
                items = collections.get(collection_name, {})
                hits = []
                for meta in items.values():
                    scope = meta.get("scope", "")
                    if f'"{scope}"' not in flt:
                        continue
                    hits.append({"entity": {"metadata": dict(meta)}})
                return [hits]

            def close(self):
                pass

        return _Client()


def _start_fake_embedding_server():
    calls: list[list[str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            inputs = body.get("input", [])
            if isinstance(inputs, str):
                inputs = [inputs]
            calls.append(list(inputs))
            data = [
                {
                    "object": "embedding",
                    "index": idx,
                    "embedding": [0.1, 0.2, 0.3, 0.4],
                }
                for idx, _ in enumerate(inputs)
            ]
            raw = json.dumps({"object": "list", "data": data}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, calls


def _install_fake_stores(monkeypatch, graph_store, milvus_hub):
    sql_validation._snapshot_cache = None

    @contextmanager
    def fake_storage(values, stats=None):
        model = values.get("embedding_model") or "model/bge-m3"
        cache = open_embedding_cache(values, model=model)
        col = knowledge.collection_name(values)
        reader = milvus_hub.reader_for(col)
        yield StorageHandles(
            project_context.from_values(values).generation,
            graph_store,
            reader,
            values,
            _embedding_stats=stats,
            _embedding_cache=cache,
            _embedding_model=model,
            _embedding_dimension=4,
        )

    monkeypatch.setattr(knowledge, "build_storage", fake_storage)
    monkeypatch.setattr(knowledge, "graph", lambda values: graph_store)
    monkeypatch.setattr(knowledge, "read_graph", lambda values: graph_store)
    import pymilvus

    monkeypatch.setattr(
        knowledge,
        "MilvusClient",
        lambda uri=None: milvus_hub.client(),
    )
    monkeypatch.setattr(
        pymilvus,
        "MilvusClient",
        lambda uri=None: milvus_hub.client(),
    )
    monkeypatch.setattr(
        knowledge,
        "read_vectors",
        lambda values: milvus_hub.reader_for(knowledge.collection_name(values)),
    )


def _setup_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("DATAAGENT_DTB_CONFIG_DIR", str(tmp_path / "dtb"))

    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    admin_token = initialize()
    return TestClient(app), admin_token


def _write_project_sources(src_dir: Path) -> None:
    (src_dir / "knowledge" / "metrics").mkdir(parents=True, exist_ok=True)
    (src_dir / "knowledge" / "metrics" / "gmv.md").write_text(
        "# GMV口径\n\n统计支付成功订单金额。\n",
        encoding="utf-8",
    )
    (src_dir / "canonical").mkdir(parents=True, exist_ok=True)
    (src_dir / "canonical" / "tables.json").write_text(
        json.dumps(
            [{"database": "shop", "name": "orders", "comment": "订单表"}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (src_dir / "canonical" / "fields.json").write_text(
        json.dumps(
            [
                {
                    "database": "shop",
                    "table": "orders",
                    "name": "id",
                    "type": "bigint",
                    "comment": "主键",
                    "partition": False,
                },
                {
                    "database": "shop",
                    "table": "orders",
                    "name": "amount",
                    "type": "decimal",
                    "comment": "金额",
                    "partition": False,
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_candidate_generation_isolated_reuses_embedding_cache_and_tears_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    emb_server, emb_calls = _start_fake_embedding_server()
    try:
        emb_url = f"http://127.0.0.1:{emb_server.server_address[1]}/v1"
        graph_store = _MultiCollectionFakeGraph()
        milvus_hub = _MultiCollectionFakeMilvus()
        _install_fake_stores(monkeypatch, graph_store, milvus_hub)

        c, admin_token = _setup_client(tmp_path, monkeypatch)
        with c:
            headers = {"Authorization": f"Bearer {admin_token}"}
            proj_resp = c.post(
                "/api/v1/projects",
                headers=headers,
                json={"name": "Cand Project", "type": "warehouse"},
            )
            assert proj_resp.status_code == 201, proj_resp.text
            project_id = proj_resp.json()["id"]

            set_resp = c.put(
                "/api/v1/settings",
                headers=headers,
                json={
                    "values": {
                        "neo4j_uri": "bolt://127.0.0.1:7687",
                        "neo4j_user": "neo4j",
                        "neo4j_password": "test-password",
                        "milvus_uri": "http://127.0.0.1:19530",
                        "embedding_url": emb_url,
                        "embedding_model": "mock-emb",
                        "generation_retention": "2",
                    }
                },
            )
            assert set_resp.status_code == 200, set_resp.text

            src_dir = project_context.project_source_directory(project_id)
            _write_project_sources(src_dir)
            assert (
                c.put(
                    "/api/v1/plugins/source/markdown",
                    headers=headers,
                    params={"project": project_id},
                    json={"enabled": True, "parameters": {"path": "."}},
                ).status_code
                == 200
            )
            assert (
                c.put(
                    "/api/v1/plugins/source/canonical",
                    headers=headers,
                    params={"project": project_id},
                    json={"enabled": True, "parameters": {}},
                ).status_code
                == 200
            )

            # Build formal active generation
            with sessions().begin() as db:
                b1 = Build(
                    project_id=project_id,
                    build_type="full",
                    status="running",
                    started_at="2026-11-18T00:00:00Z",
                    counts="{}",
                )
                db.add(b1)
                db.flush()
                b1_id = b1.id
            knowledge.run_build(b1_id)

            with sessions()() as db:
                b1_row = db.get(Build, b1_id)
                assert b1_row.status == "succeeded", b1_row.error
                formal_ptr = generation_pointer(db, project_id)
                assert formal_ptr is not None
                formal_gen = formal_ptr.generation

            calls_after_formal = len(emb_calls)
            assert calls_after_formal >= 1
            original_gmv = (src_dir / "knowledge" / "metrics" / "gmv.md").read_text(
                encoding="utf-8"
            )

            # Build isolated candidate generation with overlaid document change
            with knowledge.candidate_generation(
                project_id,
                overlay_files={
                    "knowledge/metrics/refund.md": "# 退款口径\n\n退款金额扣减口径说明_CANDIDATE_ONLY。\n"
                },
            ) as cand:
                assert cand.generation != formal_gen
                # 1. Real source_directory untouched
                assert not (src_dir / "knowledge" / "metrics" / "refund.md").exists()
                assert (src_dir / "knowledge" / "metrics" / "gmv.md").read_text(
                    encoding="utf-8"
                ) == original_gmv

                # 2. GenerationPointer untouched
                with sessions()() as db:
                    assert generation_pointer(db, project_id).generation == formal_gen
                    row = db.get(KnowledgeGeneration, cand.generation)
                    assert row is not None and row.is_candidate is True

                # 3. Excluded from list_generations and switch_generation
                gens_resp = c.get(
                    "/api/v1/generations",
                    headers=headers,
                    params={"project": project_id},
                )
                assert gens_resp.status_code == 200
                listed = [g["name"] for g in gens_resp.json()["generations"]]
                assert formal_gen in listed
                assert cand.generation not in listed
                switch_resp = c.post(
                    f"/api/v1/generations/{cand.generation}/switch",
                    headers=headers,
                    params={"project": project_id},
                )
                assert switch_resp.status_code == 404

                # 4. bind_generation sees candidate; outer scope still sees active
                outer_hits = knowledge.search(
                    "CANDIDATE_ONLY", "all", 5, project_id=project_id
                )
                assert not any("refund.md" in (h.get("ref") or "") for h in outer_hits)
                with project_context.bind_generation(project_id, cand.generation):
                    cand_hits = knowledge.search(
                        "CANDIDATE_ONLY", "all", 5, project_id=project_id
                    )
                    assert any("refund.md" in (h.get("ref") or "") for h in cand_hits)

                # 5. embedding-cache.sqlite reused for unchanged items
                assert cand.embedding_counts["embedding_cache_hits"] > 0
                new_emb_texts = [
                    t for batch in emb_calls[calls_after_formal:] for t in batch
                ]
                assert any("CANDIDATE_ONLY" in t for t in new_emb_texts)
                assert not any("支付成功订单金额" in t for t in new_emb_texts)

                # 6. Formal retention quota (even retention=1) must NOT touch candidate
                knowledge.prune_generations(
                    {
                        **knowledge.settings(project_id),
                        project_context.GENERATION_RETENTION_KEY: "1",
                    }
                )
                with sessions()() as db:
                    assert db.get(KnowledgeGeneration, cand.generation) is not None

            # 7. Deterministic teardown removed Neo4j, Milvus, DB row, and workdir
            assert not any(
                n["props"].get("generation") == cand.generation
                for n in graph_store.nodes
            )
            cand_col = project_context.STORAGE_PREFIX + cand.generation
            assert cand_col not in milvus_hub.collections
            assert not cand.source_directory.exists()
            with sessions()() as db:
                assert db.get(KnowledgeGeneration, cand.generation) is None
                assert generation_pointer(db, project_id).generation == formal_gen
    finally:
        emb_server.shutdown()


def test_orphan_candidate_cleanup_on_startup_and_project_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    graph_store = _MultiCollectionFakeGraph()
    milvus_hub = _MultiCollectionFakeMilvus()
    _install_fake_stores(monkeypatch, graph_store, milvus_hub)

    c, admin_token = _setup_client(tmp_path, monkeypatch)
    with c:
        headers = {"Authorization": f"Bearer {admin_token}"}
        proj = c.post(
            "/api/v1/projects",
            headers=headers,
            json={"name": "Orphan Cleanup Proj", "type": "warehouse"},
        )
        assert proj.status_code == 201
        project_id = proj.json()["id"]
        assert (
            c.put(
                "/api/v1/settings",
                headers=headers,
                json={
                    "values": {
                        "neo4j_uri": "bolt://127.0.0.1:7687",
                        "neo4j_user": "neo4j",
                        "neo4j_password": "test-password",
                        "milvus_uri": "http://127.0.0.1:19530",
                    }
                },
            ).status_code
            == 200
        )

        orphan_gen = f"{project_id}_cand_orphan123"
        orphan_dir = (
            project_context.projects_root() / project_id / ".candidates" / orphan_gen
        )
        orphan_dir.mkdir(parents=True, exist_ok=True)
        (orphan_dir / "stale.txt").write_text("stale", encoding="utf-8")

        graph_store.nodes.append(
            {
                "id": 999,
                "labels": {"SDAEntity", "SDATable"},
                "props": {"generation": orphan_gen, "name": "ghost"},
            }
        )
        milvus_hub.collections[project_context.STORAGE_PREFIX + orphan_gen] = {
            "v1": {"scope": "tables"}
        }
        with sessions().begin() as db:
            db.add(
                KnowledgeGeneration(
                    name=orphan_gen,
                    project_id=project_id,
                    build_id=None,
                    is_candidate=True,
                    created_at="2026-11-18T00:00:00Z",
                )
            )

    # Simulate app restart after crash -> lifespan runs orphan cleanup
    from semantica_dataagent.app import app

    with TestClient(app) as tc:
        assert not any(
            n["props"].get("generation") == orphan_gen for n in graph_store.nodes
        )
        assert (
            project_context.STORAGE_PREFIX + orphan_gen
        ) not in milvus_hub.collections
        assert not orphan_dir.exists()
        with sessions()() as db:
            assert db.get(KnowledgeGeneration, orphan_gen) is None

        # Also test project_deletion sweeps leftover candidates
        orphan_2 = f"{project_id}_cand_orphan456"
        graph_store.nodes.append(
            {
                "id": 1000,
                "labels": {"SDAEntity", "SDATable"},
                "props": {"generation": orphan_2, "name": "ghost2"},
            }
        )
        milvus_hub.collections[project_context.STORAGE_PREFIX + orphan_2] = {
            "v1": {"scope": "tables"}
        }
        with sessions().begin() as db:
            db.add(
                KnowledgeGeneration(
                    name=orphan_2,
                    project_id=project_id,
                    build_id=0,
                    is_candidate=True,
                    created_at="2026-11-18T00:00:00Z",
                )
            )
        arch = tc.post(f"/api/v1/projects/{project_id}/archive", headers=headers)
        assert arch.status_code == 200, arch.text
        project_deletion.prepare_deletion(
            project_id, "Orphan Cleanup Proj", "a", "admin"
        )
        project_deletion.run_deletion(project_id)
        assert not any(
            n["props"].get("generation") == orphan_2 for n in graph_store.nodes
        )
        assert (project_context.STORAGE_PREFIX + orphan_2) not in milvus_hub.collections
        with sessions()() as db:
            assert db.get(KnowledgeGeneration, orphan_2) is None
