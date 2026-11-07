"""SQLite-backed embedding vector cache shared by builds.

The cache stores one row per ``(model, dimension, sha256(text))`` and keeps
vectors as little-endian float32 bytes.  A cache problem never fails a build:
callers receive a disabled cache object after the first error, and the error is
reported through ``logging``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sqlite3
import struct
import time
from contextlib import closing
from pathlib import Path

SETTING_KEY = "embedding_cache_max_mb"
PATH_ENV = "DATAAGENT_EMBEDDING_CACHE_PATH"
CACHE_FILE_NAME = "embedding-cache.sqlite"
DEFAULT_MAX_MB = 500
_ENTRY_OVERHEAD = 40

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    model TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    text_hash TEXT NOT NULL,
    vector BLOB NOT NULL,
    last_used INTEGER NOT NULL,
    PRIMARY KEY (model, dimension, text_hash)
);
CREATE TABLE IF NOT EXISTS dimensions (
    model TEXT PRIMARY KEY,
    dimension INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
"""


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def float32_values(vector):
    """Round-trip a vector through float32 so cached and fresh builds match."""
    return list(
        struct.unpack(f"<{len(vector)}f", struct.pack(f"<{len(vector)}f", *vector))
    )


def _blob(vector) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _values(blob: bytes):
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def _default_warning(message: str) -> None:
    logger.warning(message)


def _max_bytes(values) -> int:
    raw = values.get(SETTING_KEY)
    if raw is None or raw == "":
        return DEFAULT_MAX_MB * 1024 * 1024
    try:
        if isinstance(raw, bool):
            raise TypeError
        megabytes = float(raw)
    except (TypeError, ValueError):
        _default_warning("embedding cache disabled: invalid embedding_cache_max_mb")
        return 0
    if not math.isfinite(megabytes) or megabytes < 0:
        _default_warning("embedding cache disabled: invalid embedding_cache_max_mb")
        return 0
    return int(megabytes * 1024 * 1024)


def cache_path(values) -> Path | None:
    override = os.environ.get(PATH_ENV)
    if override:
        return Path(override)
    database = os.environ.get("DATAAGENT_DB_PATH")
    if not database:
        return None
    return Path(database).parent / CACHE_FILE_NAME


class EmbeddingCache:
    """One model's cache; operations open short-lived SQLite connections."""

    def __init__(self, path, max_bytes: int, model: str, dimension=None, warn=None):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.model = model
        self.dimension = dimension
        self.enabled = max_bytes > 0
        self._warn = warn or _default_warning

    def _connect(self):
        return sqlite3.connect(str(self.path), timeout=5.0)

    def _disable(self, exc: Exception) -> None:
        if not self.enabled:
            return
        self.enabled = False
        self._warn(
            f"embedding cache disabled after {type(exc).__name__} at {self.path}: {exc}"
        )

    def check(self) -> bool:
        if not self.enabled:
            return False
        try:
            with closing(self._connect()) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.execute("PRAGMA busy_timeout=5000")
                connection.executescript(_SCHEMA)
                connection.commit()
            if self.dimension is None:
                self.dimension = self.known_dimension()
            return True
        except Exception as exc:  # noqa: BLE001 - cache degradation is intentional
            self._disable(exc)
            return False

    def known_dimension(self):
        if not self.enabled:
            return None
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT dimension FROM dimensions WHERE model = ?",
                    (self.model,),
                ).fetchone()
            return int(row[0]) if row else None
        except Exception as exc:  # noqa: BLE001 - cache degradation is intentional
            self._disable(exc)
            return None

    def remember_dimension(self, dimension: int) -> None:
        if not self.enabled:
            return
        self.dimension = int(dimension)
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    "INSERT INTO dimensions (model, dimension, updated_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(model) DO UPDATE SET "
                    "dimension = excluded.dimension, updated_at = excluded.updated_at",
                    (self.model, self.dimension, time.time_ns()),
                )
        except Exception as exc:  # noqa: BLE001 - cache degradation is intentional
            self._disable(exc)

    def lookup(self, texts) -> dict[str, list[float]]:
        """Return cached vectors for the given unique texts in one query."""
        if not self.enabled or self.dimension is None:
            return {}
        by_hash = {}
        for text in texts:
            by_hash[text_hash(text)] = text
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT text_hash, vector FROM embeddings "
                    "WHERE model = ? AND dimension = ? "
                    "AND text_hash IN (SELECT value FROM json_each(?))",
                    (
                        self.model,
                        self.dimension,
                        json.dumps(sorted(by_hash)),
                    ),
                ).fetchall()
        except Exception as exc:  # noqa: BLE001 - cache degradation is intentional
            self._disable(exc)
            return {}
        found = {}
        for text_hash_value, blob in rows:
            if len(blob) != self.dimension * 4:
                continue
            text = by_hash.get(text_hash_value)
            if text is not None:
                found[text] = _values(blob)
        return found

    def store(self, hit_texts, new_vectors) -> None:
        """Touch hits and insert misses in one transaction, then evict."""
        if not self.enabled or self.dimension is None:
            return
        now = time.time_ns()
        inserted = [
            (
                self.model,
                self.dimension,
                text_hash(text),
                _blob(vector),
                now + index,
            )
            for index, (text, vector) in enumerate(new_vectors.items())
        ]
        touched = [
            (
                now + len(inserted) + index,
                self.model,
                self.dimension,
                text_hash(text),
            )
            for index, text in enumerate(hit_texts)
        ]
        try:
            with closing(self._connect()) as connection, connection:
                if inserted:
                    connection.executemany(
                        "INSERT INTO embeddings "
                        "(model, dimension, text_hash, vector, last_used) "
                        "VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(model, dimension, text_hash) DO UPDATE SET "
                        "vector = excluded.vector, last_used = excluded.last_used",
                        inserted,
                    )
                if touched:
                    connection.executemany(
                        "UPDATE embeddings SET last_used = ? "
                        "WHERE model = ? AND dimension = ? AND text_hash = ?",
                        touched,
                    )
                connection.execute(
                    "INSERT INTO dimensions (model, dimension, updated_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(model) DO UPDATE SET "
                    "dimension = excluded.dimension, updated_at = excluded.updated_at",
                    (self.model, self.dimension, now),
                )
                self._evict(connection, {row[2] for row in inserted + touched})
        except Exception as exc:  # noqa: BLE001 - cache degradation is intentional
            self._disable(exc)

    def _evict(self, connection, keep_hashes: set[str]) -> None:
        size = connection.execute(
            "SELECT COALESCE(SUM(length(vector) + length(text_hash) + ?), 0) "
            "FROM embeddings",
            (_ENTRY_OVERHEAD,),
        ).fetchone()[0]
        if size <= self.max_bytes:
            return
        keep = json.dumps(sorted(keep_hashes))
        while size > self.max_bytes:
            rows = connection.execute(
                "SELECT rowid, length(vector) + length(text_hash) + ? AS size "
                "FROM embeddings "
                "WHERE NOT (model = ? AND dimension = ? "
                "AND text_hash IN (SELECT value FROM json_each(?))) "
                "ORDER BY last_used ASC, rowid ASC LIMIT 200",
                (_ENTRY_OVERHEAD, self.model, self.dimension, keep),
            ).fetchall()
            if not rows:
                rows = connection.execute(
                    "SELECT rowid, length(vector) + length(text_hash) + ? AS size "
                    "FROM embeddings ORDER BY last_used ASC, rowid ASC LIMIT 200",
                    (_ENTRY_OVERHEAD,),
                ).fetchall()
            if not rows:
                return
            connection.executemany(
                "DELETE FROM embeddings WHERE rowid = ?",
                [(row[0],) for row in rows],
            )
            size -= sum(row[1] for row in rows)


def open_embedding_cache(values, *, model=None, dimension=None, warn=None):
    """Open the configured cache, or return None when it is disabled/unusable."""
    maximum = _max_bytes(values)
    if maximum <= 0:
        return None
    path = cache_path(values)
    if path is None:
        return None
    resolved_model = model or values.get("embedding_model") or "model/bge-m3"
    cache = EmbeddingCache(path, maximum, resolved_model, dimension, warn)
    cache.check()
    return cache
