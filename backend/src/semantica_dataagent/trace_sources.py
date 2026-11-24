"""Pluggable external TraceSource implementations (Langfuse REST API)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx

from . import semantic_overrides
from .plugins import TraceConversation, TraceMessage, TraceQuery

_REDACTED = "[REDACTED]"
_HEADER_LINE_RE = re.compile(
    r"(?im)\b(authorization|cookie|x-api-key|x-auth-token)\s*:\s*[^\r\n]*"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s,;\"']+")
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|apikey|access_key|secret_key|auth_token|private_key|token|authorization)\s*[:=]\s*['\"]?[^\s,;'\"]+"
)
_SK_RE = re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}")
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
)
_CONN_STR_RE = re.compile(r"[a-z][a-z0-9+.-]*://[^:\s/@]+:[^@\s/@]+@")

_RAW_ROW_KEYS = frozenset(
    {
        "rows",
        "row_data",
        "sample_rows",
        "preview_rows",
        "data_rows",
        "result_rows",
    }
)
_TOOL_INTERNAL_KEYS = frozenset(
    {
        "tool_calls",
        "tool_use",
        "tool_result",
        "function_call",
        "observations",
        "spans",
        "arguments",
        "credentials",
        "credential",
        "secret",
        "secrets",
        "password",
        "passwd",
        "api_key",
        "apikey",
        "auth_token",
        "access_token",
        "refresh_token",
        "private_key",
        "authorization",
        "cookie",
        "headers",
    }
)
_QUERY_RESULT_PREAMBLE = "查询已执行，请根据以下结果信息给出结论："


def scrub_trace_text(value: str, secrets: tuple[str, ...] = ()) -> str:
    """Scrub secrets, credentials, headers, query-result rows, and prompt injections."""
    cleaned = str(value or "")
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, _REDACTED)
    cleaned = _PRIVATE_KEY_RE.sub(_REDACTED, cleaned)
    cleaned = _CONN_STR_RE.sub(
        lambda m: m.group(0).split("://", 1)[0] + "://" + _REDACTED + "@", cleaned
    )
    cleaned = _HEADER_LINE_RE.sub(lambda m: f"{m.group(1)}: {_REDACTED}", cleaned)
    cleaned = _BEARER_RE.sub(f"Bearer {_REDACTED}", cleaned)
    cleaned = _ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}={_REDACTED}", cleaned)
    cleaned = _SK_RE.sub(_REDACTED, cleaned)
    cleaned = _strip_embedded_query_rows(cleaned)
    cleaned = semantic_overrides.scrub_untrusted_text(cleaned)
    return cleaned.strip()[:12000]


def _strip_row_keys_recursive(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            k: _strip_row_keys_recursive(v)
            for k, v in value.items()
            if str(k).lower() not in _RAW_ROW_KEYS
            and str(k).lower() not in _TOOL_INTERNAL_KEYS
        }
    if isinstance(value, list):
        return [_strip_row_keys_recursive(item) for item in value]
    return value


def _contains_raw_row_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for k, v in value.items():
            if str(k).lower() in _RAW_ROW_KEYS or _contains_raw_row_key(v):
                return True
    elif isinstance(value, list):
        return any(_contains_raw_row_key(item) for item in value)
    return False


def _strip_embedded_query_rows(text: str) -> str:
    """Strip inline query-execution result row payloads fail-closed."""
    if _QUERY_RESULT_PREAMBLE in text:
        prefix, _, rest = text.partition(_QUERY_RESULT_PREAMBLE)
        stripped_rest = rest.strip()
        if stripped_rest.startswith("{"):
            try:
                parsed = json.loads(stripped_rest)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                safe_summary = _strip_row_keys_recursive(parsed)
                note = (
                    f"{_QUERY_RESULT_PREAMBLE} "
                    f"{json.dumps(safe_summary, ensure_ascii=False)}"
                )
                return (
                    (prefix.strip() + "\n" + note).strip() if prefix.strip() else note
                )
        # Fail closed when JSON is truncated, non-dict, or has trailing text.
        return (
            (prefix.strip() + "\n" + _QUERY_RESULT_PREAMBLE).strip()
            if prefix.strip()
            else "查询结果解读"
        )
    return text


def _parse_timestamp(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=UTC) if raw.tzinfo is None else raw.astimezone(UTC)
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (
        parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    )


def _extract_content_text(raw: Any, secrets: tuple[str, ...]) -> str:
    """Extract readable text from string/list/dict message content, dropping tool internals & rows."""
    if isinstance(raw, str):
        return scrub_trace_text(raw, secrets)
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, str):
                text = scrub_trace_text(item, secrets)
                if text:
                    parts.append(text)
            elif isinstance(item, Mapping):
                block_type = str(item.get("type") or "").lower()
                if block_type in {
                    "tool_use",
                    "tool_result",
                    "thinking",
                    "function_call",
                }:
                    continue
                if block_type == "text" or "text" in item:
                    text = scrub_trace_text(str(item.get("text") or ""), secrets)
                    if text:
                        parts.append(text)
                elif "content" in item and not _contains_raw_row_key(item):
                    text = _extract_content_text(item.get("content"), secrets)
                    if text:
                        parts.append(text)
        return "\n".join(parts).strip()
    if isinstance(raw, Mapping):
        if _contains_raw_row_key(raw):
            return ""
        for key in ("content", "text", "question", "prompt", "answer", "message"):
            val = raw.get(key)
            if isinstance(val, (str, list)):
                extracted = _extract_content_text(val, secrets)
                if extracted:
                    return extracted
    return ""


def _extract_messages_from_list(
    items: Sequence[Any], secrets: tuple[str, ...]
) -> list[TraceMessage]:
    messages: list[TraceMessage] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        if item.get("tool_calls") or item.get("function_call"):
            # Keep only explicit textual content if present; never serialize tool call payloads.
            content = _extract_content_text(item.get("content"), secrets)
        else:
            content = _extract_content_text(item.get("content"), secrets)
        if not content:
            continue
        ts = _parse_timestamp(item.get("timestamp") or item.get("created_at"))
        messages.append(
            TraceMessage(
                role="user" if role == "user" else "assistant",
                content=content,
                timestamp=ts,
            )
        )
    return messages


def _extract_side_messages(
    payload: Any,
    *,
    default_role: str,
    secrets: tuple[str, ...],
    timestamp: datetime | None,
) -> list[TraceMessage]:
    if payload is None:
        return []
    if isinstance(payload, list):
        if any(isinstance(item, Mapping) and "role" in item for item in payload):
            return _extract_messages_from_list(payload, secrets)
        text = _extract_content_text(payload, secrets)
        return (
            [TraceMessage(role=default_role, content=text, timestamp=timestamp)]  # type: ignore[arg-type]
            if text
            else []
        )
    if isinstance(payload, Mapping):
        if isinstance(payload.get("messages"), list):
            return _extract_messages_from_list(payload["messages"], secrets)
        if "role" in payload:
            return _extract_messages_from_list([payload], secrets)
        text = _extract_content_text(payload, secrets)
        return (
            [TraceMessage(role=default_role, content=text, timestamp=timestamp)]  # type: ignore[arg-type]
            if text
            else []
        )
    if isinstance(payload, str):
        text = scrub_trace_text(payload, secrets)
        return (
            [TraceMessage(role=default_role, content=text, timestamp=timestamp)]  # type: ignore[arg-type]
            if text
            else []
        )
    return []


def _extract_trace_messages(
    trace: Mapping[str, Any],
    *,
    secrets: tuple[str, ...],
    trace_ts: datetime | None,
) -> list[TraceMessage]:
    metadata = trace.get("metadata")
    if isinstance(metadata, Mapping) and isinstance(metadata.get("messages"), list):
        extracted = _extract_messages_from_list(metadata["messages"], secrets)
        if extracted:
            return extracted

    messages: list[TraceMessage] = []
    messages.extend(
        _extract_side_messages(
            trace.get("input"),
            default_role="user",
            secrets=secrets,
            timestamp=trace_ts,
        )
    )
    messages.extend(
        _extract_side_messages(
            trace.get("output"),
            default_role="assistant",
            secrets=secrets,
            timestamp=trace_ts,
        )
    )
    return messages


def _matches_project(trace: Mapping[str, Any], project_id: str) -> bool:
    """Enforce strict client-side project isolation on Langfuse trace metadata and tags."""
    metadata = trace.get("metadata")
    meta_project = metadata.get("project_id") if isinstance(metadata, Mapping) else None
    if meta_project is not None:
        return str(meta_project) == project_id
    tags = trace.get("tags")
    if isinstance(tags, list):
        expected_tag = f"project:{project_id}"
        return any(str(tag) == expected_tag for tag in tags)
    return False


def _sanitize_metadata(metadata: Any, secrets: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(metadata, Mapping):
        return {}
    clean: dict[str, Any] = {}
    for key in ("project_id", "agent", "provider", "model", "source", "token_name"):
        val = metadata.get(key)
        if isinstance(val, str) and val:
            clean[key] = scrub_trace_text(val, secrets)[:200]
    return clean


def _validate_base_url(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Langfuse base_url is required")
    url = raw.strip().rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Langfuse base_url must be a valid http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            "Langfuse base_url must not contain credentials or query parameters"
        )
    return url


class LangfuseTraceSource:
    """Built-in TraceSource plugin reading scrubbed human-AI messages from Langfuse REST API."""

    id = "langfuse"
    version = "1.0.0"
    display_name = "Langfuse"
    parameters_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "base_url": {
                "type": "string",
                "format": "uri",
                "title": "Langfuse Base URL",
                "description": "Langfuse 服务根地址，例如 https://langfuse.example.com",
            },
            "public_key": {
                "type": "string",
                "minLength": 1,
                "title": "Public Key",
                "description": "Langfuse Public Key",
            },
            "secret_key": {
                "type": "string",
                "format": "password",
                "minLength": 1,
                "title": "Secret Key",
                "description": "Langfuse Secret Key",
            },
            "trace_name": {
                "type": "string",
                "title": "Trace Name",
                "description": "可选 Trace 名称过滤（例如 online-conversation）",
            },
            "environment": {
                "type": "string",
                "title": "Environment",
                "description": "可选 Langfuse 环境过滤",
            },
            "page_size": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "default": 50,
                "title": "Page Size",
            },
            "timeout_seconds": {
                "type": "number",
                "minimum": 1,
                "maximum": 120,
                "default": 15,
                "title": "Timeout (seconds)",
            },
        },
        "required": ["base_url", "public_key", "secret_key"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        *,
        client_factory: Callable[..., httpx.Client] | None = None,
    ) -> None:
        self._client_factory = client_factory or httpx.Client

    def fetch_traces(self, query: TraceQuery) -> list[TraceConversation]:
        if not query.project_id or not isinstance(query.project_id, str):
            raise ValueError("project_id is required")

        params_cfg = query.parameters or {}
        base_url = _validate_base_url(params_cfg.get("base_url"))
        public_key = str(params_cfg.get("public_key") or "").strip()
        secret_key = str(
            (query.secrets or {}).get("secret_key")
            or params_cfg.get("secret_key")
            or ""
        ).strip()
        if not public_key or not secret_key:
            raise ValueError("Langfuse public_key and secret_key are required")

        timeout = float(params_cfg.get("timeout_seconds") or 15.0)
        page_size = max(1, min(100, int(params_cfg.get("page_size") or 50)))
        limit = max(1, int(query.limit))
        trace_name = str(params_cfg.get("trace_name") or "").strip() or None
        environment = str(params_cfg.get("environment") or "").strip() or None

        since_utc = _parse_timestamp(query.since)
        until_utc = _parse_timestamp(query.until)
        secrets = tuple(
            v for v in (*(query.secrets or {}).values(), secret_key, public_key) if v
        )

        conversations: list[TraceConversation] = []
        seen_ids: set[str] = set()
        seen_cursors: set[str] = set()
        page = 1
        cursor: str | None = None

        with self._client_factory(
            timeout=timeout, auth=(public_key, secret_key)
        ) as client:
            while len(conversations) < limit:
                req_params: dict[str, Any] = {
                    "limit": page_size,
                    "tags": f"project:{query.project_id}",
                }
                if cursor is not None:
                    req_params["cursor"] = cursor
                else:
                    req_params["page"] = page
                if since_utc is not None:
                    req_params["fromTimestamp"] = since_utc.isoformat()
                if until_utc is not None:
                    req_params["toTimestamp"] = until_utc.isoformat()
                if trace_name:
                    req_params["name"] = trace_name
                if environment:
                    req_params["environment"] = environment

                response = client.get(
                    f"{base_url}/api/public/traces", params=req_params
                )
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, Mapping):
                    break
                items = body.get("data")
                if not isinstance(items, list) or not items:
                    break

                for raw_trace in items:
                    if not isinstance(raw_trace, Mapping):
                        continue
                    tid = str(raw_trace.get("id") or "").strip()
                    if not tid or tid in seen_ids:
                        continue
                    if not _matches_project(raw_trace, query.project_id):
                        continue
                    trace_ts = _parse_timestamp(
                        raw_trace.get("timestamp")
                        or raw_trace.get("createdAt")
                        or raw_trace.get("startedAt")
                    )
                    if since_utc is not None and (
                        trace_ts is None or trace_ts < since_utc
                    ):
                        continue
                    if until_utc is not None and (
                        trace_ts is None or trace_ts > until_utc
                    ):
                        continue

                    messages = _extract_trace_messages(
                        raw_trace, secrets=secrets, trace_ts=trace_ts
                    )
                    if not messages:
                        continue

                    seen_ids.add(tid)
                    ended_at = _parse_timestamp(raw_trace.get("updatedAt")) or trace_ts
                    session_id = raw_trace.get("sessionId")
                    conversations.append(
                        TraceConversation(
                            trace_id=tid,
                            project_id=query.project_id,
                            messages=messages,
                            started_at=trace_ts,
                            ended_at=ended_at,
                            session_id=str(session_id) if session_id else None,
                            metadata=_sanitize_metadata(
                                raw_trace.get("metadata"), secrets
                            ),
                        )
                    )
                    if len(conversations) >= limit:
                        break

                meta = body.get("meta")
                if not isinstance(meta, Mapping):
                    break
                next_cursor = meta.get("nextCursor") or meta.get("next_cursor")
                if isinstance(next_cursor, str) and next_cursor:
                    if next_cursor in seen_cursors:
                        break
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
                    continue

                total_pages = meta.get("totalPages")
                current_page = meta.get("page") or page
                if (
                    isinstance(total_pages, int)
                    and isinstance(current_page, int)
                    and current_page < total_pages
                ):
                    page = current_page + 1
                    continue
                break

        epoch_min = datetime.min.replace(tzinfo=UTC)
        conversations.sort(key=lambda c: (c.started_at or epoch_min, c.trace_id))
        return conversations
