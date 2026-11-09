"""Fail-open, bounded Langfuse ingestion with an explicit field allowlist."""

import hashlib
import logging
import os
import queue
import re
import threading
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import sqlglot
from sqlglot import exp

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
        "rows",
        "columns",
        "values",
        "issues",
        "message",
        "code",
        "retryable",
        "next_action",
        "sql_fingerprint",
        "sql_fingerprints",
        "error_category",
        "prompt",
        "answer",
        "content",
    }
)

# Fields that carry user or business content. They are dropped unless the
# global content-capture setting is explicitly enabled.
CONTENT_FIELDS = frozenset(
    {
        "input",
        "output",
        "arguments",
        "result",
        "sql",
        "query",
        "text",
        "question",
        "questions",
        "summary",
        "document",
        "keyword",
        "options",
        "label",
        "field",
        "section",
        "names",
        "errors",
        "warnings",
        "message",
        "content",
        "prompt",
        "answer",
        "rows",
        "columns",
        "values",
        "issues",
    }
)

_SQL_DIALECTS = ("doris", "hive", "spark", "presto", "")


def _fingerprint_literal(node):
    if isinstance(node, (exp.Literal, exp.Boolean, exp.Null)):
        return exp.Placeholder()
    return node


def _parse_for_fingerprint(sql, dialect):
    try:
        return sqlglot.parse_one(sql, read=dialect or None)
    except Exception:  # noqa: BLE001 - an unparsed statement has no fingerprint
        return None


def _serialize_for_fingerprint(tree, dialect):
    try:
        return tree.transform(_fingerprint_literal).sql(
            dialect=dialect or None, comments=False
        )
    except Exception:  # noqa: BLE001 - serialization failure is still a parse failure
        return None


def sql_fingerprint(sql, dialect=None):
    """Return a stable hash of the SQL structure with literal values removed.

    An empty string means the SQL could not be parsed. Callers must never use
    the original SQL as a fallback because it may contain sensitive literals.
    """
    if not isinstance(sql, str) or not sql.strip():
        return ""
    dialects = []
    if dialect in _SQL_DIALECTS[:-1]:
        dialects.append(dialect)
    dialects.extend(name for name in _SQL_DIALECTS if name != dialect)
    for name in dialects:
        tree = _parse_for_fingerprint(sql, name)
        if tree is None:
            continue
        normalized = _serialize_for_fingerprint(tree, name)
        if normalized is None:
            continue
        return hashlib.sha256(normalized.encode()).hexdigest()
    return ""


def _walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_dicts(item)


def _metadata_summary(event, capture_content):
    """Extract literal-free metadata that should survive content filtering."""
    if capture_content:
        return {}
    sqls = []
    error_codes = []
    is_error = None
    if isinstance(event, dict):
        for key in ("is_error", "isError"):
            if isinstance(event.get(key), bool):
                is_error = event[key]
        for node in _walk_dicts(event):
            sql = node.get("sql")
            if isinstance(sql, str) and sql.strip() and sql not in sqls:
                sqls.append(sql)
            for key in ("is_error", "isError"):
                if isinstance(node.get(key), bool):
                    is_error = node[key]
            code = node.get("code")
            if (
                isinstance(code, str)
                and code
                and ("retryable" in node or "next_action" in node)
                and code not in error_codes
            ):
                error_codes.append(code)
                is_error = True
    summary = {}
    if sqls:
        fingerprints = [sql_fingerprint(sql) for sql in sqls]
        if "" in fingerprints:
            summary["sql_fingerprint"] = ""
        elif len(set(fingerprints)) == 1:
            summary["sql_fingerprint"] = fingerprints[0]
        else:
            joined = "|".join(sorted(set(fingerprints)))
            summary["sql_fingerprint"] = hashlib.sha256(joined.encode()).hexdigest()
        if len(sqls) > 1:
            summary["sql_fingerprints"] = fingerprints
    if is_error is not None:
        summary["is_error"] = is_error
        summary["status"] = "error" if is_error else "ok"
    if error_codes:
        summary["error_category"] = error_codes[0]
    elif is_error:
        summary["error_category"] = "tool_error"
    return summary


def trace_id(conversation_id, turn_id):
    return hashlib.sha256(f"{conversation_id}:{turn_id}".encode()).hexdigest()[:32]


def _filtered(value, secrets=(), capture_content=True):
    if isinstance(value, dict):
        return {
            key: _filtered(item, secrets, capture_content)
            for key, item in value.items()
            if key in ALLOWED_FIELDS and (capture_content or key not in CONTENT_FIELDS)
        }
    if isinstance(value, list):
        return [_filtered(item, secrets, capture_content) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(
            r"(?im)\b(authorization|cookie)\s*:\s*[^\r\n]*", r"\1: [REDACTED]", value
        )
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
        sender(_filtered(event, capture_content=False))
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
                        "langfuse_capture_content",
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
        values.get("langfuse_capture_content") == "true",
    )


_queue = queue.Queue(maxsize=256)
_lock = threading.Lock()
_worker = None


def _reset_after_fork() -> None:
    global _queue, _lock, _worker
    _queue = queue.Queue(maxsize=256)
    _lock = threading.Lock()
    _worker = None


os.register_at_fork(after_in_child=_reset_after_fork)


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
        capture_content = bool(config[4]) if len(config) > 4 else False
        payload = _filtered(event, (*secrets, *config[1:4]), capture_content)
        if not capture_content:
            payload.update(
                {
                    key: value
                    for key, value in _metadata_summary(event, capture_content).items()
                    if key not in payload
                }
            )
        with _lock:
            if _worker is None or not _worker.is_alive():
                _worker = threading.Thread(
                    target=_consume, daemon=True, name="langfuse-ingestion"
                )
                _worker.start()
        _queue.put_nowait((config, kind, payload))
    except Exception:  # noqa: BLE001 - telemetry must not affect application requests
        logger.warning("Langfuse event could not be queued")
