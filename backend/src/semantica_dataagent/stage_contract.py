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
        self._embedding_inputs += len(texts)
        vectors_by_text = {}
        for offset in range(0, len(texts), 32):
            batch = slice(offset, offset + 32)
            pending = list(
                dict.fromkeys(
                    text for text in texts[batch] if text not in vectors_by_text
                )
            )
            if pending:
                embedded = embed(self._settings, pending, self._embedding_stats)
                if len(embedded) != len(pending):
                    raise ValueError("Embedding response has a different length")
                vectors_by_text.update(zip(pending, embedded, strict=True))
                self._embedding_unique += len(pending)
            self._vectors.add_vectors(
                [vectors_by_text[text] for text in texts[batch]],
                ids=ids[batch],
                metadata=[
                    {
                        **item,
                        "generation": self.generation,
                        **(
                            {"build_id": self.build_id}
                            if self.build_id is not None
                            else {}
                        ),
                    }
                    for item in metadata[batch]
                ],
            )


@dataclass(frozen=True)
class StageResult:
    counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)
