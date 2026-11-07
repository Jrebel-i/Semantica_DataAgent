"""Embedding client behavior against a real local HTTP socket."""

import json
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from semantica_dataagent.embedding_client import (
    EmbeddingClient,
    EmbeddingError,
    EmbeddingStats,
    config_from_values,
)


class FakeEmbedding:
    def __init__(self, actions=None, default=None):
        self.actions = list(actions or [])
        self.default = default or {"status": 200}
        self.dimension = 3
        self.lock = threading.Lock()
        self.requests = 0
        self.active = 0
        self.max_active = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.server = None
        self.thread = None

    def start(self):
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                with state.lock:
                    state.requests += 1
                    state.active += 1
                    state.max_active = max(state.max_active, state.active)
                    state.entered.set()
                try:
                    action = state._next()
                    delay = action.get("delay", 0)
                    if delay:
                        time.sleep(delay)
                    if action.get("gate"):
                        state.release.wait(timeout=10)
                    if action.get("disconnect"):
                        self.connection.setsockopt(
                            socket.SOL_SOCKET,
                            socket.SO_LINGER,
                            struct.pack("ii", 1, 0),
                        )
                        self.connection.close()
                        self.close_connection = True
                        return
                    status = action.get("status", 200)
                    if status == 200:
                        inputs = json.loads(body)["input"]
                        embedding = (
                            [0.1, 0.2, 0.3]
                            if state.dimension == 3
                            else [0.1] * state.dimension
                        )
                        payload = json.dumps(
                            {
                                "data": [
                                    {"index": index, "embedding": embedding}
                                    for index in range(len(inputs))
                                ]
                            }
                        ).encode()
                    else:
                        payload = b'{"detail": "failed"}'
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    try:
                        self.wfile.write(payload)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                finally:
                    with state.lock:
                        state.active -= 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True
        )
        self.thread.start()
        return self

    def _next(self):
        with self.lock:
            if self.actions:
                return self.actions.pop(0)
        return self.default

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def counts(self):
        with self.lock:
            return self.active, self.max_active

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)


@pytest.fixture
def server():
    instance = FakeEmbedding().start()
    try:
        yield instance
    finally:
        instance.release.set()
        instance.stop()


def values(url, **overrides):
    result = {"embedding_url": url, "embedding_model": "test/model"}
    result.update({key: str(value) for key, value in overrides.items()})
    return result


def test_retries_5xx_with_exponential_backoff_and_counts(server):
    server.actions = [{"status": 500}, {"status": 503}, {"status": 200}]
    sleeps = []
    stats = EmbeddingStats()
    client = EmbeddingClient(
        values(server.url, embedding_retries=3, embedding_backoff_seconds=0.25),
        stats=stats,
        sleep=sleeps.append,
    )

    assert client.embed(["a", "b"]) == [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]]
    assert sleeps == [0.25, 0.5]
    assert server.requests == 3
    counts = stats.counts()
    assert counts["embedding_requests"] == 1
    assert counts["embedding_successful_batches"] == 1
    assert counts["embedding_retries"] == 2
    assert counts["embedding_elapsed_ms"] >= 0


def test_retries_a_dropped_connection(server):
    server.actions = [{"disconnect": True}, {"status": 200}]
    sleeps = []
    stats = EmbeddingStats()
    client = EmbeddingClient(
        values(server.url, embedding_retries=1, embedding_backoff_seconds=0.5),
        stats=stats,
        sleep=sleeps.append,
    )

    assert client.embed(["a"]) == [[0.1, 0.2, 0.3]]
    assert sleeps == [0.5]
    assert server.requests == 2
    assert stats.counts()["embedding_retries"] == 1
    assert stats.counts()["embedding_successful_batches"] == 1


def test_timeout_is_retried_then_reports_the_attempt_limit(server):
    server.actions = [{"delay": 0.5}, {"delay": 0.5}]
    stats = EmbeddingStats()
    client = EmbeddingClient(
        values(
            server.url,
            embedding_timeout_seconds=0.1,
            embedding_timeout_per_input_seconds=0,
            embedding_retries=1,
            embedding_backoff_seconds=0,
        ),
        stats=stats,
        sleep=lambda _: None,
    )

    with pytest.raises(EmbeddingError, match="after 2 attempts") as error:
        client.embed(["a"])
    assert "ReadTimeout" in str(error.value)
    assert server.requests == 2
    assert stats.counts()["embedding_requests"] == 1
    assert stats.counts()["embedding_retries"] == 1
    assert stats.counts()["embedding_successful_batches"] == 0


@pytest.mark.parametrize("status", [400, 429])
def test_4xx_is_not_retried(server, status):
    server.actions = [{"status": status}, {"status": 200}]
    sleeps = []
    client = EmbeddingClient(
        values(server.url, embedding_retries=3, embedding_backoff_seconds=0.5),
        sleep=sleeps.append,
    )

    with pytest.raises(EmbeddingError, match=f"HTTP {status}"):
        client.embed(["a"])
    assert sleeps == []
    assert server.requests == 1


def test_timeout_grows_with_the_batch_size(server):
    server.actions = [{"delay": 0.4}, {"delay": 0.4}]
    client = EmbeddingClient(
        values(
            server.url,
            embedding_timeout_seconds=0.1,
            embedding_timeout_per_input_seconds=0.25,
            embedding_retries=0,
            embedding_backoff_seconds=0,
        ),
        sleep=lambda _: None,
    )

    with pytest.raises(EmbeddingError):
        client.embed(["a"])
    assert client.embed(["a", "b"]) == [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]]


def test_concurrent_requests_never_exceed_the_limit(server):
    server.default = {"gate": True}
    client = EmbeddingClient(
        values(
            server.url,
            embedding_concurrency=2,
            embedding_timeout_seconds=5,
            embedding_retries=0,
        )
    )
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(client.embed, [f"text-{index}"]) for index in range(6)]
        assert server.entered.wait(timeout=2)
        deadline = time.monotonic() + 2
        while server.counts()[0] < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.2)
        active, maximum = server.counts()
        assert active == 2
        assert maximum == 2
        server.release.set()
        results = [future.result(timeout=5) for future in futures]
    assert len(results) == 6
    assert server.counts()[1] == 2


def test_two_clients_share_the_process_wide_limit(server):
    server.default = {"gate": True}
    first = EmbeddingClient(values(server.url, embedding_concurrency=1))
    second = EmbeddingClient(values(server.url, embedding_concurrency=1))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(first.embed, ["a"]), pool.submit(second.embed, ["b"])]
        assert server.entered.wait(timeout=2)
        time.sleep(0.2)
        active, maximum = server.counts()
        assert active == 1
        assert maximum == 1
        server.release.set()
        for future in futures:
            assert future.result(timeout=5) == [[0.1, 0.2, 0.3]]


def test_config_defaults_and_validation():
    defaults = config_from_values({})
    assert defaults.concurrency == 2
    assert defaults.timeout_for(1) == 80.0
    assert defaults.timeout_for(32) == 700.0
    for key, value in [
        ("embedding_concurrency", "0"),
        ("embedding_retries", "11"),
        ("embedding_timeout_seconds", "0"),
        ("embedding_backoff_seconds", "-1"),
        ("embedding_concurrency", "many"),
    ]:
        with pytest.raises(ValueError, match=key):
            config_from_values({key: value})


def test_build_record_includes_embedding_statistics(tmp_path, monkeypatch, server):
    from contextlib import contextmanager

    from cryptography.fernet import Fernet

    from semantica_dataagent import knowledge, relationships, stages
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.db import Build, sessions
    from semantica_dataagent.stage_contract import StageResult, StorageHandles

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    initialize()
    monkeypatch.setattr(knowledge, "configured", list)
    monkeypatch.setattr(
        knowledge,
        "settings",
        lambda: values(
            server.url,
            embedding_retries=2,
            embedding_backoff_seconds=0,
            knowledge_generation="generated",
            source_directory=str(tmp_path),
            neo4j_uri="bolt://localhost",
            neo4j_user="neo4j",
            milvus_uri="http://localhost",
        ),
    )

    class Vectors:
        def __init__(self):
            self.rows = []

        def add_vectors(self, vectors, ids, metadata):
            self.rows.append((vectors, ids, metadata))

    vectors = Vectors()

    @contextmanager
    def storage(vals, stats=None):
        yield StorageHandles(
            "generated", object(), vectors, vals, _embedding_stats=stats
        )

    monkeypatch.setattr(knowledge, "build_storage", storage)
    monkeypatch.setattr(relationships, "reconcile", lambda *args: None)
    monkeypatch.setattr(knowledge, "read_graph", lambda vals: None)
    monkeypatch.setattr(knowledge, "read_vectors", lambda vals: None)

    def stage(records, generation, handles):
        handles.add_vectors(["table:one"], ["one table"], [{"scope": "tables"}])
        return StageResult(counts={"table": 1})

    monkeypatch.setattr(stages, "STAGES", [stage])
    server.actions = [{"status": 503}, {"status": 200}]
    with sessions().begin() as db:
        row = Build(status="running", started_at="now", counts="{}")
        db.add(row)
        db.flush()
        build_id = row.id
    knowledge.run_build(build_id)
    with sessions()() as db:
        row = db.get(Build, build_id)
        counts = json.loads(row.counts)
        assert row.status == "succeeded", row.error
        assert counts["table"] == 1
        assert counts["embedding_requests"] == 1
        assert counts["embedding_successful_batches"] == 1
        assert counts["embedding_retries"] == 1
        assert counts["embedding_elapsed_ms"] >= 0
    assert vectors.rows[0][1] == ["table:one"]


@pytest.mark.parametrize("dimension", [512, 1024])
def test_build_storage_uses_the_embedding_dimension(server, monkeypatch, dimension):
    from semantica_dataagent import knowledge

    server.dimension = dimension
    created = {}

    class Graph:
        def execute_query(self, query, parameters):
            records = [{"deleted": 0}] if "RETURN count(*) AS deleted" in query else []
            return {"success": True, "records": records}

        def close(self):
            pass

    class Collection:
        def __init__(self):
            self.loaded = False

        def load(self):
            self.loaded = True

    class Client:
        def disconnect(self):
            pass

    class Vectors:
        def __init__(self):
            self.client = Client()
            self.collection = Collection()
            self.rows = []

        def create_collection(self, name, vector_dimension, metric_type):
            created.update(name=name, dimension=vector_dimension, metric=metric_type)

        def add_vectors(self, vectors, ids, metadata):
            assert len(vectors) == len(ids) == len(metadata)
            assert all(len(vector) == created["dimension"] for vector in vectors)
            self.rows.extend(vectors)

    vectors = Vectors()
    monkeypatch.setattr(knowledge, "graph", lambda _: Graph())
    monkeypatch.setattr(knowledge, "milvus", lambda _: vectors)
    monkeypatch.setattr(knowledge.utility, "has_collection", lambda _: False)
    with knowledge.build_storage(
        values(server.url, knowledge_generation="dimension_test")
    ) as storage:
        storage.add_vectors(["table:one"], ["one table"], [{"scope": "tables"}])

    assert created == {
        "name": "sda_dimension_test",
        "dimension": dimension,
        "metric": "COSINE",
    }
    assert vectors.collection.loaded
    assert len(vectors.rows) == 1
    assert len(vectors.rows[0]) == dimension


@pytest.mark.parametrize("dimension", [512, 1024])
def test_search_embeds_the_query_at_the_configured_dimension(
    server, monkeypatch, dimension
):
    from semantica_dataagent import knowledge

    server.dimension = dimension

    class Vectors:
        def __init__(self):
            self.queries = []

        def search(self, **kwargs):
            self.queries.append(kwargs["data"][0])
            return [[]]

    vectors = Vectors()
    monkeypatch.setattr(
        knowledge,
        "settings",
        lambda: values(server.url, knowledge_generation="dimension_test"),
    )
    monkeypatch.setattr(knowledge, "read_graph", lambda _: object())
    monkeypatch.setattr(knowledge, "execute", lambda *args: [])
    monkeypatch.setattr(knowledge, "read_vectors", lambda _: vectors)

    assert knowledge.search("query", "tables", 5) == []
    assert len(vectors.queries) == 1
    assert len(vectors.queries[0]) == dimension


def test_add_vectors_embeds_each_distinct_text_once(monkeypatch):
    from semantica_dataagent import knowledge
    from semantica_dataagent.stage_contract import StorageHandles

    calls = []

    def fake_embed(values, texts, stats=None):
        calls.append(list(texts))
        return [
            [float(len(text)), float(sum(ord(char) for char in text) % 97)]
            for text in texts
        ]

    class Vectors:
        def __init__(self):
            self.rows = []

        def add_vectors(self, vectors, ids, metadata):
            self.rows.append((vectors, ids, metadata))

    monkeypatch.setattr(knowledge, "embed", fake_embed)
    vectors = Vectors()
    handles = StorageHandles("generation", object(), vectors, {})
    ids = [f"table:{index}" for index in range(40)]
    texts = [f"text-{index}" for index in range(40)]
    texts[10] = "shared"
    texts[11] = "shared"
    texts[32] = texts[0]
    texts[34] = "shared"
    metadata = [{"scope": "tables", "position": index} for index in range(40)]

    handles.add_vectors(ids, texts, metadata)

    embedded = [text for call in calls for text in call]
    assert all(len(call) <= 32 for call in calls)
    assert len(embedded) == len(set(texts))
    assert sorted(embedded) == sorted(set(texts))

    written_ids = [item for _, row_ids, _ in vectors.rows for item in row_ids]
    assert written_ids == ids
    expected = {
        identifier: [
            float(len(text)),
            float(sum(ord(char) for char in text) % 97),
        ]
        for identifier, text in zip(ids, texts, strict=True)
    }
    position = 0
    for row_vectors, row_ids, row_metadata in vectors.rows:
        for vector, identifier, item in zip(
            row_vectors, row_ids, row_metadata, strict=True
        ):
            assert vector == expected[identifier]
            assert item == {
                "scope": "tables",
                "position": position,
                "generation": "generation",
            }
            position += 1
    assert handles.embedding_counts() == {
        "embedding_inputs": 40,
        "embedding_unique": len(set(texts)),
    }


def test_build_counts_and_stage_log_record_embedding_dedupe(
    tmp_path, monkeypatch, server
):
    from contextlib import contextmanager

    from cryptography.fernet import Fernet

    from semantica_dataagent import knowledge, relationships, stages
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.db import Build, sessions
    from semantica_dataagent.stage_contract import StageResult, StorageHandles

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    initialize()
    monkeypatch.setattr(knowledge, "configured", list)
    monkeypatch.setattr(
        knowledge,
        "settings",
        lambda: values(
            server.url,
            embedding_retries=0,
            embedding_backoff_seconds=0,
            knowledge_generation="generated",
            source_directory=str(tmp_path),
            neo4j_uri="bolt://localhost",
            neo4j_user="neo4j",
            milvus_uri="http://localhost",
        ),
    )

    class Vectors:
        def __init__(self):
            self.rows = []

        def add_vectors(self, vectors, ids, metadata):
            self.rows.append((vectors, ids, metadata))

    vectors = Vectors()

    @contextmanager
    def storage(vals, stats=None):
        yield StorageHandles(
            "generated", object(), vectors, vals, _embedding_stats=stats
        )

    monkeypatch.setattr(knowledge, "build_storage", storage)
    monkeypatch.setattr(relationships, "reconcile", lambda *args: None)
    monkeypatch.setattr(knowledge, "read_graph", lambda vals: None)
    monkeypatch.setattr(knowledge, "read_vectors", lambda vals: None)

    def stage(records, generation, handles):
        identifiers = [f"table:{index}" for index in range(40)]
        texts = ["repeated" if index < 30 else "other" for index in range(40)]
        handles.add_vectors(
            identifiers,
            texts,
            [{"scope": "tables"} for _ in identifiers],
        )
        return StageResult(counts={"table": len(identifiers)})

    monkeypatch.setattr(stages, "STAGES", [stage])
    with sessions().begin() as db:
        row = Build(status="running", started_at="now", counts="{}")
        db.add(row)
        db.flush()
        build_id = row.id
    knowledge.run_build(build_id)
    with sessions()() as db:
        row = db.get(Build, build_id)
        counts = json.loads(row.counts)
        logs = json.loads(row.logs)
        assert row.status == "succeeded", row.error
        assert counts["embedding_inputs"] == 40
        assert counts["embedding_unique"] == 2
        assert counts["embedding_requests"] == 1
    completed = [
        log["message"]
        for log in logs
        if log["level"] == "info" and log["message"].startswith("Completed")
    ]
    assert any(
        "embedding_inputs=40" in message and "embedding_unique=2" in message
        for message in completed
    ), completed
    assert len(vectors.rows) == 2
