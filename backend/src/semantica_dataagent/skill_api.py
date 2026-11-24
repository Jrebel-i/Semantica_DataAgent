"""Authenticated download of the retrieval skill for one project type."""

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, StrictBool
from sqlalchemy import delete, text, update
from sqlalchemy.exc import IntegrityError

from .api_time import UtcDatetime
from .auth import Requester, admin, current_requester
from .db import CatalogSkill, CatalogSkillProject, Project, SkillAudit, Token, sessions
from .project_api import project_scope, require_query_project_access
from .project_context import ProjectContext
from .skill import read_skill, skill_filename
from .skill_catalog import MAX_PACKAGE_BYTES, builtins, validate_package, view

router = APIRouter()


class SkillEnabled(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


class SkillMetadata(BaseModel):
    name: str
    description: str
    project_types: list[Literal["warehouse", "document"]]
    projects: list[str]
    secrets: list[str]
    dependencies: list[str]
    trust_warning: str | None


class SkillView(SkillMetadata):
    source: Literal["builtin", "upload"]
    protected: bool
    enabled: bool


class SkillAuditView(BaseModel):
    actor_id: str
    action: str
    skill_name: str
    created_at: UtcDatetime


def _begin_admin_write(db, actor: Requester):
    db.execute(text("BEGIN IMMEDIATE"))
    token = (
        db.query(Token)
        .filter_by(id=actor.id, revoked=False, role="admin")
        .one_or_none()
    )
    if token is None:
        raise HTTPException(403, "Admin required")


@router.get("/api/v1/skills", response_model=list[SkillView])
def list_skills(_admin: Annotated[str, Depends(admin)]):
    with sessions()() as db:
        return builtins() + [
            view(row) for row in db.query(CatalogSkill).order_by(CatalogSkill.name)
        ]


async def _read_package(request: Request):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_PACKAGE_BYTES:
            raise HTTPException(413, "Skill package too large")
        data.extend(chunk)
    try:
        metadata = validate_package(bytes(data))
    except ValueError as exc:
        raise HTTPException(422, "Invalid Skill package") from exc
    return bytes(data), metadata


_ZIP_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "application/zip": {"schema": {"type": "string", "format": "binary"}}
        },
    }
}


@router.post(
    "/api/v1/skills/preview", response_model=SkillMetadata, openapi_extra=_ZIP_BODY
)
async def preview_skill(request: Request, _admin: Annotated[str, Depends(admin)]):
    _, metadata = await _read_package(request)
    return metadata


@router.get("/api/v1/skills/audit", response_model=list[SkillAuditView])
def skill_audit(
    _admin: Annotated[str, Depends(admin)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    with sessions()() as db:
        rows = (
            db.query(SkillAudit)
            .order_by(SkillAudit.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return [
            SkillAuditView(
                actor_id=row.actor_id,
                action=row.action,
                skill_name=row.skill_name,
                created_at=row.created_at,
            )
            for row in rows
        ]


@router.post(
    "/api/v1/skills", status_code=201, response_model=SkillView, openapi_extra=_ZIP_BODY
)
async def upload_skill(
    request: Request,
    _admin: Annotated[str, Depends(admin)],
    actor: Annotated[Requester, Depends(current_requester)],
):
    data, metadata = await _read_package(request)
    if metadata["name"] in {item["name"] for item in builtins()}:
        raise HTTPException(409, "Skill name already exists")
    row = CatalogSkill(
        name=metadata["name"],
        description=metadata["description"],
        metadata_json=json.dumps(metadata),
        archive=bytes(data),
        enabled=False,
    )
    try:
        with sessions().begin() as db:
            _begin_admin_write(db, actor)
            db.add(row)
            db.flush()
            for project_id in metadata["projects"]:
                project = db.get(Project, project_id)
                if project is None or project.status != "active":
                    raise HTTPException(422, "Invalid Skill project")
                db.add(CatalogSkillProject(skill_name=row.name, project_id=project_id))
            db.add(SkillAudit(actor_id=actor.id, action="upload", skill_name=row.name))
            db.flush()
            result = view(row)
    except IntegrityError as exc:
        raise HTTPException(409, "Skill name already exists") from exc
    return result


@router.get("/api/v1/skills/available", response_model=list[SkillView])
def available_skills(
    scope: Annotated[ProjectContext, Depends(project_scope)],
    actor: Annotated[Requester, Depends(current_requester)],
):
    with sessions()() as db:
        project = require_query_project_access(
            db, scope.project_id, actor, not_found_detail="Project not found"
        )
        items = builtins() + [
            view(row) for row in db.query(CatalogSkill).filter_by(enabled=True)
        ]
        allowed = {
            row.skill_name
            for row in db.query(CatalogSkillProject).filter_by(
                project_id=scope.project_id
            )
        }
        return [
            item
            for item in items
            if project.type in item["project_types"]
            and (not item["projects"] or item["name"] in allowed)
        ]


def _protect_builtin(name: str) -> None:
    if name in {item["name"] for item in builtins()}:
        raise HTTPException(409, "Built-in Skill is protected")


@router.put("/api/v1/skills/{name}/enabled", response_model=SkillView)
def set_skill_enabled(
    name: str,
    body: SkillEnabled,
    _admin: Annotated[str, Depends(admin)],
    actor: Annotated[Requester, Depends(current_requester)],
):
    _protect_builtin(name)
    with sessions().begin() as db:
        _begin_admin_write(db, actor)
        result = db.execute(
            update(CatalogSkill)
            .where(CatalogSkill.name == name)
            .values(enabled=body.enabled)
        )
        if not result.rowcount:
            raise HTTPException(404, "Skill not found")
        db.add(
            SkillAudit(
                actor_id=actor.id,
                action="enable" if body.enabled else "disable",
                skill_name=name,
            )
        )
        return view(db.get(CatalogSkill, name))


@router.delete("/api/v1/skills/{name}", status_code=204)
def delete_skill(
    name: str,
    _admin: Annotated[str, Depends(admin)],
    actor: Annotated[Requester, Depends(current_requester)],
):
    _protect_builtin(name)
    with sessions().begin() as db:
        _begin_admin_write(db, actor)
        result = db.execute(delete(CatalogSkill).where(CatalogSkill.name == name))
        if not result.rowcount:
            raise HTTPException(404, "Skill not found")
        db.execute(
            delete(CatalogSkillProject).where(CatalogSkillProject.skill_name == name)
        )
        db.add(SkillAudit(actor_id=actor.id, action="delete", skill_name=name))
    return Response(status_code=204)


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
