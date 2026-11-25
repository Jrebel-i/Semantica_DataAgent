"""Authenticated download of the retrieval skill for one project type."""

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, StrictBool
from sqlalchemy import delete, exists, func, or_, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import load_only
from starlette.concurrency import run_in_threadpool

from .api_time import UtcDatetime
from .auth import Requester, admin, current_requester
from .db import (
    CatalogSkill,
    CatalogSkillProject,
    Project,
    SkillAudit,
    SkillSecret,
    Token,
    sessions,
)
from .project_api import active_skill_project, project_scope, require_skill_project
from .project_context import ProjectContext
from .skill import read_skill, skill_filename
from .skill_catalog import (
    MAX_PACKAGE_BYTES,
    TRUST_WARNING,
    builtins,
    prepare_skill,
    require_prepared_skill,
    validate_package,
    view,
)

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


class AvailableSkillView(SkillView):
    configured_secrets: list[str] = []
    missing_secrets: list[str] = []
    ready: bool = True


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
            view(row)
            for row in db.query(CatalogSkill)
            .options(
                load_only(
                    CatalogSkill.name, CatalogSkill.metadata_json, CatalogSkill.enabled
                )
            )
            .order_by(CatalogSkill.name)
        ]


async def _read_package(request: Request):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_PACKAGE_BYTES:
            raise HTTPException(413, "Skill package too large")
        data.extend(chunk)
    try:
        metadata = await run_in_threadpool(validate_package, bytes(data))
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
    return {**metadata, "trust_warning": TRUST_WARNING}


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
    return await run_in_threadpool(_persist_skill, data, metadata, actor)


def _persist_skill(data: bytes, metadata: dict, actor: Requester):
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


@router.get("/api/v1/skills/available", response_model=list[AvailableSkillView])
def available_skills(
    scope: Annotated[ProjectContext, Depends(project_scope)],
    actor: Annotated[Requester, Depends(current_requester)],
):
    with sessions()() as db:
        project_type = require_skill_project(db, actor, scope.project_id)
        allowed = exists().where(
            CatalogSkillProject.skill_name == CatalogSkill.name,
            CatalogSkillProject.project_id == scope.project_id,
        )
        rows = (
            db.query(CatalogSkill)
            .options(
                load_only(
                    CatalogSkill.name, CatalogSkill.metadata_json, CatalogSkill.enabled
                )
            )
            .filter(CatalogSkill.enabled.is_(True))
            .filter(active_skill_project(actor, scope.project_id, project_type))
            .filter(
                or_(
                    func.json_array_length(
                        func.json_extract(CatalogSkill.metadata_json, "$.projects")
                    )
                    == 0,
                    allowed,
                )
            )
        )
        items = builtins() + [view(row) for row in rows]
        stored_by_skill: dict[str, set[str]] = {}
        for secret_row in db.query(
            SkillSecret.skill_name, SkillSecret.secret_name
        ).filter_by(owner_id=actor.id, project_id=scope.project_id):
            stored_by_skill.setdefault(secret_row.skill_name, set()).add(
                secret_row.secret_name
            )
        require_skill_project(db, actor, scope.project_id)
        result = []
        for item in items:
            if project_type not in item["project_types"]:
                continue
            declared = item["secrets"]
            configured_set = stored_by_skill.get(item["name"], set())
            configured_secrets = [s for s in declared if s in configured_set]
            missing_secrets = [s for s in declared if s not in configured_set]
            result.append(
                {
                    **item,
                    "projects": [scope.project_id] if item["projects"] else [],
                    "configured_secrets": configured_secrets,
                    "missing_secrets": missing_secrets,
                    "ready": not missing_secrets,
                }
            )
        return result


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
        db.execute(delete(SkillSecret).where(SkillSecret.skill_name == name))
        db.execute(
            delete(CatalogSkillProject).where(CatalogSkillProject.skill_name == name)
        )
        db.add(SkillAudit(actor_id=actor.id, action="delete", skill_name=name))
    return Response(status_code=204)


@router.get("/api/v1/skills/{name}/secrets", response_model=dict[str, bool])
def secret_status(
    name: str,
    scope: Annotated[ProjectContext, Depends(project_scope)],
    actor: Annotated[Requester, Depends(current_requester)],
):
    version, skill = prepare_skill(actor, scope.project_id, name)
    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        require_prepared_skill(db, actor, scope.project_id, name, version)
        stored = {
            row.secret_name
            for row in db.query(SkillSecret.secret_name).filter_by(
                owner_id=actor.id, project_id=scope.project_id, skill_name=name
            )
        }
        return {key: key in stored for key in skill.secrets}


@router.put(
    "/api/v1/skills/{name}/secrets/{secret_name}",
    status_code=204,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["value"],
                        "additionalProperties": False,
                        "properties": {
                            "value": {
                                "type": "string",
                                "format": "password",
                                "writeOnly": True,
                            }
                        },
                    }
                }
            },
        }
    },
)
async def put_secret(
    name: str,
    secret_name: str,
    request: Request,
    scope: Annotated[ProjectContext, Depends(project_scope)],
    actor: Annotated[Requester, Depends(current_requester)],
):
    try:
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 16384:
                raise ValueError()
            raw.extend(chunk)
        body = json.loads(raw)
        if (
            not isinstance(body, dict)
            or set(body) != {"value"}
            or not isinstance(body["value"], str)
            or not body["value"]
            or len(body["value"].encode()) > 8192
        ):
            raise ValueError()
    except (ValueError, UnicodeError):
        raise HTTPException(422, "Invalid secret") from None
    await run_in_threadpool(
        _store_secret, name, secret_name, body["value"], scope.project_id, actor
    )
    return Response(status_code=204)


def _store_secret(name, secret_name, value, project_id, actor):
    from .crypto import encrypt

    version, skill = prepare_skill(actor, project_id, name)
    ciphertext = encrypt(value)
    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        require_prepared_skill(db, actor, project_id, name, version)
        if secret_name not in skill.secrets:
            raise HTTPException(404, "Secret declaration not found")
        row = db.get(SkillSecret, (actor.id, project_id, name, secret_name))
        if row is None:
            db.add(
                SkillSecret(
                    owner_id=actor.id,
                    project_id=project_id,
                    skill_name=name,
                    secret_name=secret_name,
                    ciphertext=ciphertext,
                )
            )
        else:
            row.ciphertext = ciphertext


@router.delete("/api/v1/skills/{name}/secrets/{secret_name}", status_code=204)
def delete_secret(
    name: str,
    secret_name: str,
    scope: Annotated[ProjectContext, Depends(project_scope)],
    actor: Annotated[Requester, Depends(current_requester)],
):
    version, skill = prepare_skill(actor, scope.project_id, name)
    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        require_prepared_skill(db, actor, scope.project_id, name, version)
        if secret_name not in skill.secrets:
            raise HTTPException(404, "Secret declaration not found")
        db.execute(
            delete(SkillSecret).where(
                SkillSecret.owner_id == actor.id,
                SkillSecret.project_id == scope.project_id,
                SkillSecret.skill_name == name,
                SkillSecret.secret_name == secret_name,
            )
        )
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
