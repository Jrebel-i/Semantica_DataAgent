"""Record and browse knowledge gaps."""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import update

from .api_time import UtcDatetime
from .auth import Requester
from .auth import current_requester as rest_requester
from .db import KnowledgeGap, sessions
from .mcp_context import current_requester
from .project_api import admin_project_scope, is_project_admin, project_scope
from .project_audit import actor_name
from .project_context import ProjectContext

router = APIRouter()
Text = Annotated[str, Field(min_length=1, max_length=20000, pattern=r"\S")]


def report_knowledge_gap(
    question: Text, missing: Text, project_id: str = "default"
) -> dict:
    """Record missing knowledge that prevents answering a question."""
    requester = current_requester()
    project_id = project_id or "default"
    gap_id = uuid.uuid4().hex
    with sessions().begin() as db:
        db.add(
            KnowledgeGap(
                id=gap_id,
                project_id=project_id,
                owner_id=requester.id,
                question=question,
                missing=missing,
            )
        )
    return {"gap_id": gap_id}


class GapView(BaseModel):
    id: str
    project_id: str
    owner_id: str
    question: str
    missing: str
    created_at: UtcDatetime
    status: str
    reviewed_at: UtcDatetime | None
    reviewer_id: str | None
    reviewer_name: str | None
    can_review: bool = False

    model_config = {"from_attributes": True}


@router.get("/api/v1/knowledge-gaps", response_model=list[GapView])
def list_gaps(
    scope: Annotated[ProjectContext, Depends(project_scope)],
    requester: Annotated[Requester, Depends(rest_requester)],
):
    can_review = is_project_admin(scope, requester)
    with sessions()() as db:
        return [
            GapView.model_validate(row).model_copy(update={"can_review": can_review})
            for row in db.query(KnowledgeGap)
            .filter(KnowledgeGap.project_id == scope.project_id)
            .order_by(KnowledgeGap.created_at.desc())
            .all()
        ]


class GapReview(BaseModel):
    status: Literal["confirmed", "rejected"]


@router.post("/api/v1/knowledge-gaps/{gap_id}/review", response_model=GapView)
def review_gap(
    gap_id: str,
    body: GapReview,
    scope: Annotated[ProjectContext, Depends(admin_project_scope)],
    requester: Annotated[Requester, Depends(rest_requester)],
):
    with sessions().begin() as db:
        result = db.execute(
            update(KnowledgeGap)
            .where(
                KnowledgeGap.id == gap_id,
                KnowledgeGap.project_id == scope.project_id,
                KnowledgeGap.status == "pending",
            )
            .values(
                status=body.status,
                reviewed_at=datetime.now(UTC),
                reviewer_id=requester.id,
                reviewer_name=actor_name(db, requester),
            )
        )
        if result.rowcount != 1:
            exists = (
                db.query(KnowledgeGap.id)
                .filter_by(id=gap_id, project_id=scope.project_id)
                .first()
            )
            raise HTTPException(
                409 if exists else 404,
                "Knowledge gap already reviewed"
                if exists
                else "Knowledge gap not found",
            )
        return GapView.model_validate(db.get(KnowledgeGap, gap_id)).model_copy(
            update={"can_review": True}
        )
