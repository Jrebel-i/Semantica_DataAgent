"""Source-isolated per-project evaluation question bank API and helpers."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import sqlglot
import yaml
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlglot import exp

from . import eval as eval_mod
from . import project_context, sql_validation
from .api_time import UtcDatetime
from .auth import Requester
from .auth import current_requester as rest_requester
from .db import Conversation, CorrectionProposal, Turn, TurnEvent, sessions
from .eval import (
    DEFAULT_CATEGORY,
    DIALECTS,
    SPLITS,
    _columns,
    _table_aliases,
    assign_splits,
    load_questions,
)
from .project_api import (
    admin_project_scope,
    is_project_admin,
    project_scope,
    resolve_project_scope,
)
from .storage_errors import StorageUnavailable

router = APIRouter()

EvalSplit = Literal["dev", "holdout"]
EvalSource = Literal["manual", "feedback", "correction"]
EvalDialect = Literal["hive", "mysql", "clickhouse", "postgresql", "trino"]

NonEmptyText = Annotated[str, Field(min_length=1, max_length=20000, pattern=r"\S")]


class EvalQuestionView(BaseModel):
    id: str
    question: str
    caliber: str | None = None
    reference_sql: str | None = None
    expected_tables: list[str] = Field(default_factory=list)
    expected_fields: list[str] = Field(default_factory=list)
    notes: str = ""
    dialect: str = "hive"
    clarify: bool = False
    category: str = DEFAULT_CATEGORY
    split: EvalSplit = "dev"
    answer_confirmed: bool = False
    source: EvalSource = "manual"
    conversation_id: str | None = None
    turn_id: int | None = None
    proposal_id: str | None = None
    created_by: str | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    redacted: bool = False


class EvalQuestionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, min_length=1, max_length=120)
    question: NonEmptyText
    caliber: NonEmptyText
    reference_sql: NonEmptyText
    expected_tables: list[str] | None = None
    expected_fields: list[str] | None = None
    notes: str = Field(default="", max_length=5000)
    dialect: EvalDialect = "hive"
    clarify: bool = False
    category: str = Field(default=DEFAULT_CATEGORY, min_length=1, max_length=120)
    split: EvalSplit | None = None
    answer_confirmed: bool = True

    @field_validator(
        "id", "question", "caliber", "reference_sql", "notes", "category", mode="before"
    )
    @classmethod
    def _strip_strings(cls, value):
        return value.strip() if isinstance(value, str) else value


class EvalQuestionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: NonEmptyText | None = None
    caliber: NonEmptyText | None = None
    reference_sql: NonEmptyText | None = None
    expected_tables: list[str] | None = None
    expected_fields: list[str] | None = None
    notes: str | None = Field(default=None, max_length=5000)
    dialect: EvalDialect | None = None
    clarify: bool | None = None
    category: str | None = Field(default=None, min_length=1, max_length=120)
    split: EvalSplit | None = None
    answer_confirmed: bool | None = None

    @field_validator(
        "question", "caliber", "reference_sql", "notes", "category", mode="before"
    )
    @classmethod
    def _strip_strings(cls, value):
        return value.strip() if isinstance(value, str) else value


@contextmanager
def _project_eval_lock(project_id: str):
    """Coordinate concurrent YAML read-modify-write via a per-project OS flock (fork-safe)."""
    eval_dir = project_context.project_eval_directory(project_id)
    eval_dir.mkdir(parents=True, exist_ok=True)
    lock_path = eval_dir / ".questions.lock"
    with open(lock_path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield eval_dir
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_write_yaml(path: Path, payload: dict[str, Any]) -> None:
    text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    if payload.get("questions"):
        # Validate before persisting so eval.load_questions / eval-exec always accept it.
        load_questions(path, text=text)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".questions-", suffix=".yaml", dir=path.parent
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _read_raw_rows(project_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = project_context.eval_questions_path(project_id)
    if not path.exists():
        return {"questions": []}, []
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {"questions": []}, []
    try:
        payload = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid question YAML: {exc}") from None
    if payload is None:
        return {"questions": []}, []
    if isinstance(payload, list):
        rows = payload
        payload = {"questions": rows}
    elif isinstance(payload, dict):
        rows = payload.get("questions") or []
        if not isinstance(rows, list):
            raise TypeError("question set 'questions' must be a list")
        payload = dict(payload)
        payload["questions"] = rows
    else:
        raise TypeError("question set must be a mapping or list")
    return payload, [dict(row) for row in rows if isinstance(row, dict)]


def extract_tables_and_fields(
    sql: str, dialect: str = "hive"
) -> tuple[list[str], list[str]]:
    """Extract referenced tables and columns (or table.*) from a SQL query via sqlglot."""
    dialect_name = dialect.lower() if dialect else "hive"
    if dialect_name not in DIALECTS:
        dialect_name = "hive"
    try:
        tree = sqlglot.parse_one(sql, read=dialect_name)
    except (sqlglot.errors.SqlglotError, ValueError) as exc:
        raise ValueError(f"Invalid reference_sql: {exc}") from None
    if tree is None:
        raise ValueError("reference_sql cannot be empty")
    cte_names = {
        cte.alias_or_name.lower()
        for cte in tree.find_all(exp.CTE)
        if getattr(cte, "alias_or_name", None)
    }
    _, aliases = _table_aliases(tree)
    ordered_tables: list[str] = []
    for table in tree.find_all(exp.Table):
        if not table.name:
            continue
        full = f"{table.db}.{table.name}".lower() if table.db else table.name.lower()
        if full in cte_names and not table.db:
            continue
        if full not in ordered_tables:
            ordered_tables.append(full)
    # Remove CTE aliases from alias map so fields qualify cleanly against real tables.
    real_aliases = {k: v for k, v in aliases.items() if v not in cte_names}
    ordered_fields: list[str] = []
    for owner, name in sorted(
        _columns(tree, real_aliases), key=lambda item: (item[0] or "", item[1])
    ):
        if owner in cte_names:
            continue
        resolved_owner = owner or (
            ordered_tables[0] if len(ordered_tables) == 1 else None
        )
        qualified = f"{resolved_owner}.{name}" if resolved_owner else name
        if qualified not in ordered_fields:
            ordered_fields.append(qualified)
    if not ordered_fields and ordered_tables:
        ordered_fields = [f"{table}.*" for table in ordered_tables]
    return ordered_tables, ordered_fields


def _clean_name_list(items: list[str] | None) -> list[str]:
    if not items:
        return []
    cleaned: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _parse_utc(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
            return (
                parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            )
        except ValueError:
            pass
    return fallback


def _assign_stratified_split_for_confirmed(
    all_rows: list[dict[str, Any]],
    target_rows: list[dict[str, Any]],
) -> None:
    """Assign dev/holdout for newly confirmed rows using 70/30 stratified quotas without flipping prior holdouts."""
    confirmed = [row for row in all_rows if row.get("answer_confirmed") is True]
    if not confirmed:
        for row in target_rows:
            row["split"] = "dev"
        return
    simulated = [
        {
            "id": str(row.get("id") or f"tmp-{idx}"),
            "category": str(row.get("category") or DEFAULT_CATEGORY).strip()
            or DEFAULT_CATEGORY,
        }
        for idx, row in enumerate(confirmed)
    ]
    assign_splits(simulated, holdout_ratio=eval_mod.DEFAULT_HOLDOUT_RATIO)
    desired_by_cat: dict[str, int] = {}
    for item in simulated:
        cat = item["category"]
        if item["split"] == "holdout":
            desired_by_cat[cat] = desired_by_cat.get(cat, 0) + 1
    total_desired_holdout = sum(desired_by_cat.values())

    target_ids = {id(row) for row in target_rows}
    pinned = [row for row in confirmed if id(row) not in target_ids]
    existing_by_cat: dict[str, int] = {}
    existing_total_holdout = 0
    for row in pinned:
        if row.get("split") == "holdout":
            cat = (
                str(row.get("category") or DEFAULT_CATEGORY).strip() or DEFAULT_CATEGORY
            )
            existing_by_cat[cat] = existing_by_cat.get(cat, 0) + 1
            existing_total_holdout += 1

    for row in target_rows:
        if row.get("answer_confirmed") is not True:
            row["split"] = "dev"
            continue
        cat = str(row.get("category") or DEFAULT_CATEGORY).strip() or DEFAULT_CATEGORY
        if (
            existing_by_cat.get(cat, 0) < desired_by_cat.get(cat, 0)
            and existing_total_holdout < total_desired_holdout
        ):
            row["split"] = "holdout"
            existing_by_cat[cat] = existing_by_cat.get(cat, 0) + 1
            existing_total_holdout += 1
        else:
            row["split"] = "dev"


def _row_to_view(row: dict[str, Any], *, is_admin: bool) -> EvalQuestionView:
    now = datetime.now(UTC)
    created_at = _parse_utc(row.get("created_at"), now)
    updated_at = _parse_utc(row.get("updated_at"), created_at)
    split_val = str(row.get("split") or "dev").strip().lower()
    split: EvalSplit = "holdout" if split_val == "holdout" else "dev"
    answer_confirmed = bool(row.get("answer_confirmed", False))
    source_val = str(row.get("source") or "manual")
    source: EvalSource = (
        source_val if source_val in ("manual", "feedback", "correction") else "manual"
    )
    should_redact = not is_admin and (split == "holdout" or not answer_confirmed)
    if should_redact:
        return EvalQuestionView(
            id=str(row["id"]),
            question=str(row["question"]),
            caliber=None,
            reference_sql=None,
            expected_tables=[],
            expected_fields=[],
            notes="",
            dialect=str(row.get("dialect") or "hive"),
            clarify=bool(row.get("clarify", False)),
            category=str(row.get("category") or DEFAULT_CATEGORY),
            split=split,
            answer_confirmed=answer_confirmed,
            source=source,
            conversation_id=None,
            turn_id=None,
            proposal_id=None,
            created_by=None,
            created_at=created_at,
            updated_at=updated_at,
            redacted=True,
        )
    return EvalQuestionView(
        id=str(row["id"]),
        question=str(row["question"]),
        caliber=str(row.get("caliber") or ""),
        reference_sql=str(row.get("reference_sql") or ""),
        expected_tables=list(row.get("expected_tables") or []),
        expected_fields=list(row.get("expected_fields") or []),
        notes=str(row.get("notes") or ""),
        dialect=str(row.get("dialect") or "hive"),
        clarify=bool(row.get("clarify", False)),
        category=str(row.get("category") or DEFAULT_CATEGORY),
        split=split,
        answer_confirmed=answer_confirmed,
        source=source,
        conversation_id=row.get("conversation_id"),
        turn_id=row.get("turn_id"),
        proposal_id=row.get("proposal_id"),
        created_by=row.get("created_by"),
        created_at=created_at,
        updated_at=updated_at,
        redacted=False,
    )


def upsert_eval_question(
    project_id: str,
    *,
    question_id: str | None = None,
    match_conversation_turn: tuple[str, int] | None = None,
    match_proposal_id: str | None = None,
    question: str,
    caliber: str,
    reference_sql: str,
    expected_tables: list[str] | None = None,
    expected_fields: list[str] | None = None,
    notes: str = "",
    dialect: str = "hive",
    clarify: bool = False,
    category: str = DEFAULT_CATEGORY,
    split: EvalSplit | None = None,
    answer_confirmed: bool = False,
    source: EvalSource = "manual",
    conversation_id: str | None = None,
    turn_id: int | None = None,
    proposal_id: str | None = None,
    created_by: str | None = None,
) -> dict[str, Any]:
    """Create or update a source-isolated eval question under the per-project lock."""
    extracted_tables, extracted_fields = extract_tables_and_fields(
        reference_sql, dialect=dialect
    )
    resolved_tables = _clean_name_list(expected_tables) or extracted_tables
    resolved_fields = _clean_name_list(expected_fields) or extracted_fields
    if not resolved_tables or not resolved_fields:
        raise ValueError(
            "expected_tables and expected_fields are required when SQL references no tables"
        )
    now_iso = datetime.now(UTC).isoformat()

    with _project_eval_lock(project_id):
        path = project_context.eval_questions_path(project_id)
        payload, rows = _read_raw_rows(project_id)
        existing: dict[str, Any] | None = None
        if match_conversation_turn is not None:
            conv_id, t_id = match_conversation_turn
            existing = next(
                (
                    row
                    for row in rows
                    if row.get("conversation_id") == conv_id
                    and row.get("turn_id") == t_id
                ),
                None,
            )
        if existing is None and match_proposal_id is not None:
            existing = next(
                (row for row in rows if row.get("proposal_id") == match_proposal_id),
                None,
            )
        if (
            existing is None
            and question_id is not None
            and any(str(row.get("id")) == question_id for row in rows)
        ):
            raise ValueError(f"Duplicate question id: {question_id}")

        if existing is not None:
            was_confirmed = bool(existing.get("answer_confirmed", False))
            existing["question"] = question.strip()
            existing["caliber"] = caliber.strip()
            existing["reference_sql"] = reference_sql.strip()
            existing["expected_tables"] = resolved_tables
            existing["expected_fields"] = resolved_fields
            if notes:
                existing["notes"] = notes.strip()
            existing["dialect"] = dialect.lower()
            existing["clarify"] = bool(clarify)
            existing["category"] = (
                category or DEFAULT_CATEGORY
            ).strip() or DEFAULT_CATEGORY
            if proposal_id is not None:
                existing["proposal_id"] = proposal_id
            if conversation_id is not None:
                existing["conversation_id"] = conversation_id
            if turn_id is not None:
                existing["turn_id"] = turn_id
            existing["updated_at"] = now_iso
            if answer_confirmed and not was_confirmed:
                existing["answer_confirmed"] = True
                if split is not None:
                    existing["split"] = split
                else:
                    _assign_stratified_split_for_confirmed(rows, [existing])
            elif split is not None:
                existing["split"] = split
            target_row = existing
        else:
            new_id = question_id or f"eq-{uuid.uuid4().hex[:12]}"
            target_row = {
                "id": new_id,
                "question": question.strip(),
                "expected_tables": resolved_tables,
                "expected_fields": resolved_fields,
                "caliber": caliber.strip(),
                "reference_sql": reference_sql.strip(),
                "notes": notes.strip(),
                "dialect": dialect.lower(),
                "clarify": bool(clarify),
                "category": (category or DEFAULT_CATEGORY).strip() or DEFAULT_CATEGORY,
                "split": split or "dev",
                "answer_confirmed": bool(answer_confirmed),
                "source": source,
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                "proposal_id": proposal_id,
                "created_by": created_by,
                "created_at": now_iso,
                "updated_at": now_iso,
            }
            rows.append(target_row)
            if answer_confirmed and split is None:
                _assign_stratified_split_for_confirmed(rows, [target_row])

        payload["questions"] = rows
        _atomic_write_yaml(path, payload)
        return dict(target_row)


def find_feedback_question_by_turn(
    project_id: str, conversation_id: str, turn_id: int
) -> dict[str, Any] | None:
    """Return an existing feedback question linked to a conversation turn if present."""
    path = project_context.eval_questions_path(project_id)
    if not path.exists():
        return None
    try:
        _, rows = _read_raw_rows(project_id)
    except (ValueError, TypeError):
        return None
    return next(
        (
            row
            for row in rows
            if row.get("conversation_id") == conversation_id
            and row.get("turn_id") == turn_id
        ),
        None,
    )


def list_feedback_turn_ids(
    project_id: str, conversation_id: str
) -> dict[int, dict[str, Any]]:
    """Return mapping of turn_id -> eval question metadata for a conversation."""
    path = project_context.eval_questions_path(project_id)
    if not path.exists():
        return {}
    try:
        _, rows = _read_raw_rows(project_id)
    except (ValueError, TypeError):
        return {}
    result: dict[int, dict[str, Any]] = {}
    for row in rows:
        if row.get("conversation_id") == conversation_id and isinstance(
            row.get("turn_id"), int
        ):
            result[int(row["turn_id"])] = row
    return result


@router.get("/api/v1/eval-questions", response_model=list[EvalQuestionView])
def list_eval_questions(
    scope: Annotated[project_context.ProjectContext, Depends(project_scope)],
    requester: Annotated[Requester, Depends(rest_requester)],
    split: Annotated[EvalSplit | None, Query()] = None,
    answer_confirmed: Annotated[bool | None, Query()] = None,
    category: Annotated[str | None, Query()] = None,
    source: Annotated[EvalSource | None, Query()] = None,
):
    is_admin = is_project_admin(scope, requester)
    try:
        _, rows = _read_raw_rows(scope.project_id)
    except (ValueError, TypeError) as exc:
        raise HTTPException(500, detail=str(exc)) from None
    filtered: list[EvalQuestionView] = []
    for row in rows:
        if split is not None and str(row.get("split") or "dev").lower() != split:
            continue
        if (
            answer_confirmed is not None
            and bool(row.get("answer_confirmed", False)) != answer_confirmed
        ):
            continue
        if (
            category is not None
            and str(row.get("category") or DEFAULT_CATEGORY).strip() != category.strip()
        ):
            continue
        if source is not None and str(row.get("source") or "manual") != source:
            continue
        filtered.append(_row_to_view(row, is_admin=is_admin))
    return filtered


@router.post("/api/v1/eval-questions", response_model=EvalQuestionView, status_code=201)
def create_eval_question(
    body: EvalQuestionCreate,
    scope: Annotated[project_context.ProjectContext, Depends(admin_project_scope)],
    requester: Annotated[Requester, Depends(rest_requester)],
):
    try:
        row = upsert_eval_question(
            scope.project_id,
            question_id=body.id,
            question=body.question,
            caliber=body.caliber,
            reference_sql=body.reference_sql,
            expected_tables=body.expected_tables,
            expected_fields=body.expected_fields,
            notes=body.notes,
            dialect=body.dialect,
            clarify=body.clarify,
            category=body.category,
            split=body.split,
            answer_confirmed=body.answer_confirmed,
            source="manual",
            created_by=requester.id,
        )
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None
    return _row_to_view(row, is_admin=True)


@router.patch("/api/v1/eval-questions/{question_id}", response_model=EvalQuestionView)
def update_eval_question(
    question_id: str,
    body: EvalQuestionPatch,
    scope: Annotated[project_context.ProjectContext, Depends(admin_project_scope)],
):
    fields = body.model_fields_set
    if not fields:
        raise HTTPException(422, detail="At least one field must be updated")
    with _project_eval_lock(scope.project_id):
        path = project_context.eval_questions_path(scope.project_id)
        try:
            payload, rows = _read_raw_rows(scope.project_id)
        except (ValueError, TypeError) as exc:
            raise HTTPException(500, detail=str(exc)) from None
        target = next((row for row in rows if str(row.get("id")) == question_id), None)
        if target is None:
            raise HTTPException(404, detail="Eval question not found")

        was_confirmed = bool(target.get("answer_confirmed", False))
        if "question" in fields and body.question is not None:
            target["question"] = body.question
        if "caliber" in fields and body.caliber is not None:
            target["caliber"] = body.caliber
        if "dialect" in fields and body.dialect is not None:
            target["dialect"] = body.dialect
        if "reference_sql" in fields and body.reference_sql is not None:
            target["reference_sql"] = body.reference_sql
            if "expected_tables" not in fields or "expected_fields" not in fields:
                try:
                    ext_tables, ext_fields = extract_tables_and_fields(
                        body.reference_sql,
                        dialect=str(target.get("dialect") or "hive"),
                    )
                except ValueError as exc:
                    raise HTTPException(422, detail=str(exc)) from None
                if "expected_tables" not in fields and ext_tables:
                    target["expected_tables"] = ext_tables
                if "expected_fields" not in fields and ext_fields:
                    target["expected_fields"] = ext_fields
        if "expected_tables" in fields and body.expected_tables is not None:
            cleaned_tables = _clean_name_list(body.expected_tables)
            if not cleaned_tables:
                raise HTTPException(422, detail="expected_tables must not be empty")
            target["expected_tables"] = cleaned_tables
        if "expected_fields" in fields and body.expected_fields is not None:
            cleaned_fields = _clean_name_list(body.expected_fields)
            if not cleaned_fields:
                raise HTTPException(422, detail="expected_fields must not be empty")
            target["expected_fields"] = cleaned_fields
        if "notes" in fields and body.notes is not None:
            target["notes"] = body.notes
        if "clarify" in fields and body.clarify is not None:
            target["clarify"] = body.clarify
        if "category" in fields and body.category is not None:
            target["category"] = body.category
        if "answer_confirmed" in fields and body.answer_confirmed is not None:
            target["answer_confirmed"] = body.answer_confirmed

        if "split" in fields and body.split is not None:
            if body.split not in SPLITS:
                raise HTTPException(422, detail="split must be dev or holdout")
            target["split"] = body.split
        elif (
            "answer_confirmed" in fields
            and body.answer_confirmed is True
            and not was_confirmed
        ):
            _assign_stratified_split_for_confirmed(rows, [target])

        target["updated_at"] = datetime.now(UTC).isoformat()
        payload["questions"] = rows
        try:
            _atomic_write_yaml(path, payload)
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from None
        return _row_to_view(target, is_admin=True)


@router.delete("/api/v1/eval-questions/{question_id}", status_code=204)
def delete_eval_question(
    question_id: str,
    scope: Annotated[project_context.ProjectContext, Depends(admin_project_scope)],
):
    with _project_eval_lock(scope.project_id):
        path = project_context.eval_questions_path(scope.project_id)
        try:
            payload, rows = _read_raw_rows(scope.project_id)
        except (ValueError, TypeError) as exc:
            raise HTTPException(500, detail=str(exc)) from None
        remaining = [row for row in rows if str(row.get("id")) != question_id]
        if len(remaining) == len(rows):
            raise HTTPException(404, detail="Eval question not found")
        payload["questions"] = remaining
        _atomic_write_yaml(path, payload)
    return Response(status_code=204)


class TurnFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rating: Literal["accurate"] = "accurate"
    reference_sql: str | None = Field(default=None, max_length=20000)
    caliber: str | None = Field(default=None, max_length=5000)
    expected_tables: list[str] | None = None
    expected_fields: list[str] | None = None
    notes: str = Field(default="", max_length=5000)
    dialect: EvalDialect | None = None
    category: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("reference_sql", "caliber", "notes", "category", mode="before")
    @classmethod
    def _strip_optional_strings(cls, value):
        return value.strip() if isinstance(value, str) else value


class TurnFeedbackView(BaseModel):
    rating: Literal["accurate"] = "accurate"
    question_id: str
    answer_confirmed: bool
    split: EvalSplit
    eval_question: EvalQuestionView


def turn_validated_sql(
    sql_blocks: list[dict[str, Any]], events: list[TurnEvent]
) -> tuple[str, str] | None:
    """Return (sql, dialect) when a turn has validate_sql-passed or explicitly validated SQL."""
    for item in reversed(sql_blocks):
        if not isinstance(item, dict):
            continue
        sql_text = str(item.get("sql") or "").strip()
        if sql_text and (item.get("validated") is True or item.get("status") == "pass"):
            dialect = str(item.get("dialect") or "hive").lower()
            return sql_text, (dialect if dialect in DIALECTS else "hive")

    has_validate_pass = False
    for event in events:
        if getattr(event, "type", None) != "tool_end":
            continue
        raw = getattr(event, "data", None)
        if isinstance(raw, str):
            try:
                payload = json.loads(raw)
            except ValueError:
                continue
        elif isinstance(raw, dict):
            payload = raw
        else:
            continue
        if (
            str(payload.get("name") or "").split("__")[-1] == "validate_sql"
            and not payload.get("is_error")
            and "status=pass" in str(payload.get("summary") or "")
        ):
            has_validate_pass = True
            break

    if not has_validate_pass:
        return None

    for item in reversed(sql_blocks):
        if isinstance(item, dict):
            sql_text = str(item.get("sql") or "").strip()
            if sql_text:
                dialect = str(item.get("dialect") or "hive").lower()
                return sql_text, (dialect if dialect in DIALECTS else "hive")
    return None


def validate_sql_if_available(
    sql: str, dialect: str, project_id: str
) -> tuple[bool, str | None]:
    """Validate SQL syntax via sqlglot and semantic correctness via validate_sql when available."""
    cleaned = sql.strip()
    if not cleaned:
        return False, "SQL cannot be blank"
    dialect_name = dialect.lower() if dialect else "hive"
    parse_dialect = dialect_name if dialect_name in DIALECTS else "hive"
    try:
        tables, fields = extract_tables_and_fields(cleaned, dialect=parse_dialect)
    except ValueError as exc:
        return False, str(exc)
    if not tables or not fields:
        return False, "reference_sql must reference at least one table"

    validator_dialect: Literal["hive", "spark", "doris"] = (
        parse_dialect if parse_dialect in ("hive", "spark", "doris") else "hive"  # type: ignore[assignment]
    )
    try:
        result = sql_validation.validate_sql(
            cleaned, dialect=validator_dialect, project_id=project_id
        )
    except (KeyError, StorageUnavailable, ValueError):
        # Offline tests/projects without Neo4j snapshot fall back to verified sqlglot syntax.
        return True, None
    if isinstance(result, dict):
        status = str(result.get("status") or "").lower()
        if status and status != "pass":
            issues = result.get("issues") or []
            first_msg = (
                issues[0].get("message")
                if issues and isinstance(issues[0], dict)
                else None
            )
            return False, str(first_msg or f"validate_sql status={status}")
    return True, None


@router.post(
    "/api/v1/conversations/{conversation_id}/turns/{turn_id}/feedback",
    response_model=TurnFeedbackView,
    status_code=201,
)
def record_turn_feedback(
    conversation_id: str,
    turn_id: int,
    requester: Annotated[Requester, Depends(rest_requester)],
    body: Annotated[TurnFeedbackRequest | None, Body()] = None,
):
    payload = body or TurnFeedbackRequest()
    with sessions()() as db:
        conversation = db.get(Conversation, conversation_id)
        turn = db.get(Turn, turn_id)
        if (
            conversation is None
            or conversation.owner_id != requester.id
            or turn is None
            or turn.conversation_id != conversation_id
        ):
            raise HTTPException(404, detail="Conversation or turn not found")
        sql_blocks = json.loads(turn.sql or "[]")
        events = (
            db.query(TurnEvent)
            .filter(TurnEvent.turn_id == turn_id)
            .order_by(TurnEvent.seq)
            .all()
        )
        turn_question = turn.question
        turn_project_id = conversation.project_id

    scope = resolve_project_scope(turn_project_id, requester, allow_frozen=False)
    is_admin = is_project_admin(scope, requester)

    validated = turn_validated_sql(sql_blocks, events)
    chosen_sql: str | None = None
    chosen_dialect: str = payload.dialect or "hive"
    if payload.reference_sql:
        ok, err = validate_sql_if_available(
            payload.reference_sql, chosen_dialect, scope.project_id
        )
        if not ok:
            raise HTTPException(422, detail=err or "reference_sql failed validation")
        chosen_sql = payload.reference_sql
    elif validated is not None:
        chosen_sql, detected_dialect = validated
        if payload.dialect is None:
            chosen_dialect = detected_dialect
    else:
        raise HTTPException(
            422,
            detail="该轮对话缺少通过 validate_sql 校验的 SQL 或确认参考 SQL",
        )

    caliber = (
        payload.caliber or ""
    ).strip() or f"对话确认口径：{turn_question.strip()}"
    try:
        saved = upsert_eval_question(
            scope.project_id,
            match_conversation_turn=(conversation_id, turn_id),
            question=turn_question,
            caliber=caliber,
            reference_sql=chosen_sql,
            expected_tables=payload.expected_tables,
            expected_fields=payload.expected_fields,
            notes=payload.notes,
            dialect=chosen_dialect,
            category=payload.category or DEFAULT_CATEGORY,
            answer_confirmed=is_admin,
            source="feedback",
            conversation_id=conversation_id,
            turn_id=turn_id,
            created_by=requester.id,
        )
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None

    view = _row_to_view(saved, is_admin=is_admin)
    return TurnFeedbackView(
        rating=payload.rating,
        question_id=view.id,
        answer_confirmed=bool(saved.get("answer_confirmed", False)),
        split="holdout" if saved.get("split") == "holdout" else "dev",
        eval_question=view,
    )


def promote_correction_to_eval_question(
    proposal_id: str,
    *,
    approval_eval: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Promote or append a validated-SQL approved correction into confirmed 评测题集."""
    with sessions()() as db:
        row = db.get(CorrectionProposal, proposal_id)
        if row is None or row.status not in {"approved", "auto_applied"}:
            return None
        project_id = row.project_id
        conversation_id = row.conversation_id
        owner_id = row.owner_id
        reviewer_id = row.reviewer_id
        reason = row.reason
        gate_raw = json.loads(row.gate_report) if row.gate_report else {}
        structured = json.loads(row.structured_patch) if row.structured_patch else None

    candidate: dict[str, Any] = {}
    if isinstance(gate_raw, dict) and isinstance(gate_raw.get("eval_candidate"), dict):
        candidate.update(gate_raw["eval_candidate"])
    if approval_eval:
        for key, value in approval_eval.items():
            if value is not None:
                candidate[key] = value

    turn_id: int | None = candidate.get("turn_id")
    question: str | None = candidate.get("question")
    reference_sql: str | None = candidate.get("reference_sql")
    caliber: str | None = candidate.get("caliber")
    dialect: str = str(candidate.get("dialect") or "hive")
    category: str = str(candidate.get("category") or DEFAULT_CATEGORY)
    expected_tables = candidate.get("expected_tables")
    expected_fields = candidate.get("expected_fields")
    already_turn_validated = False

    # Check existing feedback draft or validated-SQL turn in linked conversation.
    existing_draft: dict[str, Any] | None = None
    if conversation_id:
        path = project_context.eval_questions_path(project_id)
        if path.exists():
            try:
                _, rows = _read_raw_rows(project_id)
            except (ValueError, TypeError):
                rows = []
            for item in rows:
                if item.get("conversation_id") != conversation_id:
                    continue
                if turn_id is not None and item.get("turn_id") != turn_id:
                    continue
                existing_draft = item
                break
        if existing_draft is not None:
            turn_id = turn_id or existing_draft.get("turn_id")
            question = question or existing_draft.get("question")
            reference_sql = reference_sql or existing_draft.get("reference_sql")
            caliber = caliber or existing_draft.get("caliber")
            dialect = str(
                candidate.get("dialect") or existing_draft.get("dialect") or "hive"
            )
            already_turn_validated = True

    if (not reference_sql or not question) and conversation_id:
        with sessions()() as db:
            turn_query = db.query(Turn).filter(Turn.conversation_id == conversation_id)
            if turn_id is not None:
                turn_query = turn_query.filter(Turn.id == turn_id)
            else:
                turn_query = turn_query.order_by(Turn.sequence.desc())
            turns = turn_query.all()
            for turn in turns:
                sql_blocks = json.loads(turn.sql or "[]")
                events = (
                    db.query(TurnEvent)
                    .filter(TurnEvent.turn_id == turn.id)
                    .order_by(TurnEvent.seq)
                    .all()
                )
                validated = turn_validated_sql(sql_blocks, events)
                if validated is not None:
                    sql_str, det_dialect = validated
                    question = question or turn.question
                    reference_sql = reference_sql or sql_str
                    turn_id = turn_id or turn.id
                    if not candidate.get("dialect"):
                        dialect = det_dialect
                    already_turn_validated = True
                    break

    if not reference_sql and isinstance(structured, dict):
        metrics = structured.get("metrics") or {}
        if isinstance(metrics, dict):
            for metric_spec in metrics.values():
                if not isinstance(metric_spec, dict):
                    continue
                expr = str(metric_spec.get("expression") or "").strip()
                if expr.lower().startswith(("select ", "with ")):
                    reference_sql = expr
                    question = (
                        question
                        or f"指标「{metric_spec.get('name') or '口径'}」如何计算？"
                    )
                    caliber = (
                        caliber
                        or str(metric_spec.get("description") or reason or "").strip()
                    )
                    break

    if not reference_sql:
        return None

    # Always run semantic SQL validator when available so explicitly invalid SQL is excluded.
    ok, _ = validate_sql_if_available(reference_sql, dialect, project_id)
    if not ok and not already_turn_validated:
        return None
    if not ok and candidate.get("reference_sql"):
        return None

    final_question = (question or reason or "").strip()
    final_caliber = (caliber or reason or final_question).strip()
    if not final_question or not final_caliber:
        return None

    try:
        return upsert_eval_question(
            project_id,
            match_conversation_turn=(
                (conversation_id, turn_id)
                if conversation_id and turn_id is not None
                else None
            ),
            match_proposal_id=proposal_id,
            question=final_question,
            caliber=final_caliber,
            reference_sql=reference_sql,
            expected_tables=expected_tables,
            expected_fields=expected_fields,
            dialect=dialect,
            category=category,
            answer_confirmed=True,
            source="correction",
            conversation_id=conversation_id,
            turn_id=turn_id,
            proposal_id=proposal_id,
            created_by=reviewer_id or owner_id,
        )
    except ValueError:
        return None
