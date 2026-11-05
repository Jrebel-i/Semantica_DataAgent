"""Record and browse knowledge gaps."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .auth import current_user
from .db import KnowledgeGap, sessions
from .mcp_context import current_requester

router = APIRouter()
Text = Annotated[str, Field(min_length=1, max_length=20000, pattern=r"\S")]


def report_knowledge_gap(question: Text, missing: Text) -> dict:
    """Record missing knowledge that prevents answering a question."""
    requester = current_requester()
    gap_id = uuid.uuid4().hex
    with sessions().begin() as db:
        db.add(
            KnowledgeGap(
                id=gap_id, owner_id=requester.id, question=question, missing=missing
            )
        )
    return {"gap_id": gap_id}


class GapView(BaseModel):
    id: str
    owner_id: str
    question: str
    missing: str
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("/api/v1/knowledge-gaps", response_model=list[GapView])
def list_gaps(_role: Annotated[str, Depends(current_user)]):
    with sessions()() as db:
        return [
            GapView.model_validate(row)
            for row in db.query(KnowledgeGap)
            .order_by(KnowledgeGap.created_at.desc())
            .all()
        ]
