"""Fork-safe, sanitized, untruncated 问答轨迹 (InteractionTrajectory) persistence."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta
from threading import Lock
from typing import Any

from sqlalchemy.orm import Session

from . import semantic_overrides, telemetry
from .api_time import as_utc_or_now as _as_utc
from .db import InteractionTrajectory, sessions

LOCAL_MCP_IDLE_WINDOW = timedelta(minutes=30)

_REDACTED = "[REDACTED]"

_STRIPPED_KEYS = frozenset(
    {
        "rows",
        "row_data",
        "sample_rows",
        "preview_rows",
        "data_rows",
        "errormessage",
        "error_message",
        "download_urls",
        "download_url",
        "file_content",
        "file_contents",
        "attachment_content",
        "attachment_contents",
        "raw_content",
        "raw_text",
        "raw_bytes",
        "raw_error",
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

_ATTACHMENT_CONTENT_KEYS = frozenset(
    {
        "content",
        "text",
        "body",
        "bytes",
        "base64",
        "data",
        "file_content",
        "file_contents",
        "raw",
    }
)

_HEADER_LINE_RE = re.compile(
    r"(?im)\b(authorization|cookie|x-api-key|x-auth-token)\s*:\s*[^\r\n]*"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s,;\"']+")
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|apikey|access_key|secret_key|auth_token|private_key|token|authorization)\s*[:=]\s*['\"]?[^\s,;'\"]+"
)
_SK_RE = re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}")

_write_lock = Lock()


def _reset_after_fork() -> None:
    """Reinitialize module lock in forked child processes."""
    global _write_lock
    _write_lock = Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


def scrub_text(value: str, secrets: tuple[str, ...] = ()) -> str:
    """Scrub secrets, headers, credentials, and prompt injections without truncating."""
    cleaned = str(value or "")
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, _REDACTED)
    cleaned = _HEADER_LINE_RE.sub(lambda m: f"{m.group(1)}: {_REDACTED}", cleaned)
    cleaned = _BEARER_RE.sub(f"Bearer {_REDACTED}", cleaned)
    cleaned = _ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}={_REDACTED}", cleaned)
    cleaned = _SK_RE.sub(_REDACTED, cleaned)
    return semantic_overrides.scrub_untrusted_text(cleaned)


def _scrub_attachment_item(item: Any, secrets: tuple[str, ...]) -> Any:
    if not isinstance(item, dict):
        return _scrub_value(item, secrets)
    sanitized: dict[str, Any] = {}
    for key, val in item.items():
        if key.lower() in _ATTACHMENT_CONTENT_KEYS or key.lower() in _STRIPPED_KEYS:
            continue
        sanitized[key] = _scrub_value(val, secrets)
    return sanitized


def _scrub_value(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in _STRIPPED_KEYS:
                continue
            if lowered == "attachments" and isinstance(item, list):
                result[key] = [_scrub_attachment_item(att, secrets) for att in item]
                continue
            result[key] = _scrub_value(item, secrets)
        return result
    if isinstance(value, list):
        return [_scrub_value(item, secrets) for item in value]
    if isinstance(value, str):
        return scrub_text(value, secrets)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return scrub_text(str(value), secrets)


def _loads_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _append_unique(existing: list[str], candidates: list[str]) -> list[str]:
    seen = set(existing)
    merged = list(existing)
    for item in candidates:
        if isinstance(item, str) and item and item not in seen:
            seen.add(item)
            merged.append(item)
    return merged


def _extract_refs_from_tool(tool_name: str, arguments: Any, output: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(output, dict):
        for key in ("result", "items"):
            entries = output.get(key)
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict) and isinstance(entry.get("ref"), str):
                        refs.append(entry["ref"])
        if isinstance(output.get("ref"), str):
            refs.append(output["ref"])
        questions = output.get("questions")
        if isinstance(questions, list):
            for question in questions:
                if not isinstance(question, dict):
                    continue
                for option in question.get("options") or ():
                    if isinstance(option, dict) and isinstance(option.get("ref"), str):
                        refs.append(option["ref"])
    if isinstance(arguments, dict):
        if isinstance(arguments.get("ref"), str):
            refs.append(arguments["ref"])
        if isinstance(arguments.get("refs"), list):
            refs.extend(r for r in arguments["refs"] if isinstance(r, str))
        if tool_name == "request_clarification":
            for question in arguments.get("questions") or ():
                if not isinstance(question, dict):
                    continue
                for option in question.get("options") or ():
                    if isinstance(option, dict) and isinstance(option.get("ref"), str):
                        refs.append(option["ref"])
    return refs


def _extract_clarification(
    tool_name: str, arguments: Any, output: Any
) -> dict[str, Any] | None:
    if tool_name != "request_clarification":
        return None
    if isinstance(output, dict) and output.get("questions"):
        return dict(output)
    if isinstance(arguments, dict) and arguments.get("questions"):
        return {"questions": list(arguments["questions"])}
    return None


def _extract_validation(
    tool_name: str, arguments: Any, output: Any
) -> dict[str, Any] | None:
    if tool_name != "validate_sql":
        return None
    sql = arguments.get("sql") if isinstance(arguments, dict) else ""
    dialect = arguments.get("dialect") if isinstance(arguments, dict) else None
    status = output.get("status") if isinstance(output, dict) else None
    issues = output.get("issues") if isinstance(output, dict) else []
    return {
        "sql": sql or "",
        "dialect": dialect,
        "status": status,
        "issues": issues if isinstance(issues, list) else [],
    }


def _parse_window_index(session_key: str) -> int:
    match = re.search(r":w(\d+)(?::[0-9a-f]+)?$", session_key)
    return int(match.group(1)) if match else 0


def _next_local_window_identity(
    db: Session, project_id: str, token_id: str | None, now: datetime
) -> tuple[str, str]:
    tok = token_id or "anon"
    existing_keys = [
        row[0]
        for row in db.query(InteractionTrajectory.session_key)
        .filter(
            InteractionTrajectory.project_id == project_id,
            InteractionTrajectory.token_id == tok,
            InteractionTrajectory.source_type == "local_mcp",
        )
        .all()
    ]
    existing_set = set(existing_keys)
    next_idx = max((_parse_window_index(k) for k in existing_keys), default=0) + 1
    while True:
        window_key = f"{_as_utc(now).isoformat()}:w{next_idx}:{uuid.uuid4().hex[:8]}"
        candidate_key = f"local_mcp:{project_id}:{tok}:{window_key}"
        if candidate_key not in existing_set:
            break
        next_idx += 1
    trace_id = telemetry.local_mcp_trace_id(project_id, tok, window_key)
    return candidate_key, trace_id


def _apply_tool_call_to_row(
    row: InteractionTrajectory,
    *,
    tool_name: str,
    arguments: Any,
    output: Any,
    is_error: bool,
    secrets: tuple[str, ...],
    now: datetime,
) -> None:
    clean_args = _scrub_value(arguments, secrets)
    clean_output = _scrub_value(output, secrets)

    tool_calls = _loads_list(row.tool_calls)
    tool_calls.append(
        {
            "tool_name": tool_name,
            "arguments": clean_args,
            "output": clean_output,
            "is_error": bool(is_error),
            "recorded_at": now.isoformat(),
        }
    )
    row.tool_calls = _dumps(tool_calls)

    refs = _extract_refs_from_tool(tool_name, clean_args, clean_output)
    if refs:
        row.matched_refs = _dumps(_append_unique(_loads_list(row.matched_refs), refs))

    clarification = _extract_clarification(tool_name, clean_args, clean_output)
    if clarification is not None:
        clarifications = _loads_list(row.clarifications)
        clarifications.append(clarification)
        row.clarifications = _dumps(clarifications)

    validation = _extract_validation(tool_name, clean_args, clean_output)
    if validation is not None:
        validations = _loads_list(row.sql_validations)
        validations.append(validation)
        row.sql_validations = _dumps(validations)

    if (
        row.source_type == "local_mcp"
        and not row.question
        and tool_name == "search_knowledge"
        and isinstance(clean_args, dict)
        and isinstance(clean_args.get("query"), str)
    ):
        row.question = clean_args["query"]

    row.last_activity_at = now
    row.updated_at = now


def start_online_trajectory(
    *,
    project_id: str,
    conversation_id: str,
    turn_id: int,
    token_id: str | None = None,
    question: str = "",
    secrets: tuple[str, ...] = (),
    now: datetime | None = None,
    langfuse_trace_id: str | None = None,
) -> str:
    """Create or update an online turn trajectory row and return its Langfuse traceId."""
    ts = _as_utc(now)
    trace = langfuse_trace_id or telemetry.trace_id(conversation_id, turn_id)
    session_key = f"online:{conversation_id}:{turn_id}"
    clean_question = scrub_text(question, secrets)

    with _write_lock, sessions().begin() as db:
        row = (
            db.query(InteractionTrajectory)
            .filter(InteractionTrajectory.session_key == session_key)
            .first()
        )
        if row is None:
            row = InteractionTrajectory(
                project_id=project_id,
                source_type="online",
                session_key=session_key,
                conversation_id=conversation_id,
                turn_id=turn_id,
                token_id=token_id,
                langfuse_trace_id=trace,
                status="running",
                question=clean_question,
                last_activity_at=ts,
                recorded_at=ts,
                updated_at=ts,
            )
            db.add(row)
        else:
            row.project_id = project_id
            if token_id is not None:
                row.token_id = token_id
            if clean_question:
                row.question = clean_question
            row.langfuse_trace_id = trace
            row.last_activity_at = ts
            row.updated_at = ts
    return trace


def append_online_event(
    *,
    conversation_id: str,
    turn_id: int,
    event_type: str,
    data: Any,
    secrets: tuple[str, ...] = (),
    now: datetime | None = None,
) -> None:
    """Fold a sanitized non-MCP turn event (e.g. clarification or SQL plan) into the turn row."""
    if event_type in ("token", "thinking", "ping"):
        return
    ts = _as_utc(now)
    session_key = f"online:{conversation_id}:{turn_id}"
    clean_data = _scrub_value(data, secrets)

    with _write_lock, sessions().begin() as db:
        row = (
            db.query(InteractionTrajectory)
            .filter(InteractionTrajectory.session_key == session_key)
            .first()
        )
        if row is None:
            return
        if event_type == "clarification" and isinstance(clean_data, dict):
            clarifications = _loads_list(row.clarifications)
            clarifications.append(clean_data)
            row.clarifications = _dumps(clarifications)
            refs = _extract_refs_from_tool("request_clarification", None, clean_data)
            if refs:
                row.matched_refs = _dumps(
                    _append_unique(_loads_list(row.matched_refs), refs)
                )
        row.last_activity_at = ts
        row.updated_at = ts


def record_mcp_tool_call(
    *,
    project_id: str,
    token_id: str | None,
    conversation_id: str | None,
    turn_id: int | None,
    is_online_turn: bool,
    tool_name: str,
    arguments: Any,
    output: Any,
    is_error: bool,
    secrets: tuple[str, ...] = (),
    now: datetime | None = None,
) -> str:
    """Persist an untruncated scrubbed MCP tool call and return the trajectory traceId."""
    ts = _as_utc(now)

    with _write_lock, sessions().begin() as db:
        if is_online_turn and conversation_id and turn_id is not None:
            session_key = f"online:{conversation_id}:{turn_id}"
            row = (
                db.query(InteractionTrajectory)
                .filter(InteractionTrajectory.session_key == session_key)
                .first()
            )
            if row is None:
                trace = telemetry.trace_id(conversation_id, turn_id)
                row = InteractionTrajectory(
                    project_id=project_id,
                    source_type="online",
                    session_key=session_key,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    token_id=token_id,
                    langfuse_trace_id=trace,
                    status="running",
                    last_activity_at=ts,
                    recorded_at=ts,
                    updated_at=ts,
                )
                db.add(row)
        else:
            tok = token_id or "anon"
            latest = (
                db.query(InteractionTrajectory)
                .filter(
                    InteractionTrajectory.project_id == project_id,
                    InteractionTrajectory.token_id == tok,
                    InteractionTrajectory.source_type == "local_mcp",
                )
                .order_by(
                    InteractionTrajectory.last_activity_at.desc(),
                    InteractionTrajectory.id.desc(),
                )
                .first()
            )
            if (
                latest is not None
                and (ts - _as_utc(latest.last_activity_at)) <= LOCAL_MCP_IDLE_WINDOW
            ):
                row = latest
            else:
                session_key, trace = _next_local_window_identity(
                    db, project_id, tok, ts
                )
                row = InteractionTrajectory(
                    project_id=project_id,
                    source_type="local_mcp",
                    session_key=session_key,
                    token_id=tok,
                    langfuse_trace_id=trace,
                    status="completed",
                    window_started_at=ts,
                    last_activity_at=ts,
                    recorded_at=ts,
                    updated_at=ts,
                )
                db.add(row)

        _apply_tool_call_to_row(
            row,
            tool_name=tool_name,
            arguments=arguments,
            output=output,
            is_error=is_error,
            secrets=secrets,
            now=ts,
        )
        trace_id = row.langfuse_trace_id or uuid.uuid4().hex
    return trace_id


def finish_online_trajectory(
    *,
    conversation_id: str,
    turn_id: int,
    status: str = "completed",
    answer: str = "",
    final_sql: list[dict[str, Any]] | None = None,
    citations: list[dict[str, Any]] | None = None,
    secrets: tuple[str, ...] = (),
    now: datetime | None = None,
) -> None:
    """Finalize an online turn trajectory with complete answer, final SQL, and citations."""
    ts = _as_utc(now)
    session_key = f"online:{conversation_id}:{turn_id}"

    with _write_lock, sessions().begin() as db:
        row = (
            db.query(InteractionTrajectory)
            .filter(InteractionTrajectory.session_key == session_key)
            .first()
        )
        if row is None:
            return
        row.status = status
        if answer or status in ("completed", "failed"):
            row.answer = scrub_text(answer, secrets)
        if final_sql is not None:
            row.final_sql = _dumps(_scrub_value(final_sql, secrets))
        if citations is not None:
            row.citations = _dumps(_scrub_value(citations, secrets))
            citation_refs = [
                c["ref"]
                for c in citations
                if isinstance(c, dict) and isinstance(c.get("ref"), str)
            ]
            if citation_refs:
                row.matched_refs = _dumps(
                    _append_unique(_loads_list(row.matched_refs), citation_refs)
                )
        row.last_activity_at = ts
        row.updated_at = ts


def _serialize_row(row: InteractionTrajectory) -> dict[str, Any]:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "source_type": row.source_type,
        "session_key": row.session_key,
        "conversation_id": row.conversation_id,
        "turn_id": row.turn_id,
        "token_id": row.token_id,
        "langfuse_trace_id": row.langfuse_trace_id,
        "status": row.status,
        "question": row.question,
        "answer": row.answer,
        "tool_calls": _loads_list(row.tool_calls),
        "matched_refs": _loads_list(row.matched_refs),
        "clarifications": _loads_list(row.clarifications),
        "sql_validations": _loads_list(row.sql_validations),
        "final_sql": _loads_list(row.final_sql),
        "citations": _loads_list(row.citations),
        "window_started_at": _as_utc(row.window_started_at)
        if row.window_started_at is not None
        else None,
        "last_activity_at": _as_utc(row.last_activity_at),
        "recorded_at": _as_utc(row.recorded_at),
        "updated_at": _as_utc(row.updated_at),
    }


def list_trajectories(
    project_id: str,
    *,
    after_id: int | None = None,
    source_type: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Read project-scoped trajectories in checkpoint-cursor order."""
    with sessions()() as db:
        query = db.query(InteractionTrajectory).filter(
            InteractionTrajectory.project_id == project_id
        )
        if after_id is not None:
            query = query.filter(InteractionTrajectory.id > after_id)
        if source_type is not None:
            query = query.filter(InteractionTrajectory.source_type == source_type)
        query = query.order_by(InteractionTrajectory.id.asc())
        if limit is not None:
            query = query.limit(limit)
        return [_serialize_row(row) for row in query.all()]


def delete_conversation_trajectories(db: Session, conversation_id: str) -> int:
    """Synchronously delete all trajectory rows belonging to a conversation."""
    return (
        db.query(InteractionTrajectory)
        .filter(InteractionTrajectory.conversation_id == conversation_id)
        .delete(synchronize_session=False)
    )


def delete_project_trajectories(db: Session, project_id: str) -> int:
    """Synchronously delete all trajectory rows belonging to a project."""
    return (
        db.query(InteractionTrajectory)
        .filter(InteractionTrajectory.project_id == project_id)
        .delete(synchronize_session=False)
    )


def delete_expired_local_trajectories(db: Session, cutoff: datetime) -> int:
    """Synchronously purge local_mcp trajectory windows older than the retention cutoff."""
    return (
        db.query(InteractionTrajectory)
        .filter(
            InteractionTrajectory.source_type == "local_mcp",
            InteractionTrajectory.last_activity_at < cutoff,
        )
        .delete(synchronize_session=False)
    )
