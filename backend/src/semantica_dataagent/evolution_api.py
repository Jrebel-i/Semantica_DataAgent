"""REST endpoints for project evolution configuration, manual triggers, and run inspection (#296)."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import evolution_runner, project_context
from .api_time import UtcDatetime
from .auth import Requester, current_requester
from .db import (
    EvolutionCheckpoint,
    EvolutionRound,
    EvolutionRun,
    ProjectSetting,
    Provider,
    sessions,
)
from .evolution_scheduler import evolution_scheduler
from .project_api import (
    _require_project_admin,
    actor_name,
    admin_project_path_scope,
    admin_project_scope,
    resolve_project_scope,
)
from .project_audit import record_project_audit
from .project_context import (
    ARCHIVED_DETAIL,
    AUTO_EVOLVE_ENABLED_KEY,
    EVOLUTION_JUDGE_MODEL_KEY,
    EVOLUTION_JUDGE_PROVIDER_ID_KEY,
    EVOLUTION_MAX_EVAL_CASES_KEY,
    EVOLUTION_MAX_IDLE_DAYS_KEY,
    EVOLUTION_MAX_ROUNDS_KEY,
    EVOLUTION_MIN_INTERVAL_HOURS_KEY,
    EVOLUTION_MIN_TRAJECTORIES_KEY,
    EVOLUTION_MODEL_KEY,
    EVOLUTION_PROVIDER_ID_KEY,
    FROZEN_STATUSES,
    ProjectContext,
)

router = APIRouter()


class EvolutionConfigView(BaseModel):
    auto_evolve_enabled: bool
    min_trajectories: int
    min_interval_hours: float
    max_idle_days: float
    max_rounds: int
    max_eval_cases: int
    evolution_provider_id: str | None = None
    evolution_model: str | None = None
    judge_provider_id: str | None = None
    judge_model: str | None = None
    checkpoint_trajectory_id: int | None = None
    checkpoint_at: UtcDatetime | None = None
    pending_trajectories: int = 0


class EvolutionConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_evolve_enabled: bool | None = None
    min_trajectories: int | None = Field(default=None, ge=1, le=10000)
    min_interval_hours: float | None = Field(default=None, ge=0, le=8760)
    max_idle_days: float | None = Field(default=None, ge=1, le=365)
    max_rounds: int | None = Field(default=None, ge=1, le=20)
    max_eval_cases: int | None = Field(default=None, ge=1, le=200)
    evolution_provider_id: str | None = None
    evolution_model: str | None = None
    judge_provider_id: str | None = None
    judge_model: str | None = None

    @field_validator(
        "evolution_provider_id",
        "evolution_model",
        "judge_provider_id",
        "judge_model",
        mode="before",
    )
    @classmethod
    def _normalize_optional_str(cls, value):
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


class EvolutionRoundView(BaseModel):
    id: str
    run_id: str
    project_id: str
    round_number: int
    signal_kind: str
    hypothesis: str
    evidence_trajectory_ids: list[int] = Field(default_factory=list)
    target: str = ""
    target_kind: str = "override"
    candidate_change: dict[str, Any] = Field(default_factory=dict)
    l1_gate_report: dict[str, Any] | None = None
    l2_gate_report: dict[str, Any] | None = None
    decision: Literal["running", "accept", "reject", "incomplete"]
    reject_reason: str | None = None
    proposal_id: str | None = None
    started_at: UtcDatetime
    finished_at: UtcDatetime | None = None


class EvolutionRunView(BaseModel):
    id: str
    project_id: str
    trigger_type: str
    status: Literal["running", "accepted", "incomplete"]
    checkpoint_before_trajectory_id: int | None = None
    checkpoint_after_trajectory_id: int | None = None
    frozen_max_trajectory_id: int | None = None
    diagnosis_trajectory_ids: list[int] = Field(default_factory=list)
    reserve_trajectory_ids: list[int] = Field(default_factory=list)
    signal_summary: dict[str, Any] = Field(default_factory=dict)
    max_rounds: int
    max_eval_cases: int
    rounds_completed: int
    stop_reason: str | None = None
    error: str | None = None
    proposal_id: str | None = None
    created_by: str | None = None
    started_at: UtcDatetime
    finished_at: UtcDatetime | None = None
    created_at: UtcDatetime
    rounds: list[EvolutionRoundView] = Field(default_factory=list)


class EvolutionRunTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_rounds: int | None = Field(default=None, ge=1, le=20)
    max_eval_cases: int | None = Field(default=None, ge=1, le=200)


def _build_config_view(project_id: str) -> EvolutionConfigView:
    cfg = project_context.evolution_config(project_id)
    with sessions()() as db:
        ckpt = db.get(EvolutionCheckpoint, project_id)
        ckpt_id = ckpt.checkpoint_trajectory_id if ckpt is not None else None
        ckpt_at = ckpt.checkpoint_at if ckpt is not None else None
    pending_count = 0
    return EvolutionConfigView(
        **cfg,
        checkpoint_trajectory_id=ckpt_id,
        checkpoint_at=ckpt_at,
        pending_trajectories=pending_count,
    )


def _validate_provider_model(
    db, provider_id: str | None, model: str | None, label: str
) -> None:
    if provider_id is None:
        if model is not None:
            raise HTTPException(
                status_code=422,
                detail=f"{label}_model requires {label}_provider_id",
            )
        return
    prov = db.get(Provider, provider_id)
    if prov is None:
        raise HTTPException(
            status_code=422, detail=f"{label} provider not found: {provider_id}"
        )
    if model is not None:
        try:
            models = json.loads(prov.models or "[]")
        except ValueError:
            models = []
        if model not in models:
            raise HTTPException(
                status_code=422,
                detail=f"Model {model!r} is not configured on provider {prov.name}",
            )


def _upsert_project_setting(db, project_id: str, key: str, value: str | None) -> None:
    row = db.get(ProjectSetting, (project_id, key))
    if value is None:
        if row is not None:
            db.delete(row)
        return
    if row is None:
        db.add(ProjectSetting(project_id=project_id, key=key, value=value))
    else:
        row.value = value


@router.get(
    "/api/v1/projects/{project_id}/evolution-config",
    response_model=EvolutionConfigView,
)
def get_project_evolution_config(
    project_id: str,
    scope: Annotated[ProjectContext, Depends(admin_project_path_scope)],
):
    return _build_config_view(scope.project_id)


def _resolve_provider_pair_update(
    db,
    body: EvolutionConfigUpdate,
    current: dict[str, Any],
    *,
    prefix: str,
) -> tuple[str | None, str | None]:
    prov_field = f"{prefix}_provider_id"
    model_field = f"{prefix}_model"
    next_prov = (
        getattr(body, prov_field)
        if prov_field in body.model_fields_set
        else current[prov_field]
    )
    next_model = (
        getattr(body, model_field)
        if model_field in body.model_fields_set
        else current[model_field]
    )
    if (
        prov_field in body.model_fields_set
        and getattr(body, prov_field) is None
        and model_field not in body.model_fields_set
    ):
        next_model = None
    _validate_provider_model(db, next_prov, next_model, prefix)
    return next_prov, next_model


@router.put(
    "/api/v1/projects/{project_id}/evolution-config",
    response_model=EvolutionConfigView,
)
def put_project_evolution_config(
    project_id: str,
    body: EvolutionConfigUpdate,
    scope: Annotated[ProjectContext, Depends(admin_project_path_scope)],
    requester: Annotated[Requester, Depends(current_requester)],
):
    if scope.status in FROZEN_STATUSES:
        raise HTTPException(status_code=409, detail=ARCHIVED_DETAIL)

    with sessions().begin() as db:
        current = project_context.evolution_config(scope.project_id, db)
        next_evo_prov, next_evo_model = _resolve_provider_pair_update(
            db, body, current, prefix="evolution"
        )
        next_jdg_prov, next_jdg_model = _resolve_provider_pair_update(
            db, body, current, prefix="judge"
        )

        updates: list[tuple[str, str | None]] = []
        if "auto_evolve_enabled" in body.model_fields_set:
            updates.append(
                (
                    AUTO_EVOLVE_ENABLED_KEY,
                    None
                    if body.auto_evolve_enabled is None
                    else ("true" if body.auto_evolve_enabled else "false"),
                )
            )
        if "min_trajectories" in body.model_fields_set:
            updates.append(
                (
                    EVOLUTION_MIN_TRAJECTORIES_KEY,
                    None
                    if body.min_trajectories is None
                    else str(body.min_trajectories),
                )
            )
        if "min_interval_hours" in body.model_fields_set:
            updates.append(
                (
                    EVOLUTION_MIN_INTERVAL_HOURS_KEY,
                    None
                    if body.min_interval_hours is None
                    else str(body.min_interval_hours),
                )
            )
        if "max_idle_days" in body.model_fields_set:
            updates.append(
                (
                    EVOLUTION_MAX_IDLE_DAYS_KEY,
                    None if body.max_idle_days is None else str(body.max_idle_days),
                )
            )
        if "max_rounds" in body.model_fields_set:
            updates.append(
                (
                    EVOLUTION_MAX_ROUNDS_KEY,
                    None if body.max_rounds is None else str(body.max_rounds),
                )
            )
        if "max_eval_cases" in body.model_fields_set:
            updates.append(
                (
                    EVOLUTION_MAX_EVAL_CASES_KEY,
                    None if body.max_eval_cases is None else str(body.max_eval_cases),
                )
            )
        if "evolution_provider_id" in body.model_fields_set:
            updates.append((EVOLUTION_PROVIDER_ID_KEY, next_evo_prov))
        if (
            "evolution_model" in body.model_fields_set
            or "evolution_provider_id" in body.model_fields_set
        ):
            updates.append((EVOLUTION_MODEL_KEY, next_evo_model))
        if "judge_provider_id" in body.model_fields_set:
            updates.append((EVOLUTION_JUDGE_PROVIDER_ID_KEY, next_jdg_prov))
        if (
            "judge_model" in body.model_fields_set
            or "judge_provider_id" in body.model_fields_set
        ):
            updates.append((EVOLUTION_JUDGE_MODEL_KEY, next_jdg_model))

        for setting_key, val in updates:
            _upsert_project_setting(db, scope.project_id, setting_key, val)
        db.flush()
        saved = project_context.evolution_config(scope.project_id, db)
        record_project_audit(
            db,
            project_id=scope.project_id,
            actor_id=requester.id,
            actor_name=actor_name(db, requester),
            action="project.evolution_config",
            detail=saved,
        )

    evolution_scheduler.wake()
    return _build_config_view(scope.project_id)


@router.post(
    "/api/v1/evolution/runs",
    status_code=202,
    response_model=EvolutionRunView,
)
def trigger_evolution_run(
    scope: Annotated[ProjectContext, Depends(admin_project_scope)],
    requester: Annotated[Requester, Depends(current_requester)],
    body: EvolutionRunTrigger | None = None,
):
    try:
        created = evolution_runner.create_evolution_run(
            scope.project_id,
            trigger_type="manual",
            created_by=requester.id,
            max_rounds=body.max_rounds if body else None,
            max_eval_cases=body.max_eval_cases if body else None,
        )
    except evolution_runner.EvolutionFrozenError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except evolution_runner.EvolutionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None

    evolution_scheduler.dispatch_run(created["id"])
    return EvolutionRunView.model_validate(created)


def _admin_read_project_scope(
    requester: Annotated[Requester, Depends(current_requester)],
    project: Annotated[str | None, Query()] = None,
) -> ProjectContext:
    scope = resolve_project_scope(project, requester, allow_frozen=True)
    return _require_project_admin(scope, requester)


@router.get("/api/v1/evolution/runs", response_model=list[EvolutionRunView])
def list_evolution_runs(
    scope: Annotated[ProjectContext, Depends(_admin_read_project_scope)],
):
    with sessions()() as db:
        runs = (
            db.query(EvolutionRun)
            .filter(EvolutionRun.project_id == scope.project_id)
            .order_by(EvolutionRun.created_at.desc(), EvolutionRun.id.desc())
            .all()
        )
        run_ids = [r.id for r in runs]
        rounds_by_run: dict[str, list[EvolutionRound]] = {}
        if run_ids:
            for rnd in (
                db.query(EvolutionRound)
                .filter(EvolutionRound.run_id.in_(run_ids))
                .order_by(EvolutionRound.round_number.asc())
                .all()
            ):
                rounds_by_run.setdefault(rnd.run_id, []).append(rnd)
        return [
            EvolutionRunView.model_validate(
                evolution_runner.serialize_run(r, rounds_by_run.get(r.id, ()))
            )
            for r in runs
        ]


@router.get("/api/v1/evolution/runs/{run_id}", response_model=EvolutionRunView)
def get_evolution_run(
    run_id: str,
    scope: Annotated[ProjectContext, Depends(_admin_read_project_scope)],
):
    with sessions()() as db:
        run = db.get(EvolutionRun, run_id)
        if run is None or run.project_id != scope.project_id:
            raise HTTPException(status_code=404, detail="Evolution run not found")
        rounds = (
            db.query(EvolutionRound)
            .filter(EvolutionRound.run_id == run_id)
            .order_by(EvolutionRound.round_number.asc())
            .all()
        )
        return EvolutionRunView.model_validate(
            evolution_runner.serialize_run(run, rounds)
        )
