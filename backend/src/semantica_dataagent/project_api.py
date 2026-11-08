"""Project listing, membership, audit, and system-admin metadata updates."""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, text

from .api_time import UtcDatetime
from .auth import Requester, admin, current_requester
from .db import (
    GenerationPointer,
    KnowledgeGeneration,
    Project,
    ProjectAudit,
    ProjectMember,
    ProjectSetting,
    Setting,
    Token,
    sessions,
)
from .project_audit import actor_name, record_project_audit
from .project_context import (
    DEFAULT_PROJECT_ID,
    GENERATION_PREFIX_KEY,
    SCHEDULE_INTERVAL_KEY,
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
TokenRole = Literal["user", "admin"]


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


class ProjectMemberView(BaseModel):
    id: str
    name: str
    role: ProjectRole


class ProjectMemberUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: ProjectRole


class ProjectUserView(BaseModel):
    id: str
    name: str
    role: TokenRole


class ProjectAuditView(BaseModel):
    id: int
    project_id: str
    actor_id: str
    actor_name: str
    action: str
    detail: dict
    created_at: UtcDatetime


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


def _accessible_project_ids(db, requester: Requester) -> list[str]:
    if requester.role == "admin":
        return [
            row.id
            for row in db.query(Project.id).order_by(Project.created_at, Project.id)
        ]
    return [
        row.project_id
        for row in db.query(ProjectMember.project_id)
        .join(Project, Project.id == ProjectMember.project_id)
        .filter(ProjectMember.token_id == requester.id)
        .order_by(Project.created_at, Project.id)
        .all()
    ]


def _resolve_project_context(
    project_id: str | None, requester: Requester
) -> ProjectContext:
    with sessions()() as db:
        if project_id is not None:
            if db.get(Project, project_id) is None:
                raise HTTPException(status_code=404, detail="Project not found")
            if (
                requester.role != "admin"
                and db.get(ProjectMember, (project_id, requester.id)) is None
            ):
                raise HTTPException(status_code=404, detail="Project not found")
            resolved = project_id
        else:
            accessible = _accessible_project_ids(db, requester)
            if DEFAULT_PROJECT_ID in accessible:
                resolved = DEFAULT_PROJECT_ID
            elif len(accessible) == 1:
                resolved = accessible[0]
            elif accessible:
                raise HTTPException(status_code=422, detail="请指定项目")
            else:
                raise HTTPException(status_code=422, detail="请指定项目")
    try:
        return current_project(resolved)
    except ProjectNotFound as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc


def _require_project_admin(
    scope: ProjectContext, requester: Requester
) -> ProjectContext:
    if requester.role == "admin":
        return scope
    with sessions()() as db:
        member = db.get(ProjectMember, (scope.project_id, requester.id))
        if member is not None and member.role == "admin":
            return scope
    raise HTTPException(status_code=403, detail="Project admin required")


def project_scope(
    requester: Annotated[Requester, Depends(current_requester)],
    project: Annotated[str | None, Query()] = None,
) -> ProjectContext:
    """Resolve the requested project and apply the membership read rule.

    An explicit ``project`` must exist and the caller must be a system
    administrator or a member of it.  Without an explicit project the default
    project is used when accessible; otherwise a caller with exactly one
    accessible project gets that project, and everyone else must specify one.
    """
    return _resolve_project_context(project, requester)


def admin_project_scope(
    scope: Annotated[ProjectContext, Depends(project_scope)],
    requester: Annotated[Requester, Depends(current_requester)],
) -> ProjectContext:
    """Require a system administrator or a project administrator."""
    return _require_project_admin(scope, requester)


def project_path_scope(
    project_id: str,
    requester: Annotated[Requester, Depends(current_requester)],
) -> ProjectContext:
    return _resolve_project_context(project_id, requester)


def admin_project_path_scope(
    scope: Annotated[ProjectContext, Depends(project_path_scope)],
    requester: Annotated[Requester, Depends(current_requester)],
) -> ProjectContext:
    return _require_project_admin(scope, requester)


def project_scope_by_path(
    project_id: str,
    requester: Annotated[Requester, Depends(current_requester)],
) -> ProjectContext:
    """Resolve a project named in the URL through the shared read dependency."""
    return project_scope(requester, project=project_id)


def admin_project_scope_by_path(
    project_id: str,
    requester: Annotated[Requester, Depends(current_requester)],
) -> ProjectContext:
    """Resolve a URL project and apply the same admin rules as the query form."""
    scope = project_scope(requester, project=project_id)
    admin(requester.role)
    return admin_project_scope(scope, requester.role)


class ProjectScheduleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interval_seconds: int = Field(ge=0, le=604800)


class ProjectScheduleView(BaseModel):
    interval_seconds: int


def _schedule_interval(db, project_id: str) -> int:
    if project_id == DEFAULT_PROJECT_ID:
        row = db.get(Setting, SCHEDULE_INTERVAL_KEY)
    else:
        row = db.get(ProjectSetting, (project_id, SCHEDULE_INTERVAL_KEY))
    return int(row.value) if row is not None and row.value else 0


def _save_schedule_interval(db, project_id: str, interval: int) -> None:
    value = str(interval)
    if project_id == DEFAULT_PROJECT_ID:
        row = db.get(Setting, SCHEDULE_INTERVAL_KEY)
        if row is None:
            db.add(Setting(key=SCHEDULE_INTERVAL_KEY, value=value))
        else:
            row.value = value
    else:
        row = db.get(ProjectSetting, (project_id, SCHEDULE_INTERVAL_KEY))
        if row is None:
            db.add(
                ProjectSetting(
                    project_id=project_id,
                    key=SCHEDULE_INTERVAL_KEY,
                    value=value,
                )
            )
        else:
            row.value = value


@router.get(
    "/api/v1/projects/{project_id}/schedule", response_model=ProjectScheduleView
)
def get_project_schedule(
    project_id: str,
    scope: Annotated[ProjectContext, Depends(admin_project_scope_by_path)],
):
    with sessions()() as db:
        return ProjectScheduleView(
            interval_seconds=_schedule_interval(db, scope.project_id)
        )


@router.put(
    "/api/v1/projects/{project_id}/schedule", response_model=ProjectScheduleView
)
def set_project_schedule(
    project_id: str,
    body: ProjectScheduleUpdate,
    scope: Annotated[ProjectContext, Depends(admin_project_scope_by_path)],
):
    with sessions().begin() as db:
        _save_schedule_interval(db, scope.project_id, body.interval_seconds)
    from .scheduler import scheduler

    scheduler.configure(body.interval_seconds, project_id=scope.project_id)
    return ProjectScheduleView(interval_seconds=body.interval_seconds)


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


def create_project_record(
    name: str,
    project_type: ProjectType,
    *,
    actor_id: str | None = None,
    actor_name: str | None = None,
) -> Project:
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
        try:
            (staging / "source").mkdir(parents=True, exist_ok=False)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            _remove_empty_root(root)
            raise
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
            if actor_id is not None:
                record_project_audit(
                    db,
                    project_id=project_id,
                    actor_id=actor_id,
                    actor_name=actor_name or actor_id,
                    action="project.create",
                    detail={"name": name, "type": project_type},
                )
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            _remove_empty_root(root)
            raise
    try:
        os.rename(staging, final_source.parent)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        with sessions().begin() as db:
            db.query(ProjectAudit).filter(
                ProjectAudit.project_id == project_id
            ).delete()
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
    requester: Annotated[Requester, Depends(current_requester)],
    _: Annotated[str, Depends(admin)],
):
    try:
        with sessions()() as db:
            name = actor_name(db, requester)
        project = create_project_record(
            body.name,
            body.type,
            actor_id=requester.id,
            actor_name=name,
        )
    except ProjectNameTaken as exc:
        raise HTTPException(
            status_code=409, detail="Project name already exists"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    from .scheduler import scheduler

    scheduler.configure(0, project_id=project.id)
    return _view(project, "admin")


@router.patch("/api/v1/projects/{project_id}", response_model=ProjectView)
def rename_project(
    project_id: str,
    body: ProjectUpdate,
    requester: Annotated[Requester, Depends(current_requester)],
    _: Annotated[str, Depends(admin)],
):
    with sessions().begin() as db:
        project = db.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        previous = project.name
        project.name = body.name
        db.flush()
        if body.name != previous:
            record_project_audit(
                db,
                project_id=project_id,
                actor_id=requester.id,
                actor_name=actor_name(db, requester),
                action="project.rename",
                detail={"from": previous, "to": body.name},
            )
        return _view(project, "admin")


def _member_admin_count(db, project_id: str) -> int:
    return (
        db.query(ProjectMember)
        .join(Token, Token.id == ProjectMember.token_id)
        .filter(
            ProjectMember.project_id == project_id,
            ProjectMember.role == "admin",
            Token.revoked.is_(False),
        )
        .count()
    )


@router.get(
    "/api/v1/projects/{project_id}/members",
    response_model=list[ProjectMemberView],
)
def list_project_members(
    project_id: str,
    scope: Annotated[ProjectContext, Depends(admin_project_path_scope)],
):
    with sessions()() as db:
        rows = (
            db.query(Token)
            .join(ProjectMember, ProjectMember.token_id == Token.id)
            .filter(
                ProjectMember.project_id == scope.project_id,
                Token.revoked.is_(False),
            )
            .order_by(Token.name, Token.id)
            .all()
        )
        roles = {
            member.token_id: member.role
            for member in db.query(ProjectMember)
            .filter(ProjectMember.project_id == scope.project_id)
            .all()
        }
        return [
            ProjectMemberView(id=row.id, name=row.name, role=roles[row.id])
            for row in rows
        ]


@router.put(
    "/api/v1/projects/{project_id}/members/{token_id}",
    response_model=ProjectMemberView,
)
def put_project_member(
    project_id: str,
    token_id: str,
    body: ProjectMemberUpdate,
    scope: Annotated[ProjectContext, Depends(admin_project_path_scope)],
    requester: Annotated[Requester, Depends(current_requester)],
):
    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        token = (
            db.query(Token)
            .filter(Token.id == token_id, Token.revoked.is_(False))
            .first()
        )
        if token is None:
            raise HTTPException(status_code=404, detail="Token not found")
        if token.role == "admin":
            raise HTTPException(
                status_code=422,
                detail="System administrator tokens cannot be project members",
            )
        member = db.get(ProjectMember, (scope.project_id, token_id))
        previous = member.role if member is not None else None
        if (
            previous == "admin"
            and body.role != "admin"
            and _member_admin_count(db, scope.project_id) <= 1
        ):
            raise HTTPException(
                status_code=409,
                detail="Cannot remove or demote the last project administrator",
            )
        if member is None:
            member = ProjectMember(
                project_id=scope.project_id, token_id=token_id, role=body.role
            )
            db.add(member)
            action = "member.add"
        else:
            member.role = body.role
            action = "member.update"
        db.flush()
        record_project_audit(
            db,
            project_id=scope.project_id,
            actor_id=requester.id,
            actor_name=actor_name(db, requester),
            action=action,
            detail={
                "token_id": token_id,
                "token_name": token.name,
                "from": previous,
                "to": body.role,
            },
        )
        return ProjectMemberView(id=token.id, name=token.name, role=body.role)


@router.delete(
    "/api/v1/projects/{project_id}/members/{token_id}",
    status_code=204,
)
def delete_project_member(
    project_id: str,
    token_id: str,
    scope: Annotated[ProjectContext, Depends(admin_project_path_scope)],
    requester: Annotated[Requester, Depends(current_requester)],
):
    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        token = (
            db.query(Token)
            .filter(Token.id == token_id, Token.revoked.is_(False))
            .first()
        )
        if token is None:
            raise HTTPException(status_code=404, detail="Token not found")
        member = db.get(ProjectMember, (scope.project_id, token_id))
        if member is None:
            raise HTTPException(status_code=404, detail="Membership not found")
        if member.role == "admin" and _member_admin_count(db, scope.project_id) <= 1:
            raise HTTPException(
                status_code=409,
                detail="Cannot remove or demote the last project administrator",
            )
        db.delete(member)
        db.flush()
        record_project_audit(
            db,
            project_id=scope.project_id,
            actor_id=requester.id,
            actor_name=actor_name(db, requester),
            action="member.remove",
            detail={
                "token_id": token_id,
                "token_name": token.name,
                "from": member.role,
                "to": None,
            },
        )


@router.get("/api/v1/users", response_model=list[ProjectUserView])
def list_users(requester: Annotated[Requester, Depends(current_requester)]):
    with sessions()() as db:
        if requester.role != "admin":
            manages = (
                db.query(ProjectMember)
                .filter(
                    ProjectMember.token_id == requester.id,
                    ProjectMember.role == "admin",
                )
                .first()
            )
            if manages is None:
                raise HTTPException(status_code=403, detail="Project admin required")
        return [
            ProjectUserView(id=row.id, name=row.name, role=row.role)
            for row in db.query(Token)
            .filter(Token.revoked.is_(False))
            .order_by(Token.name, Token.id)
            .all()
        ]


@router.get(
    "/api/v1/projects/{project_id}/audit",
    response_model=list[ProjectAuditView],
)
def list_project_audit(
    project_id: str,
    scope: Annotated[ProjectContext, Depends(admin_project_path_scope)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    with sessions()() as db:
        rows = (
            db.query(ProjectAudit)
            .filter(ProjectAudit.project_id == scope.project_id)
            .order_by(ProjectAudit.created_at.desc(), ProjectAudit.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        result = []
        for row in rows:
            try:
                detail = json.loads(row.detail)
            except (TypeError, json.JSONDecodeError):
                detail = {}
            result.append(
                ProjectAuditView(
                    id=row.id,
                    project_id=row.project_id,
                    actor_id=row.actor_id,
                    actor_name=row.actor_name,
                    action=row.action,
                    detail=detail if isinstance(detail, dict) else {},
                    created_at=row.created_at,
                )
            )
        return result
