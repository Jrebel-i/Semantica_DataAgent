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
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

from .db import GenerationPointer, PluginConfig, Project, Setting, sessions

DEFAULT_PROJECT_ID = "default"
DEFAULT_PROJECT_NAME = "业务数据"
DEFAULT_PROJECT_TYPE = "warehouse"
DEFAULT_PROJECT_STATUS = "active"

GENERATION_KEY = "knowledge_generation"
SOURCE_DIRECTORY_KEY = "source_directory"
GENERATION_RETENTION_KEY = "generation_retention"
SCHEDULE_INTERVAL_KEY = "schedule_interval_seconds"
DIMENSION_VALUE_LIMIT_KEY = "dimension_value_limit"

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


def _read_settings(db) -> dict[str, str]:
    values = {row.key: row.value for row in db.query(Setting).all()}
    pointer = db.get(GenerationPointer, 1)
    if pointer:
        values[GENERATION_KEY] = pointer.generation
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


def _identity(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    for attribute in ("project_id", "id"):
        found = getattr(value, attribute, None)
        if isinstance(found, str):
            return found
    return None


@lru_cache(maxsize=256)
def _resolve_project_id(
    project_id: str | None,
    request_id: str | None,
    token_id: str | None,
    conversation_id: str | None,
) -> str:
    """The #173 resolution rule: every context maps to the default project."""
    return DEFAULT_PROJECT_ID


def resolve_project_id(
    project_id: str | None = None,
    *,
    request: Any = None,
    token: Any = None,
    conversation: Any = None,
) -> str:
    """Resolve an explicit id or a request/token/conversation context.

    The resolution result is cached because callers may invoke it on every
    retrieval.  Project-level values are still loaded from the settings table
    once per operation, exactly as before this refactor.
    """
    return _resolve_project_id(
        project_id,
        _identity(request),
        _identity(token),
        _identity(conversation),
    )


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
        return STORAGE_PREFIX

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
        return plugin_configs()

    def plugin_config(self, category: str, plugin_id: str) -> tuple[bool, dict] | None:
        return plugin_config(category, plugin_id)

    def upsert_plugin_config(
        self,
        db,
        category: str,
        plugin_id: str,
        enabled: bool,
        parameters: Mapping[str, Any],
    ):
        return upsert_plugin_config(db, category, plugin_id, enabled, parameters)

    def enqueue_build(self, build_type: str, triggered_by: str, *, notify: bool = True):
        return enqueue_build(build_type, triggered_by, notify=notify)

    def add_queued_build(
        self,
        db,
        build_type: str,
        triggered_by: str,
        correction_proposal_id: str | None = None,
    ) -> int:
        return add_queued_build(
            db, build_type, triggered_by, correction_proposal_id=correction_proposal_id
        )

    def wake_build_queue(self) -> None:
        wake_build_queue()

    def configure_schedule(self, interval: int) -> None:
        configure_schedule(interval)

    def start_schedule(self) -> None:
        from .scheduler import scheduler

        scheduler.start(self.schedule_interval_seconds)

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
    """Load the resolved project's values and metadata.

    The resolution rule is still the #173 rule (always the default project).
    Display metadata now comes from the projects table while the project-scoped
    configuration keeps using the existing settings rows.
    """
    resolved = resolve_project_id(
        project_id, request=request, token=token, conversation=conversation
    )
    with sessions()() as db:
        project = db.get(Project, resolved)
        values = normalize_values(_read_settings(db))
    if project is None:
        return ProjectContext(resolved, values)
    return ProjectContext(resolved, values, project.name, project.type, project.status)


def from_values(
    values: Mapping[str, Any],
    project_id: str = DEFAULT_PROJECT_ID,
    *,
    name: str = DEFAULT_PROJECT_NAME,
    project_type: str = DEFAULT_PROJECT_TYPE,
    status: str = DEFAULT_PROJECT_STATUS,
) -> ProjectContext:
    return ProjectContext.from_values(
        values,
        project_id,
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


def source_directory_value() -> str | None:
    with sessions()() as db:
        row = db.get(Setting, SOURCE_DIRECTORY_KEY)
        return row.value if row is not None and row.value else None


def plugin_configs() -> dict[tuple[str, str], tuple[bool, dict]]:
    with sessions()() as db:
        return {
            (row.category, row.plugin_id): (row.enabled, json.loads(row.parameters))
            for row in db.query(PluginConfig).all()
        }


def plugin_config(category: str, plugin_id: str) -> tuple[bool, dict] | None:
    with sessions()() as db:
        row = db.get(PluginConfig, (category, plugin_id))
        if row is None:
            return None
        return row.enabled, json.loads(row.parameters)


def upsert_plugin_config(
    db,
    category: str,
    plugin_id: str,
    enabled: bool,
    parameters: Mapping[str, Any],
):
    row = db.get(PluginConfig, (category, plugin_id))
    if row is None:
        row = PluginConfig(
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


def add_queued_build(
    db,
    build_type: str,
    triggered_by: str,
    correction_proposal_id: str | None = None,
) -> int:
    from .build_queue import add_queued_build as _add_queued_build

    return _add_queued_build(
        db, build_type, triggered_by, correction_proposal_id=correction_proposal_id
    )


def enqueue_build(build_type: str, triggered_by: str, *, notify: bool = True) -> int:
    from .build_queue import enqueue_build as _enqueue_build

    return _enqueue_build(build_type, triggered_by, notify=notify)


def wake_build_queue() -> None:
    from .build_queue import build_queue

    build_queue.enqueue()


def configure_schedule(interval: int) -> None:
    from .scheduler import scheduler

    scheduler.configure(interval)


def start_schedule() -> None:
    from .scheduler import scheduler

    scheduler.start(current_project().schedule_interval_seconds)
