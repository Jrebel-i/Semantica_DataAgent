"""Project audit recording shared by project and membership endpoints."""

from __future__ import annotations

import json

from .db import ProjectAudit, Token


def actor_name(db, requester) -> str:
    row = db.query(Token).filter(Token.id == requester.id).first()
    return row.name if row is not None else requester.id


def record_project_audit(
    db, *, project_id: str, actor_id: str, actor_name: str, action: str, detail: dict
) -> None:
    db.add(
        ProjectAudit(
            project_id=project_id,
            actor_id=actor_id,
            actor_name=actor_name,
            action=action,
            detail=json.dumps(detail, ensure_ascii=False, sort_keys=True),
        )
    )
