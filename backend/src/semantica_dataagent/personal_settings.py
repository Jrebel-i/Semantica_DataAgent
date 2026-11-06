"""Private credentials attached to the authenticated caller."""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .auth import Requester, current_requester
from .crypto import encrypt
from .db import PersonalDtbToken, sessions

router = APIRouter()


class DtbTokenInput(BaseModel):
    token: str = Field(max_length=8192)


class DtbTokenStatus(BaseModel):
    configured: bool


@router.get("/api/v1/me/dtb-token", response_model=DtbTokenStatus)
def token_status(user: Annotated[Requester, Depends(current_requester)]):
    with sessions()() as db:
        return DtbTokenStatus(configured=db.get(PersonalDtbToken, user.id) is not None)


@router.put("/api/v1/me/dtb-token", response_model=DtbTokenStatus)
def save_token(
    body: DtbTokenInput, user: Annotated[Requester, Depends(current_requester)]
):
    with sessions().begin() as db:
        row = db.get(PersonalDtbToken, user.id)
        if not body.token:
            if row is not None:
                db.delete(row)
        elif row is None:
            db.add(
                PersonalDtbToken(owner_id=user.id, encrypted_token=encrypt(body.token))
            )
        else:
            row.encrypted_token = encrypt(body.token)
    return DtbTokenStatus(configured=bool(body.token))
