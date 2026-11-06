"""Shared append-only interface for entity-writing stages."""

from dataclasses import dataclass, field

WRITE_BATCH_SIZE = 2000
DELETE_BATCH_SIZE = 2000


def delete_batches(execute, match_query, variable, parameters, *, detach=False):
    clause = "DETACH DELETE" if detach else "DELETE"
    query = f"{match_query} WITH {variable} LIMIT $batch_size {clause} {variable} RETURN count(*) AS deleted"
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

    def add_vectors(self, ids, texts, metadata):
        from .knowledge import embed

        if not (len(ids) == len(texts) == len(metadata)):
            raise ValueError("Vector ids, texts and metadata must have equal lengths")
        if any(not item.get("scope") for item in metadata):
            raise ValueError("Each vector must have a scope")
        for offset in range(0, len(texts), 32):
            batch = slice(offset, offset + 32)
            self._vectors.add_vectors(
                embed(self._settings, texts[batch], self._embedding_stats),
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
