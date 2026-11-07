"""Shared append-only interface for entity-writing stages."""

from dataclasses import dataclass, field
from threading import Lock

WRITE_BATCH_SIZE = 2000
DELETE_BATCH_SIZE = 2000


def delete_batches(execute, match_query, variable, parameters, *, detach=False):
    queries = []
    if detach:
        queries.append(
            f"{match_query} MATCH ({variable})-[batch_relationship]-() "
            "WITH DISTINCT batch_relationship LIMIT $batch_size "
            "DELETE batch_relationship RETURN count(*) AS deleted"
        )
    clause = "DETACH DELETE" if detach else "DELETE"
    queries.append(
        f"{match_query} WITH {variable} LIMIT $batch_size {clause} {variable} RETURN count(*) AS deleted"
    )
    for query in queries:
        while True:
            rows = execute(query, {**parameters, "batch_size": DELETE_BATCH_SIZE})
            if not rows:
                raise RuntimeError("Batch deletion returned no count")
            if not rows[0]["deleted"]:
                break


@dataclass
class StorageHandles:
    generation: str
    _graph: object
    _vectors: object
    _settings: dict[str, str]
    _embedding_stats: object | None = None
    build_id: int | None = None
    all_records: list | None = None
    is_incremental: bool = False
    _embedding_inputs: int = 0
    _embedding_unique: int = 0
    _embedding_cache_hits: int = 0
    _embedding_cache_misses: int = 0
    _embedding_cache: object | None = None
    _embedding_model: str | None = None
    _embedding_dimension: int | None = None
    _cache_initialized: bool = False
    _lock: object = field(default_factory=Lock)

    def execute(self, query, parameters):
        from .knowledge import execute

        rows = parameters.get("rows")
        if (
            "UNWIND $rows AS row" not in query
            or rows is None
            or len(rows) <= WRITE_BATCH_SIZE
        ):
            return execute(self._graph, query, parameters)
        result = []
        for offset in range(0, len(rows), WRITE_BATCH_SIZE):
            result.extend(
                execute(
                    self._graph,
                    query,
                    {**parameters, "rows": rows[offset : offset + WRITE_BATCH_SIZE]},
                )
            )
        return result

    def embedding_counts(self) -> dict[str, int]:
        return {
            "embedding_inputs": self._embedding_inputs,
            "embedding_unique": self._embedding_unique,
            "embedding_cache_hits": self._embedding_cache_hits,
            "embedding_cache_misses": self._embedding_cache_misses,
        }

    def _cache_for_call(self):
        with self._lock:
            if self._embedding_cache is None and not self._cache_initialized:
                from .embedding_cache import open_embedding_cache

                self._embedding_cache = open_embedding_cache(
                    self._settings,
                    model=self._embedding_model,
                    dimension=self._embedding_dimension,
                )
            self._cache_initialized = True
            cache = self._embedding_cache
            if cache is None or not cache.enabled:
                return None
            if self._embedding_dimension is not None:
                cache.dimension = self._embedding_dimension
            else:
                self._embedding_dimension = cache.dimension
            return cache

    def add_vectors(self, ids, texts, metadata):
        from .embedding_cache import float32_values
        from .knowledge import embed

        if not (len(ids) == len(texts) == len(metadata)):
            raise ValueError("Vector ids, texts and metadata must have equal lengths")
        if any(not item.get("scope") for item in metadata):
            raise ValueError("Each vector must have a scope")
        texts = list(texts)
        if not texts:
            return
        positions = {}
        for index, text in enumerate(texts):
            positions.setdefault(text, []).append(index)
        unique_texts = list(positions)
        with self._lock:
            self._embedding_inputs += len(texts)
            self._embedding_unique += len(unique_texts)
        cache = self._cache_for_call()
        vectors = {}
        hits = misses = 0
        if cache is not None:
            misses = len(unique_texts)
            if cache.dimension is not None:
                vectors = cache.lookup(unique_texts)
                if cache.enabled:
                    hits = len(vectors)
                    misses -= hits
                else:
                    cache = None
                    vectors = {}
                    misses = 0

        missing = [text for text in unique_texts if text not in vectors]
        new_vectors = {}
        for offset in range(0, len(missing), 32):
            batch = missing[offset : offset + 32]
            embedded = embed(self._settings, batch, self._embedding_stats)
            if len(embedded) != len(batch):
                raise ValueError("Embedding response has a different length")
            for text, vector in zip(batch, embedded, strict=True):
                if cache is not None:
                    vector = float32_values(vector)
                new_vectors[text] = vector
                vectors[text] = vector

        if cache is not None:
            if self._embedding_dimension is None and new_vectors:
                self._embedding_dimension = len(next(iter(new_vectors.values())))
                cache.dimension = self._embedding_dimension
            hit_texts = [text for text in unique_texts if text not in new_vectors]
            with self._lock:
                self._embedding_cache_hits += hits
                self._embedding_cache_misses += misses
            cache.store(hit_texts, new_vectors)

        pending_vectors, pending_ids, pending_metadata = [], [], []

        def flush():
            if not pending_ids:
                return
            self._vectors.add_vectors(
                pending_vectors.copy(),
                ids=pending_ids.copy(),
                metadata=pending_metadata.copy(),
            )
            pending_vectors.clear()
            pending_ids.clear()
            pending_metadata.clear()

        for text in unique_texts:
            vector = vectors[text]
            for index in positions[text]:
                pending_vectors.append(vector)
                pending_ids.append(ids[index])
                pending_metadata.append(
                    {
                        **metadata[index],
                        "generation": self.generation,
                        **(
                            {"build_id": self.build_id}
                            if self.build_id is not None
                            else {}
                        ),
                    }
                )
                if len(pending_ids) == 32:
                    flush()
        flush()


@dataclass(frozen=True)
class StageResult:
    counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)
