"""Authenticated download of the retrieval skill for one project type."""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from .project_api import project_scope
from .project_context import ProjectContext
from .skill import read_skill, skill_filename

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
def download_skill(scope: Annotated[ProjectContext, Depends(project_scope)]):
    filename = skill_filename(scope.project_type)
    return Response(
        content=read_skill(scope.project_type),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
