"""Project-scoped puller instances and optional query executor configuration."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import crypto, project_context
from .api_time import UtcDatetime
from .auth import current_user
from .db import ProjectPullInstance, sessions
from .plugins import discover, secret_parameter_keys
from .project_api import (
    admin_project_path_write_scope,
    project_path_scope,
)
from .project_context import ProjectContext

router = APIRouter()

INSTANCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
CATALOG_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
TASK_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
CRON_PART_RE = re.compile(r"^(\*|\d+)(?:-(\d+))?(?:/(\d+))?$")

_CRON_BOUNDS: tuple[tuple[str, int, int], ...] = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day", 1, 31),
    ("month", 1, 12),
    ("weekday", 0, 7),
)


def _validate_cron_field(expr: str, label: str, low: int, high: int) -> None:
    if not expr:
        raise ValueError(f"Cron {label} 字段不能为空")
    for token in expr.split(","):
        match = CRON_PART_RE.fullmatch(token)
        if match is None:
            raise ValueError(f"Cron {label} 字段语法无效：{token!r}")
        start_raw, end_raw, step_raw = match.groups()
        if step_raw is not None:
            step = int(step_raw)
            if step < 1 or step > high:
                raise ValueError(f"Cron {label} 字段步长越界：{token!r}")
        if start_raw == "*":
            if end_raw is not None:
                raise ValueError(f"Cron {label} 字段范围无效：{token!r}")
            continue
        start = int(start_raw)
        if not low <= start <= high:
            raise ValueError(f"Cron {label} 字段数值越界：{token!r}")
        if end_raw is not None:
            end = int(end_raw)
            if not low <= end <= high or end < start:
                raise ValueError(f"Cron {label} 字段区间无效：{token!r}")
        elif step_raw is not None:
            raise ValueError(
                f"Cron {label} 字段仅允许对 '*' 或 'a-b' 指定步长：{token!r}"
            )


def normalize_schedule_cron(raw: str | None) -> str | None:
    """Normalize 'HH:MM' or 5-field cron strings; blank/None disables scheduling."""
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    hhmm = HHMM_RE.fullmatch(cleaned)
    if hhmm is not None:
        hour, minute = int(hhmm.group(1)), int(hhmm.group(2))
        return f"{minute} {hour} * * *"
    parts = cleaned.split()
    if len(parts) != 5:
        raise ValueError("定时表达式仅支持 HH:MM 或 5 段 Cron 表达式")
    for part, (label, low, high) in zip(parts, _CRON_BOUNDS, strict=True):
        _validate_cron_field(part, label, low, high)
    return " ".join(parts)


def validate_timezone(tz: str | None) -> str:
    """Validate an IANA timezone name, defaulting to Asia/Shanghai."""
    if tz is None or not tz.strip():
        return "Asia/Shanghai"
    cleaned = tz.strip()
    try:
        ZoneInfo(cleaned)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ValueError(f"无效时区：{cleaned}") from exc
    return cleaned


def validate_drop_threshold(value: float | None) -> float:
    """Validate table-drop circuit-breaker ratio in (0, 1]."""
    if value is None:
        return 0.5
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0.0 < float(value) <= 1.0
    ):
        raise ValueError("表量骤降熔断阈值必须在 (0, 1] 范围内")
    return float(value)


def _normalize_optional_identifier(
    value: str | None, pattern: re.Pattern[str], label: str
) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if pattern.fullmatch(cleaned) is None:
        raise ValueError(f"{label}格式无效：{cleaned!r}")
    return cleaned


def _parse_json_dict(raw: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def decrypt_instance_secrets(raw_json: str | None) -> dict[str, str]:
    """Decrypt a stored JSON mapping of ciphertext secrets to plaintext values."""
    stored = _parse_json_dict(raw_json)
    return {
        str(key): crypto.decrypt(value)
        for key, value in stored.items()
        if isinstance(value, str) and value
    }


def merge_encrypted_secrets(
    existing_raw: str | None,
    incoming: Mapping[str, Any],
    secret_keys: set[str],
) -> str:
    """Update encrypted secret map while keeping masked '***' and clearing empty values."""
    current = _parse_json_dict(existing_raw)
    merged: dict[str, str] = {
        str(k): v for k, v in current.items() if isinstance(v, str) and v
    }
    for key in secret_keys:
        if key not in incoming:
            continue
        value = incoming[key]
        if value == "***":
            continue
        if value is None or value == "":
            merged.pop(key, None)
        else:
            merged[key] = crypto.encrypt(str(value))
    return json.dumps(merged, sort_keys=True)


def validate_instance_parameters(
    plugin,
    raw_params: Mapping[str, Any],
    *,
    existing_secrets: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate puller parameters against JSON Schema and split public vs secret fields."""
    schema = plugin.parameters_schema
    secret_keys = secret_parameter_keys(schema)
    public_params = {
        key: value for key, value in raw_params.items() if key not in secret_keys
    }
    secret_updates = {key: raw_params[key] for key in secret_keys if key in raw_params}

    for_validation = dict(public_params)
    for key in secret_keys:
        has_existing = bool(existing_secrets.get(key))
        if key in raw_params:
            val = raw_params[key]
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
    return public_params, secret_updates


class PullerPluginView(BaseModel):
    id: str
    version: str
    display_name: str
    parameters_schema: dict[str, Any]
    secret_keys: list[str]


class PullInstanceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    plugin_id: str
    display_name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    parameters: dict[str, Any] = Field(default_factory=dict)
    schedule_cron: str | None = None
    timezone: str = "Asia/Shanghai"
    catalog_prefix: str | None = None
    task_namespace: str | None = None
    drop_threshold: float = 0.5

    @field_validator("id", "plugin_id", "display_name", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class PullInstanceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    enabled: bool | None = None
    parameters: dict[str, Any] | None = None
    schedule_cron: str | None = None
    timezone: str | None = None
    catalog_prefix: str | None = None
    task_namespace: str | None = None
    drop_threshold: float | None = None

    @field_validator("display_name", mode="before")
    @classmethod
    def _strip_display_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class PullInstanceView(BaseModel):
    id: str
    project_id: str
    plugin_id: str
    display_name: str
    enabled: bool
    parameters: dict[str, Any]
    configured_secrets: list[str]
    secret_configured: bool
    schedule_cron: str | None
    timezone: str
    catalog_prefix: str | None
    task_namespace: str | None
    drop_threshold: float
    last_table_count: int | None
    last_triggered_at: UtcDatetime | None
    created_at: UtcDatetime
    updated_at: UtcDatetime


class ProjectExecutorView(BaseModel):
    id: str
    name: str
    enabled: bool
    parameters: dict[str, Any]
    parameters_schema: dict[str, Any]
    compatible: bool
    reason: str | None


class ProjectExecutorUpdateItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    enabled: bool
    parameters: dict[str, Any] = Field(default_factory=dict)


class ProjectExecutorsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    executors: list[ProjectExecutorUpdateItem]


def _instance_view(row: ProjectPullInstance, plugin=None) -> PullInstanceView:
    stored_secrets = _parse_json_dict(row.encrypted_secrets)
    secret_keys = secret_parameter_keys(plugin.parameters_schema) if plugin else set()
    public_params = {
        key: value
        for key, value in _parse_json_dict(row.parameters).items()
        if key not in secret_keys and key not in stored_secrets
    }
    configured_secrets = sorted(
        key for key, val in stored_secrets.items() if isinstance(val, str) and val
    )
    return PullInstanceView(
        id=row.id,
        project_id=row.project_id,
        plugin_id=row.plugin_id,
        display_name=row.display_name,
        enabled=row.enabled,
        parameters=public_params,
        configured_secrets=configured_secrets,
        secret_configured=bool(configured_secrets),
        schedule_cron=row.schedule_cron,
        timezone=row.timezone,
        catalog_prefix=row.catalog_prefix,
        task_namespace=row.task_namespace,
        drop_threshold=row.drop_threshold,
        last_table_count=row.last_table_count,
        last_triggered_at=row.last_triggered_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _require_warehouse_project(scope: ProjectContext) -> None:
    if scope.project_type != "warehouse":
        raise HTTPException(
            status_code=422, detail="文档型项目不支持配置数仓数据源拉取器"
        )


def _project_executors(scope: ProjectContext) -> list[ProjectExecutorView]:
    discovered = discover()
    saved = project_context.plugin_configs(scope.project_id)
    compatible = scope.project_type != "document"
    rows: list[ProjectExecutorView] = []
    for (category, plugin_id), plugin in sorted(
        discovered.items(), key=lambda item: item[1].id
    ):
        if category != "executor":
            continue
        enabled, parameters = saved.get(("executor", plugin_id), (False, {}))
        if not compatible:
            enabled = False
        rows.append(
            ProjectExecutorView(
                id=plugin_id,
                name=plugin.display_name,
                enabled=enabled,
                parameters=parameters,
                parameters_schema=plugin.parameters_schema,
                compatible=compatible,
                reason=None if compatible else "查询执行器仅支持数仓型项目",
            )
        )
    return rows


@router.get("/api/v1/puller-plugins", response_model=list[PullerPluginView])
def list_puller_plugins(_: Annotated[str, Depends(current_user)]):
    discovered = discover()
    return [
        PullerPluginView(
            id=plugin.id,
            version=plugin.version,
            display_name=plugin.display_name,
            parameters_schema=plugin.parameters_schema,
            secret_keys=sorted(secret_parameter_keys(plugin.parameters_schema)),
        )
        for (category, _), plugin in sorted(
            discovered.items(), key=lambda item: item[1].id
        )
        if category == "puller"
    ]


@router.get(
    "/api/v1/projects/{project_id}/pull-instances",
    response_model=list[PullInstanceView],
)
def list_pull_instances(
    project_id: str,
    scope: Annotated[ProjectContext, Depends(project_path_scope)],
):
    discovered = discover()
    with sessions()() as db:
        rows = (
            db.query(ProjectPullInstance)
            .filter(ProjectPullInstance.project_id == scope.project_id)
            .order_by(ProjectPullInstance.created_at, ProjectPullInstance.id)
            .all()
        )
        return [
            _instance_view(row, discovered.get(("puller", row.plugin_id)))
            for row in rows
        ]


@router.post(
    "/api/v1/projects/{project_id}/pull-instances",
    response_model=PullInstanceView,
    status_code=201,
)
def create_pull_instance(
    project_id: str,
    body: PullInstanceCreate,
    scope: Annotated[ProjectContext, Depends(admin_project_path_write_scope)],
):
    _require_warehouse_project(scope)
    if INSTANCE_ID_RE.fullmatch(body.id) is None:
        raise HTTPException(
            status_code=422,
            detail="实例标识仅允许 1-48 位小写字母、数字、下划线或连字符，且须以字母或数字开头",
        )
    plugin = discover().get(("puller", body.plugin_id))
    if plugin is None:
        raise HTTPException(status_code=422, detail=f"未知拉取器插件：{body.plugin_id}")

    try:
        schedule_cron = normalize_schedule_cron(body.schedule_cron)
        timezone = validate_timezone(body.timezone)
        catalog_prefix = _normalize_optional_identifier(
            body.catalog_prefix, CATALOG_PREFIX_RE, "Catalog 前缀"
        )
        task_namespace = _normalize_optional_identifier(
            body.task_namespace, TASK_NAMESPACE_RE, "任务命名空间"
        )
        drop_threshold = validate_drop_threshold(body.drop_threshold)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    public_params, _ = validate_instance_parameters(
        plugin, body.parameters, existing_secrets={}
    )
    secret_keys = secret_parameter_keys(plugin.parameters_schema)
    encrypted_secrets = merge_encrypted_secrets(None, body.parameters, secret_keys)
    now = datetime.now(UTC)

    with sessions().begin() as db:
        if db.get(ProjectPullInstance, (scope.project_id, body.id)) is not None:
            raise HTTPException(status_code=409, detail=f"拉取器实例已存在：{body.id}")
        row = ProjectPullInstance(
            project_id=scope.project_id,
            id=body.id,
            plugin_id=body.plugin_id,
            display_name=body.display_name,
            enabled=body.enabled,
            parameters=json.dumps(public_params, sort_keys=True, ensure_ascii=False),
            encrypted_secrets=encrypted_secrets,
            schedule_cron=schedule_cron,
            timezone=timezone,
            catalog_prefix=catalog_prefix,
            task_namespace=task_namespace,
            drop_threshold=drop_threshold,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        db.flush()
        return _instance_view(row, plugin)


@router.put(
    "/api/v1/projects/{project_id}/pull-instances/{instance_id}",
    response_model=PullInstanceView,
)
def update_pull_instance(
    project_id: str,
    instance_id: str,
    body: PullInstanceUpdate,
    scope: Annotated[ProjectContext, Depends(admin_project_path_write_scope)],
):
    _require_warehouse_project(scope)
    discovered = discover()
    with sessions().begin() as db:
        row = db.get(ProjectPullInstance, (scope.project_id, instance_id))
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"拉取器实例不存在：{instance_id}"
            )
        plugin = discovered.get(("puller", row.plugin_id))
        if plugin is None:
            raise HTTPException(
                status_code=422, detail=f"拉取器插件未安装：{row.plugin_id}"
            )

        if "display_name" in body.model_fields_set:
            if not body.display_name:
                raise HTTPException(status_code=422, detail="显示名称不能为空")
            row.display_name = body.display_name
        if "enabled" in body.model_fields_set:
            if body.enabled is None:
                raise HTTPException(status_code=422, detail="enabled 不能为空")
            row.enabled = body.enabled
        try:
            if "schedule_cron" in body.model_fields_set:
                row.schedule_cron = normalize_schedule_cron(body.schedule_cron)
            if "timezone" in body.model_fields_set:
                row.timezone = validate_timezone(body.timezone)
            if "catalog_prefix" in body.model_fields_set:
                row.catalog_prefix = _normalize_optional_identifier(
                    body.catalog_prefix, CATALOG_PREFIX_RE, "Catalog 前缀"
                )
            if "task_namespace" in body.model_fields_set:
                row.task_namespace = _normalize_optional_identifier(
                    body.task_namespace, TASK_NAMESPACE_RE, "任务命名空间"
                )
            if "drop_threshold" in body.model_fields_set:
                row.drop_threshold = validate_drop_threshold(body.drop_threshold)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        if "parameters" in body.model_fields_set:
            if body.parameters is None:
                raise HTTPException(status_code=422, detail="parameters 不能为空")
            existing_secrets = _parse_json_dict(row.encrypted_secrets)
            public_params, _ = validate_instance_parameters(
                plugin, body.parameters, existing_secrets=existing_secrets
            )
            secret_keys = secret_parameter_keys(plugin.parameters_schema)
            row.parameters = json.dumps(
                public_params, sort_keys=True, ensure_ascii=False
            )
            row.encrypted_secrets = merge_encrypted_secrets(
                row.encrypted_secrets, body.parameters, secret_keys
            )

        row.updated_at = datetime.now(UTC)
        db.flush()
        return _instance_view(row, plugin)


@router.delete(
    "/api/v1/projects/{project_id}/pull-instances/{instance_id}",
    status_code=204,
)
def delete_pull_instance(
    project_id: str,
    instance_id: str,
    scope: Annotated[ProjectContext, Depends(admin_project_path_write_scope)],
):
    _require_warehouse_project(scope)
    with sessions().begin() as db:
        row = db.get(ProjectPullInstance, (scope.project_id, instance_id))
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"拉取器实例不存在：{instance_id}"
            )
        db.delete(row)


@router.get(
    "/api/v1/projects/{project_id}/executors",
    response_model=list[ProjectExecutorView],
)
def list_project_executors(
    project_id: str,
    scope: Annotated[ProjectContext, Depends(project_path_scope)],
):
    return _project_executors(scope)


@router.put(
    "/api/v1/projects/{project_id}/executors",
    response_model=list[ProjectExecutorView],
)
def put_project_executors(
    project_id: str,
    body: ProjectExecutorsUpdate,
    scope: Annotated[ProjectContext, Depends(admin_project_path_write_scope)],
):
    discovered = discover()
    seen: set[str] = set()
    for item in body.executors:
        if item.id in seen:
            raise HTTPException(status_code=422, detail=f"查询执行器重复：{item.id}")
        seen.add(item.id)
        plugin = discovered.get(("executor", item.id))
        if plugin is None:
            raise HTTPException(status_code=422, detail=f"未知查询执行器：{item.id}")
        errors = sorted(
            Draft202012Validator(plugin.parameters_schema).iter_errors(item.parameters),
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
        if item.enabled and scope.project_type == "document":
            raise HTTPException(status_code=422, detail="查询执行器仅支持数仓型项目")

    saved = project_context.plugin_configs(scope.project_id)
    effective: dict[str, bool] = {
        pid: enabled for (cat, pid), (enabled, _) in saved.items() if cat == "executor"
    }
    for item in body.executors:
        effective[item.id] = item.enabled
    if sum(1 for enabled in effective.values() if enabled) > 1:
        raise HTTPException(
            status_code=422, detail="同一项目同时只能启用一个查询执行器"
        )

    with sessions().begin() as db:
        for item in body.executors:
            project_context.upsert_plugin_config(
                db,
                "executor",
                item.id,
                item.enabled,
                item.parameters,
                scope.project_id,
            )
    return _project_executors(scope)
