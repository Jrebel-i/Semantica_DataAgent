"""Project listing, membership, and system-admin metadata updates."""

from __future__ import annotations

import os
import re
import shutil
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func

from .api_time import UtcDatetime
from .auth import Requester, admin, current_requester
from .db import (
    GenerationPointer,
    KnowledgeGeneration,
    Project,
    ProjectMember,
    ProjectSetting,
    Token,
    sessions,
)
from .project_context import (
    DEFAULT_PROJECT_ID,
    GENERATION_PREFIX_KEY,
    SOURCE_DIRECTORY_KEY,
    ProjectContext,
    ProjectNotFound,
    current_project,
    project_source_directory,
    projects_root,
)

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


class ProjectCreate(ProjectUpdate):
    type: ProjectType


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


class ProjectNameTaken(ValueError):
    """A project with the requested display name already exists."""


def project_scope(
    requester: Annotated[Requester, Depends(current_requester)],
    project: Annotated[str | None, Query()] = None,
) -> ProjectContext:
    """Resolve the requested project and apply the transitional permission rule."""
    try:
        context = current_project(project)
    except ProjectNotFound as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc
    if context.project_id != DEFAULT_PROJECT_ID and requester.role != "admin":
        raise HTTPException(status_code=404, detail="Project not found")
    return context


def admin_project_scope(
    scope: Annotated[ProjectContext, Depends(project_scope)],
    _: Annotated[str, Depends(admin)],
) -> ProjectContext:
    """Require an administrator after resolving the project.

    Resolving first keeps non-default projects invisible (404) to regular
    users instead of leaking their existence with a 403.
    """
    return scope


def project_id_candidate(name: str) -> str:
    """Build a storage-safe id from an ASCII view of the project name."""
    base = re.sub(r"[^a-z0-9]+", "", name.lower())[:24]
    if not base:
        base = "project"
    if not base[0].isalpha():
        base = ("p" + base)[:24]
    return base


def _unique_project_id(db, name: str) -> str:
    base = project_id_candidate(name)
    candidate = base
    suffix = 2
    while candidate == DEFAULT_PROJECT_ID or db.get(Project, candidate) is not None:
        tail = str(suffix)
        candidate = base[: 24 - len(tail)] + tail
        suffix += 1
    return candidate


def _remove_empty_root(root) -> None:
    try:
        root.rmdir()
    except OSError:
        pass


def create_project_record(name: str, project_type: ProjectType) -> Project:
    """Create a project, its settings, initial generation and source directory."""
    name = name.strip()
    if not name:
        raise ValueError("Project name is required")
    root = projects_root()
    staging = root / f".creating-{uuid.uuid4().hex}"
    with sessions().begin() as db:
        if (
            db.query(Project).filter(func.lower(Project.name) == name.lower()).first()
            is not None
        ):
            raise ProjectNameTaken(name)
        project_id = _unique_project_id(db, name)
        final_source = project_source_directory(project_id)
        root.mkdir(parents=True, exist_ok=True)
        (staging / "source").mkdir(parents=True, exist_ok=False)
        try:
            project = Project(
                id=project_id,
                name=name,
                type=project_type,
                status="active",
            )
            db.add(project)
            db.flush()
            db.add_all(
                [
                    ProjectSetting(
                        project_id=project_id,
                        key=SOURCE_DIRECTORY_KEY,
                        value=str(final_source),
                    ),
                    ProjectSetting(
                        project_id=project_id,
                        key=GENERATION_PREFIX_KEY,
                        value=project_id + "_",
                    ),
                ]
            )
            generation = project_id + "_initial"
            db.add(KnowledgeGeneration(name=generation, project_id=project_id))
            db.add(GenerationPointer(project_id=project_id, generation=generation))
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            _remove_empty_root(root)
            raise
    try:
        os.rename(staging, final_source.parent)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        with sessions().begin() as db:
            db.query(ProjectSetting).filter(
                ProjectSetting.project_id == project_id
            ).delete()
            db.query(KnowledgeGeneration).filter(
                KnowledgeGeneration.project_id == project_id
            ).delete()
            db.query(GenerationPointer).filter(
                GenerationPointer.project_id == project_id
            ).delete()
            db.query(Project).filter(Project.id == project_id).delete()
        _remove_empty_root(root)
        raise
    with sessions()() as db:
        return db.get(Project, project_id)


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


@router.post("/api/v1/projects", response_model=ProjectView, status_code=201)
def create_project(
    body: ProjectCreate,
    _: Annotated[str, Depends(admin)],
):
    try:
        project = create_project_record(body.name, body.type)
    except ProjectNameTaken as exc:
        raise HTTPException(
            status_code=409, detail="Project name already exists"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _view(project, "admin")


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
