"""Evidence-backed clarification requests for online and local MCP callers."""

import json
import uuid
from typing import Annotated

from pydantic import BaseModel, Field

from . import knowledge
from .db import Clarification, Conversation, sessions
from .mcp_context import current_conversation_id, current_requester


class ClarificationOption(BaseModel):
    label: str = Field(min_length=1, max_length=500, pattern=r"\S")
    ref: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class ClarificationQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    options: list[ClarificationOption] = Field(min_length=2, max_length=5)


Questions = Annotated[list[ClarificationQuestion], Field(min_length=1, max_length=3)]


def _question(value) -> ClarificationQuestion:
    if isinstance(value, ClarificationQuestion):
        return value
    return ClarificationQuestion.model_validate(value)


def _owned_conversation(conversation_id: str, owner_id: str) -> None:
    with sessions()() as db:
        row = db.get(Conversation, conversation_id)
    if row is None or row.owner_id != owner_id:
        raise ValueError("Conversation not found")


def request_clarification(questions: Questions) -> dict:
    """Ask the questioner to pick an evidence-backed interpretation.

    A clarification never changes retrieval or answer rules.  The caller's
    next message is an ordinary turn in the same conversation, so a choice only
    applies to the question being answered.
    """
    requester = current_requester()
    conversation_id = current_conversation_id()
    if conversation_id is not None:
        _owned_conversation(conversation_id, requester.id)

    normalized = [_question(item) for item in questions]
    if not 1 <= len(normalized) <= 3:
        raise ValueError("Clarification requires 1 to 3 questions")
    refs = [option.ref for question in normalized for option in question.options]
    if not refs:
        raise ValueError("Clarification requires evidence references")
    existing = knowledge.existing_refs(refs)
    missing = sorted(set(refs) - existing)
    if missing:
        raise ValueError(
            "Evidence reference is not in the current knowledge generation: "
            + ", ".join(missing)
        )

    rendered = []
    for question in normalized:
        rendered.append(
            {
                "id": uuid.uuid4().hex,
                "question": question.question,
                "options": [
                    {
                        "id": uuid.uuid4().hex,
                        "label": option.label,
                        "ref": option.ref,
                    }
                    for option in question.options
                ],
            }
        )

    clarification_id = uuid.uuid4().hex
    row = Clarification(
        id=clarification_id,
        owner_id=requester.id,
        conversation_id=conversation_id,
        questions=json.dumps(rendered, ensure_ascii=False),
        status="pending",
    )
    with sessions().begin() as db:
        db.add(row)
    return {
        "clarification_id": clarification_id,
        "conversation_id": conversation_id,
        "status": "pending",
        "questions": rendered,
    }
