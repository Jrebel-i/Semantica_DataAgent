from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .db import Token, digest, sessions

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Requester:
    id: str
    role: str
    token: str


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
