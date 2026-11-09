"""Project listing, membership, audit, and system-admin metadata updates."""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, text

from . import auto_build, project_context
from .api_time import UtcDatetime
from .auth import Requester, admin, current_requester
from .db import (
    GenerationPointer,
    KnowledgeGeneration,
    Project,
    ProjectAudit,
    ProjectDeletion,
    ProjectMember,
    ProjectSetting,
    Setting,
    Token,
    sessions,
)
from .project_audit import actor_name, record_project_audit
from .project_context import (
    ARCHIVED_DETAIL,
    DEFAULT_PROJECT_ID,
    FROZEN_STATUSES,
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
ProjectStatus = Literal["active", "archived", "deleting"]
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


class ProjectDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)


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


def _accessible_projects_query(db, requester: Requester):
    query = db.query(Project)
    if requester.role != "admin":
        query = query.join(
            ProjectMember, ProjectMember.project_id == Project.id
        ).filter(ProjectMember.token_id == requester.id)
    return query.order_by(Project.created_at, Project.id)


def accessible_projects(requester: Requester) -> list[Project]:
    """Return the projects this caller may read, in stable creation order."""
    with sessions()() as db:
        return list(_accessible_projects_query(db, requester))


def _accessible_project_ids(db, requester: Requester) -> list[str]:
    return [row.id for row in _accessible_projects_query(db, requester)]


def _resolve_project_context(
    project_id: str | None, requester: Requester, *, allow_frozen: bool = False
) -> ProjectContext:
    with sessions()() as db:
        if project_id is not None:
            project = db.get(Project, project_id)
            if project is None:
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
            project = db.get(Project, resolved)
        if (
            not allow_frozen
            and project is not None
            and project.status in FROZEN_STATUSES
        ):
            raise HTTPException(status_code=409, detail=ARCHIVED_DETAIL)
    try:
        return current_project(resolved)
    except ProjectNotFound as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc


def resolve_project_scope(
    project_id: str, requester: Requester, *, allow_frozen: bool = False
) -> ProjectContext:
    """Public entry to the single access rule used by REST and MCP."""
    return _resolve_project_context(project_id, requester, allow_frozen=allow_frozen)


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
    return _resolve_project_context(project_id, requester, allow_frozen=True)


def admin_project_path_scope(
    scope: Annotated[ProjectContext, Depends(project_path_scope)],
    requester: Annotated[Requester, Depends(current_requester)],
) -> ProjectContext:
    return _require_project_admin(scope, requester)


def admin_project_path_write_scope(
    project_id: str,
    requester: Annotated[Requester, Depends(current_requester)],
) -> ProjectContext:
    scope = _resolve_project_context(project_id, requester)
    return _require_project_admin(scope, requester)


ProjectSourceState = Literal["pending", "building", "effective", "failed", "unknown"]


class AutoBuildUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class AutoBuildView(BaseModel):
    enabled: bool


class ProjectSourceStatus(BaseModel):
    state: ProjectSourceState
    due_in_seconds: int | None
    last_build_id: int | None
    source_commit: str | None
    auto_build: bool
    error: str | None


class ProjectScheduleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interval_seconds: int = Field(ge=0, le=604800)


class ProjectScheduleView(BaseModel):
    interval_seconds: int


class AdapterPullUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    parameters: dict = Field(default_factory=dict)


class ProjectAdapterUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    enabled: bool
    parameters: dict = Field(default_factory=dict)
    pull: AdapterPullUpdate | None = None


class ProjectAdaptersUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapters: list[ProjectAdapterUpdate]


class AdapterPullView(BaseModel):
    enabled: bool
    parameters: dict
    parameters_schema: dict
    token_configured: bool


class ProjectAdapterView(BaseModel):
    id: str
    name: str
    project_types: list[str]
    enabled: bool
    parameters: dict
    parameters_schema: dict
    compatible: bool
    reason: str | None
    pull: AdapterPullView | None = None


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
    scope: Annotated[ProjectContext, Depends(admin_project_path_scope)],
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
    scope: Annotated[ProjectContext, Depends(admin_project_path_scope)],
):
    with sessions().begin() as db:
        _save_schedule_interval(db, scope.project_id, body.interval_seconds)
    from .scheduler import scheduler

    scheduler.configure(body.interval_seconds, project_id=scope.project_id)
    return ProjectScheduleView(interval_seconds=body.interval_seconds)


@router.get("/api/v1/projects/{project_id}/auto-build", response_model=AutoBuildView)
def get_project_auto_build(
    project_id: str,
    scope: Annotated[ProjectContext, Depends(admin_project_path_scope)],
):
    return AutoBuildView(enabled=auto_build.auto_build_enabled(scope.project_id))


@router.put("/api/v1/projects/{project_id}/auto-build", response_model=AutoBuildView)
def set_project_auto_build(
    project_id: str,
    body: AutoBuildUpdate,
    scope: Annotated[ProjectContext, Depends(admin_project_path_scope)],
):
    with sessions().begin() as db:
        row = db.get(ProjectSetting, (scope.project_id, auto_build.AUTO_BUILD_KEY))
        value = "true" if body.enabled else "false"
        if row is None:
            db.add(
                ProjectSetting(
                    project_id=scope.project_id,
                    key=auto_build.AUTO_BUILD_KEY,
                    value=value,
                )
            )
        else:
            row.value = value
        if not body.enabled:
            auto_build.discard_pending_builds(scope.project_id, db)
    return AutoBuildView(enabled=body.enabled)


ADAPTER_DEFAULT_ENABLED = frozenset({"canonical", "markdown"})
DTB_PULL_TOKEN_KEY = "puller.dtb.token"
PROJECT_TYPE_LABELS = {"warehouse": "数仓型", "document": "文档型"}


def _adapter_reason(project_type: str, supported: tuple[str, ...]) -> str:
    labels = "、".join(PROJECT_TYPE_LABELS.get(item, item) for item in supported)
    return f"该适配器仅支持{labels}项目，不能用于{PROJECT_TYPE_LABELS.get(project_type, project_type)}项目"


def _project_adapters(scope: ProjectContext) -> list[ProjectAdapterView]:
    from .plugins import discover
    from .project_types import source_project_types, supports_project_type

    discovered = discover()
    saved = project_context.plugin_configs(scope.project_id)
    rows = []
    for (category, plugin_id), plugin in sorted(
        discovered.items(), key=lambda item: item[1].id
    ):
        if category != "source":
            continue
        supported = source_project_types(plugin)
        compatible = supports_project_type(plugin, scope.project_type)
        enabled, parameters = saved.get(
            (category, plugin_id),
            (plugin_id in ADAPTER_DEFAULT_ENABLED, {}),
        )
        if not compatible:
            enabled = False
        pull = None
        if plugin_id == "dtb":
            puller = discovered.get(("puller", "dtb"))
            pull_enabled, pull_parameters = saved.get(("puller", "dtb"), (False, {}))
            if not compatible:
                pull_enabled = False
            pull = AdapterPullView(
                enabled=pull_enabled,
                parameters={
                    key: value
                    for key, value in pull_parameters.items()
                    if key != "token"
                },
                parameters_schema=(
                    puller.parameters_schema if puller is not None else {}
                ),
                token_configured=bool(
                    project_context.plugin_secret(scope.project_id, DTB_PULL_TOKEN_KEY)
                ),
            )
        rows.append(
            ProjectAdapterView(
                id=plugin_id,
                name=plugin.display_name,
                project_types=list(supported),
                enabled=enabled,
                parameters=parameters,
                parameters_schema=plugin.parameters_schema,
                compatible=compatible,
                reason=None
                if compatible
                else _adapter_reason(scope.project_type, supported),
                pull=pull,
            )
        )
    return rows


@router.get(
    "/api/v1/projects/{project_id}/adapters",
    response_model=list[ProjectAdapterView],
)
def list_project_adapters(
    project_id: str,
    scope: Annotated[ProjectContext, Depends(project_path_scope)],
):
    return _project_adapters(scope)


@router.put(
    "/api/v1/projects/{project_id}/adapters",
    response_model=list[ProjectAdapterView],
)
def put_project_adapters(
    project_id: str,
    body: ProjectAdaptersUpdate,
    scope: Annotated[ProjectContext, Depends(admin_project_path_write_scope)],
):
    from jsonschema import Draft202012Validator

    from .crypto import encrypt
    from .plugins import discover
    from .project_types import source_project_types, supports_project_type

    discovered = discover()
    seen = set()
    for update in body.adapters:
        if update.id in seen:
            raise HTTPException(status_code=422, detail=f"适配器重复：{update.id}")
        seen.add(update.id)
        plugin = discovered.get(("source", update.id))
        if plugin is None:
            raise HTTPException(status_code=422, detail=f"未知源适配器：{update.id}")
        errors = sorted(
            Draft202012Validator(plugin.parameters_schema).iter_errors(
                update.parameters
            ),
            key=lambda error: list(map(str, error.absolute_path)),
        )
        if errors:
            raise HTTPException(
                status_code=422,
                detail=[
                    {"path": list(error.absolute_path), "message": error.message}
                    for error in errors
                ],
            )
        compatible = supports_project_type(plugin, scope.project_type)
        if update.enabled and not compatible:
            raise HTTPException(
                status_code=422,
                detail=_adapter_reason(
                    scope.project_type, source_project_types(plugin)
                ),
            )
        if update.pull is not None:
            if update.id != "dtb":
                raise HTTPException(status_code=422, detail="该适配器没有拉取配置")
            puller = discovered.get(("puller", "dtb"))
            if puller is None:
                raise HTTPException(status_code=422, detail="DTB 拉取器未注册")
            errors = sorted(
                Draft202012Validator(puller.parameters_schema).iter_errors(
                    update.pull.parameters
                ),
                key=lambda error: list(map(str, error.absolute_path)),
            )
            if errors:
                raise HTTPException(
                    status_code=422,
                    detail=[
                        {
                            "path": list(error.absolute_path),
                            "message": error.message,
                        }
                        for error in errors
                    ],
                )
            if update.pull.enabled and not compatible:
                raise HTTPException(
                    status_code=422,
                    detail=_adapter_reason(
                        scope.project_type, source_project_types(plugin)
                    ),
                )
    with sessions().begin() as db:
        schedules = []
        for update in body.adapters:
            project_context.upsert_plugin_config(
                db,
                "source",
                update.id,
                update.enabled,
                update.parameters,
                scope.project_id,
            )
            if update.pull is not None:
                parameters = dict(update.pull.parameters)
                token = parameters.pop("token", None)
                project_context.upsert_plugin_config(
                    db,
                    "puller",
                    "dtb",
                    update.pull.enabled,
                    parameters,
                    scope.project_id,
                )
                if token is not None and token != "***":
                    project_context.set_plugin_secret(
                        db,
                        scope.project_id,
                        DTB_PULL_TOKEN_KEY,
                        encrypt(token) if token else None,
                    )
                interval = (
                    update.pull.parameters.get("interval_seconds", 0)
                    if update.pull.enabled
                    else 0
                )
                schedules.append((scope.project_id, interval))
    from .scheduler import pull_scheduler

    for target, interval in schedules:
        pull_scheduler.configure(interval, target)
    return _project_adapters(scope)


@router.get(
    "/api/v1/projects/{project_id}/source-status",
    response_model=ProjectSourceStatus,
)
def get_project_source_status(
    project_id: str,
    scope: Annotated[ProjectContext, Depends(project_path_scope)],
):
    return ProjectSourceStatus(**auto_build.source_status(scope.project_id))


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
        db.query(ProjectDeletion).filter(
            ProjectDeletion.project_id == project_id,
            ProjectDeletion.status == "completed",
        ).delete(synchronize_session=False)
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


@router.post("/api/v1/projects/{project_id}/archive", response_model=ProjectView)
def archive_project_route(
    project_id: str,
    requester: Annotated[Requester, Depends(current_requester)],
    _: Annotated[str, Depends(admin)],
):
    from .project_deletion import archive_project

    with sessions()() as db:
        name = actor_name(db, requester)
    archive_project(project_id, requester.id, name)
    with sessions()() as db:
        project = db.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        return _view(project, "admin")


@router.post("/api/v1/projects/{project_id}/restore", response_model=ProjectView)
def restore_project_route(
    project_id: str,
    requester: Annotated[Requester, Depends(current_requester)],
    _: Annotated[str, Depends(admin)],
):
    from .project_deletion import restore_project

    with sessions()() as db:
        name = actor_name(db, requester)
    restore_project(project_id, requester.id, name)
    with sessions()() as db:
        project = db.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        return _view(project, "admin")


@router.post("/api/v1/projects/{project_id}/delete", status_code=204)
def delete_project_route(
    project_id: str,
    body: ProjectDeleteRequest,
    requester: Annotated[Requester, Depends(current_requester)],
    _: Annotated[str, Depends(admin)],
):
    from .project_deletion import prepare_deletion, run_deletion

    with sessions()() as db:
        name = actor_name(db, requester)
    state = prepare_deletion(project_id, body.name, requester.id, name)
    if state == "completed":
        return Response(status_code=204)
    try:
        run_deletion(project_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="项目删除失败，可重试") from exc
    return Response(status_code=204)


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
