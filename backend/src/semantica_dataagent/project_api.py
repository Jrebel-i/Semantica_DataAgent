"""Project listing, membership, and system-admin metadata updates."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .api_time import UtcDatetime
from .auth import Requester, admin, current_requester
from .db import Project, ProjectMember, Token, sessions
from .project_context import DEFAULT_PROJECT_ID

router = APIRouter()

ProjectType = Literal["warehouse", "document"]
ProjectStatus = Literal["active", "archived"]
ProjectRole = Literal["admin", "member"]


class ProjectUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, value):
        return value.strip() if isinstance(value, str) else value


class ProjectView(BaseModel):
    id: str
    name: str
    type: ProjectType
    status: ProjectStatus
    created_at: UtcDatetime
    my_role: ProjectRole


def ensure_default_project_member(db, token: Token) -> None:
    """Add a new regular user token to the default project.

    System administrators are implicit administrators of every project and do
    not need a membership row.
    """
    if token.role != "user":
        return
    key = (DEFAULT_PROJECT_ID, token.id)
    if db.get(ProjectMember, key) is None:
        db.add(
            ProjectMember(
                project_id=DEFAULT_PROJECT_ID,
                token_id=token.id,
                role="member",
            )
        )


def _view(project: Project, my_role: ProjectRole) -> ProjectView:
    return ProjectView(
        id=project.id,
        name=project.name,
        type=project.type,
        status=project.status,
        created_at=project.created_at,
        my_role=my_role,
    )


@router.get("/api/v1/projects", response_model=list[ProjectView])
def list_projects(
    requester: Annotated[Requester, Depends(current_requester)],
):
    with sessions()() as db:
        projects = db.query(Project).order_by(Project.created_at, Project.id)
        if requester.role == "admin":
            return [_view(project, "admin") for project in projects.all()]
        memberships = {
            member.project_id: member.role
            for member in db.query(ProjectMember)
            .filter(ProjectMember.token_id == requester.id)
            .all()
        }
        if not memberships:
            return []
        return [
            _view(project, memberships[project.id])
            for project in projects.filter(Project.id.in_(memberships)).all()
        ]


@router.patch("/api/v1/projects/{project_id}", response_model=ProjectView)
def rename_project(
    project_id: str,
    body: ProjectUpdate,
    _: Annotated[str, Depends(admin)],
):
    with sessions().begin() as db:
        project = db.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        project.name = body.name
        db.flush()
        return _view(project, "admin")
