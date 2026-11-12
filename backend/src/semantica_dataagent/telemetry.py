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
UNKNOWN_TOOL_NAME = "unknown_tool"
_FIXED_OBSERVATION_NAMES = frozenset(
    {"online-conversation", "local-mcp", UNKNOWN_TOOL_NAME}
)
# Replaced as one atomic snapshot at startup; readers never mutate it.
_trusted_tool_names = frozenset()
_TABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*){0,2}$")
_ERROR_CATEGORIES = frozenset(
    {"unavailable", "invalid_input", "internal_error", "tool_error"}
)
_SQL_TOOLS = frozenset({"validate_sql", "propose_query", "execute_query"})
_TABLE_ARGUMENTS = {
    "describe_tables": ("names",),
    "lookup_dim_values": ("table",),
    "get_lineage": ("table",),
    "find_join_path": ("tables",),
    "sample_table_data": ("table",),
}
_METADATA_KEYS = frozenset(
    {"duration_ms", "agent", "provider", "model", "source", "token_name"}
)
_ENVELOPE_FIELDS = frozenset(
    {
        "id",
        "traceId",
        "parentObservationId",
        "name",
        "startTime",
        "endTime",
        "started_at",
        "ended_at",
        "metadata",
        "agent",
        "provider",
        "model",
        "source",
        "token_name",
        "duration_ms",
    }
)


def register_tool_names(names):
    """Replace the trusted tool names with the server's actual registry."""
    global _trusted_tool_names
    _trusted_tool_names = frozenset(
        name for name in names if isinstance(name, str) and name
    )


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


def _trusted_tool_name(event, secrets=()):
    name = event.get("name") if isinstance(event, dict) else None
    if not isinstance(name, str) or _scrub(name, secrets) != name:
        return None
    if name in _FIXED_OBSERVATION_NAMES:
        return name
    return name if name in _trusted_tool_names else None


def _input_arguments(event):
    arguments = event.get("input") if isinstance(event, dict) else None
    return arguments if isinstance(arguments, dict) else None


def _sql_from_arguments(event, name):
    if name not in _SQL_TOOLS:
        return "", None
    arguments = _input_arguments(event)
    sql = arguments.get("sql") if arguments else None
    if not isinstance(sql, str) or not sql.strip():
        return "", None
    dialect = arguments.get("dialect")
    return sql, dialect if dialect in ("hive", "spark", "doris") else None


def _safe_table_name(value, secrets):
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= 512
        or not _TABLE_NAME.fullmatch(value)
    ):
        return None
    scrubbed = _scrub(value, secrets)
    if scrubbed != value or not _TABLE_NAME.fullmatch(scrubbed):
        return None
    return scrubbed


def _sql_table_names(sql, dialect, secrets):
    tree = _parse_for_fingerprint(sql, dialect)
    if tree is None:
        return []
    ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    names = []
    for table in tree.find_all(exp.Table):
        if table.name.lower() in ctes:
            continue
        name = ".".join(part for part in (table.db, table.name) if part)
        safe = _safe_table_name(name, secrets)
        if safe and safe not in names:
            names.append(safe)
            if len(names) == 64:
                break
    return names


def _table_names(event, name, secrets):
    """Return only table names from the tool's declared argument positions."""
    if name in _SQL_TOOLS:
        sql, dialect = _sql_from_arguments(event, name)
        return _sql_table_names(sql, dialect, secrets) if sql else []
    keys = _TABLE_ARGUMENTS.get(name)
    arguments = _input_arguments(event)
    if not keys or not arguments:
        return []
    names = []
    for key in keys:
        value = arguments.get(key)
        if isinstance(value, str):
            candidates = [value]
        elif isinstance(value, list) and key in ("names", "tables"):
            candidates = value
        else:
            continue
        for item in candidates:
            safe = _safe_table_name(item, secrets)
            if safe and safe not in names:
                names.append(safe)
                if len(names) == 64:
                    return names
    return names


def _error_category(event, is_error):
    """Accept only the fixed error codes emitted by the tool boundary."""
    if is_error is not True:
        return None
    output = event.get("output") if isinstance(event, dict) else None
    code = output.get("code") if isinstance(output, dict) else None
    return code if isinstance(code, str) and code in _ERROR_CATEGORIES else None


def _metadata_summary(event, secrets=()):
    """Extract metadata from declared protocol positions, never by key name."""
    summary = {}
    if not isinstance(event, dict):
        return summary
    name = _trusted_tool_name(event, secrets)
    is_error = event.get("is_error")
    is_error = is_error if isinstance(is_error, bool) else None
    sql, dialect = _sql_from_arguments(event, name)
    if sql:
        summary["sql_fingerprint"] = sql_fingerprint(sql, dialect)
    tables = _table_names(event, name, secrets)
    if len(tables) == 1:
        summary["table"] = tables[0]
    elif tables:
        summary["tables"] = sorted(tables)
    if is_error is not None:
        summary["is_error"] = is_error
        summary["status"] = "error" if is_error else "ok"
    if is_error:
        summary["error_category"] = _error_category(event, is_error) or "tool_error"
    return _scrub(summary, secrets)


def trace_id(conversation_id, turn_id):
    return hashlib.sha256(f"{conversation_id}:{turn_id}".encode()).hexdigest()[:32]


def _scrub(value, secrets=()):
    if isinstance(value, dict):
        return {key: _scrub(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item, secrets) for item in value]
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


def _default_metadata(metadata, secrets):
    if not isinstance(metadata, dict):
        return None
    clean = {}
    for key in _METADATA_KEYS:
        value = metadata.get(key)
        if key == "duration_ms":
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                clean[key] = value
        elif isinstance(value, str) and value:
            clean[key] = _scrub(value, secrets)[:200]
    return clean


def _default_envelope(event, secrets):
    """Keep only protocol envelope fields and trusted metadata keys."""
    if not isinstance(event, dict):
        return {}
    payload = {}
    for key in _ENVELOPE_FIELDS - {"metadata", "name"}:
        value = event.get(key)
        if key == "duration_ms":
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                payload[key] = value
        elif isinstance(value, str) and value:
            payload[key] = _scrub(value, secrets)[:256]
    name = _trusted_tool_name(event, secrets)
    if name:
        payload["name"] = name
    metadata = _default_metadata(event.get("metadata"), secrets)
    if metadata:
        payload["metadata"] = metadata
    return payload


def _add_metadata_summary(payload, event, secrets):
    """Persist trusted derived fields where the receiver actually stores them."""
    summary = _metadata_summary(event, secrets)
    if not summary:
        return
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    payload["metadata"] = {**metadata, **summary}


def _filtered(value, secrets=(), capture_content=False, _top_level=True):
    if not capture_content:
        return _default_envelope(value, secrets)
    if isinstance(value, dict):
        return {
            key: _filtered(item, secrets, True, False)
            for key, item in value.items()
            if key in ALLOWED_FIELDS
            and not (
                _top_level
                and key == "name"
                and _trusted_tool_name(value, secrets) is None
            )
        }
    if isinstance(value, list):
        return [_filtered(item, secrets, True, False) for item in value]
    if isinstance(value, str):
        return _scrub(value, secrets)
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
        all_secrets = (*secrets, *config[1:4])
        payload = _filtered(event, all_secrets, capture_content)
        _add_metadata_summary(payload, event, all_secrets)
        with _lock:
            if _worker is None or not _worker.is_alive():
                _worker = threading.Thread(
                    target=_consume, daemon=True, name="langfuse-ingestion"
                )
                _worker.start()
        _queue.put_nowait((config, kind, payload))
    except Exception:  # noqa: BLE001 - telemetry must not affect application requests
        logger.warning("Langfuse event could not be queued")
