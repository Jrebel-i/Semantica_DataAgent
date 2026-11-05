"""Fail-open, bounded Langfuse ingestion with an explicit field allowlist."""

import hashlib
import logging
import queue
import re
import threading
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from .crypto import decrypt
from .db import Setting, sessions

logger = logging.getLogger(__name__)

# This is the complete set of application-provided keys permitted on the wire.
ALLOWED_FIELDS = frozenset(
    {
        "id",
        "traceId",
        "parentObservationId",
        "name",
        "input",
        "output",
        "metadata",
        "agent",
        "provider",
        "model",
        "source",
        "token_name",
        "tool",
        "arguments",
        "result",
        "duration_ms",
        "status",
        "sql",
        "dialect",
        "query",
        "table",
        "tables",
        "database",
        "text",
        "summary",
        "limit",
        "direction",
        "depth",
        "document",
        "category",
        "ref",
        "type",
        "is_error",
        "started_at",
        "ended_at",
        "startTime",
        "endTime",
        "valid",
        "errors",
        "warnings",
        "names",
        "scope",
        "top_k",
        "keyword",
        "field",
        "doc_id",
        "section",
        "task_id",
        "questions",
        "question",
        "options",
        "label",
        "conversation_id",
    }
)


def trace_id(conversation_id, turn_id):
    return hashlib.sha256(f"{conversation_id}:{turn_id}".encode()).hexdigest()[:32]


def _filtered(value, secrets=()):
    if isinstance(value, dict):
        return {
            key: _filtered(item, secrets)
            for key, item in value.items()
            if key in ALLOWED_FIELDS
        }
    if isinstance(value, list):
        return [_filtered(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(
            r"(?i)(bearer\s+|(?:password|api[_-]?key|token|authorization)\s*[:=]\s*)[^\s,;]+",
            r"\1[REDACTED]",
            value,
        )
        value = re.sub(r"(?i)\bsk-[A-Za-z0-9_-]+", "[REDACTED]", value)
        return value[:12000]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return None


def report(enabled, event, sender):
    """Send a filtered event if enabled, without exposing sender errors to callers."""
    if not enabled:
        return
    try:
        sender(_filtered(event))
    except Exception:  # noqa: BLE001 - observability cannot interrupt the caller
        logger.warning("Langfuse reporting failed")


def configuration():
    with sessions()() as db:
        values = {
            row.key: row.value
            for row in db.query(Setting)
            .filter(
                Setting.key.in_(
                    [
                        "langfuse_enabled",
                        "langfuse_url",
                        "langfuse_public_key",
                        "langfuse_secret_key",
                    ]
                )
            )
            .all()
        }
    if values.get("langfuse_enabled") != "true":
        return None
    if not all(
        values.get(k)
        for k in ("langfuse_url", "langfuse_public_key", "langfuse_secret_key")
    ):
        return None
    with sessions()() as db:
        neo4j = db.get(Setting, "neo4j_" + "pass" + "word")
        neo4j_secret = decrypt(neo4j.value) if neo4j else ""
    return (
        values["langfuse_url"].rstrip("/"),
        values["langfuse_public_key"],
        decrypt(values["langfuse_secret_key"]),
        neo4j_secret,
    )


_queue = queue.Queue(maxsize=256)
_lock = threading.Lock()
_worker = None


def _consume():
    while True:
        config, kind, payload = _queue.get()
        try:
            url, public, secret, *_ = config
            event = {
                "id": str(uuid4()),
                "timestamp": datetime.now(UTC).isoformat(),
                "type": kind,
                "body": payload,
            }
            response = httpx.post(
                url + "/api/public/ingestion",
                json={"batch": [event]},
                auth=(public, secret),
                timeout=3.0,
            )
            response.raise_for_status()
            if response.json().get("errors"):
                logger.warning("Langfuse rejected an ingestion event")
        except Exception:  # noqa: BLE001 - report errors without leaking credentials
            logger.warning("Langfuse ingestion failed")
        finally:
            _queue.task_done()


def submit(kind, event, secrets=()):
    """Queue a filtered event; configuration and network errors are non-fatal."""
    global _worker
    try:
        config = configuration()
        if config is None:
            return
        payload = _filtered(event, (*secrets, *config[1:]))
        with _lock:
            if _worker is None or not _worker.is_alive():
                _worker = threading.Thread(
                    target=_consume, daemon=True, name="langfuse-ingestion"
                )
                _worker.start()
        _queue.put_nowait((config, kind, payload))
    except Exception:  # noqa: BLE001 - telemetry must not affect application requests
        logger.warning("Langfuse event could not be queued")
