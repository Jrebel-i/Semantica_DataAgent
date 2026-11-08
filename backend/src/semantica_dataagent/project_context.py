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
import os
import re
import shutil
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
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

PROJECTS_ROOT_ENV = "DATAAGENT_PROJECTS_ROOT"
PROJECT_ID_PATTERN = re.compile(r"[a-z][a-z0-9]{0,23}")
GENERATION_PREFIX_PATTERN = re.compile(r"[a-z0-9_]{1,25}")

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
        values = normalize_values(_read_settings(db, resolved))
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


def generation_is_built(project_id: str, generation: str) -> bool:
    """True when the generation has been produced by a successful build."""
    with sessions()() as db:
        row = db.get(KnowledgeGeneration, generation)
        return bool(
            row is not None
            and row.project_id == project_id
            and row.build_id is not None
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


PLUGIN_SECRET_KEYS = frozenset({"puller.dtb.token"})


def plugin_secret(project_id: str, key: str) -> str | None:
    """Read a project-scoped plugin secret, falling back to the legacy global row."""
    with sessions()() as db:
        row = db.get(ProjectSetting, (project_id, key))
        if row is None and project_id == DEFAULT_PROJECT_ID:
            row = db.get(Setting, key)
        return row.value if row is not None else None


def set_plugin_secret(db, project_id: str, key: str, value: str | None) -> None:
    row = db.get(ProjectSetting, (project_id, key))
    if not value:
        if row is not None:
            db.delete(row)
        return
    if row is None:
        db.add(ProjectSetting(project_id=project_id, key=key, value=value))
    else:
        row.value = value


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
