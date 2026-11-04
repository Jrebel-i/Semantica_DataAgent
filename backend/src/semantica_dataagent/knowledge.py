"""Build and query the current generation of warehouse metadata."""

import json
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from urllib.parse import quote, urlsplit

import numpy as np
from cryptography.fernet import Fernet
from pymilvus import MilvusClient, utility
from semantica.graph_store import GraphStore
from semantica.vector_store.milvus_store import MilvusStore

from .canonical import KINDS
from .db import Build, Setting, sessions
from .embedding_client import EmbeddingStats
from .embedding_client import embed as embedding_embed
from .filters import filter_records, get_filters
from .plugins import configured


def settings():
    with sessions()() as db:
        values = {row.key: row.value for row in db.query(Setting).all()}
    if values.get("neo4j_password"):
        values["neo4j_password"] = (
            Fernet(os.environ["DATAAGENT_ENCRYPTION_KEY"].encode())
            .decrypt(values["neo4j_password"].encode())
            .decode()
        )
    values.setdefault("embedding_model", "model/bge-m3")
    values.setdefault("knowledge_generation", "default")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", values["knowledge_generation"]):
        raise ValueError("Invalid generation")
    return values


def graph(values):
    store = GraphStore(
        backend="neo4j",
        uri=values["neo4j_uri"],
        user=values["neo4j_user"],
        password=values["neo4j_password"],
    )
    store.connect()
    return store


_read_lock = Lock()
_read_graphs = {}
_read_vectors = {}


def _reader(cache, key, factory):
    with _read_lock:
        existing = cache.get(key)
    if existing is not None:
        return existing
    created = factory()
    with _read_lock:
        existing = cache.setdefault(key, created)
    if existing is not created:
        created.close()
    return existing


def read_graph(values):
    key = (values["neo4j_uri"], values["neo4j_user"], values["neo4j_password"])
    return _reader(_read_graphs, key, lambda: graph(values))


def read_vectors(values):
    key = (values["milvus_uri"], collection_name(values))

    def connect():
        client = MilvusClient(uri=key[0])
        try:
            client.load_collection(key[1])
        except Exception:
            client.close()
            raise
        return client

    return _reader(_read_vectors, key, connect)


def close_readers():
    with _read_lock:
        readers = list(_read_graphs.values()) + list(_read_vectors.values())
        _read_graphs.clear()
        _read_vectors.clear()
    for reader in readers:
        reader.close()


def execute(store, query, parameters):
    result = store.execute_query(query, parameters)
    if not result.get("success"):
        raise RuntimeError("Graph query failed")
    return result["records"]


def embed(values, texts, stats=None):
    return embedding_embed(values, texts, stats)


def collection_name(values):
    return "sda_" + values["knowledge_generation"]


def milvus(values):
    uri = urlsplit(values["milvus_uri"])
    store = MilvusStore(
        host=uri.hostname, port=uri.port or (443 if uri.scheme == "https" else 19530)
    )
    store.connect(secure=uri.scheme == "https")
    return store


def reference(kind, data):
    key = ".".join(
        quote(str(data[k]), safe="").replace(".", "%2E")
        for k in (
            ("database", "name") if kind == "table" else ("database", "table", "name")
        )
    )
    return f"{kind}:{key}"


@contextmanager
def build_storage(values, embedding_stats=None):
    """Initialize the generation once, then share append-only stage handles."""
    from .stage_contract import StorageHandles

    stats = embedding_stats if embedding_stats is not None else EmbeddingStats()
    generation = values["knowledge_generation"]
    dimension = len(embed(values, ["knowledge"], stats)[0])
    graph_store = graph(values)
    try:
        vector_store = milvus(values)
        try:
            execute(
                graph_store,
                "MATCH (n:SDAEntity {generation:$generation}) DETACH DELETE n",
                {"generation": generation},
            )
            name = collection_name(values)
            if utility.has_collection(name):
                utility.drop_collection(name)
            vector_store.create_collection(name, dimension, metric_type="COSINE")
            yield StorageHandles(
                generation, graph_store, vector_store, values, _embedding_stats=stats
            )
            vector_store.collection.load()
        finally:
            vector_store.client.disconnect()
    finally:
        graph_store.close()


def run_build(build_id):
    status, counts, error = "failed", {}, None
    failures = []
    stats = EmbeddingStats()
    try:
        values = settings()
        records = []
        adapter_stats = {}
        for category, plugin, enabled, parameters in configured():
            if category == "source" and enabled:
                result = plugin.read(Path(values["source_directory"]), parameters)
                records.extend(result.records)
                adapter_stats.update(
                    {f"{plugin.id}_{key}": value for key, value in result.stats.items()}
                )
        records, filtered = filter_records(records, get_filters())
        counts = dict.fromkeys(KINDS.values(), 0)
        counts.update(Counter(r.kind for r in records))
        counts.update(adapter_stats)
        counts["filtered"] = filtered
        from .stages import STAGES

        with build_storage(values, stats) as storage:
            for stage in STAGES:
                result = stage(records, values["knowledge_generation"], storage)
                counts.update(result.counts)
                failures.extend(result.failures)
                if result.errors:
                    raise ValueError("; ".join(result.errors))
        read_graph(values)
        read_vectors(values)
        status = "succeeded"
    except Exception as exc:  # noqa: BLE001 - record failure of background work
        error = str(exc)
    finally:
        counts.update(stats.counts())
        with sessions().begin() as db:
            build = db.get(Build, build_id)
            build.status = status
            build.counts = json.dumps(counts)
            build.error = error
            build.parse_failures = json.dumps(failures, ensure_ascii=False)
            build.finished_at = datetime.now(UTC).isoformat()


def describe(names=None, database=None, search=None):
    values = settings()
    store = read_graph(values)
    rows = execute(
        store,
        """MATCH (t:SDATable {generation:$generation})
            WHERE ($names IS NULL OR t.name IN $names OR t.database + '.' + t.name IN $names)
              AND ($database IS NULL OR t.database = $database)
              AND ($search IS NULL OR toLower(t.name) CONTAINS toLower($search))
            OPTIONAL MATCH (t)-[:HAS_FIELD]->(f:SDAField {generation:$generation})
            WITH t, collect(properties(f)) AS fields
            OPTIONAL MATCH (t)-[:HAS_DOCUMENT]->(d:SDADocument {generation:$generation})
            RETURN properties(t) AS table, fields, collect({ref:d.ref, path:d.path, category:d.category}) AS documents ORDER BY table.database,table.name""",
        {
            "generation": values["knowledge_generation"],
            "names": names,
            "database": database,
            "search": search,
        },
    )
    return [
        {
            **r["table"],
            "fields": r["fields"],
            "documents": [d for d in r["documents"] if d["ref"]],
        }
        for r in rows
    ]


def search(query, scope, top_k, category=None):
    values = settings()
    vector = np.array(embed(values, [query])[0], dtype=np.float32)
    candidate_limit = 100 if scope == "tasks" else top_k

    def exact():
        if scope == "docs":
            return []
        store = read_graph(values)
        label = {"tables": "SDATable", "fields": "SDAField", "tasks": "SDATask"}[scope]
        return [
            r["item"]
            for r in execute(
                store,
                f"MATCH (n:{label} {{generation:$generation}}) WHERE toLower(n.name) = toLower($query) RETURN properties(n) AS item LIMIT $limit",
                {
                    "generation": values["knowledge_generation"],
                    "query": query,
                    "limit": candidate_limit,
                },
            )
        ]

    def semantic():
        client = read_vectors(values)
        hits = client.search(
            collection_name=collection_name(values),
            data=[vector.tolist()],
            limit=candidate_limit,
            filter=f'metadata["scope"] == "{scope}" and metadata["generation"] == "{values["knowledge_generation"]}"'
            + (
                f' and metadata["category"] == {json.dumps(category, ensure_ascii=False)}'
                if category is not None
                else ""
            ),
            output_fields=["metadata"],
            search_params={"metric_type": "COSINE"},
        )
        return [hit["entity"]["metadata"] for hit in hits[0]]

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(exact), pool.submit(semantic)
        merged = a.result() + b.result()
    unique = {item["ref"]: item for item in reversed(merged)}
    results = list(reversed(list(unique.values())))
    if scope == "tasks":
        from .task_stage import rank_task_hits

        results = rank_task_hits(results)
    return results[:top_k]
