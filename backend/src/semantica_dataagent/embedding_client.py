"""Embedding HTTP client with a process-wide limit, timeouts and bounded retries.

Policy: one limiter is shared by every embedding call in this process, including
builds and MCP retrieval. A build applies the configured limit to each request it
makes; because model settings come from one row set, requests use the same limit.
Calls that do not fit wait for a slot, so a retrieval query cannot multiply the
load produced by a build (and vice versa).
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

import httpx

DEFAULTS = {
    "concurrency": 2,
    "timeout_seconds": 60.0,
    "timeout_per_input_seconds": 20.0,
    "retries": 2,
    "backoff_seconds": 1.0,
}

RANGES = {
    "concurrency": (1, 32),
    "timeout_seconds": (0.001, 3600.0),
    "timeout_per_input_seconds": (0.0, 3600.0),
    "retries": (0, 10),
    "backoff_seconds": (0.0, 60.0),
}

SETTING_KEYS = {
    "concurrency": "embedding_concurrency",
    "timeout_seconds": "embedding_timeout_seconds",
    "timeout_per_input_seconds": "embedding_timeout_per_input_seconds",
    "retries": "embedding_retries",
    "backoff_seconds": "embedding_backoff_seconds",
}

_RETRYABLE_TRANSPORT = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)


class EmbeddingError(RuntimeError):
    """An embedding request failed without a remaining retry."""


class _RetryableStatus(Exception):
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")


@dataclass(frozen=True)
class EmbeddingConfig:
    concurrency: int
    timeout_seconds: float
    timeout_per_input_seconds: float
    retries: int
    backoff_seconds: float

    def timeout_for(self, inputs: int) -> float:
        return self.timeout_seconds + self.timeout_per_input_seconds * inputs


def _raw(values: Mapping[str, object], key: str):
    value = values.get(SETTING_KEYS[key])
    if value is None or value == "":
        return None
    return value


def _integer(values, key):
    raw = _raw(values, key)
    if raw is None:
        return DEFAULTS[key]
    try:
        if isinstance(raw, bool):
            raise TypeError
        number = int(str(raw))
    except (TypeError, ValueError):
        raise ValueError(f"Invalid {SETTING_KEYS[key]}") from None
    low, high = RANGES[key]
    if not low <= number <= high:
        raise ValueError(f"Invalid {SETTING_KEYS[key]}")
    return number


def _number(values, key):
    raw = _raw(values, key)
    if raw is None:
        return DEFAULTS[key]
    try:
        if isinstance(raw, bool):
            raise TypeError
        number = float(str(raw))
    except (TypeError, ValueError):
        raise ValueError(f"Invalid {SETTING_KEYS[key]}") from None
    if not math.isfinite(number):
        raise ValueError(f"Invalid {SETTING_KEYS[key]}")
    low, high = RANGES[key]
    if not low <= number <= high:
        raise ValueError(f"Invalid {SETTING_KEYS[key]}")
    return number


def config_from_values(values: Mapping[str, object]) -> EmbeddingConfig:
    """Read runtime settings; missing values use the documented defaults."""
    return EmbeddingConfig(
        concurrency=_integer(values, "concurrency"),
        timeout_seconds=_number(values, "timeout_seconds"),
        timeout_per_input_seconds=_number(values, "timeout_per_input_seconds"),
        retries=_integer(values, "retries"),
        backoff_seconds=_number(values, "backoff_seconds"),
    )


def validate_settings(values: Mapping[str, object]) -> None:
    """Raise ValueError when one of the embedding settings is invalid."""
    config_from_values(values)


class EmbeddingStats:
    """Thread-safe counters for one build's embedding work.

    ``embedding_elapsed_ms`` is cumulative time spent inside logical requests,
    including retries and backoff waits.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._requests = 0
        self._successful = 0
        self._retries = 0
        self._elapsed = 0.0

    def request(self):
        with self._lock:
            self._requests += 1

    def success(self):
        with self._lock:
            self._successful += 1

    def retry(self):
        with self._lock:
            self._retries += 1

    def add_elapsed(self, seconds: float):
        with self._lock:
            self._elapsed += seconds

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {
                "embedding_requests": self._requests,
                "embedding_successful_batches": self._successful,
                "embedding_retries": self._retries,
                "embedding_elapsed_ms": round(self._elapsed * 1000),
            }


class _ConcurrencyLimiter:
    def __init__(self):
        self._condition = threading.Condition()
        self._limit = DEFAULTS["concurrency"]
        self._active = 0

    @contextmanager
    def slot(self, limit: int):
        with self._condition:
            self._limit = limit
            self._condition.notify_all()
            self._condition.wait_for(lambda: self._active < self._limit)
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify()


_LIMITER = _ConcurrencyLimiter()


def _parse_vectors(payload, inputs: int) -> list[list[float]]:
    data = payload["data"]
    ordered = sorted(data, key=lambda item: item["index"])
    if [item["index"] for item in ordered] != list(range(inputs)):
        raise ValueError("Embedding response has missing or duplicate indexes")
    vectors = [item["embedding"] for item in ordered]
    if (
        not vectors
        or not vectors[0]
        or any(
            len(vector) != len(vectors[0])
            or not all(math.isfinite(value) for value in vector)
            for vector in vectors
        )
    ):
        raise ValueError("Invalid embedding vectors")
    return vectors


class EmbeddingClient:
    """One embedding endpoint plus runtime limits; safe to share between stages."""

    def __init__(
        self,
        values: Mapping[str, object],
        *,
        stats: EmbeddingStats | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config_from_values(values)
        url = values.get("embedding_url")
        if not url:
            raise EmbeddingError("embedding_url is not configured")
        self.url = str(url).rstrip("/") + "/embeddings"
        self.model = values.get("embedding_model") or "model/bge-m3"
        self.stats = stats
        self._sleep = sleep

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        texts = list(texts)
        if not texts:
            return []
        started = time.monotonic()
        if self.stats is not None:
            self.stats.request()
        try:
            return self._post(texts)
        finally:
            if self.stats is not None:
                self.stats.add_elapsed(time.monotonic() - started)

    def _post(self, texts: list[str]) -> list[list[float]]:
        timeout = self.config.timeout_for(len(texts))
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            attempt = 0
            while True:
                attempt += 1
                failed = None
                cause = None
                try:
                    with _LIMITER.slot(self.config.concurrency):
                        response = client.post(
                            self.url,
                            json={"model": self.model, "input": texts},
                        )
                    if response.status_code >= 500:
                        raise _RetryableStatus(response.status_code)
                    response.raise_for_status()
                    vectors = _parse_vectors(response.json(), len(texts))
                    if self.stats is not None:
                        self.stats.success()
                    return vectors
                except _RetryableStatus as exc:
                    failed = str(exc)
                    cause = exc
                except _RETRYABLE_TRANSPORT as exc:
                    failed = f"{type(exc).__name__}: {exc}"
                    cause = exc
                except httpx.HTTPStatusError as exc:
                    raise EmbeddingError(
                        f"Embedding request failed: HTTP {exc.response.status_code}"
                    ) from None
                assert failed is not None
                if attempt > self.config.retries:
                    raise EmbeddingError(
                        f"Embedding request failed after {attempt} attempts: {failed}"
                    ) from cause
                if self.stats is not None:
                    self.stats.retry()
                self._sleep(self.config.backoff_seconds * (2 ** (attempt - 1)))


def embed(values: Mapping[str, object], texts, stats: EmbeddingStats | None = None):
    """Compatibility helper used by knowledge embedding and retrieval paths."""
    return EmbeddingClient(values, stats=stats).embed(texts)
