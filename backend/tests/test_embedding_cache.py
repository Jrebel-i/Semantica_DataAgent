"""Cross-build embedding cache behavior (#119)."""

import json
import logging
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from semantica_dataagent import knowledge
from semantica_dataagent.stage_contract import StorageHandles


class Recorder:
    def __init__(self, dimension=3):
        self.dimension = dimension
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, values, texts, stats=None):
        texts = list(texts)
        with self.lock:
            self.calls.append(texts)
            dimension = self.dimension
        return [
            [float(len(text)), float(sum(ord(char) for char in text) % 97)]
            + [0.1] * (dimension - 2)
            for text in texts
        ]


class Vectors:
    def __init__(self):
        self.vectors = {}

    def add_vectors(self, vectors, ids, metadata):
        for vector, identifier in zip(vectors, ids, strict=True):
            self.vectors[identifier] = list(vector)


def settings(tmp_path, **overrides):
    values = {
        "embedding_url": "http://127.0.0.1:1",
        "embedding_model": "model/test",
        "embedding_cache_max_mb": "500",
    }
    values.update(overrides)
    return values


def use_cache(tmp_path, monkeypatch, name="cache.sqlite"):
    path = tmp_path / name
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_EMBEDDING_CACHE_PATH", str(path))
    return path


def add(handles, texts, scope="tables"):
    ids = [f"{scope}:{index}" for index in range(len(texts))]
    handles.add_vectors(ids, texts, [{"scope": scope} for _ in ids])
    return ids


def test_second_add_vectors_hits_cache_without_embedding_calls(tmp_path, monkeypatch):
    cache_path = use_cache(tmp_path, monkeypatch)
    recorder = Recorder()
    monkeypatch.setattr(knowledge, "embed", recorder)

    texts = ["alpha", "beta", "alpha"]
    first_vectors = Vectors()
    first = StorageHandles("generation", object(), first_vectors, settings(tmp_path))
    ids = add(first, texts)
    assert recorder.calls == [["alpha", "beta"]]

    recorder.calls.clear()
    second_vectors = Vectors()
    second = StorageHandles("generation", object(), second_vectors, settings(tmp_path))
    add(second, texts)

    assert recorder.calls == []
    assert second_vectors.vectors == first_vectors.vectors
    assert second.embedding_counts() == {
        "embedding_inputs": 3,
        "embedding_unique": 2,
        "embedding_cache_hits": 2,
        "embedding_cache_misses": 0,
    }
    assert cache_path.is_file()
    assert set(ids) == set(second_vectors.vectors)


def test_cache_key_includes_model_and_dimension(tmp_path, monkeypatch):
    use_cache(tmp_path, monkeypatch)
    recorder = Recorder()
    monkeypatch.setattr(knowledge, "embed", recorder)

    first = StorageHandles(
        "generation",
        object(),
        Vectors(),
        settings(tmp_path, embedding_model="model/a", _embedding_dimension="3"),
    )
    add(first, ["alpha"])
    assert recorder.calls == [["alpha"]]

    repeat = StorageHandles(
        "generation",
        object(),
        Vectors(),
        settings(tmp_path, embedding_model="model/a", _embedding_dimension="3"),
    )
    add(repeat, ["alpha"])
    assert recorder.calls == [["alpha"]]

    recorder.dimension = 3
    other_model = StorageHandles(
        "generation",
        object(),
        Vectors(),
        settings(tmp_path, embedding_model="model/b", _embedding_dimension="3"),
    )
    add(other_model, ["alpha"])
    assert recorder.calls == [["alpha"], ["alpha"]]

    recorder.dimension = 2
    other_dimension = StorageHandles(
        "generation",
        object(),
        Vectors(),
        settings(tmp_path, embedding_model="model/a", _embedding_dimension="2"),
    )
    ids = add(other_dimension, ["alpha"])
    assert recorder.calls == [["alpha"], ["alpha"], ["alpha"]]
    assert len(other_dimension._vectors.vectors[ids[0]]) == 2


def test_corrupt_cache_file_degrades_and_logs(tmp_path, monkeypatch, caplog):
    cache_path = tmp_path / "cache.sqlite"
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_EMBEDDING_CACHE_PATH", str(cache_path))
    cache_path.write_bytes(b"this is not a sqlite database")
    recorder = Recorder()
    monkeypatch.setattr(knowledge, "embed", recorder)

    with caplog.at_level(logging.WARNING):
        handles = StorageHandles("generation", object(), Vectors(), settings(tmp_path))
        ids = add(handles, ["alpha"])

    assert recorder.calls == [["alpha"]]
    assert set(ids) == set(handles._vectors.vectors)
    assert any(
        "embedding cache" in record.message.lower()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    )


def test_read_only_cache_directory_degrades_and_logs(tmp_path, monkeypatch, caplog):
    if os.geteuid() == 0:
        pytest.skip("root can write into a read-only directory")
    cache_dir = tmp_path / "cache-dir"
    cache_dir.mkdir()
    cache_path = cache_dir / "embedding-cache.sqlite"
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_EMBEDDING_CACHE_PATH", str(cache_path))
    cache_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        recorder = Recorder()
        monkeypatch.setattr(knowledge, "embed", recorder)
        with caplog.at_level(logging.WARNING):
            handles = StorageHandles(
                "generation", object(), Vectors(), settings(tmp_path)
            )
            add(handles, ["alpha"])
    finally:
        cache_dir.chmod(stat.S_IRWXU)

    assert recorder.calls == [["alpha"]]
    assert not cache_path.exists()
    assert any(
        "embedding cache" in record.message.lower()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    )


def test_capacity_evicts_least_recently_used_entries(tmp_path, monkeypatch):
    use_cache(tmp_path, monkeypatch)
    recorder = Recorder(dimension=1024)
    monkeypatch.setattr(knowledge, "embed", recorder)
    values = settings(tmp_path, embedding_cache_max_mb="0.005")

    add(StorageHandles("generation", object(), Vectors(), values), ["old"])
    add(StorageHandles("generation", object(), Vectors(), values), ["new"])
    assert recorder.calls == [["old"], ["new"]]

    add(StorageHandles("generation", object(), Vectors(), values), ["old"])
    assert recorder.calls == [["old"], ["new"], ["old"]]


def test_concurrent_add_vectors_do_not_fail(tmp_path, monkeypatch):
    use_cache(tmp_path, monkeypatch)
    recorder = Recorder()
    monkeypatch.setattr(knowledge, "embed", recorder)
    handles = StorageHandles("generation", object(), Vectors(), settings(tmp_path))

    def run(index):
        handles.add_vectors(
            [f"table:{index}"], [f"text-{index}"], [{"scope": "tables"}]
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, index) for index in range(2)]
        for future in futures:
            future.result(timeout=10)

    assert len(handles._vectors.vectors) == 2
    assert sorted(text for call in recorder.calls for text in call) == [
        "text-0",
        "text-1",
    ]


def test_disabled_cache_matches_dedupe_only_behavior(tmp_path, monkeypatch):
    cache_path = use_cache(tmp_path, monkeypatch)
    recorder = Recorder()
    monkeypatch.setattr(knowledge, "embed", recorder)
    values = settings(tmp_path, embedding_cache_max_mb="0")

    first_vectors = Vectors()
    first = StorageHandles("generation", object(), first_vectors, values)
    add(first, ["alpha", "alpha"])
    assert recorder.calls == [["alpha"]]
    assert first.embedding_counts() == {
        "embedding_inputs": 2,
        "embedding_unique": 1,
        "embedding_cache_hits": 0,
        "embedding_cache_misses": 0,
    }

    second_vectors = Vectors()
    second = StorageHandles("generation", object(), second_vectors, values)
    add(second, ["alpha"])
    assert recorder.calls == [["alpha"], ["alpha"]]
    assert first_vectors.vectors["tables:0"] == second_vectors.vectors["tables:0"]
    assert not cache_path.exists()


def test_build_succeeds_with_corrupt_cache_file(tmp_path, monkeypatch, caplog):
    from contextlib import contextmanager

    from cryptography.fernet import Fernet

    from semantica_dataagent import relationships, stages
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.db import Build, sessions
    from semantica_dataagent.stage_contract import StageResult

    cache_path = tmp_path / "cache.sqlite"
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_EMBEDDING_CACHE_PATH", str(cache_path))
    initialize()
    cache_path.write_bytes(b"not a database")
    recording = Recorder()
    monkeypatch.setattr(knowledge, "embed", recording)
    monkeypatch.setattr(knowledge, "configured", list)
    monkeypatch.setattr(
        knowledge,
        "settings",
        lambda: settings(
            tmp_path,
            knowledge_generation="generated",
            source_directory=str(tmp_path),
            neo4j_uri="bolt://localhost",
            neo4j_user="neo4j",
            milvus_uri="http://localhost",
        ),
    )

    @contextmanager
    def storage(values, stats=None):
        yield StorageHandles(
            "generated", object(), Vectors(), values, _embedding_stats=stats
        )

    monkeypatch.setattr(knowledge, "build_storage", storage)
    monkeypatch.setattr(relationships, "reconcile", lambda *args: None)
    monkeypatch.setattr(knowledge, "read_graph", lambda vals: None)
    monkeypatch.setattr(knowledge, "read_vectors", lambda vals: None)

    def stage(records, generation, handles):
        add(handles, ["alpha"])
        return StageResult(counts={"table": 1})

    monkeypatch.setattr(stages, "STAGES", [stage])
    with sessions().begin() as db:
        row = Build(status="running", started_at="now", counts="{}")
        db.add(row)
        db.flush()
        build_id = row.id

    with caplog.at_level(logging.WARNING):
        knowledge.run_build(build_id)

    with sessions()() as db:
        row = db.get(Build, build_id)
        assert row.status == "succeeded", row.error
        counts = json.loads(row.counts)
        assert counts.get("embedding_cache_misses") == 0
        assert counts.get("embedding_cache_hits") == 0
    assert recording.calls == [["alpha"]]
    assert any(
        "embedding cache" in record.message.lower()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    )


def test_build_storage_skips_the_dimension_probe_on_a_cache_hit(tmp_path, monkeypatch):
    use_cache(tmp_path, monkeypatch)
    recorder = Recorder(dimension=512)
    monkeypatch.setattr(knowledge, "embed", recorder)

    class Graph:
        def execute_query(self, query, parameters):
            records = [{"deleted": 0}] if "RETURN count(*) AS deleted" in query else []
            return {"success": True, "records": records}

        def close(self):
            pass

    class Collection:
        def load(self):
            pass

    class Client:
        def disconnect(self):
            pass

    class Vectors:
        def __init__(self):
            self.client = Client()
            self.collection = Collection()
            self.dimensions = []

        def create_collection(self, name, dimension, metric_type):
            self.dimensions.append(dimension)

        def add_vectors(self, vectors, ids, metadata):
            pass

    vectors = Vectors()
    monkeypatch.setattr(knowledge, "graph", lambda _: Graph())
    monkeypatch.setattr(knowledge, "milvus", lambda _: vectors)
    monkeypatch.setattr(knowledge.utility, "has_collection", lambda _: False)
    values = settings(tmp_path, knowledge_generation="cache_probe")

    for _ in range(2):
        with knowledge.build_storage(values):
            pass

    assert recorder.calls == [["knowledge"]]
    assert vectors.dimensions == [512, 512]
