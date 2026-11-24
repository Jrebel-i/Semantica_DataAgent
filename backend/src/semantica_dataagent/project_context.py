"""Resolve the current project and expose its project-level configuration.

This is the single entry point for project-scoped reads.  It currently always
resolves to the default project: the existing business knowledge base.  A
future ticket can add real project records and switch the resolution rule here
without touching every caller.

Project-level values are the knowledge generation, source directory, generation
retention, build schedule, dimension value limit, plugin configuration, build
queue and correction write-back target.  Global values such as the Neo4j and
Milvus connections, embedding service and provider credentials stay where they
are; this module only selects the project-scoped slice.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

from .db import (
    KnowledgeGeneration,
    PluginConfig,
    Project,
    ProjectSetting,
    Setting,
    generation_pointer,
    sessions,
)

DEFAULT_PROJECT_ID = "default"
DEFAULT_PROJECT_NAME = "业务数据"
DEFAULT_PROJECT_TYPE = "warehouse"
DEFAULT_PROJECT_STATUS = "active"
ACTIVE_STATUS = "active"
ARCHIVED_STATUS = "archived"
DELETING_STATUS = "deleting"
FROZEN_STATUSES = frozenset({ARCHIVED_STATUS, DELETING_STATUS})
ARCHIVED_DETAIL = "项目已归档"

GENERATION_KEY = "knowledge_generation"
SOURCE_DIRECTORY_KEY = "source_directory"
GENERATION_RETENTION_KEY = "generation_retention"
SCHEDULE_INTERVAL_KEY = "schedule_interval_seconds"
DIMENSION_VALUE_LIMIT_KEY = "dimension_value_limit"
GENERATION_PREFIX_KEY = "generation_prefix"
CORRECTION_REQUIRE_REVIEW_KEY = "correction_require_review"

AUTO_EVOLVE_ENABLED_KEY = "evolution_auto_enabled"
EVOLUTION_MIN_TRAJECTORIES_KEY = "evolution_min_trajectories"
EVOLUTION_MIN_INTERVAL_HOURS_KEY = "evolution_min_interval_hours"
EVOLUTION_MAX_IDLE_DAYS_KEY = "evolution_max_idle_days"
EVOLUTION_MAX_ROUNDS_KEY = "evolution_max_rounds"
EVOLUTION_MAX_EVAL_CASES_KEY = "evolution_max_eval_cases"
EVOLUTION_PROVIDER_ID_KEY = "evolution_provider_id"
EVOLUTION_MODEL_KEY = "evolution_model"
EVOLUTION_JUDGE_PROVIDER_ID_KEY = "evolution_judge_provider_id"
EVOLUTION_JUDGE_MODEL_KEY = "evolution_judge_model"
EVOLUTION_MAX_TRACES_PER_RUN_KEY = "evolution_max_traces_per_run"

DEFAULT_AUTO_EVOLVE_ENABLED = False
DEFAULT_EVOLUTION_MIN_TRAJECTORIES = 30
DEFAULT_EVOLUTION_MIN_INTERVAL_HOURS = 24
DEFAULT_EVOLUTION_MAX_IDLE_DAYS = 7
DEFAULT_EVOLUTION_MAX_ROUNDS = 3
DEFAULT_EVOLUTION_MAX_EVAL_CASES = 20
DEFAULT_EVOLUTION_MAX_TRACES_PER_RUN = 200

DISCOVERY_PRESET_KEY = "discovery_preset"
DISCOVERY_SOURCE_TABLE_KEY = "discovery_source_table"
DISCOVERY_SQL_COLUMN_KEY = "discovery_sql_column"
DISCOVERY_STATUS_COLUMN_KEY = "discovery_status_column"
DISCOVERY_TIME_COLUMN_KEY = "discovery_time_column"
DISCOVERY_DATABASE_COLUMN_KEY = "discovery_database_column"
DISCOVERY_CALLER_COLUMN_KEY = "discovery_caller_column"
DISCOVERY_DIALECT_COLUMN_KEY = "discovery_dialect_column"
DISCOVERY_SUCCESS_STATUSES_KEY = "discovery_success_statuses"
DISCOVERY_LOOKBACK_DAYS_KEY = "discovery_lookback_days"
DISCOVERY_MAX_QUERIES_KEY = "discovery_max_queries"
DISCOVERY_MIN_FREQUENCY_KEY = "discovery_min_frequency"
DISCOVERY_MAX_FK_CANDIDATES_KEY = "discovery_max_fk_candidates"
DISCOVERY_SAMPLE_LIMIT_KEY = "discovery_sample_limit"
DISCOVERY_MIN_INCLUSION_RATIO_KEY = "discovery_min_inclusion_ratio"
DISCOVERY_PROBE_TIMEOUT_SECONDS_KEY = "discovery_probe_timeout_seconds"
DISCOVERY_PROBE_CONCURRENCY_KEY = "discovery_probe_concurrency"
DISCOVERY_PROVIDER_ID_KEY = "discovery_provider_id"
DISCOVERY_MODEL_KEY = "discovery_model"

DISCOVERY_SETTING_KEYS = (
    DISCOVERY_PRESET_KEY,
    DISCOVERY_SOURCE_TABLE_KEY,
    DISCOVERY_SQL_COLUMN_KEY,
    DISCOVERY_STATUS_COLUMN_KEY,
    DISCOVERY_TIME_COLUMN_KEY,
    DISCOVERY_DATABASE_COLUMN_KEY,
    DISCOVERY_CALLER_COLUMN_KEY,
    DISCOVERY_DIALECT_COLUMN_KEY,
    DISCOVERY_SUCCESS_STATUSES_KEY,
    DISCOVERY_LOOKBACK_DAYS_KEY,
    DISCOVERY_MAX_QUERIES_KEY,
    DISCOVERY_MIN_FREQUENCY_KEY,
    DISCOVERY_MAX_FK_CANDIDATES_KEY,
    DISCOVERY_SAMPLE_LIMIT_KEY,
    DISCOVERY_MIN_INCLUSION_RATIO_KEY,
    DISCOVERY_PROBE_TIMEOUT_SECONDS_KEY,
    DISCOVERY_PROBE_CONCURRENCY_KEY,
    DISCOVERY_PROVIDER_ID_KEY,
    DISCOVERY_MODEL_KEY,
)

DEFAULT_DISCOVERY_PRESET = "doris_audit_log"
DEFAULT_DISCOVERY_LOOKBACK_DAYS = 7
DEFAULT_DISCOVERY_MAX_QUERIES = 5000
DEFAULT_DISCOVERY_MIN_FREQUENCY = 3
DEFAULT_DISCOVERY_MAX_FK_CANDIDATES = 20
DEFAULT_DISCOVERY_SAMPLE_LIMIT = 1000
DEFAULT_DISCOVERY_MIN_INCLUSION_RATIO = 0.8
DEFAULT_DISCOVERY_PROBE_TIMEOUT_SECONDS = 30
DEFAULT_DISCOVERY_PROBE_CONCURRENCY = 2

DISCOVERY_PRESETS: dict[str, dict[str, Any]] = {
    "doris_audit_log": {
        "preset": "doris_audit_log",
        "source_table": "__internal_schema.audit_log",
        "sql_column": "stmt",
        "status_column": "state",
        "time_column": "time",
        "database_column": "db",
        "caller_column": "user",
        "dialect_column": None,
        "success_statuses": ["EOF", "OK"],
    },
    "dtb_tracking_table": {
        "preset": "dtb_tracking_table",
        "source_table": "mysql.data_tool_box_emr.query_history",
        "sql_column": "query_sql",
        "status_column": "status",
        "time_column": "create_time",
        "database_column": "database_name",
        "caller_column": "user_name",
        "dialect_column": "engine_type",
        "success_statuses": ["SUCCESS", "SUCCEEDED", "FINISHED"],
    },
}

SQL_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
SQL_TABLE_IDENTIFIER_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]{0,127}(?:\.[A-Za-z_][A-Za-z0-9_]{0,127}){0,2}$"
)
DISCOVERY_STATUS_LITERAL_PATTERN = re.compile(r"^[\w.-]{1,64}$")

PROJECTS_ROOT_ENV = "DATAAGENT_PROJECTS_ROOT"
PROJECT_ID_PATTERN = re.compile(r"[a-z][a-z0-9]{0,23}")
GENERATION_PREFIX_PATTERN = re.compile(r"[a-z0-9_]{1,25}")


def parse_bool_setting(raw: str | None, *, default: bool = True) -> bool:
    """Parse a persisted boolean setting, falling back on unset or blank values."""
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if not normalized:
        return default
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    return default


def evolution_policy(project_id: str, db=None) -> dict[str, bool]:
    """Return a project's correction review switch."""
    if db is None:
        with sessions()() as own:
            return evolution_policy(project_id, own)
    corr = db.get(ProjectSetting, (project_id, CORRECTION_REQUIRE_REVIEW_KEY))
    return {
        "correction_require_review": parse_bool_setting(
            corr.value if corr is not None else None, default=True
        ),
    }


def _parse_int_setting(
    raw: str | None, *, default: int, minimum: int, maximum: int
) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        parsed = int(raw.strip())
    except ValueError:
        return default
    return max(minimum, min(maximum, parsed))


def _parse_float_setting(
    raw: str | None, *, default: float, minimum: float, maximum: float
) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        parsed = float(raw.strip())
    except ValueError:
        return default
    if math.isnan(parsed):  # NaN guard
        return default
    return max(minimum, min(maximum, parsed))


def _optional_str_setting(raw: str | None) -> str | None:
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


def evolution_config(project_id: str, db=None) -> dict[str, Any]:
    """Return a project's auto-evolution schedule and model settings with safe defaults."""
    if db is None:
        with sessions()() as own:
            return evolution_config(project_id, own)
    keys = (
        AUTO_EVOLVE_ENABLED_KEY,
        EVOLUTION_MIN_TRAJECTORIES_KEY,
        EVOLUTION_MIN_INTERVAL_HOURS_KEY,
        EVOLUTION_MAX_IDLE_DAYS_KEY,
        EVOLUTION_MAX_ROUNDS_KEY,
        EVOLUTION_MAX_EVAL_CASES_KEY,
        EVOLUTION_PROVIDER_ID_KEY,
        EVOLUTION_MODEL_KEY,
        EVOLUTION_JUDGE_PROVIDER_ID_KEY,
        EVOLUTION_JUDGE_MODEL_KEY,
        EVOLUTION_MAX_TRACES_PER_RUN_KEY,
    )
    rows = {
        row.key: row.value
        for row in db.query(ProjectSetting)
        .filter(
            ProjectSetting.project_id == project_id,
            ProjectSetting.key.in_(keys),
        )
        .all()
    }
    return {
        "auto_evolve_enabled": parse_bool_setting(
            rows.get(AUTO_EVOLVE_ENABLED_KEY), default=DEFAULT_AUTO_EVOLVE_ENABLED
        ),
        "min_trajectories": _parse_int_setting(
            rows.get(EVOLUTION_MIN_TRAJECTORIES_KEY),
            default=DEFAULT_EVOLUTION_MIN_TRAJECTORIES,
            minimum=1,
            maximum=10000,
        ),
        "min_interval_hours": _parse_float_setting(
            rows.get(EVOLUTION_MIN_INTERVAL_HOURS_KEY),
            default=float(DEFAULT_EVOLUTION_MIN_INTERVAL_HOURS),
            minimum=0.0,
            maximum=8760.0,
        ),
        "max_idle_days": _parse_float_setting(
            rows.get(EVOLUTION_MAX_IDLE_DAYS_KEY),
            default=float(DEFAULT_EVOLUTION_MAX_IDLE_DAYS),
            minimum=1.0,
            maximum=365.0,
        ),
        "max_rounds": _parse_int_setting(
            rows.get(EVOLUTION_MAX_ROUNDS_KEY),
            default=DEFAULT_EVOLUTION_MAX_ROUNDS,
            minimum=1,
            maximum=20,
        ),
        "max_eval_cases": _parse_int_setting(
            rows.get(EVOLUTION_MAX_EVAL_CASES_KEY),
            default=DEFAULT_EVOLUTION_MAX_EVAL_CASES,
            minimum=1,
            maximum=200,
        ),
        "evolution_provider_id": _optional_str_setting(
            rows.get(EVOLUTION_PROVIDER_ID_KEY)
        ),
        "evolution_model": _optional_str_setting(rows.get(EVOLUTION_MODEL_KEY)),
        "judge_provider_id": _optional_str_setting(
            rows.get(EVOLUTION_JUDGE_PROVIDER_ID_KEY)
        ),
        "judge_model": _optional_str_setting(rows.get(EVOLUTION_JUDGE_MODEL_KEY)),
        "max_traces_per_run": _parse_int_setting(
            rows.get(EVOLUTION_MAX_TRACES_PER_RUN_KEY),
            default=DEFAULT_EVOLUTION_MAX_TRACES_PER_RUN,
            minimum=1,
            maximum=2000,
        ),
    }


def is_valid_sql_identifier(value: Any) -> bool:
    """Return True if value is a safe single SQL identifier (column or segment)."""
    return isinstance(value, str) and bool(SQL_IDENTIFIER_PATTERN.fullmatch(value))


def is_valid_table_identifier(value: Any) -> bool:
    """Return True if value is a safe 1-, 2-, or 3-part SQL table identifier."""
    return isinstance(value, str) and bool(
        SQL_TABLE_IDENTIFIER_PATTERN.fullmatch(value)
    )


def is_valid_status_literal(value: Any) -> bool:
    """Return True if value is a safe non-empty status literal without injection chars."""
    if not isinstance(value, str) or not value:
        return False
    if any(ch.isspace() for ch in value) or "--" in value:
        return False
    return bool(DISCOVERY_STATUS_LITERAL_PATTERN.fullmatch(value))


def _parse_finite_float_setting(
    raw: str | None, *, default: float, minimum: float, maximum: float
) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        parsed = float(raw.strip())
    except ValueError:
        return default
    if not math.isfinite(parsed):
        return default
    return max(minimum, min(maximum, parsed))


def _parse_discovery_identifier(
    raw: str | None, *, default: str, is_table: bool = False
) -> str:
    if raw is None:
        return default
    cleaned = raw.strip()
    check = is_valid_table_identifier if is_table else is_valid_sql_identifier
    return cleaned if check(cleaned) else default


def _parse_optional_discovery_column(
    rows: Mapping[str, str], key: str, default: str | None
) -> str | None:
    if key not in rows:
        return default
    cleaned = (rows.get(key) or "").strip()
    if not cleaned:
        return None
    return cleaned if is_valid_sql_identifier(cleaned) else default


def _parse_discovery_success_statuses(
    raw: str | None, *, default: list[str]
) -> list[str]:
    if raw is None or not raw.strip():
        return list(default)
    try:
        parsed = json.loads(raw)
    except ValueError:
        return list(default)
    if not isinstance(parsed, list) or not parsed:
        return list(default)
    result: list[str] = []
    seen: set[str] = set()
    for item in parsed:
        if not isinstance(item, str):
            return list(default)
        cleaned = item.strip()
        if not is_valid_status_literal(cleaned):
            return list(default)
        if cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result or list(default)


def discovery_presets() -> dict[str, dict[str, Any]]:
    """Return deep copies of the built-in query-history preset templates."""
    return {
        name: {**tpl, "success_statuses": list(tpl["success_statuses"])}
        for name, tpl in DISCOVERY_PRESETS.items()
    }


def discovery_config(project_id: str, db=None) -> dict[str, Any]:
    """Return a project's query-history and data-probing config with safe defaults."""
    if db is None:
        with sessions()() as own:
            return discovery_config(project_id, own)
    rows = {
        row.key: row.value
        for row in db.query(ProjectSetting)
        .filter(
            ProjectSetting.project_id == project_id,
            ProjectSetting.key.in_(DISCOVERY_SETTING_KEYS),
        )
        .all()
    }
    raw_preset = _optional_str_setting(rows.get(DISCOVERY_PRESET_KEY))
    preset = raw_preset if raw_preset in DISCOVERY_PRESETS else DEFAULT_DISCOVERY_PRESET
    template = DISCOVERY_PRESETS[preset]

    return {
        "preset": preset,
        "source_table": _parse_discovery_identifier(
            rows.get(DISCOVERY_SOURCE_TABLE_KEY),
            default=template["source_table"],
            is_table=True,
        ),
        "sql_column": _parse_discovery_identifier(
            rows.get(DISCOVERY_SQL_COLUMN_KEY),
            default=template["sql_column"],
        ),
        "status_column": _parse_discovery_identifier(
            rows.get(DISCOVERY_STATUS_COLUMN_KEY),
            default=template["status_column"],
        ),
        "time_column": _parse_discovery_identifier(
            rows.get(DISCOVERY_TIME_COLUMN_KEY),
            default=template["time_column"],
        ),
        "database_column": _parse_optional_discovery_column(
            rows, DISCOVERY_DATABASE_COLUMN_KEY, template["database_column"]
        ),
        "caller_column": _parse_optional_discovery_column(
            rows, DISCOVERY_CALLER_COLUMN_KEY, template["caller_column"]
        ),
        "dialect_column": _parse_optional_discovery_column(
            rows, DISCOVERY_DIALECT_COLUMN_KEY, template["dialect_column"]
        ),
        "success_statuses": _parse_discovery_success_statuses(
            rows.get(DISCOVERY_SUCCESS_STATUSES_KEY),
            default=template["success_statuses"],
        ),
        "lookback_days": _parse_int_setting(
            rows.get(DISCOVERY_LOOKBACK_DAYS_KEY),
            default=DEFAULT_DISCOVERY_LOOKBACK_DAYS,
            minimum=1,
            maximum=90,
        ),
        "max_queries": _parse_int_setting(
            rows.get(DISCOVERY_MAX_QUERIES_KEY),
            default=DEFAULT_DISCOVERY_MAX_QUERIES,
            minimum=50,
            maximum=20000,
        ),
        "min_frequency": _parse_int_setting(
            rows.get(DISCOVERY_MIN_FREQUENCY_KEY),
            default=DEFAULT_DISCOVERY_MIN_FREQUENCY,
            minimum=1,
            maximum=500,
        ),
        "max_fk_candidates": _parse_int_setting(
            rows.get(DISCOVERY_MAX_FK_CANDIDATES_KEY),
            default=DEFAULT_DISCOVERY_MAX_FK_CANDIDATES,
            minimum=0,
            maximum=50,
        ),
        "sample_limit": _parse_int_setting(
            rows.get(DISCOVERY_SAMPLE_LIMIT_KEY),
            default=DEFAULT_DISCOVERY_SAMPLE_LIMIT,
            minimum=100,
            maximum=5000,
        ),
        "min_inclusion_ratio": _parse_finite_float_setting(
            rows.get(DISCOVERY_MIN_INCLUSION_RATIO_KEY),
            default=DEFAULT_DISCOVERY_MIN_INCLUSION_RATIO,
            minimum=0.5,
            maximum=1.0,
        ),
        "probe_timeout_seconds": _parse_int_setting(
            rows.get(DISCOVERY_PROBE_TIMEOUT_SECONDS_KEY),
            default=DEFAULT_DISCOVERY_PROBE_TIMEOUT_SECONDS,
            minimum=5,
            maximum=120,
        ),
        "probe_concurrency": _parse_int_setting(
            rows.get(DISCOVERY_PROBE_CONCURRENCY_KEY),
            default=DEFAULT_DISCOVERY_PROBE_CONCURRENCY,
            minimum=1,
            maximum=2,
        ),
        "provider_id": _optional_str_setting(rows.get(DISCOVERY_PROVIDER_ID_KEY)),
        "model": _optional_str_setting(rows.get(DISCOVERY_MODEL_KEY)),
    }


PROJECT_SETTING_KEYS = frozenset(
    {
        GENERATION_KEY,
        SOURCE_DIRECTORY_KEY,
        GENERATION_RETENTION_KEY,
        SCHEDULE_INTERVAL_KEY,
        DIMENSION_VALUE_LIMIT_KEY,
    }
)

STORAGE_PREFIX = "sda_"
_GENERATION_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")

_bound_generations: ContextVar[dict[str, str] | None] = ContextVar(
    "project_bound_generations", default=None
)


@contextmanager
def bind_generation(project_id: str, generation: str):
    """Temporarily aim all reads for ``project_id`` at an isolated generation."""
    current = dict(_bound_generations.get() or {})
    current[project_id] = generation
    token = _bound_generations.set(current)
    try:
        yield
    finally:
        _bound_generations.reset(token)


class ProjectNotFound(LookupError):
    """Raised when a caller asks for a project that has no record."""


def neo4j_password(values: Mapping[str, Any]) -> str | None:
    encrypted = values.get("neo4j_password")
    if encrypted:
        return (
            Fernet(os.environ["DATAAGENT_ENCRYPTION_KEY"].encode())
            .decrypt(encrypted.encode())
            .decode()
        )
    user, separator, password = os.environ.get("DATAAGENT_NEO4J_AUTH", "").partition(
        "/"
    )
    if separator and password and user == values.get("neo4j_user"):
        return password
    return None


def _read_settings(db, project_id: str = DEFAULT_PROJECT_ID) -> dict[str, str]:
    values = {row.key: row.value for row in db.query(Setting).all()}
    if project_id != DEFAULT_PROJECT_ID:
        values.pop(SCHEDULE_INTERVAL_KEY, None)
        values.update(
            {
                row.key: row.value
                for row in db.query(ProjectSetting)
                .filter(ProjectSetting.project_id == project_id)
                .all()
            }
        )
    pointer = generation_pointer(db, project_id)
    if pointer:
        values[GENERATION_KEY] = pointer.generation
    values["_project_id"] = project_id
    return values


def normalize_values(values: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the defaults and secret handling previously done by settings()."""
    result = dict(values)
    password = neo4j_password(result)
    if password:
        result["neo4j_password"] = password
    result.setdefault("embedding_model", "model/bge-m3")
    result.setdefault(GENERATION_KEY, DEFAULT_PROJECT_ID)
    if not _GENERATION_PATTERN.fullmatch(result[GENERATION_KEY]):
        raise ValueError("Invalid generation")
    return result


def resolve_project_id(
    project_id: str | None = None,
    *,
    request: Any = None,
    token: Any = None,
    conversation: Any = None,
) -> str:
    """Resolve an explicit project id; requests, tokens and conversations stay default."""
    if project_id is None:
        return DEFAULT_PROJECT_ID
    with sessions()() as db:
        if db.get(Project, project_id) is None:
            raise ProjectNotFound(project_id)
    return project_id


@dataclass(frozen=True)
class ProjectContext:
    """Project identity plus the rules that derive project-scoped resources."""

    project_id: str
    _values: Mapping[str, Any]
    name: str = DEFAULT_PROJECT_NAME
    project_type: str = DEFAULT_PROJECT_TYPE
    status: str = DEFAULT_PROJECT_STATUS

    @classmethod
    def from_values(
        cls,
        values: Mapping[str, Any],
        project_id: str = DEFAULT_PROJECT_ID,
        *,
        name: str = DEFAULT_PROJECT_NAME,
        project_type: str = DEFAULT_PROJECT_TYPE,
        status: str = DEFAULT_PROJECT_STATUS,
    ) -> ProjectContext:
        return cls(project_id, dict(values), name, project_type, status)

    @property
    def values(self) -> dict[str, Any]:
        return dict(self._values)

    @property
    def generation(self) -> str:
        return self._values[GENERATION_KEY]

    @property
    def generation_prefix(self) -> str:
        value = self._values.get(GENERATION_PREFIX_KEY)
        if isinstance(value, str) and value:
            return value
        return self.project_id + "_"

    def new_generation(self) -> str:
        """Create a fresh generation name that keeps the project's prefix."""
        prefix = self.generation_prefix
        base = self.generation
        if self.project_id == DEFAULT_PROJECT_ID:
            return base[:30] + "_" + uuid.uuid4().hex
        if not base.startswith(prefix):
            base = prefix + base
        return base[:30] + "_" + uuid.uuid4().hex

    @property
    def source_directory_value(self) -> str | None:
        value = self._values.get(SOURCE_DIRECTORY_KEY)
        return value or None

    @property
    def source_directory(self) -> Path:
        return Path(self._values[SOURCE_DIRECTORY_KEY]).resolve()

    @property
    def generation_retention(self) -> int:
        return int(self._values.get(GENERATION_RETENTION_KEY, "2"))

    @property
    def schedule_interval_seconds(self) -> int:
        if SCHEDULE_INTERVAL_KEY not in self._values:
            return 0
        return int(self._values[SCHEDULE_INTERVAL_KEY])

    @property
    def dimension_value_limit(self) -> str:
        return self._values.get(DIMENSION_VALUE_LIMIT_KEY, "1000")

    @property
    def dimension_value_limit_int(self) -> int:
        return int(self.dimension_value_limit)

    def collection_name(self, generation: str | None = None) -> str:
        return STORAGE_PREFIX + (generation or self.generation)

    @classmethod
    def index_name(cls, name: str) -> str:
        return STORAGE_PREFIX + name

    @classmethod
    def label(cls, name: str) -> str:
        return "SDA" + name

    def plugin_configs(self) -> dict[tuple[str, str], tuple[bool, dict]]:
        return plugin_configs(self.project_id)

    def plugin_config(self, category: str, plugin_id: str) -> tuple[bool, dict] | None:
        return plugin_config(category, plugin_id, self.project_id)

    def upsert_plugin_config(
        self,
        db,
        category: str,
        plugin_id: str,
        enabled: bool,
        parameters: Mapping[str, Any],
    ):
        return upsert_plugin_config(
            db, category, plugin_id, enabled, parameters, self.project_id
        )

    def enqueue_build(self, build_type: str, triggered_by: str, *, notify: bool = True):
        return enqueue_build(
            build_type,
            triggered_by,
            project_id=self.project_id,
            notify=notify,
        )

    def add_queued_build(
        self,
        db,
        build_type: str,
        triggered_by: str,
        correction_proposal_id: str | None = None,
    ) -> int:
        return add_queued_build(
            db,
            build_type,
            triggered_by,
            correction_proposal_id=correction_proposal_id,
            project_id=self.project_id,
        )

    def wake_build_queue(self) -> None:
        wake_build_queue()

    def configure_schedule(self, interval: int) -> None:
        configure_schedule(interval, self.project_id)

    def start_schedule(self) -> None:
        from .scheduler import scheduler

        scheduler.start(self.schedule_interval_seconds, project_id=self.project_id)

    @contextmanager
    def source_repository(self):
        from .source_directory import repository

        directory = self.source_directory_value
        if directory is None:
            raise ValueError("Source directory is not configured")
        with repository(directory) as root:
            yield root


def current_project(
    project_id: str | None = None,
    *,
    request: Any = None,
    token: Any = None,
    conversation: Any = None,
) -> ProjectContext:
    """Load the resolved project's values and metadata."""
    resolved = resolve_project_id(
        project_id, request=request, token=token, conversation=conversation
    )
    with sessions()() as db:
        project = db.get(Project, resolved)
        if project is None:
            raise ProjectNotFound(resolved)
        values = _read_settings(db, resolved)
    overrides = _bound_generations.get()
    if overrides and resolved in overrides:
        values[GENERATION_KEY] = overrides[resolved]
    values = normalize_values(values)
    return ProjectContext(resolved, values, project.name, project.type, project.status)


def project_status(project_id: str = DEFAULT_PROJECT_ID) -> str:
    """Return a project's lifecycle status; the implicit default is active."""
    with sessions()() as db:
        project = db.get(Project, project_id)
        if project is None:
            raise ProjectNotFound(project_id)
        return project.status


def project_is_frozen(project_id: str = DEFAULT_PROJECT_ID) -> bool:
    """Archived and deleting projects reject project-scoped work."""
    try:
        return project_status(project_id) in FROZEN_STATUSES
    except ProjectNotFound:
        return True


def project_type(project_id: str | None = None) -> str:
    """Return a project's immutable type, defaulting the default project."""
    resolved = resolve_project_id(project_id)
    if resolved == DEFAULT_PROJECT_ID:
        return DEFAULT_PROJECT_TYPE
    with sessions()() as db:
        project = db.get(Project, resolved)
        if project is None:
            raise ProjectNotFound(resolved)
        return project.type


def from_values(
    values: Mapping[str, Any],
    project_id: str | None = None,
    *,
    name: str = DEFAULT_PROJECT_NAME,
    project_type: str = DEFAULT_PROJECT_TYPE,
    status: str = DEFAULT_PROJECT_STATUS,
) -> ProjectContext:
    resolved = project_id or values.get("_project_id") or DEFAULT_PROJECT_ID
    return ProjectContext.from_values(
        values,
        resolved,
        name=name,
        project_type=project_type,
        status=status,
    )


def with_generation(values: Mapping[str, Any], generation: str) -> dict[str, Any]:
    """Return a copy of settings values aimed at another generation."""
    result = dict(values)
    result[GENERATION_KEY] = generation
    return result


def with_source_directory(
    values: Mapping[str, Any], directory: str | Path
) -> dict[str, Any]:
    """Return a copy of settings values aimed at an isolated source working copy."""
    result = dict(values)
    result[SOURCE_DIRECTORY_KEY] = str(Path(directory).resolve())
    return result


def index_name(name: str) -> str:
    return ProjectContext.index_name(name)


def label(name: str) -> str:
    return ProjectContext.label(name)


def source_directory_value(project_id: str | None = None) -> str | None:
    resolved = resolve_project_id(project_id)
    with sessions()() as db:
        if resolved != DEFAULT_PROJECT_ID:
            row = db.get(ProjectSetting, (resolved, SOURCE_DIRECTORY_KEY))
            return row.value if row is not None and row.value else None
        row = db.get(Setting, SOURCE_DIRECTORY_KEY)
        return row.value if row is not None and row.value else None


def projects_root() -> Path:
    configured = os.environ.get(PROJECTS_ROOT_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    database = Path(os.environ.get("DATAAGENT_DB_PATH", "data/app.db"))
    return database.expanduser().resolve().parent / "projects"


def project_source_directory(project_id: str) -> Path:
    return projects_root() / project_id / "source"


def candidate_generations_dir(project_id: str) -> Path:
    """Directory holding isolated temporary working copies for candidate generations."""
    return projects_root() / project_id / ".candidates"


def project_eval_directory(project_id: str) -> Path:
    """Return the source-isolated eval directory for a project."""
    root = projects_root().resolve()
    project_dir = (root / project_id).resolve()
    if project_dir.parent != root or project_dir == root:
        raise ValueError(f"Invalid project directory for eval questions: {project_id}")
    return project_dir / "eval"


def eval_questions_path(project_id: str) -> Path:
    """Return the per-project questions.yaml path, verified outside source_directory."""
    target = (project_eval_directory(project_id) / "questions.yaml").resolve()
    candidates = [project_source_directory(project_id)]
    configured_source = source_directory_value(project_id)
    if configured_source:
        candidates.append(Path(configured_source))
    if project_id == DEFAULT_PROJECT_ID and os.environ.get("DATAAGENT_SOURCE_DIR"):
        candidates.append(Path(os.environ["DATAAGENT_SOURCE_DIR"]))
    for source_path in candidates:
        source_root = source_path.expanduser().resolve()
        if target == source_root or target.is_relative_to(source_root):
            raise RuntimeError(
                f"Eval questions path {target} must stay isolated outside source directory {source_root}"
            )
    return target


def delete_project_eval_directory(project_id: str) -> None:
    """Remove a project's source-isolated eval directory if present."""
    eval_dir = project_eval_directory(project_id)
    if eval_dir.is_symlink():
        eval_dir.unlink()
    elif eval_dir.exists():
        shutil.rmtree(eval_dir)


def generation_is_built(project_id: str, generation: str) -> bool:
    """True when the generation has been produced by a successful build."""
    with sessions()() as db:
        row = db.get(KnowledgeGeneration, generation)
        return bool(
            row is not None
            and row.project_id == project_id
            and row.build_id is not None
            and not row.deleting
        )


def recover_projects() -> None:
    """Remove half-created project directories and restore missing source dirs."""
    root = projects_root()
    if not root.exists():
        return
    with sessions()() as db:
        known = {row.id: row.status for row in db.query(Project).all()}
    for entry in root.iterdir():
        if not entry.is_dir() or entry.is_symlink():
            continue
        if entry.name.startswith(".creating-"):
            shutil.rmtree(entry, ignore_errors=True)
            continue
        status = known.get(entry.name)
        if status is None:
            try:
                entry.rmdir()
            except OSError:
                pass
            continue
        if status == DELETING_STATUS:
            shutil.rmtree(entry, ignore_errors=True)
            continue
        if entry.name != DEFAULT_PROJECT_ID:
            source = entry / "source"
            if not source.exists():
                source.mkdir(parents=True, exist_ok=True)
    for project_id, status in known.items():
        if project_id == DEFAULT_PROJECT_ID or status == DELETING_STATUS:
            continue
        project_source_directory(project_id).mkdir(parents=True, exist_ok=True)
    try:
        root.rmdir()
    except OSError:
        pass


def plugin_configs(
    project_id: str = DEFAULT_PROJECT_ID,
) -> dict[tuple[str, str], tuple[bool, dict]]:
    with sessions()() as db:
        return {
            (row.category, row.plugin_id): (row.enabled, json.loads(row.parameters))
            for row in db.query(PluginConfig)
            .filter(PluginConfig.project_id == project_id)
            .all()
        }


def plugin_intervals(category: str, plugin_id: str) -> dict[str, int]:
    """Enabled intervals for one plugin, keyed by project id."""
    with sessions()() as db:
        rows = (
            db.query(PluginConfig)
            .filter(
                PluginConfig.category == category,
                PluginConfig.plugin_id == plugin_id,
                PluginConfig.enabled.is_(True),
            )
            .all()
        )
    result = {}
    for row in rows:
        try:
            parameters = json.loads(row.parameters)
        except (TypeError, ValueError):
            continue
        value = parameters.get("interval_seconds", 0)
        if isinstance(value, int) and value > 0:
            result[row.project_id] = value
    return result


def plugin_config(
    category: str,
    plugin_id: str,
    project_id: str = DEFAULT_PROJECT_ID,
) -> tuple[bool, dict] | None:
    with sessions()() as db:
        row = db.get(PluginConfig, (project_id, category, plugin_id))
        if row is None:
            return None
        return row.enabled, json.loads(row.parameters)


def upsert_plugin_config(
    db,
    category: str,
    plugin_id: str,
    enabled: bool,
    parameters: Mapping[str, Any],
    project_id: str = DEFAULT_PROJECT_ID,
):
    row = db.get(PluginConfig, (project_id, category, plugin_id))
    if row is None:
        row = PluginConfig(
            project_id=project_id,
            category=category,
            plugin_id=plugin_id,
            enabled=enabled,
            parameters=json.dumps(parameters),
        )
        db.add(row)
    else:
        row.enabled = enabled
        row.parameters = json.dumps(parameters)
    return row


def delete_plugin_configs(db, project_id: str) -> None:
    """Remove every adapter/puller configuration row owned by a project."""
    db.query(PluginConfig).filter(PluginConfig.project_id == project_id).delete(
        synchronize_session=False
    )


def plugin_secret(project_id: str, key: str) -> str | None:
    """Read a project-scoped plugin secret, falling back to the legacy global row."""
    with sessions()() as db:
        row = db.get(ProjectSetting, (project_id, key))
        if row is None and project_id == DEFAULT_PROJECT_ID:
            row = db.get(Setting, key)
        return row.value if row is not None else None


def set_plugin_secret(db, project_id: str, key: str, value: str | None) -> None:
    row = db.get(ProjectSetting, (project_id, key))
    legacy = db.get(Setting, key) if project_id == DEFAULT_PROJECT_ID else None
    if not value:
        if row is not None:
            db.delete(row)
        if legacy is not None:
            db.delete(legacy)
        return
    if row is None:
        db.add(ProjectSetting(project_id=project_id, key=key, value=value))
    else:
        row.value = value
    if project_id == DEFAULT_PROJECT_ID:
        if legacy is None:
            db.add(Setting(key=key, value=value))
        else:
            legacy.value = value


def add_queued_build(
    db,
    build_type: str,
    triggered_by: str,
    correction_proposal_id: str | None = None,
    project_id: str = DEFAULT_PROJECT_ID,
) -> int:
    from .build_queue import add_queued_build as _add_queued_build

    return _add_queued_build(
        db,
        build_type,
        triggered_by,
        correction_proposal_id=correction_proposal_id,
        project_id=project_id,
    )


def enqueue_build(
    build_type: str,
    triggered_by: str,
    *,
    project_id: str = DEFAULT_PROJECT_ID,
    notify: bool = True,
    dedupe: bool = False,
) -> int:
    from .build_queue import enqueue_build as _enqueue_build

    return _enqueue_build(
        build_type,
        triggered_by,
        project_id=project_id,
        notify=notify,
        dedupe=dedupe,
    )


def wake_build_queue() -> None:
    from .build_queue import build_queue

    build_queue.enqueue()


def schedule_interval(project_id: str = DEFAULT_PROJECT_ID) -> int:
    with sessions()() as db:
        if project_id == DEFAULT_PROJECT_ID:
            row = db.get(Setting, SCHEDULE_INTERVAL_KEY)
        else:
            row = db.get(ProjectSetting, (project_id, SCHEDULE_INTERVAL_KEY))
        return int(row.value) if row is not None and row.value else 0


def project_schedule_intervals() -> dict[str, int]:
    """Read every project's interval; a broken project does not hide the others."""
    with sessions()() as db:
        project_ids = [row.id for row in db.query(Project).all()]
    result = {DEFAULT_PROJECT_ID: schedule_interval(DEFAULT_PROJECT_ID)}
    for project_id in project_ids:
        if project_id == DEFAULT_PROJECT_ID:
            continue
        try:
            result[project_id] = schedule_interval(project_id)
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "Could not read schedule for project %s", project_id
            )
            result[project_id] = 0
    return result


def configure_schedule(interval: int, project_id: str = DEFAULT_PROJECT_ID) -> None:
    from .scheduler import scheduler

    scheduler.configure(interval, project_id=project_id)


def start_schedule() -> None:
    from .scheduler import scheduler

    intervals = project_schedule_intervals()
    scheduler.start(intervals.pop(DEFAULT_PROJECT_ID, 0))
    for project_id, interval in intervals.items():
        scheduler.configure(interval, project_id=project_id)
