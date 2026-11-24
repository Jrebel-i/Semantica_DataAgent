"""REST endpoints for TraceSource plugins, project evolution config, triggers, and runs (ADR-0013)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import evolution_runner, plugins, project_context, pull_instances
from .api_time import UtcDatetime, as_utc
from .auth import Requester, current_requester, current_user
from .db import (
    EvolutionRun,
    ProjectEvolutionConfig,
    ProjectSetting,
    Provider,
    sessions,
)
from .evolution_scheduler import evolution_scheduler, next_run_at_for_config
from .project_api import admin_project_path_scope
from .project_audit import actor_name, record_project_audit
from .project_context import (
    ARCHIVED_DETAIL,
    EVOLUTION_MAX_TRACES_PER_RUN_KEY,
    EVOLUTION_MODEL_KEY,
    EVOLUTION_PROVIDER_ID_KEY,
    FROZEN_STATUSES,
    ProjectContext,
)

router = APIRouter()


class TraceSourcePluginView(BaseModel):
    id: str
    version: str
    display_name: str
    parameters_schema: dict[str, Any]
    secret_parameter_keys: list[str]


class EvolutionConfigView(BaseModel):
    project_id: str
    enabled: bool
    trace_source_plugin: str | None = None
    trace_source_parameters: dict[str, Any] = Field(default_factory=dict)
    trace_source_secrets: dict[str, str] = Field(default_factory=dict)
    configured_secrets: list[str] = Field(default_factory=list)
    secret_configured: bool = False
    schedule_cron: str | None = None
    timezone: str = "Asia/Shanghai"
    evolution_provider_id: str | None = None
    evolution_model: str | None = None
    max_traces_per_run: int = 200
    last_trace_timestamp: UtcDatetime | None = None
    last_triggered_at: UtcDatetime | None = None
    next_run_at: UtcDatetime | None = None
    global_langfuse_configured: bool = False


class EvolutionConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    trace_source_plugin: str | None = None
    trace_source_parameters: dict[str, Any] | None = None
    trace_source_secrets: dict[str, str | None] | None = None
    schedule_cron: str | None = None
    timezone: str | None = None
    evolution_provider_id: str | None = None
    evolution_model: str | None = None
    max_traces_per_run: int | None = Field(default=None, ge=1, le=2000)

    @field_validator("max_traces_per_run", mode="before")
    @classmethod
    def _reject_bool_int(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("整数参数不接受布尔值")  # noqa: TRY004 - Pydantic requires ValueError for 422
        return value

    @field_validator(
        "trace_source_plugin",
        "evolution_provider_id",
        "evolution_model",
        mode="before",
    )
    @classmethod
    def _normalize_optional_str(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


class EvolutionRunTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    since: datetime | None = None
    until: datetime | None = None
    max_traces: int | None = Field(default=None, ge=1, le=2000)
    provider_id: str | None = None
    model: str | None = None

    @field_validator("max_traces", mode="before")
    @classmethod
    def _reject_bool_int(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("整数参数不接受布尔值")  # noqa: TRY004 - Pydantic requires ValueError for 422
        return value

    @field_validator("provider_id", "model", mode="before")
    @classmethod
    def _normalize_optional_str(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


class EvolutionRunView(BaseModel):
    id: str
    project_id: str
    trigger_type: str
    status: Literal["running", "succeeded", "failed", "incomplete", "accepted"]
    trace_source_plugin: str | None = None
    traces_fetched: int = 0
    proposals_created: int = 0
    duplicates_skipped: int = 0
    l1_rejected: int = 0
    proposal_ids: list[str] = Field(default_factory=list)
    proposal_id: str | None = None
    window_since: UtcDatetime | None = None
    window_until: UtcDatetime | None = None
    summary: str = ""
    error: str | None = None
    created_by: str | None = None
    started_at: UtcDatetime
    finished_at: UtcDatetime | None = None
    created_at: UtcDatetime


def _parse_json_dict(raw: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


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


def _build_config_view(project_id: str, db=None) -> EvolutionConfigView:
    if db is None:
        with sessions()() as own:
            return _build_config_view(project_id, own)
    row = db.get(ProjectEvolutionConfig, project_id)
    cfg = project_context.evolution_config(project_id, db)
    discovered = plugins.discover()
    plugin_id = (
        (row.trace_source_plugin or "").strip() if row is not None else ""
    ) or None
    plugin = discovered.get(("trace_source", plugin_id)) if plugin_id else None
    secret_keys = (
        plugins.secret_parameter_keys(plugin.parameters_schema)
        if plugin is not None
        else set()
    )
    stored_secrets = _parse_json_dict(
        row.trace_source_encrypted_secrets if row is not None else "{}"
    )
    configured_secrets = sorted(
        k for k, v in stored_secrets.items() if isinstance(v, str) and v
    )
    raw_params = _parse_json_dict(
        row.trace_source_parameters if row is not None else "{}"
    )
    public_params = {
        k: v
        for k, v in raw_params.items()
        if k not in secret_keys and k not in stored_secrets
    }
    evo_prov = (
        (row.provider_id or "").strip()
        if row is not None and row.provider_id
        else cfg.get("evolution_provider_id")
    )
    evo_model = (
        (row.model or "").strip()
        if row is not None and row.model
        else cfg.get("evolution_model")
    )
    return EvolutionConfigView(
        project_id=project_id,
        enabled=bool(row.enabled) if row is not None else False,
        trace_source_plugin=plugin_id,
        trace_source_parameters=public_params,
        trace_source_secrets={k: "***" for k in configured_secrets},
        configured_secrets=configured_secrets,
        secret_configured=bool(configured_secrets),
        schedule_cron=row.schedule_cron if row is not None else None,
        timezone=(row.timezone or "Asia/Shanghai")
        if row is not None
        else "Asia/Shanghai",
        evolution_provider_id=evo_prov or None,
        evolution_model=evo_model or None,
        max_traces_per_run=int(cfg["max_traces_per_run"]),
        last_trace_timestamp=row.last_trace_timestamp if row is not None else None,
        last_triggered_at=row.last_triggered_at if row is not None else None,
        next_run_at=next_run_at_for_config(row),
        global_langfuse_configured=evolution_runner.has_global_langfuse_fallback(db),
    )


def _validate_trace_source_params(
    plugin: Any,
    public_params: dict[str, Any],
    incoming_secrets: dict[str, Any],
    existing_secrets: dict[str, Any],
) -> None:
    schema = plugin.parameters_schema
    secret_keys = plugins.secret_parameter_keys(schema)
    unknown_secrets = sorted(set(incoming_secrets) - secret_keys)
    if unknown_secrets:
        raise HTTPException(
            status_code=422,
            detail=f"未知对话追踪源密钥字段：{', '.join(unknown_secrets)}",
        )
    for_validation = dict(public_params)
    for key in secret_keys:
        has_existing = bool(existing_secrets.get(key))
        if key in incoming_secrets:
            val = incoming_secrets[key]
            if val == "***":
                if has_existing:
                    for_validation[key] = "placeholder"
            elif val not in (None, ""):
                for_validation[key] = val
        elif has_existing:
            for_validation[key] = "placeholder"

    errors = sorted(
        Draft202012Validator(schema).iter_errors(for_validation),
        key=lambda err: list(map(str, err.absolute_path)),
    )
    if errors:
        raise HTTPException(
            status_code=422,
            detail=[
                {"path": list(err.absolute_path), "message": err.message}
                for err in errors
            ],
        )


@router.get("/api/v1/trace-source-plugins", response_model=list[TraceSourcePluginView])
def list_trace_source_plugins(_: Annotated[str, Depends(current_user)]):
    discovered = plugins.discover()
    return [
        TraceSourcePluginView(
            id=plugin.id,
            version=plugin.version,
            display_name=plugin.display_name,
            parameters_schema=plugin.parameters_schema,
            secret_parameter_keys=sorted(
                plugins.secret_parameter_keys(plugin.parameters_schema)
            ),
        )
        for (category, _), plugin in sorted(
            discovered.items(), key=lambda item: item[1].id
        )
        if category == "trace_source"
    ]


@router.get(
    "/api/v1/projects/{project_id}/evolution/config",
    response_model=EvolutionConfigView,
)
def get_project_evolution_config(
    project_id: str,
    scope: Annotated[ProjectContext, Depends(admin_project_path_scope)],
):
    return _build_config_view(scope.project_id)


@router.put(
    "/api/v1/projects/{project_id}/evolution/config",
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

    discovered = plugins.discover()
    now = datetime.now(UTC)

    with sessions().begin() as db:
        row = db.get(ProjectEvolutionConfig, scope.project_id)
        if row is None:
            row = ProjectEvolutionConfig(
                project_id=scope.project_id,
                enabled=False,
                trace_source_parameters="{}",
                trace_source_encrypted_secrets="{}",
                timezone="Asia/Shanghai",
                created_at=now,
                updated_at=now,
            )
            db.add(row)

        current_cfg = project_context.evolution_config(scope.project_id, db)
        cur_prov = (row.provider_id or "").strip() or current_cfg.get(
            "evolution_provider_id"
        )
        cur_model = (row.model or "").strip() or current_cfg.get("evolution_model")

        next_prov = (
            body.evolution_provider_id
            if "evolution_provider_id" in body.model_fields_set
            else cur_prov
        )
        next_model = (
            body.evolution_model
            if "evolution_model" in body.model_fields_set
            else cur_model
        )
        if (
            "evolution_provider_id" in body.model_fields_set
            and body.evolution_provider_id is None
            and "evolution_model" not in body.model_fields_set
        ):
            next_model = None
        _validate_provider_model(db, next_prov, next_model, "evolution")

        if "schedule_cron" in body.model_fields_set:
            try:
                row.schedule_cron = pull_instances.normalize_schedule_cron(
                    body.schedule_cron
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        if "timezone" in body.model_fields_set:
            try:
                row.timezone = pull_instances.validate_timezone(body.timezone)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        if "enabled" in body.model_fields_set:
            if body.enabled is None:
                raise HTTPException(status_code=422, detail="enabled 不能为空")
            row.enabled = body.enabled

        prev_plugin_id = (row.trace_source_plugin or "").strip() or None
        next_plugin_id = (
            body.trace_source_plugin
            if "trace_source_plugin" in body.model_fields_set
            else prev_plugin_id
        )
        plugin = None
        if next_plugin_id is not None:
            plugin = discovered.get(("trace_source", next_plugin_id))
            if plugin is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"未知对话追踪源插件：{next_plugin_id}",
                )

        plugin_changed = (
            "trace_source_plugin" in body.model_fields_set
            and next_plugin_id != prev_plugin_id
        )
        if plugin_changed:
            row.trace_source_plugin = next_plugin_id
            if next_plugin_id is None:
                row.trace_source_parameters = "{}"
                row.trace_source_encrypted_secrets = "{}"
            elif "trace_source_parameters" not in body.model_fields_set:
                row.trace_source_parameters = "{}"
                if "trace_source_secrets" not in body.model_fields_set:
                    row.trace_source_encrypted_secrets = "{}"

        if plugin is None and (
            body.trace_source_parameters or body.trace_source_secrets
        ):
            raise HTTPException(
                status_code=422,
                detail="配置追踪源参数或密钥前必须先选择 trace_source_plugin",
            )

        if plugin is not None:
            secret_keys = plugins.secret_parameter_keys(plugin.parameters_schema)
            existing_secrets = (
                {}
                if plugin_changed
                else _parse_json_dict(row.trace_source_encrypted_secrets)
            )
            if "trace_source_parameters" in body.model_fields_set:
                if body.trace_source_parameters is None:
                    raise HTTPException(
                        status_code=422, detail="trace_source_parameters 不能为空"
                    )
                raw_incoming = dict(body.trace_source_parameters)
            else:
                raw_incoming = (
                    {}
                    if plugin_changed
                    else _parse_json_dict(row.trace_source_parameters)
                )

            public_params = {
                k: v for k, v in raw_incoming.items() if k not in secret_keys
            }
            incoming_secrets = {
                k: raw_incoming[k] for k in secret_keys if k in raw_incoming
            }
            if (
                "trace_source_secrets" in body.model_fields_set
                and body.trace_source_secrets is not None
            ):
                for k, v in body.trace_source_secrets.items():
                    incoming_secrets[k] = v

            should_validate_schema = (
                row.enabled
                or "trace_source_parameters" in body.model_fields_set
                or "trace_source_secrets" in body.model_fields_set
                or "trace_source_plugin" in body.model_fields_set
            )
            if should_validate_schema:
                _validate_trace_source_params(
                    plugin, public_params, incoming_secrets, existing_secrets
                )

            row.trace_source_parameters = json.dumps(
                public_params, sort_keys=True, ensure_ascii=False
            )
            if incoming_secrets or plugin_changed:
                base_encrypted = (
                    "{}" if plugin_changed else row.trace_source_encrypted_secrets
                )
                row.trace_source_encrypted_secrets = (
                    pull_instances.merge_encrypted_secrets(
                        base_encrypted, incoming_secrets, secret_keys
                    )
                )

        if "evolution_provider_id" in body.model_fields_set:
            row.provider_id = next_prov
            _upsert_project_setting(
                db, scope.project_id, EVOLUTION_PROVIDER_ID_KEY, next_prov
            )
        if (
            "evolution_model" in body.model_fields_set
            or "evolution_provider_id" in body.model_fields_set
        ):
            row.model = next_model
            _upsert_project_setting(
                db, scope.project_id, EVOLUTION_MODEL_KEY, next_model
            )
        if "max_traces_per_run" in body.model_fields_set:
            _upsert_project_setting(
                db,
                scope.project_id,
                EVOLUTION_MAX_TRACES_PER_RUN_KEY,
                None
                if body.max_traces_per_run is None
                else str(body.max_traces_per_run),
            )

        row.updated_at = now
        db.flush()
        view = _build_config_view(scope.project_id, db)
        record_project_audit(
            db,
            project_id=scope.project_id,
            actor_id=requester.id,
            actor_name=actor_name(db, requester),
            action="project.evolution_config",
            detail={
                "enabled": view.enabled,
                "trace_source_plugin": view.trace_source_plugin,
                "trace_source_parameters": view.trace_source_parameters,
                "configured_secrets": view.configured_secrets,
                "schedule_cron": view.schedule_cron,
                "timezone": view.timezone,
                "evolution_provider_id": view.evolution_provider_id,
                "evolution_model": view.evolution_model,
                "max_traces_per_run": view.max_traces_per_run,
            },
        )

    evolution_scheduler.wake()
    return view


@router.post(
    "/api/v1/projects/{project_id}/evolution/runs",
    status_code=202,
    response_model=EvolutionRunView,
)
def trigger_evolution_run(
    project_id: str,
    scope: Annotated[ProjectContext, Depends(admin_project_path_scope)],
    requester: Annotated[Requester, Depends(current_requester)],
    body: EvolutionRunTrigger | None = None,
):
    if scope.status in FROZEN_STATUSES:
        raise HTTPException(status_code=409, detail=ARCHIVED_DETAIL)

    since_utc = as_utc(body.since) if body and body.since is not None else None
    until_utc = as_utc(body.until) if body and body.until is not None else None
    if since_utc is not None and until_utc is not None and since_utc >= until_utc:
        raise HTTPException(status_code=422, detail="since 必须早于 until")

    if body and (body.provider_id is not None or body.model is not None):
        with sessions()() as db:
            _validate_provider_model(db, body.provider_id, body.model, "evolution")

    try:
        created = evolution_runner.create_evolution_run(
            scope.project_id,
            trigger_type="manual",
            created_by=requester.id,
            since=since_utc,
            until=until_utc,
            max_traces=body.max_traces if body else None,
            provider_id=body.provider_id if body else None,
            model=body.model if body else None,
        )
    except evolution_runner.EvolutionFrozenError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except evolution_runner.EvolutionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None

    evolution_scheduler.dispatch_run(created["id"])
    return EvolutionRunView.model_validate(created)


@router.get(
    "/api/v1/projects/{project_id}/evolution/runs",
    response_model=list[EvolutionRunView],
)
def list_evolution_runs(
    project_id: str,
    scope: Annotated[ProjectContext, Depends(admin_project_path_scope)],
):
    with sessions()() as db:
        runs = (
            db.query(EvolutionRun)
            .filter(EvolutionRun.project_id == scope.project_id)
            .order_by(EvolutionRun.created_at.desc(), EvolutionRun.id.desc())
            .all()
        )
        return [
            EvolutionRunView.model_validate(evolution_runner.serialize_run(r))
            for r in runs
        ]


@router.get(
    "/api/v1/projects/{project_id}/evolution/runs/{run_id}",
    response_model=EvolutionRunView,
)
def get_evolution_run(
    project_id: str,
    run_id: str,
    scope: Annotated[ProjectContext, Depends(admin_project_path_scope)],
):
    with sessions()() as db:
        run = db.get(EvolutionRun, run_id)
        if run is None or run.project_id != scope.project_id:
            raise HTTPException(status_code=404, detail="Evolution run not found")
        return EvolutionRunView.model_validate(evolution_runner.serialize_run(run))
