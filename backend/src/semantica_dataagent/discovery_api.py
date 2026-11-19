"""REST endpoints for project-level query-history and data-probing discovery config (#303)."""

from __future__ import annotations

import json
import math
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import project_context
from .auth import Requester, current_requester
from .db import ProjectSetting, Provider, sessions
from .project_api import actor_name, admin_project_path_scope
from .project_audit import record_project_audit
from .project_context import (
    ARCHIVED_DETAIL,
    DISCOVERY_CALLER_COLUMN_KEY,
    DISCOVERY_DATABASE_COLUMN_KEY,
    DISCOVERY_DIALECT_COLUMN_KEY,
    DISCOVERY_LOOKBACK_DAYS_KEY,
    DISCOVERY_MAX_FK_CANDIDATES_KEY,
    DISCOVERY_MAX_QUERIES_KEY,
    DISCOVERY_MIN_FREQUENCY_KEY,
    DISCOVERY_MIN_INCLUSION_RATIO_KEY,
    DISCOVERY_MODEL_KEY,
    DISCOVERY_PRESET_KEY,
    DISCOVERY_PROBE_CONCURRENCY_KEY,
    DISCOVERY_PROBE_TIMEOUT_SECONDS_KEY,
    DISCOVERY_PROVIDER_ID_KEY,
    DISCOVERY_SAMPLE_LIMIT_KEY,
    DISCOVERY_SOURCE_TABLE_KEY,
    DISCOVERY_SQL_COLUMN_KEY,
    DISCOVERY_STATUS_COLUMN_KEY,
    DISCOVERY_SUCCESS_STATUSES_KEY,
    DISCOVERY_TIME_COLUMN_KEY,
    FROZEN_STATUSES,
    ProjectContext,
    is_valid_sql_identifier,
    is_valid_status_literal,
    is_valid_table_identifier,
)

router = APIRouter()

DiscoveryPreset = Literal["doris_audit_log", "dtb_tracking_table"]
DOCUMENT_DISCOVERY_DETAIL = "文档型项目不支持知识发现配置"

_TEMPLATE_FIELD_KEYS: tuple[tuple[str, str], ...] = (
    ("source_table", DISCOVERY_SOURCE_TABLE_KEY),
    ("sql_column", DISCOVERY_SQL_COLUMN_KEY),
    ("status_column", DISCOVERY_STATUS_COLUMN_KEY),
    ("time_column", DISCOVERY_TIME_COLUMN_KEY),
    ("database_column", DISCOVERY_DATABASE_COLUMN_KEY),
    ("caller_column", DISCOVERY_CALLER_COLUMN_KEY),
    ("dialect_column", DISCOVERY_DIALECT_COLUMN_KEY),
    ("success_statuses", DISCOVERY_SUCCESS_STATUSES_KEY),
)


class DiscoveryPresetTemplate(BaseModel):
    preset: DiscoveryPreset
    source_table: str
    sql_column: str
    status_column: str
    time_column: str
    database_column: str | None = None
    caller_column: str | None = None
    dialect_column: str | None = None
    success_statuses: list[str]


class DiscoveryConfigView(BaseModel):
    preset: DiscoveryPreset
    source_table: str
    sql_column: str
    status_column: str
    time_column: str
    database_column: str | None = None
    caller_column: str | None = None
    dialect_column: str | None = None
    success_statuses: list[str]
    lookback_days: int
    max_queries: int
    min_frequency: int
    max_fk_candidates: int
    sample_limit: int
    min_inclusion_ratio: float
    probe_timeout_seconds: int
    probe_concurrency: int
    provider_id: str | None = None
    model: str | None = None
    presets: dict[str, DiscoveryPresetTemplate] = Field(default_factory=dict)


class DiscoveryConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset: DiscoveryPreset | None = None
    source_table: str | None = None
    sql_column: str | None = None
    status_column: str | None = None
    time_column: str | None = None
    database_column: str | None = None
    caller_column: str | None = None
    dialect_column: str | None = None
    success_statuses: list[str] | None = None
    lookback_days: int | None = Field(default=None, ge=1, le=90)
    max_queries: int | None = Field(default=None, ge=50, le=20000)
    min_frequency: int | None = Field(default=None, ge=1, le=500)
    max_fk_candidates: int | None = Field(default=None, ge=0, le=50)
    sample_limit: int | None = Field(default=None, ge=100, le=5000)
    min_inclusion_ratio: float | None = Field(default=None, ge=0.5, le=1.0)
    probe_timeout_seconds: int | None = Field(default=None, ge=5, le=120)
    probe_concurrency: int | None = Field(default=None, ge=1, le=2)
    provider_id: str | None = None
    model: str | None = None

    @field_validator(
        "lookback_days",
        "max_queries",
        "min_frequency",
        "max_fk_candidates",
        "sample_limit",
        "probe_timeout_seconds",
        "probe_concurrency",
        mode="before",
    )
    @classmethod
    def _reject_bool_for_int(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("整数参数不接受布尔值")  # noqa: TRY004 - Pydantic requires ValueError for 422
        return value

    @field_validator("min_inclusion_ratio", mode="before")
    @classmethod
    def _validate_ratio_finite(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("包含度阈值不接受布尔值")  # noqa: TRY004 - Pydantic requires ValueError for 422
        if isinstance(value, (int, float)) and not math.isfinite(float(value)):
            raise ValueError("包含度阈值必须为有限数值")
        return value

    @field_validator("source_table")
    @classmethod
    def _validate_source_table(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not is_valid_table_identifier(value):
            raise ValueError("source_table 必须为合法的一段、两段或三段 SQL 表标识符")
        return value

    @field_validator("sql_column", "status_column", "time_column")
    @classmethod
    def _validate_required_column(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not is_valid_sql_identifier(value):
            raise ValueError("列名必须为合法 SQL 标识符，不得包含空格或特殊字符")
        return value

    @field_validator(
        "database_column", "caller_column", "dialect_column", mode="before"
    )
    @classmethod
    def _validate_optional_column(cls, value: Any) -> str | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str) or not is_valid_sql_identifier(value):
            raise ValueError("可选列名必须为合法 SQL 标识符或为空")
        return value

    @field_validator("success_statuses")
    @classmethod
    def _validate_success_statuses(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value or len(value) > 20:
            raise ValueError("success_statuses 必须包含 1 到 20 个合法状态值")
        deduped: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not is_valid_status_literal(item):
                raise ValueError(
                    "success_statuses 包含非法状态字面量，不得包含空格、引号或分号"
                )
            if item not in seen:
                seen.add(item)
                deduped.append(item)
        return deduped

    @field_validator("provider_id", "model", mode="before")
    @classmethod
    def _normalize_optional_str(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


def _require_warehouse_project(scope: ProjectContext) -> None:
    if scope.project_type != "warehouse":
        raise HTTPException(status_code=422, detail=DOCUMENT_DISCOVERY_DETAIL)


def _validate_provider_model(db, provider_id: str | None, model: str | None) -> None:
    if provider_id is None:
        if model is not None:
            raise HTTPException(
                status_code=422,
                detail="model requires provider_id",
            )
        return
    prov = db.get(Provider, provider_id)
    if prov is None:
        raise HTTPException(
            status_code=422, detail=f"discovery provider not found: {provider_id}"
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


def _build_config_view(project_id: str, db=None) -> DiscoveryConfigView:
    cfg = project_context.discovery_config(project_id, db)
    presets = {
        name: DiscoveryPresetTemplate(**tpl)
        for name, tpl in project_context.discovery_presets().items()
    }
    return DiscoveryConfigView(**cfg, presets=presets)


@router.get(
    "/api/v1/projects/{project_id}/discovery-config",
    response_model=DiscoveryConfigView,
)
def get_project_discovery_config(
    project_id: str,
    scope: Annotated[ProjectContext, Depends(admin_project_path_scope)],
):
    _require_warehouse_project(scope)
    return _build_config_view(scope.project_id)


@router.put(
    "/api/v1/projects/{project_id}/discovery-config",
    response_model=DiscoveryConfigView,
)
def put_project_discovery_config(
    project_id: str,
    body: DiscoveryConfigUpdate,
    scope: Annotated[ProjectContext, Depends(admin_project_path_scope)],
    requester: Annotated[Requester, Depends(current_requester)],
):
    if scope.status in FROZEN_STATUSES:
        raise HTTPException(status_code=409, detail=ARCHIVED_DETAIL)
    _require_warehouse_project(scope)

    non_nullable_fields = (
        "preset",
        "source_table",
        "sql_column",
        "status_column",
        "time_column",
        "success_statuses",
    )
    for field_name in non_nullable_fields:
        if field_name in body.model_fields_set and getattr(body, field_name) is None:
            raise HTTPException(status_code=422, detail=f"{field_name} 不能为空")

    with sessions().begin() as db:
        current = project_context.discovery_config(scope.project_id, db)

        next_prov = (
            body.provider_id
            if "provider_id" in body.model_fields_set
            else current["provider_id"]
        )
        next_model = (
            body.model if "model" in body.model_fields_set else current["model"]
        )
        if (
            "provider_id" in body.model_fields_set
            and body.provider_id is None
            and "model" not in body.model_fields_set
        ):
            next_model = None
        _validate_provider_model(db, next_prov, next_model)

        updates: list[tuple[str, str | None]] = []
        preset_changed = (
            "preset" in body.model_fields_set
            and body.preset is not None
            and body.preset != current["preset"]
        )
        if "preset" in body.model_fields_set and body.preset is not None:
            updates.append((DISCOVERY_PRESET_KEY, body.preset))
        if preset_changed:
            for field_name, setting_key in _TEMPLATE_FIELD_KEYS:
                if field_name not in body.model_fields_set:
                    updates.append((setting_key, None))

        for field_name, setting_key in (
            ("source_table", DISCOVERY_SOURCE_TABLE_KEY),
            ("sql_column", DISCOVERY_SQL_COLUMN_KEY),
            ("status_column", DISCOVERY_STATUS_COLUMN_KEY),
            ("time_column", DISCOVERY_TIME_COLUMN_KEY),
        ):
            if field_name in body.model_fields_set:
                updates.append((setting_key, getattr(body, field_name)))

        for field_name, setting_key in (
            ("database_column", DISCOVERY_DATABASE_COLUMN_KEY),
            ("caller_column", DISCOVERY_CALLER_COLUMN_KEY),
            ("dialect_column", DISCOVERY_DIALECT_COLUMN_KEY),
        ):
            if field_name in body.model_fields_set:
                val = getattr(body, field_name)
                # Persist empty string when explicitly cleared so preset defaults do not reappear.
                updates.append((setting_key, "" if val is None else val))

        if "success_statuses" in body.model_fields_set:
            updates.append(
                (
                    DISCOVERY_SUCCESS_STATUSES_KEY,
                    json.dumps(body.success_statuses, ensure_ascii=False),
                )
            )

        for field_name, setting_key in (
            ("lookback_days", DISCOVERY_LOOKBACK_DAYS_KEY),
            ("max_queries", DISCOVERY_MAX_QUERIES_KEY),
            ("min_frequency", DISCOVERY_MIN_FREQUENCY_KEY),
            ("max_fk_candidates", DISCOVERY_MAX_FK_CANDIDATES_KEY),
            ("sample_limit", DISCOVERY_SAMPLE_LIMIT_KEY),
            ("min_inclusion_ratio", DISCOVERY_MIN_INCLUSION_RATIO_KEY),
            ("probe_timeout_seconds", DISCOVERY_PROBE_TIMEOUT_SECONDS_KEY),
            ("probe_concurrency", DISCOVERY_PROBE_CONCURRENCY_KEY),
        ):
            if field_name in body.model_fields_set:
                val = getattr(body, field_name)
                updates.append((setting_key, None if val is None else str(val)))

        if "provider_id" in body.model_fields_set:
            updates.append((DISCOVERY_PROVIDER_ID_KEY, next_prov))
        if "model" in body.model_fields_set or "provider_id" in body.model_fields_set:
            updates.append((DISCOVERY_MODEL_KEY, next_model))

        for setting_key, val in updates:
            _upsert_project_setting(db, scope.project_id, setting_key, val)
        db.flush()

        saved = project_context.discovery_config(scope.project_id, db)
        record_project_audit(
            db,
            project_id=scope.project_id,
            actor_id=requester.id,
            actor_name=actor_name(db, requester),
            action="project.discovery_config",
            detail=saved,
        )
        return _build_config_view(scope.project_id, db)
