"""Natural-language assisted correction drafting with target routing and L1 precheck."""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import knowledge_synthesizer
from .auth import Requester
from .db import Conversation, Turn, sessions
from .knowledge_synthesizer import (
    LLMUnavailableError,
    generate_with_llm,
)
from .knowledge_synthesizer import (
    fetch_node as _fetch_node,
)
from .project_api import resolve_project_scope

TargetKind = Literal["document", "override", "ossie"]


class DraftCorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str | None = Field(default=None, max_length=120)
    conversation_id: str | None = Field(default=None, max_length=120)
    turn_id: int | None = None
    target: str | None = Field(default=None, max_length=2000)
    category: str | None = Field(default=None, max_length=120)
    description: str = Field(min_length=1, max_length=10000, pattern=r"\S")
    provider_id: str | None = None
    model: str | None = None

    @field_validator(
        "project",
        "conversation_id",
        "target",
        "category",
        "provider_id",
        "model",
        mode="before",
    )
    @classmethod
    def _strip_optional_text(cls, value):
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @field_validator("description", mode="before")
    @classmethod
    def _strip_description(cls, value):
        return value.strip() if isinstance(value, str) else value


class DraftCorrectionResponse(BaseModel):
    target: str
    target_kind: TargetKind
    patch: str
    structured_patch: dict[str, Any] | None = None
    diff: str
    gate_report: dict[str, Any]
    resolved_path: str
    reason: str
    source_generated: bool = False
    is_new_file: bool = False
    caliber: str | None = None
    reference_sql: str | None = None
    used_llm: bool = False
    is_duplicate: bool = False
    duplicate_of: str | None = None
    duplicate_reason: str | None = None


def draft_correction_in_project(
    body: DraftCorrectionRequest,
    requester: Requester,
    *,
    query_project: str | None = None,
) -> DraftCorrectionResponse:
    """Build a natural-language assisted correction preview via the unified LLM synthesizer."""
    scope = resolve_project_scope(
        body.project or query_project, requester, allow_frozen=False
    )

    question = ""
    answer = ""
    sql_blocks: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    if body.conversation_id:
        with sessions()() as db:
            conv = db.get(Conversation, body.conversation_id)
            if (
                conv is None
                or conv.owner_id != requester.id
                or conv.project_id != scope.project_id
            ):
                raise HTTPException(404, detail="Conversation not found")
            if body.turn_id is not None:
                turn = db.get(Turn, body.turn_id)
                if turn is None or turn.conversation_id != body.conversation_id:
                    raise HTTPException(404, detail="Turn not found")
            else:
                turn = (
                    db.query(Turn)
                    .filter(Turn.conversation_id == body.conversation_id)
                    .order_by(Turn.sequence.desc())
                    .first()
                )
                if turn is None:
                    raise HTTPException(404, detail="Turn not found")
            question = turn.question
            answer = turn.answer
            sql_blocks = json.loads(turn.sql or "[]")
            citations = json.loads(turn.citations or "[]")

    raw_evidence = {
        "conversation_id": body.conversation_id,
        "turn_id": body.turn_id,
        "question": question,
        "answer": answer,
        "citations": citations,
        "category": body.category,
        "description": body.description,
    }
    proposal = knowledge_synthesizer.synthesize_knowledge(
        project_id=scope.project_id,
        source_type="manual",
        description=body.description,
        raw_evidence={k: v for k, v in raw_evidence.items() if v not in (None, "", [])},
        target=body.target,
        category=body.category,
        sql_blocks=sql_blocks,
        provider_id=body.provider_id,
        model=body.model,
        llm_fn=generate_with_llm,
    )
    return DraftCorrectionResponse(
        target=proposal.target,
        target_kind=proposal.target_kind,
        patch=proposal.patch,
        structured_patch=proposal.structured_patch,
        diff=proposal.diff,
        gate_report=proposal.gate_report,
        resolved_path=proposal.resolved_path,
        reason=proposal.reason,
        source_generated=proposal.source_generated,
        is_new_file=proposal.is_new_file,
        caliber=proposal.caliber,
        reference_sql=proposal.reference_sql,
        used_llm=proposal.used_llm,
        is_duplicate=proposal.is_duplicate,
        duplicate_of=proposal.duplicate_of,
        duplicate_reason=proposal.duplicate_reason,
    )


__all__ = [
    "DraftCorrectionRequest",
    "DraftCorrectionResponse",
    "LLMUnavailableError",
    "TargetKind",
    "_fetch_node",
    "draft_correction_in_project",
    "generate_with_llm",
]
