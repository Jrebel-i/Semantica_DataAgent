import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .db import AgentCredential, Conversation, Token, Turn, digest, sessions

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Requester:
    id: str
    role: str
    token: str
    project_id: str | None = None


def _authenticated(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Requester:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    with sessions().begin() as db:
        token = db.get(Token, digest(credentials.credentials))
        if token is None or token.revoked:
            raise HTTPException(
                status_code=401,
                detail="Invalid token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        now = datetime.now(UTC).replace(tzinfo=None)
        if token.last_used_at is None or token.last_used_at < now - timedelta(
            minutes=1
        ):
            token.last_used_at = now
        return Requester(id=token.id, role=token.role, token=credentials.credentials)


def issue_agent_credential(
    owner_id: str,
    conversation_id: str,
    turn_id: int,
    project_id: str = "default",
) -> str:
    token = secrets.token_urlsafe(32)
    with sessions().begin() as db:
        conversation = db.get(Conversation, conversation_id)
        turn = db.get(Turn, turn_id)
        if (
            conversation is None
            or conversation.owner_id != owner_id
            or turn is None
            or turn.conversation_id != conversation_id
            or turn.status != "running"
        ):
            raise HTTPException(409, "Agent turn is not running")
        db.query(AgentCredential).filter_by(turn_id=turn_id).delete()
        db.add(
            AgentCredential(
                digest=digest(token),
                owner_id=owner_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                project_id=project_id,
            )
        )
    return token


def authenticate_mcp(credentials, conversation_id, turn_id) -> Requester:
    try:
        return _authenticated(credentials)
    except HTTPException:
        pass
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(401, "Invalid token")
    with sessions()() as db:
        scoped = db.get(AgentCredential, digest(credentials.credentials))
        if (
            scoped is None
            or scoped.conversation_id != conversation_id
            or str(scoped.turn_id) != turn_id
        ):
            raise HTTPException(401, "Invalid token")
        turn = db.get(Turn, scoped.turn_id)
        owner = db.query(Token).filter_by(id=scoped.owner_id, revoked=False).first()
        if (
            turn is None
            or turn.status != "running"
            or turn.conversation_id != conversation_id
            or owner is None
        ):
            raise HTTPException(401, "Invalid token")
        return Requester(
            id=owner.id,
            role=owner.role,
            token=credentials.credentials,
            project_id=scoped.project_id,
        )


def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    return _authenticated(credentials).role


def current_requester(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Requester:
    return _authenticated(credentials)


def admin(role: Annotated[str, Depends(current_user)]) -> str:
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin required")
    return role
