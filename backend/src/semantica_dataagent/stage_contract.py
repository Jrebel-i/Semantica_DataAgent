"""Shared append-only interface for entity-writing stages."""

from dataclasses import dataclass, field

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
        }

    def add_vectors(self, ids, texts, metadata):
        from .knowledge import embed

        if not (len(ids) == len(texts) == len(metadata)):
            raise ValueError("Vector ids, texts and metadata must have equal lengths")
        if any(not item.get("scope") for item in metadata):
            raise ValueError("Each vector must have a scope")
        texts = list(texts)
        if not texts:
            return
        self._embedding_inputs += len(texts)
        positions = {}
        for index, text in enumerate(texts):
            positions.setdefault(text, []).append(index)
        unique_texts = list(positions)
        self._embedding_unique += len(unique_texts)
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

        for offset in range(0, len(unique_texts), 32):
            batch = unique_texts[offset : offset + 32]
            embedded = embed(self._settings, batch, self._embedding_stats)
            if len(embedded) != len(batch):
                raise ValueError("Embedding response has a different length")
            for text, vector in zip(batch, embedded, strict=True):
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
