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


_round_trip_lock = Lock()
_round_trips = {"neo4j": 0, "milvus": 0, "embedding": 0}


def round_trip_counts():
    with _round_trip_lock:
        return dict(_round_trips)


def _count_round_trip(kind):
    with _round_trip_lock:
        _round_trips[kind] += 1


def execute(store, query, parameters):
    _count_round_trip("neo4j")
    result = store.execute_query(query, parameters)
    if not result.get("success"):
        raise RuntimeError("Graph query failed")
    return result["records"]


def embed(values, texts, stats=None):
    _count_round_trip("embedding")
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


# Ranking constants are versioned so clients can interpret scores consistently.
RANKING_VERSION = "1"
RRF_K = 60
SOURCE_WEIGHTS = {"exact": 2.0, "fulltext": 1.0, "vector": 1.0, "graph": 0.5}


def fuse(sources, limit):
    merged = {}
    for source, hits in sources.items():
        for rank, item in enumerate(hits, 1):
            entry = merged.setdefault(
                item["ref"], {**item, "score": 0.0, "sources": []}
            )
            entry["score"] += SOURCE_WEIGHTS[source] / (RRF_K + rank)
            entry["sources"].append(source)
    for entry in merged.values():
        if entry["ref"].startswith("task:") and not entry.get("has_output", False):
            entry["score"] *= 0.5
        entry["type"] = entry["ref"].split(":", 1)[0]
        entry["confidence"] = (
            "high"
            if "exact" in entry["sources"] or len(entry["sources"]) >= 2
            else "medium"
            if "fulltext" in entry["sources"]
            else "low"
        )
    return sorted(merged.values(), key=lambda x: (-x["score"], x["ref"]))[:limit]


def mark_ambiguity(results, query):
    """Flag possible conflicting definitions when different documents mention a term.

    This is deliberately conservative: it signals a need to inspect the cited
    passages, rather than claiming to understand or resolve their definitions.
    """
    candidates = [
        item
        for item in results
        if item.get("doc_id") and query.casefold() in item.get("content", "").casefold()
    ]
    refs = sorted({item["ref"] for item in candidates})
    ambiguous = len({item["doc_id"] for item in candidates}) > 1
    for item in results:
        item["ambiguous"] = ambiguous and item in candidates
        item["ambiguity_refs"] = refs if item["ambiguous"] else []
    return results


def search(query, scope, top_k, category=None):
    values = settings()
    vector = np.array(embed(values, [query])[0], dtype=np.float32)
    generation = values["knowledge_generation"]
    limit = max(50, top_k)
    scopes = ["tables", "fields", "tasks", "docs"] if scope == "all" else [scope]
    params = {
        "generation": generation,
        "query": query,
        "term": re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", query),
        "limit": limit,
        "scopes": scopes,
        "category": category,
    }

    def lexical():
        return execute(
            read_graph(values),
            """CALL {
          MATCH (n:SDAEntity {generation:$generation})
          WHERE ((n:SDATable AND 'tables' IN $scopes) OR (n:SDAField AND 'fields' IN $scopes) OR (n:SDATask AND 'tasks' IN $scopes))
            AND (toLower(n.name) = toLower($query) OR toLower(coalesce(n.database,'') + '.' + n.name) = toLower($query))
            AND $category IS NULL
          RETURN 'exact' AS source, properties(n) AS item LIMIT $limit
          UNION ALL
          CALL db.index.fulltext.queryNodes('sda_knowledge', $term, {limit:$limit}) YIELD node, score
          WHERE node.generation = $generation AND ((node:SDATable AND 'tables' IN $scopes) OR (node:SDAField AND 'fields' IN $scopes) OR (node:SDATask AND 'tasks' IN $scopes) OR (node:SDAChunk AND 'docs' IN $scopes))
            AND ($category IS NULL OR node.category = $category)
          RETURN 'fulltext' AS source, properties(node) AS item ORDER BY score DESC
        } RETURN source, item""",
            params,
        )

    def semantic():
        predicate = 'metadata["generation"] == ' + json.dumps(generation)
        predicate += ' and metadata["scope"] in ' + json.dumps(scopes)
        if category is not None:
            predicate += ' and metadata["category"] == ' + json.dumps(
                category, ensure_ascii=False
            )
        client = read_vectors(values)
        _count_round_trip("milvus")
        hits = client.search(
            collection_name=collection_name(values),
            data=[vector.tolist()],
            limit=limit,
            filter=predicate,
            output_fields=["metadata"],
            search_params={"metric_type": "COSINE"},
        )
        return [hit["entity"]["metadata"] for hit in hits[0]]

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(lexical), pool.submit(semantic)
        rows, vectors = a.result(), b.result()
    sources = {"exact": [], "fulltext": [], "vector": vectors, "graph": []}
    for row in rows:
        sources[row["source"]].append(row["item"])
    seeds = [x["ref"] for x in fuse(sources, 10)]
    if seeds:
        rows = execute(
            read_graph(values),
            """MATCH (seed:SDAEntity {generation:$generation}) WHERE seed.ref IN $seeds
        CALL {
          WITH seed
          MATCH (seed)-[:HAS_FIELD|PRODUCES|DEPENDS_ON]-(n:SDAEntity {generation:$generation})
          RETURN n
          UNION
          WITH seed
          MATCH (seed:SDATable)-[:HAS_DOCUMENT]->(:SDADocument {generation:$generation})-[:HAS_CHUNK]->(n:SDAChunk {generation:$generation})
          RETURN n
          UNION
          WITH seed
          MATCH (seed:SDAChunk)<-[:HAS_CHUNK]-(:SDADocument {generation:$generation})<-[:HAS_DOCUMENT]-(n:SDATable {generation:$generation})
          RETURN n
        }
        WITH n
        WHERE ((n:SDATable AND 'tables' IN $scopes) OR (n:SDAField AND 'fields' IN $scopes) OR (n:SDATask AND 'tasks' IN $scopes) OR (n:SDAChunk AND 'docs' IN $scopes)) AND ($category IS NULL OR n.category = $category)
        RETURN DISTINCT properties(n) AS item LIMIT $limit""",
            {**params, "seeds": seeds},
        )
        sources["graph"] = [row["item"] for row in rows]
    return mark_ambiguity(fuse(sources, limit * 4), query)[:top_k]
