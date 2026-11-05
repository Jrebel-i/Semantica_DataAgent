"""Fail-open, bounded Langfuse ingestion with an explicit field allowlist."""

import logging
import queue
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
        "valid",
        "errors",
        "warnings",
    }
)


def _filtered(value):
    if isinstance(value, dict):
        return {
            key: _filtered(item) for key, item in value.items() if key in ALLOWED_FIELDS
        }
    if isinstance(value, list):
        return [_filtered(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
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
    return (
        values["langfuse_url"].rstrip("/"),
        values["langfuse_public_key"],
        decrypt(values["langfuse_secret_key"]),
    )


_queue = queue.Queue(maxsize=256)
_lock = threading.Lock()
_worker = None


def _consume():
    while True:
        config, kind, payload = _queue.get()
        try:
            url, public, secret = config
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
        except Exception:  # noqa: BLE001 - report errors without leaking credentials
            logger.warning("Langfuse ingestion failed")
        finally:
            _queue.task_done()


def submit(kind, event):
    """Queue a filtered event; configuration and network errors are non-fatal."""
    global _worker
    try:
        config = configuration()
        if config is None:
            return
        payload = _filtered(event)
        with _lock:
            if _worker is None or not _worker.is_alive():
                _worker = threading.Thread(
                    target=_consume, daemon=True, name="langfuse-ingestion"
                )
                _worker.start()
        _queue.put_nowait((config, kind, payload))
    except Exception:  # noqa: BLE001 - telemetry must not affect application requests
        logger.warning("Langfuse event could not be queued")
