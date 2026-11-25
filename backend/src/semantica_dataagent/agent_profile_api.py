"""Admin CRUD for named Agent Profiles (#328)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from .agent_runner import get_runner
from .agent_runner_api import is_runner_available
from .api_time import UtcDatetime
from .auth import admin
from .db import (
    AgentProfile,
    AgentRunnerRecord,
    Provider,
    UserAgentDefault,
    sessions,
)

router = APIRouter()


class AgentProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200, pattern=r"\S")
    runner_id: str = Field(min_length=1, max_length=64, pattern=r"\S")
    enabled: bool = True
    is_system_default: bool = False
    default_provider_id: str | None = None
    default_model: str | None = None
    provider_models: dict[str, list[str]] = Field(default_factory=dict)


class AgentProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200, pattern=r"\S")
    runner_id: str | None = None
    enabled: bool | None = None
    is_system_default: bool | None = None
    default_provider_id: str | None = None
    default_model: str | None = None
    provider_models: dict[str, list[str]] | None = None
    version: int = Field(ge=1)


class AgentProfileView(BaseModel):
    id: str
    name: str
    runner_id: str
    enabled: bool
    is_system_default: bool
    default_provider_id: str | None
    default_model: str | None
    provider_models: dict[str, list[str]]
    missing_models: dict[str, list[str]]
    pending_verification: dict[str, list[str]]
    runner_available: bool
    version: int
    created_at: UtcDatetime
    updated_at: UtcDatetime


def _parse_model_map(raw: str | None) -> dict[str, list[str]]:
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return _normalize_model_map(parsed)


def _normalize_model_map(raw_map: dict[str, list[str]]) -> dict[str, list[str]]:
    normalized: dict[str, list[str]] = {}
    for raw_pid, raw_models in raw_map.items():
        if not isinstance(raw_pid, str) or not isinstance(raw_models, list):
            continue
        pid = raw_pid.strip()
        if not pid:
            continue
        models: list[str] = []
        for item in raw_models:
            if isinstance(item, str):
                model = item.strip()
                if model and model not in models:
                    models.append(model)
        if models:
            normalized[pid] = models
    return normalized


def _catalog_set(provider: Provider) -> set[str] | None:
    if provider.catalog_models is None:
        return None
    try:
        parsed = json.loads(provider.catalog_models)
    except (TypeError, ValueError):
        return set()
    return (
        {item for item in parsed if isinstance(item, str)}
        if isinstance(parsed, list)
        else set()
    )


def _profile_view(
    db, row: AgentProfile, providers_by_id: dict[str, Provider]
) -> AgentProfileView:
    provider_models = _parse_model_map(row.provider_models)
    pending_stored = _parse_model_map(row.pending_verification_models)
    missing_models: dict[str, list[str]] = {}
    pending_verification: dict[str, list[str]] = {}

    for pid, selected in provider_models.items():
        provider = providers_by_id.get(pid)
        if provider is None:
            missing_models[pid] = list(selected)
            continue
        catalog = _catalog_set(provider)
        if catalog is None:
            pending_for_provider = pending_stored.get(pid, [])
            kept_pending = [m for m in selected if m in pending_for_provider]
            if kept_pending:
                pending_verification[pid] = kept_pending
        else:
            missing = [m for m in selected if m not in catalog]
            if missing:
                missing_models[pid] = missing

    return AgentProfileView(
        id=row.id,
        name=row.name,
        runner_id=row.runner_id,
        enabled=bool(row.enabled),
        is_system_default=bool(row.is_system_default),
        default_provider_id=row.default_provider_id,
        default_model=row.default_model,
        provider_models=provider_models,
        missing_models=missing_models,
        pending_verification=pending_verification,
        runner_available=is_runner_available(db, row.runner_id),
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _validate_selection_against_catalog(
    providers_by_id: dict[str, Provider],
    existing_selection: dict[str, list[str]],
    new_selection: dict[str, list[str]],
) -> None:
    for pid, models in new_selection.items():
        provider = providers_by_id.get(pid)
        already = set(existing_selection.get(pid, []))
        added = [m for m in models if m not in already]
        if provider is None:
            if added or pid not in existing_selection:
                raise HTTPException(422, f"Provider not found: {pid}")
            continue
        if added:
            catalog = _catalog_set(provider)
            if catalog is None:
                raise HTTPException(
                    422, f"Provider {pid} catalog has not been fetched yet"
                )
            unknown = [m for m in added if m not in catalog]
            if unknown:
                raise HTTPException(
                    422,
                    f"Models are not in provider catalog: {', '.join(unknown)}",
                )


def _normalize_optional_pair(
    provider_id: str | None, model: str | None
) -> tuple[str | None, str | None]:
    dpid = provider_id.strip() if isinstance(provider_id, str) else None
    dmodel = model.strip() if isinstance(model, str) else None
    return (dpid or None), (dmodel or None)


def _validate_profile_state(
    db,
    providers_by_id: dict[str, Provider],
    *,
    runner_id: str,
    enabled: bool,
    is_system_default: bool,
    default_provider_id: str | None,
    default_model: str | None,
    provider_models: dict[str, list[str]],
) -> None:
    if (default_provider_id is None) != (default_model is None):
        raise HTTPException(
            422, "default_provider_id and default_model must be set together"
        )
    if is_system_default and not enabled:
        raise HTTPException(422, "Disabled agent profile cannot be system default")
    if enabled:
        if not is_runner_available(db, runner_id):
            raise HTTPException(422, "Runner is not available for enabling profile")
        if not default_provider_id or not default_model:
            raise HTTPException(
                422, "Enabled agent profile requires a valid default provider and model"
            )
        if default_model not in provider_models.get(default_provider_id, []):
            raise HTTPException(
                422, "Default model must be selected in provider_models"
            )
        provider = providers_by_id.get(default_provider_id)
        catalog = _catalog_set(provider) if provider is not None else None
        if catalog is None or default_model not in catalog:
            raise HTTPException(
                422,
                "Default model must be present in the provider's fetched catalog",
            )
    elif default_provider_id and default_model:
        if default_model not in provider_models.get(default_provider_id, []):
            raise HTTPException(
                422, "Default model must be selected in provider_models"
            )


def _prune_pending_verification(
    pending_stored: dict[str, list[str]],
    new_selection: dict[str, list[str]],
    providers_by_id: dict[str, Provider],
) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for pid, pending_models in pending_stored.items():
        provider = providers_by_id.get(pid)
        if provider is None or provider.catalog_models is not None:
            continue
        selected = set(new_selection.get(pid, []))
        kept = [m for m in pending_models if m in selected]
        if kept:
            result[pid] = kept
    return result


@router.get("/api/v1/agent-profiles", response_model=list[AgentProfileView])
def list_agent_profiles(_: Annotated[str, Depends(admin)]):
    with sessions()() as db:
        providers_by_id = {p.id: p for p in db.query(Provider).all()}
        rows = (
            db.query(AgentProfile)
            .order_by(
                AgentProfile.is_system_default.desc(),
                AgentProfile.name,
                AgentProfile.id,
            )
            .all()
        )
        return [_profile_view(db, row, providers_by_id) for row in rows]


@router.get("/api/v1/agent-profiles/{profile_id}", response_model=AgentProfileView)
def get_agent_profile(profile_id: str, _: Annotated[str, Depends(admin)]):
    with sessions()() as db:
        row = db.get(AgentProfile, profile_id)
        if row is None:
            raise HTTPException(404, "Agent profile not found")
        providers_by_id = {p.id: p for p in db.query(Provider).all()}
        return _profile_view(db, row, providers_by_id)


@router.post("/api/v1/agent-profiles", response_model=AgentProfileView, status_code=201)
def create_agent_profile(body: AgentProfileCreate, _: Annotated[str, Depends(admin)]):
    name = body.name.strip()
    runner_id = body.runner_id.strip()
    now = datetime.now(UTC)
    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        if db.query(AgentProfile).filter(AgentProfile.name == name).first() is not None:
            raise HTTPException(409, f"Agent profile name already exists: {name}")
        if (
            db.get(AgentRunnerRecord, runner_id) is None
            and get_runner(runner_id) is None
        ):
            raise HTTPException(422, f"Unknown runner: {runner_id}")

        providers_by_id = {p.id: p for p in db.query(Provider).all()}
        normalized_pm = _normalize_model_map(body.provider_models)
        _validate_selection_against_catalog(providers_by_id, {}, normalized_pm)
        dpid, dmodel = _normalize_optional_pair(
            body.default_provider_id, body.default_model
        )
        _validate_profile_state(
            db,
            providers_by_id,
            runner_id=runner_id,
            enabled=body.enabled,
            is_system_default=body.is_system_default,
            default_provider_id=dpid,
            default_model=dmodel,
            provider_models=normalized_pm,
        )
        if body.is_system_default:
            db.query(AgentProfile).filter(
                AgentProfile.is_system_default.is_(True)
            ).update({"is_system_default": False})

        row = AgentProfile(
            id=uuid.uuid4().hex,
            name=name,
            runner_id=runner_id,
            enabled=body.enabled,
            is_system_default=body.is_system_default,
            default_provider_id=dpid,
            default_model=dmodel,
            provider_models=json.dumps(normalized_pm, ensure_ascii=False),
            pending_verification_models="{}",
            version=1,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        db.flush()
        return _profile_view(db, row, providers_by_id)


@router.put("/api/v1/agent-profiles/{profile_id}", response_model=AgentProfileView)
def update_agent_profile(
    profile_id: str, body: AgentProfileUpdate, _: Annotated[str, Depends(admin)]
):
    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(AgentProfile, profile_id)
        if row is None:
            raise HTTPException(404, "Agent profile not found")
        if body.version != row.version:
            raise HTTPException(
                409,
                {
                    "message": "Agent profile was modified concurrently",
                    "current_version": row.version,
                },
            )
        if body.runner_id is not None and body.runner_id.strip() != row.runner_id:
            raise HTTPException(422, "runner_id is immutable")

        new_name = body.name.strip() if body.name is not None else row.name
        if new_name != row.name:
            conflict = (
                db.query(AgentProfile)
                .filter(AgentProfile.name == new_name, AgentProfile.id != row.id)
                .first()
            )
            if conflict is not None:
                raise HTTPException(
                    409, f"Agent profile name already exists: {new_name}"
                )

        old_pm = _parse_model_map(row.provider_models)
        new_pm = (
            _normalize_model_map(body.provider_models)
            if body.provider_models is not None
            else old_pm
        )
        new_enabled = body.enabled if body.enabled is not None else bool(row.enabled)
        new_is_system_default = (
            body.is_system_default
            if body.is_system_default is not None
            else bool(row.is_system_default)
        )
        raw_dpid = (
            body.default_provider_id
            if "default_provider_id" in body.model_fields_set
            else row.default_provider_id
        )
        raw_dmodel = (
            body.default_model
            if "default_model" in body.model_fields_set
            else row.default_model
        )
        new_dpid, new_dmodel = _normalize_optional_pair(raw_dpid, raw_dmodel)

        if row.is_system_default and (not new_is_system_default or not new_enabled):
            raise HTTPException(
                409,
                {
                    "message": "Cannot disable or unset the active system default without promoting another profile",
                    "affected_defaults": [
                        {
                            "kind": "system_default",
                            "profile_id": row.id,
                            "profile_name": row.name,
                            "provider_id": row.default_provider_id,
                            "model": row.default_model,
                        }
                    ],
                },
            )

        old_dpid, old_dmodel = row.default_provider_id, row.default_model
        if (
            old_dpid
            and old_dmodel
            and old_dmodel in old_pm.get(old_dpid, [])
            and old_dmodel not in new_pm.get(old_dpid, [])
            and (new_dpid, new_dmodel) == (old_dpid, old_dmodel)
        ):
            affected = [
                {
                    "kind": "profile_default",
                    "profile_id": row.id,
                    "profile_name": row.name,
                    "provider_id": old_dpid,
                    "model": old_dmodel,
                }
            ]
            if row.is_system_default:
                affected.append(
                    {
                        "kind": "system_default",
                        "profile_id": row.id,
                        "profile_name": row.name,
                        "provider_id": old_dpid,
                        "model": old_dmodel,
                    }
                )
            raise HTTPException(
                409,
                {
                    "message": "Cannot remove active default model without replacement",
                    "affected_defaults": affected,
                },
            )

        providers_by_id = {p.id: p for p in db.query(Provider).all()}
        _validate_selection_against_catalog(providers_by_id, old_pm, new_pm)
        _validate_profile_state(
            db,
            providers_by_id,
            runner_id=row.runner_id,
            enabled=new_enabled,
            is_system_default=new_is_system_default,
            default_provider_id=new_dpid,
            default_model=new_dmodel,
            provider_models=new_pm,
        )

        if new_is_system_default and not row.is_system_default:
            db.query(AgentProfile).filter(
                AgentProfile.id != row.id,
                AgentProfile.is_system_default.is_(True),
            ).update({"is_system_default": False})

        pruned_pending = _prune_pending_verification(
            _parse_model_map(row.pending_verification_models),
            new_pm,
            providers_by_id,
        )
        row.name = new_name
        row.enabled = new_enabled
        row.is_system_default = new_is_system_default
        row.default_provider_id = new_dpid
        row.default_model = new_dmodel
        row.provider_models = json.dumps(new_pm, ensure_ascii=False)
        row.pending_verification_models = json.dumps(pruned_pending, ensure_ascii=False)
        row.version += 1
        row.updated_at = datetime.now(UTC)
        db.flush()
        return _profile_view(db, row, providers_by_id)


@router.delete("/api/v1/agent-profiles/{profile_id}", status_code=204)
def delete_agent_profile(profile_id: str, _: Annotated[str, Depends(admin)]):
    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(AgentProfile, profile_id)
        if row is None:
            raise HTTPException(404, "Agent profile not found")
        if row.is_system_default:
            raise HTTPException(
                409,
                {
                    "message": "Cannot delete the active system default profile",
                    "affected_defaults": [
                        {
                            "kind": "system_default",
                            "profile_id": row.id,
                            "profile_name": row.name,
                            "provider_id": row.default_provider_id,
                            "model": row.default_model,
                        }
                    ],
                },
            )
        db.query(UserAgentDefault).filter(
            UserAgentDefault.agent_profile_id == profile_id
        ).delete(synchronize_session=False)
        db.delete(row)
