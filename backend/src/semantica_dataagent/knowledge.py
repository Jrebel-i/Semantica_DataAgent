"""Build and query the current generation of warehouse metadata."""

import fcntl
import hashlib
import json
import os
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from threading import Lock
from urllib.parse import quote, urlsplit

import grpc
import numpy as np
from neo4j import GraphDatabase
from neo4j.exceptions import DatabaseUnavailable, ServiceUnavailable, SessionExpired
from pymilvus import MilvusClient, utility
from pymilvus.client.types import Status
from pymilvus.exceptions import (
    ConnectError,
    MilvusException,
    MilvusUnavailableException,
)
from semantica.graph_store import GraphStore
from semantica.vector_store.milvus_store import MilvusStore

from . import project_context
from .canonical import KINDS
from .db import (
    Build,
    GenerationPointer,
    KnowledgeGeneration,
    Project,
    generation_pointer,
    sessions,
)
from .embedding_client import EmbeddingStats
from .embedding_client import embed as embedding_embed
from .filters import filter_records, get_filters
from .plugins import configured
from .refs import bounded_ref
from .stage_contract import WRITE_BATCH_SIZE, delete_batches
from .storage_errors import StorageUnavailable


def hash_difference(previous, current):
    old, new = set(previous), set(current)
    return {
        "added": new - old,
        "changed": {path for path in old & new if previous[path] != current[path]},
        "deleted": old - new,
    }


def incremental_plan(records, changes):
    selected = changes["added"] | changes["changed"] | changes.get("deleted", set())
    return [
        record
        for record in records
        if (record.source_key or record.source_file) in selected
        or record.source_file in selected
        or selected.intersection(record.source_files)
    ]


def source_file_difference(previous, current, previous_generated, current_generated):
    return hash_difference(
        {k: v for k, v in previous.items() if k not in previous_generated},
        {k: v for k, v in current.items() if k not in current_generated},
    )


def validate_records(records):
    fields = {
        "table": ("database", "name"),
        "field": ("database", "table", "name"),
        "task": ("name",),
        "document": ("category", "path"),
    }
    seen = set()
    for record in records:
        if record.kind in fields:
            key = (record.kind, *(record.data[field] for field in fields[record.kind]))
            if key in seen:
                raise ValueError("Duplicate " + record.kind + " reference")
            seen.add(key)


def settings(project_id=None):
    """Return global settings plus the current project's project-level values."""
    return project_context.current_project(project_id).values


def _project_settings(project_id=None):
    if project_id in (None, project_context.DEFAULT_PROJECT_ID):
        return settings()
    return settings(project_id)


def _close_quietly(resource):
    if resource is None:
        return
    for name in ("close", "disconnect"):
        close = getattr(resource, name, None)
        if callable(close):
            with suppress(Exception):
                close()
            return


def _neo4j_unreachable(values):
    driver = None
    try:
        driver = GraphDatabase.driver(
            values["neo4j_uri"],
            auth=(values["neo4j_user"], values["neo4j_password"]),
            connection_timeout=2,
        )
        driver.verify_connectivity()
    except (
        ServiceUnavailable,
        SessionExpired,
        DatabaseUnavailable,
        OSError,
        TimeoutError,
    ):
        return True
    except Exception:  # noqa: BLE001 - any other failure is not a connectivity outage
        return False
    finally:
        _close_quietly(driver)
    return False


_GRPC_UNAVAILABLE = {
    grpc.StatusCode.UNAVAILABLE,
    grpc.StatusCode.DEADLINE_EXCEEDED,
}


def _milvus_unavailable(exc):
    if isinstance(
        exc,
        (
            TimeoutError,
            ConnectionError,
            ConnectError,
            MilvusUnavailableException,
        ),
    ):
        return True
    if isinstance(exc, MilvusException):
        code = getattr(exc, "code", None)
        if callable(code):
            with suppress(Exception):
                code = code()
        if code == Status.CONNECT_FAILED or code in _GRPC_UNAVAILABLE:
            return True
    if isinstance(exc, grpc.RpcError):
        with suppress(Exception):
            return exc.code() in _GRPC_UNAVAILABLE
    return False


def _unavailable(exc):
    """Return True when the failure chain shows a service that cannot be reached."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(
            exc,
            (
                ServiceUnavailable,
                SessionExpired,
                DatabaseUnavailable,
                OSError,
                TimeoutError,
            ),
        ):
            return True
        if _milvus_unavailable(exc):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _neo4j_reachable(store):
    """Ask the live driver whether Neo4j can still be reached."""
    driver = getattr(getattr(store, "_store_backend", None), "_driver", None)
    verify = getattr(driver, "verify_connectivity", None)
    if not callable(verify):
        return True
    try:
        return verify() is not False
    except Exception:  # noqa: BLE001 - a failed probe means the store is unreachable
        return False


def graph(values):
    store = GraphStore(
        backend="neo4j",
        uri=values["neo4j_uri"],
        user=values["neo4j_user"],
        password=values["neo4j_password"],
    )
    try:
        store.connect()
    except Exception as exc:
        _close_quietly(store)
        if not _unavailable(exc) and not _neo4j_unreachable(values):
            raise
        raise StorageUnavailable("Neo4j storage is unavailable") from exc
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
        client = None
        try:
            client = MilvusClient(uri=key[0])
            client.load_collection(key[1])
        except Exception as exc:
            _close_quietly(client)
            if not _unavailable(exc):
                raise
            raise StorageUnavailable("Milvus storage is unavailable") from exc
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
    try:
        result = store.execute_query(query, parameters)
    except Exception as exc:
        if not _unavailable(exc) and _neo4j_reachable(store):
            raise
        raise StorageUnavailable("Neo4j storage is unavailable") from exc
    if not result.get("success"):
        raise RuntimeError("Graph query failed")
    return result["records"]


def embed(values, texts, stats=None):
    _count_round_trip("embedding")
    return embedding_embed(values, texts, stats)


def collection_name(values):
    return project_context.from_values(values).collection_name()


_milvus_connection_lock = Lock()
_milvus_users = 0


def milvus(values):
    global _milvus_users
    uri = urlsplit(values["milvus_uri"])
    store = MilvusStore(
        host=uri.hostname, port=uri.port or (443 if uri.scheme == "https" else 19530)
    )
    with _milvus_connection_lock:
        try:
            store.connect(secure=uri.scheme == "https")
        except Exception as exc:
            if not _milvus_users:
                _close_quietly(getattr(store, "client", None))
            if not _unavailable(exc):
                raise
            raise StorageUnavailable("Milvus storage is unavailable") from exc
        _milvus_users += 1
    return store


def release_milvus(store):
    """Disconnect the shared default Milvus alias only after the last build."""
    global _milvus_users
    with _milvus_connection_lock:
        _milvus_users = max(0, _milvus_users - 1)
        if _milvus_users:
            return
        _close_quietly(getattr(store, "client", None))


def reference(kind, data, counts=None):
    key = ".".join(
        quote(str(data[k]), safe="").replace(".", "%2E")
        for k in (
            ("database", "name") if kind == "table" else ("database", "table", "name")
        )
    )
    return bounded_ref(f"{kind}:{key}", counts)


def ensure_build_indexes(store):
    """Prepare lookup indexes before either kind of build touches graph data."""
    field = project_context.label("Field")
    table = project_context.label("Table")
    task = project_context.label("Task")
    document = project_context.label("Document")
    entity = project_context.label("Entity")
    category = project_context.label("Category")
    lineage = project_context.label("LineageTable")
    indexes = (
        (
            project_context.index_name("field_generation_name"),
            field,
            ("generation", "name"),
        ),
        (
            project_context.index_name("field_generation_location"),
            field,
            ("generation", "database", "table", "name"),
        ),
        (
            project_context.index_name("field_generation_table"),
            field,
            ("generation", "database", "table"),
        ),
        (
            project_context.index_name("table_generation_location"),
            table,
            ("generation", "database", "name"),
        ),
        (
            project_context.index_name("table_generation_name"),
            table,
            ("generation", "name"),
        ),
        (
            project_context.index_name("task_generation_ref"),
            task,
            ("generation", "ref"),
        ),
        (
            project_context.index_name("document_generation_ref"),
            document,
            ("generation", "ref"),
        ),
        (
            project_context.index_name("entity_generation_ref"),
            entity,
            ("generation", "ref"),
        ),
        (project_context.index_name("entity_generation"), entity, ("generation",)),
        (
            project_context.index_name("category_generation_name"),
            category,
            ("generation", "name"),
        ),
        (
            project_context.index_name("lineage_generation_name"),
            lineage,
            ("generation", "name"),
        ),
    )
    for name, label, properties in indexes:
        columns = ", ".join(f"n.{prop}" for prop in properties)
        execute(
            store,
            f"CREATE INDEX {name} IF NOT EXISTS FOR (n:{label}) ON ({columns})",
            {},
        )
    execute(store, "CALL db.awaitIndexes(3600)", {})


@contextmanager
def build_storage(values, embedding_stats=None):
    """Initialize the generation once, then share append-only stage handles."""
    from .embedding_cache import open_embedding_cache
    from .stage_contract import StorageHandles

    stats = embedding_stats if embedding_stats is not None else EmbeddingStats()
    project = project_context.from_values(values)
    generation = project.generation
    model = values.get("embedding_model") or "model/bge-m3"
    cache = open_embedding_cache(values, model=model)
    dimension = cache.dimension if cache is not None and cache.enabled else None
    if dimension is None:
        dimension = len(embed(values, ["knowledge"], stats)[0])
        if cache is not None and cache.enabled:
            cache.remember_dimension(dimension)
    graph_store = graph(values)
    try:
        ensure_build_indexes(graph_store)
        vector_store = milvus(values)
        try:
            if values.get("_build_id"):
                with sessions().begin() as db:
                    db.get(Build, values["_build_id"]).affected_files = json.dumps(
                        {"full_started": True}
                    )
            delete_batches(
                lambda query, params: execute(graph_store, query, params),
                "MATCH (n:SDAEntity {generation:$generation})",
                "n",
                {"generation": generation},
                detach=True,
            )
            name = collection_name(values)
            if utility.has_collection(name):
                utility.drop_collection(name)
            vector_store.create_collection(name, dimension, metric_type="COSINE")
            yield StorageHandles(
                generation,
                graph_store,
                vector_store,
                values,
                _embedding_stats=stats,
                _embedding_cache=cache,
                _embedding_model=model,
                _embedding_dimension=dimension,
            )
            vector_store.collection.load()
        finally:
            release_milvus(vector_store)
    finally:
        graph_store.close()


@contextmanager
def incremental_storage(values, embedding_stats=None):
    from .embedding_cache import open_embedding_cache
    from .stage_contract import StorageHandles

    stats = embedding_stats if embedding_stats is not None else EmbeddingStats()
    model = values.get("embedding_model") or "model/bge-m3"
    cache = open_embedding_cache(values, model=model)
    dimension = cache.dimension if cache is not None and cache.enabled else None
    graph_store = graph(values)
    try:
        ensure_build_indexes(graph_store)
        vector_store = milvus(values)
        try:
            vector_store.get_collection(collection_name(values))
            yield StorageHandles(
                project_context.from_values(values).generation,
                graph_store,
                vector_store,
                values,
                _embedding_stats=stats,
                _embedding_cache=cache,
                _embedding_model=model,
                _embedding_dimension=dimension,
            )
            vector_store.collection.load()
        finally:
            release_milvus(vector_store)
    finally:
        graph_store.close()


def read_sources(values, plugins):
    directory = project_context.from_values(values).source_directory
    lock = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(lock, fcntl.LOCK_SH)
        records, hashes, adapter_stats = [], {}, {}
        from .project_types import supports_project_type

        project_type = values.get("_project_type") or project_context.project_type(
            values.get("_project_id")
        )
        for category, plugin, enabled, parameters in plugins:
            if category != "source" or not enabled:
                continue
            if not supports_project_type(plugin, project_type):
                continue
            result = plugin.read(directory, parameters)
            adapter_stats.update(
                {f"{plugin.id}_{key}": value for key, value in result.stats.items()}
            )
            from .source_directory import relative, safe

            for path, digest in result.hashes.items():
                if safe(directory, relative(path)).is_file() is False and not any(
                    record.source_key == path for record in result.records
                ):
                    raise ValueError("Source hash does not identify a file")
                if (
                    relative(path).as_posix() != path
                    or not isinstance(digest, str)
                    or not digest
                ):
                    raise ValueError("Invalid source file hash")
                if path in hashes and hashes[path] != digest:
                    raise ValueError("Source adapters disagree on a file hash")
                hashes[path] = digest
            for record in result.records:
                if (
                    not record.source_file
                    or record.source_file not in result.hashes
                    or any(path not in result.hashes for path in record.source_files)
                    or (record.source_key or record.source_file) not in result.hashes
                ):
                    raise ValueError(
                        f"Source adapter {plugin.id} did not report record provenance"
                    )
            records.extend(result.records)
        commit = None
        if (directory / ".git").is_dir():
            from .source_directory import git

            try:
                head = git(directory, "rev-parse", "--verify", "HEAD").decode().strip()
            except ValueError:
                head = None
            if head is not None and not git(
                directory, "status", "--porcelain", "--untracked-files=all"
            ):
                commit = head
        return records, hashes, commit, adapter_stats
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)


def dimension_sources(records):
    result = {}
    for record in records:
        if record.kind == "dimension_value":
            table = [record.data["database"], record.data["table"]]
            for path in dict.fromkeys((record.source_file, *record.source_files)):
                if path is None:
                    continue
                tables = result.setdefault(path, [])
                if table not in tables:
                    tables.append(table)
    return result


def _load_logs(value):
    try:
        logs = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return logs if isinstance(logs, list) else []


def _record_log(build_id, logs, stage, message, level="info"):
    logs.append(
        {
            "timestamp": datetime.now(UTC).isoformat(),
            "stage": stage,
            "level": level,
            "message": message,
        }
    )
    with sessions().begin() as db:
        row = db.get(Build, build_id)
        if row is not None:
            row.logs = json.dumps(logs, ensure_ascii=False)


def _stage_name(stage):
    module = stage.__module__.rsplit(".", 1)[-1]
    return module.removesuffix("_stage")


def run_build(build_id):
    status, counts, error = "failed", {}, None
    failures = []
    new_values = None
    published = False
    stats = EmbeddingStats()
    logs = []
    try:
        with sessions().begin() as db:
            build = db.get(Build, build_id)
            if build is None:
                raise ValueError("Build record not found")
            logs = _load_logs(build.logs)
            build_type = build.build_type or "full"
        _record_log(build_id, logs, "build", f"{build_type} build started")
        with sessions()() as db:
            row = db.get(Build, build_id)
            project_id = (row.project_id if row is not None else None) or "default"
            project_row = db.get(Project, project_id)
            if project_row is None:
                raise ValueError(f"Project {project_id} not found")
            project_type = project_row.type
        values = (
            settings()
            if project_id == project_context.DEFAULT_PROJECT_ID
            else settings(project_id)
        )
        values["_project_type"] = project_type
        project = project_context.from_values(values, project_id)
        generation = project.generation
        with sessions().begin() as db:
            if generation_pointer(db, project.project_id) is None:
                if db.get(KnowledgeGeneration, generation) is None:
                    db.add(
                        KnowledgeGeneration(
                            name=generation, project_id=project.project_id
                        )
                    )
                db.add(
                    GenerationPointer(
                        project_id=project.project_id, generation=generation
                    )
                )
        if build_type == "full":
            generation = project.new_generation()
            values = project_context.with_generation(values, generation)
            project = project_context.from_values(values)
            new_values = values.copy()
        plugins = configured(
            project.project_id,
            project_type=project_type,
        )
        filters = get_filters()
        config = {
            project_context.SOURCE_DIRECTORY_KEY: str(project.source_directory),
            "neo4j_uri": values["neo4j_uri"],
            "neo4j_user": values["neo4j_user"],
            "milvus_uri": values["milvus_uri"],
            "plugins": [
                (category, plugin.id, plugin.version, parameters)
                for category, plugin, enabled, parameters in plugins
                if enabled and category in {"source", "parser"}
            ],
            "filters": filters,
            project_context.DIMENSION_VALUE_LIMIT_KEY: project.dimension_value_limit,
            "embedding_model": values["embedding_model"],
            "embedding_url": values["embedding_url"],
        }
        config_hash = hashlib.sha256(
            json.dumps(config, sort_keys=True).encode()
        ).hexdigest()
        with sessions().begin() as db:
            build = db.get(Build, build_id)
            build.generation = generation
            build.config_hash = config_hash
        records, hashes, commit, adapter_stats = read_sources(values, plugins)
        records, filtered = filter_records(records, filters)
        from .table_stage import apply_overrides

        records = apply_overrides(records, project.project_id)
        validate_records(records)
        _record_log(
            build_id,
            logs,
            "source",
            f"Read {len(records)} records from {len(hashes)} source files",
        )
        dimensions = dimension_sources(records)
        generated = {
            r.source_key for r in records if r.source_key and r.data.get("generated")
        }
        with sessions().begin() as db:
            build = db.get(Build, build_id)
            build.generation = generation
            build.source_commit = commit
            build.source_hashes = json.dumps(hashes, sort_keys=True)
            build.dimension_tables = json.dumps(dimensions, sort_keys=True)
            build.generated_files = json.dumps(sorted(generated))
            build.config_hash = config_hash
            build_type = build.build_type or "full"
            previous = (
                db.query(Build)
                .filter(
                    Build.id < build_id,
                    Build.project_id == project.project_id,
                    Build.generation == generation,
                    Build.status == "succeeded",
                    Build.source_hashes.is_not(None),
                )
                .order_by(Build.id.desc())
                .first()
            )
            if build_type == "incremental" and previous is None:
                raise ValueError(
                    "A successful full build is required before incremental builds"
                )
            if build_type == "incremental" and previous.config_hash != config_hash:
                raise ValueError("Build configuration changed; run a full build")
            previous_hashes = json.loads(previous.source_hashes) if previous else {}
            previous_generated = (
                set(json.loads(previous.generated_files or "[]")) if previous else set()
            )
            prior_dimensions = (
                json.loads(previous.dimension_tables or "{}") if previous else {}
            )
            failed_builds = (
                db.query(Build)
                .filter(
                    Build.project_id == project.project_id,
                    Build.generation == generation,
                    Build.id > (previous.id if previous else 0),
                    Build.id < build_id,
                    Build.status == "failed",
                )
                .all()
            )
            if build_type == "incremental" and any(
                (row.build_type != "incremental" and row.affected_files)
                or (
                    row.affected_files
                    and row.config_hash
                    and row.config_hash != config_hash
                )
                for row in failed_builds
            ):
                raise ValueError(
                    "A full build failed; run a full build to repair the generation"
                )
            dirty = set()
            old_ids = set()
            dirty_tables = set()
            for failed in failed_builds:
                if failed.affected_files:
                    saved = json.loads(failed.affected_files)
                    dirty.update(saved.get("files", []))
                    old_ids.update(saved.get("vector_ids", []))
                    dirty_tables.update(
                        tuple(table) for table in saved.get("dimension_tables", [])
                    )
        changes = hash_difference(previous_hashes, hashes)
        counts = dict.fromkeys(KINDS.values(), 0)
        counts.update(
            {
                "embedding_inputs": 0,
                "embedding_unique": 0,
                "embedding_cache_hits": 0,
                "embedding_cache_misses": 0,
            }
        )
        counts.update(adapter_stats)
        physical_changes = source_file_difference(
            previous_hashes, hashes, previous_generated, generated
        )
        counts.update({name: len(paths) for name, paths in physical_changes.items()})
        counts["filtered"] = filtered
        _record_log(
            build_id,
            logs,
            "plan",
            "Files: "
            + ", ".join(
                f"{name}={len(paths)}"
                for name, paths in sorted(physical_changes.items())
            )
            + f"; filtered={filtered}",
        )
        touched = set().union(*changes.values()) | dirty
        selected = (
            records
            if build_type == "full"
            else incremental_plan(
                records, {"added": touched, "changed": set(), "deleted": set()}
            )
        )
        counts.update(Counter(r.kind for r in selected))
        for path in touched:
            dirty_tables.update(
                tuple(table) for table in prior_dimensions.get(path, [])
            )
            dirty_tables.update(tuple(table) for table in dimensions.get(path, []))
        if build_type == "incremental" and dirty_tables:
            selected = [r for r in selected if r.kind != "dimension_value"] + [
                r
                for r in records
                if r.kind == "dimension_value"
                and (r.data["database"], r.data["table"]) in dirty_tables
            ]
        from .relationships import reconcile
        from .stages import STAGES

        values["_build_id"] = build_id
        manager = (
            build_storage(values, stats)
            if build_type == "full"
            else incremental_storage(values, stats)
        )
        with manager as storage:
            storage.build_id = build_id
            storage.all_records = records
            storage.is_incremental = build_type == "incremental"
            if build_type == "incremental":
                params = {
                    "generation": generation,
                    "files": sorted(touched),
                    "tables": [
                        {"database": db, "table": table}
                        for db, table in sorted(dirty_tables)
                    ],
                }
                found = storage.execute(
                    "MATCH (n:SDAEntity {generation:$generation}) WHERE n.source_file IN $files OR n.source_key IN $files OR any(s IN coalesce(n.source_files, []) WHERE s IN $files) OR (n:SDADimensionValue AND any(t IN $tables WHERE t.database = n.database AND t.table = n.table)) RETURN n.ref AS ref",
                    params,
                )
                old_ids.update(row["ref"] for row in found if row["ref"])
                with sessions().begin() as db:
                    db.get(Build, build_id).affected_files = json.dumps(
                        {
                            "files": sorted(touched),
                            "vector_ids": sorted(old_ids),
                            "dimension_tables": sorted(dirty_tables),
                        }
                    )
                delete_batches(
                    storage.execute,
                    "MATCH (:SDATable {generation:$generation})-[r:DEPENDS_ON]->(:SDATable {generation:$generation}) WHERE r.source_file IN $files",
                    "r",
                    {"generation": generation, "files": sorted(touched)},
                )
                delete_batches(
                    storage.execute,
                    "MATCH (n:SDAEntity {generation:$generation}) WHERE n.source_file IN $files OR n.source_key IN $files OR any(s IN coalesce(n.source_files, []) WHERE s IN $files) OR (n:SDADimensionValue AND any(t IN $tables WHERE t.database = n.database AND t.table = n.table))",
                    "n",
                    params,
                    detach=True,
                )
                vector_ids = sorted(old_ids)
                for offset in range(0, len(vector_ids), WRITE_BATCH_SIZE):
                    storage._vectors.delete_vectors(
                        vector_ids[offset : offset + WRITE_BATCH_SIZE]
                    )
                if dirty_tables:
                    storage.execute(
                        "UNWIND $rows AS row MATCH (f:SDAField {generation:$generation, database:row.database, table:row.table}) REMOVE f.dimension_count, f.dimension_too_many, f.dimension_samples",
                        {"generation": generation, "rows": params["tables"]},
                    )
            for stage in STAGES:
                if (
                    build_type == "incremental"
                    and stage.__module__.endswith("dimension_stage")
                    and not dirty_tables
                    and not any(record.kind == "field" for record in selected)
                ):
                    continue
                stage_started = time.monotonic()
                embedding_before = storage.embedding_counts()
                result = stage(selected, generation, storage)
                elapsed = time.monotonic() - stage_started
                embedding_after = storage.embedding_counts()
                stage_counts = dict(result.counts)
                for key in (
                    "embedding_inputs",
                    "embedding_unique",
                    "embedding_cache_hits",
                    "embedding_cache_misses",
                ):
                    delta = embedding_after[key] - embedding_before[key]
                    stage_counts[key] = delta
                    counts[key] += delta
                counts.update(result.counts)
                failures.extend(result.failures)
                summary = ", ".join(
                    f"{key}={value}" for key, value in sorted(stage_counts.items())
                )
                if result.errors:
                    _record_log(
                        build_id,
                        logs,
                        _stage_name(stage),
                        f"Failed after {elapsed:.3f}s: " + "; ".join(result.errors),
                        "error",
                    )
                else:
                    _record_log(
                        build_id,
                        logs,
                        _stage_name(stage),
                        f"Completed in {elapsed:.3f}s"
                        + (f": {summary}" if summary else ""),
                    )
                if result.errors:
                    raise ValueError("; ".join(result.errors))
            reconcile(storage, records, generation)
            _record_log(build_id, logs, "relationships", "Reconciled relationships")
        read_graph(values)
        read_vectors(values)
        _record_log(build_id, logs, "build", "Build succeeded")
        if build_type == "full":
            with sessions().begin() as db:
                db.add(
                    KnowledgeGeneration(
                        name=generation,
                        project_id=project.project_id,
                        build_id=build_id,
                        created_at=datetime.now(UTC).isoformat(),
                    )
                )
                pointer = generation_pointer(db, project.project_id)
                if pointer is None:
                    db.add(
                        GenerationPointer(
                            project_id=project.project_id, generation=generation
                        )
                    )
                else:
                    pointer.generation = generation
                build = db.get(Build, build_id)
                build.status = "succeeded"
                build.counts = json.dumps({**counts, **stats.counts()})
                build.error = None
                build.parse_failures = json.dumps(failures, ensure_ascii=False)
                build.finished_at = datetime.now(UTC).isoformat()
            published = True
            new_values = None
        if build_type == "full":
            try:
                prune_generations(values)
            except Exception as exc:  # noqa: BLE001 - retain metadata for retry
                _record_log(
                    build_id, logs, "retention", f"Cleanup failed: {exc}", "error"
                )
        status = "succeeded"
    except Exception as exc:  # noqa: BLE001 - record failure of background work
        error = str(exc)
        if published:
            status = "succeeded"
        if new_values is not None:
            try:
                delete_generation_data(new_values)
            except Exception as cleanup_error:  # noqa: BLE001
                error += f"; cleanup failed: {cleanup_error}"
        with suppress(Exception):
            _record_log(build_id, logs, "build", f"Build failed: {error}", "error")
    finally:
        counts.update(stats.counts())
        with sessions().begin() as db:
            build = db.get(Build, build_id)
            if build is not None and not published:
                build.status = status
                build.counts = json.dumps(counts)
                build.error = error
                build.parse_failures = json.dumps(failures, ensure_ascii=False)
                build.finished_at = datetime.now(UTC).isoformat()
                if status == "succeeded" and build.build_type == "incremental":
                    generation_row = db.get(KnowledgeGeneration, build.generation)
                    if generation_row is not None:
                        generation_row.build_id = build_id


def prune_generations(values):
    project = project_context.from_values(values)
    retention = project.generation_retention
    with sessions()() as db:
        pointer = generation_pointer(db, project.project_id)
        rows = (
            db.query(KnowledgeGeneration)
            .filter(KnowledgeGeneration.project_id == project.project_id)
            .order_by(KnowledgeGeneration.created_at.desc())
            .all()
        )
        keep = {pointer.generation} if pointer is not None else set()
        for row in rows:
            if len(keep) >= retention:
                break
            keep.add(row.name)
        expired = [row.name for row in rows if row.name not in keep]
    for name in expired:
        from sqlalchemy import select, update

        with sessions().begin() as db:
            claimed = db.execute(
                update(KnowledgeGeneration)
                .where(KnowledgeGeneration.name == name)
                .where(KnowledgeGeneration.project_id == project.project_id)
                .where(
                    KnowledgeGeneration.name
                    != select(GenerationPointer.generation)
                    .where(GenerationPointer.project_id == project.project_id)
                    .scalar_subquery()
                )
                .values(deleting=True)
            ).rowcount
        if not claimed:
            continue
        delete_generation_data(project_context.with_generation(values, name))
        with sessions().begin() as db:
            row = db.get(KnowledgeGeneration, name)
            pointer = generation_pointer(db, project.project_id)
            if row and (pointer is None or pointer.generation != name):
                db.delete(row)


def cleanup_interrupted_generations():
    """Retry cleanup until both stores have removed each unpublished generation."""
    import logging

    interrupted = "Build interrupted by application shutdown"
    with sessions()() as db:
        candidates = [
            (row.id, row.generation, row.project_id or "default")
            for row in db.query(Build).filter(
                Build.status == "failed",
                Build.error == interrupted,
                Build.build_type == "full",
                Build.generation.is_not(None),
            )
            if db.get(KnowledgeGeneration, row.generation) is None
            and not (
                generation_pointer(db, row.project_id or "default")
                and generation_pointer(db, row.project_id or "default").generation
                == row.generation
            )
        ]
    for build_id, generation, project_id in candidates:
        try:
            values = _project_settings(project_id)
            delete_generation_data(project_context.with_generation(values, generation))
            with sessions().begin() as db:
                row = db.get(Build, build_id)
                if row and row.error == interrupted:
                    row.error = interrupted + "; unpublished storage cleaned"
        except Exception:  # retry on next startup
            logging.getLogger(__name__).exception(
                "Could not clean interrupted generation %s", generation
            )


def delete_generation_data(values):
    project = project_context.from_values(values)
    store = graph(values)
    try:
        delete_batches(
            lambda query, params: execute(store, query, params),
            "MATCH (n:SDAEntity {generation:$generation})",
            "n",
            {"generation": project.generation},
            detach=True,
        )
    finally:
        store.close()
    client = MilvusClient(uri=values["milvus_uri"])
    try:
        name = collection_name(values)
        if client.has_collection(name):
            client.drop_collection(name)
    finally:
        client.close()


def existing_refs(refs, project_id=None):
    """Return the evidence ids present in the current knowledge generation."""
    wanted = sorted({ref for ref in refs if ref})
    if not wanted:
        return set()
    values = _project_settings(project_id)
    project = project_context.from_values(values)
    rows = execute(
        read_graph(values),
        """MATCH (n:SDAEntity {generation:$generation})
        WHERE n.ref IN $refs
        RETURN DISTINCT n.ref AS ref""",
        {"generation": project.generation, "refs": wanted},
    )
    return {row["ref"] for row in rows if row.get("ref")}


def describe(names=None, database=None, search=None, summary=False, project_id=None):
    values = _project_settings(project_id)
    project = project_context.from_values(values)
    store = read_graph(values)
    filters = """MATCH (t:SDATable {generation:$generation})
            WHERE ($names IS NULL OR t.name IN $names OR t.database + '.' + t.name IN $names)
              AND ($database IS NULL OR t.database = $database)
              AND ($search IS NULL OR toLower(t.name) CONTAINS toLower($search))"""
    parameters = {
        "generation": project.generation,
        "names": names,
        "database": database,
        "search": search,
    }
    if summary:
        rows = execute(
            store,
            filters
            + """
            OPTIONAL MATCH (t)-[:HAS_FIELD]->(f:SDAField {generation:$generation})
            WITH t, count(f) AS field_count,
                 sum(CASE WHEN f.partition = true THEN 1 ELSE 0 END) AS partition_count
            OPTIONAL MATCH (t)-[:HAS_DOCUMENT]->(d:SDADocument {generation:$generation})
            RETURN properties(t) AS table, field_count,
                   count(d) AS document_count,
                   partition_count > 0 AS has_partition
            ORDER BY table.database,table.name""",
            parameters,
        )
        return [
            {
                "name": row["table"]["name"],
                "database": row["table"]["database"],
                "comment": row["table"].get("comment", ""),
                "ref": row["table"]["ref"],
                "field_count": row["field_count"],
                "document_count": row["document_count"],
                "has_partition": bool(row["has_partition"]),
            }
            for row in rows
        ]
    rows = execute(
        store,
        filters
        + """
            OPTIONAL MATCH (t)-[:HAS_FIELD]->(f:SDAField {generation:$generation})
            WITH t, collect(properties(f)) AS fields
            OPTIONAL MATCH (t)-[:HAS_DOCUMENT]->(d:SDADocument {generation:$generation})
            RETURN properties(t) AS table, fields, collect({ref:d.ref, path:d.path, category:d.category}) AS documents ORDER BY table.database,table.name""",
        parameters,
    )
    return [
        {
            **r["table"],
            "fields": r["fields"],
            "documents": [d for d in r["documents"] if d["ref"]],
        }
        for r in rows
    ]


def dimension_values(database, table, field, project_id=None):
    """Return every indexed value for one field in the current generation."""
    values = _project_settings(project_id)
    project = project_context.from_values(values)
    rows = execute(
        read_graph(values),
        """MATCH (v:SDADimensionValue {generation:$generation})
           WHERE v.database = $database AND v.table = $table AND v.field = $field
           RETURN v.value AS value ORDER BY v.value""",
        {
            "generation": project.generation,
            "database": database,
            "table": table,
            "field": field,
        },
    )
    return [row["value"] for row in rows if isinstance(row.get("value"), str)]


# Ranking constants are versioned so clients can interpret scores consistently.
RANKING_VERSION = "3"
RRF_K = 60
SOURCE_WEIGHTS = {"exact": 2.0, "fulltext": 1.0, "vector": 1.0, "graph": 0.5}


def fuse(sources, limit, weights=None):
    weights = SOURCE_WEIGHTS if weights is None else weights
    merged = {}
    for source, hits in sources.items():
        for rank, item in enumerate(hits, 1):
            entry = merged.setdefault(
                item["ref"], {**item, "score": 0.0, "sources": []}
            )
            entry["score"] += weights[source] / (RRF_K + rank)
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


def search(query, scope, top_k, category=None, project_id=None):
    values = _project_settings(project_id)
    project = project_context.from_values(values)
    if (
        project.project_id != project_context.DEFAULT_PROJECT_ID
        and not project_context.generation_is_built(
            project.project_id, project.generation
        )
    ):
        return []
    vector = np.array(embed(values, [query])[0], dtype=np.float32)
    generation = project.generation
    limit = max(50, top_k)
    scopes = ["tables", "fields", "tasks", "docs"] if scope == "all" else [scope]
    search_scopes = ["tables", "docs"] if scope == "tables" else scopes
    params = {
        "generation": generation,
        "query": query,
        "term": re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", query),
        "limit": limit,
        "scopes": search_scopes,
        "category": category,
    }

    def lexical():
        lexical_query = """CALL {
          MATCH (n:SDAEntity {generation:$generation})
          WHERE ((n:SDATable AND 'tables' IN $scopes) OR (n:SDAField AND 'fields' IN $scopes) OR (n:SDATask AND 'tasks' IN $scopes))
            AND (toLower(n.name) = toLower($query) OR toLower(coalesce(n.database,'') + '.' + n.name) = toLower($query))
            AND $category IS NULL
          RETURN 'exact' AS source, properties(n) AS item LIMIT $limit
          UNION ALL
          CALL db.index.fulltext.queryNodes('__KNOWLEDGE_INDEX__', $term, {limit:$limit}) YIELD node, score
          WHERE node.generation = $generation AND ((node:SDATable AND 'tables' IN $scopes) OR (node:SDAField AND 'fields' IN $scopes) OR (node:SDATask AND 'tasks' IN $scopes) OR (node:SDAChunk AND 'docs' IN $scopes))
            AND ($category IS NULL OR node.category = $category)
          RETURN 'fulltext' AS source, properties(node) AS item ORDER BY score DESC
        } RETURN source, item"""
        return execute(
            read_graph(values),
            lexical_query.replace(
                "__KNOWLEDGE_INDEX__", project_context.index_name("knowledge")
            ),
            params,
        )

    def semantic():
        predicate = 'metadata["generation"] == ' + json.dumps(generation)
        predicate += ' and metadata["scope"] in ' + json.dumps(search_scopes)
        if category is not None:
            predicate += ' and metadata["category"] == ' + json.dumps(
                category, ensure_ascii=False
            )
        client = read_vectors(values)
        _count_round_trip("milvus")
        try:
            hits = client.search(
                collection_name=collection_name(values),
                data=[vector.tolist()],
                limit=limit,
                filter=predicate,
                output_fields=["metadata"],
                search_params={"metric_type": "COSINE"},
            )
        except Exception as exc:
            if not _unavailable(exc):
                raise
            raise StorageUnavailable("Milvus storage is unavailable") from exc
        return [hit["entity"]["metadata"] for hit in hits[0]]

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(lexical), pool.submit(semantic)
        rows, vectors = a.result(), b.result()
    sources = {"exact": [], "fulltext": [], "vector": vectors, "graph": []}
    for row in rows:
        sources[row["source"]].append(row["item"])
    seeds = [x["ref"] for x in fuse(sources, max(10, top_k))]
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
        WITH seed, n
        WHERE ((n:SDATable AND 'tables' IN $scopes) OR (n:SDAField AND 'fields' IN $scopes) OR (n:SDATask AND 'tasks' IN $scopes) OR (n:SDAChunk AND 'docs' IN $scopes)) AND ($category IS NULL OR n.category = $category)
        WITH DISTINCT seed, n
        ORDER BY seed.ref, n.ref
        WITH seed, collect(n)[..$limit] AS neighbors
        UNWIND neighbors AS n
        RETURN seed.ref AS seed_ref, properties(n) AS item""",
            {**params, "scopes": scopes, "seeds": seeds},
        )
        seed_order = {ref: i for i, ref in enumerate(seeds)}
        rows.sort(
            key=lambda row: (
                seed_order.get(row.get("seed_ref"), len(seeds)),
                row["item"]["ref"],
            )
        )
        seen = set()
        for row in rows:
            item = row["item"]
            if item["ref"] not in seen:
                sources["graph"].append(item)
                seen.add(item["ref"])
    if scope == "tables":
        for source in ("exact", "fulltext", "vector"):
            sources[source] = [
                item for item in sources[source] if item["ref"].startswith("table:")
            ]
    weights = {**SOURCE_WEIGHTS, "graph": 1.5} if scope == "tables" else SOURCE_WEIGHTS
    candidate_limit = (
        sum(len(items) for items in sources.values())
        if scope == "tables"
        else limit * 4
    )
    results = fuse(sources, candidate_limit, weights)
    if scope == "tables":
        results.sort(
            key=lambda item: (
                not ({"exact", "fulltext"} & set(item["sources"])),
                -item["score"],
                item["ref"],
            )
        )
    return mark_ambiguity(results, query)[:top_k]
