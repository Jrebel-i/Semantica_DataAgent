"""Authenticated download of the retrieval skill."""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from .auth import current_user
from .skill import SKILL_FILENAME, read_skill

router = APIRouter()


@router.get(
    "/api/v1/skill",
    response_class=Response,
    responses={
        200: {
            "description": "The retrieval skill as a Markdown attachment",
            "content": {"text/markdown": {"schema": {"type": "string"}}},
        }
    },
)
def download_skill(_: Annotated[str, Depends(current_user)]):
    return Response(
        content=read_skill(),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{SKILL_FILENAME}"'},
    )
