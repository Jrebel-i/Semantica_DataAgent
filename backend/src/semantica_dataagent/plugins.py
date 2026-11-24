"""Entry point contracts and persisted plugin configuration."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from importlib.metadata import entry_points
from typing import Any, ClassVar, Literal, Protocol

from jsonschema import Draft202012Validator

from . import project_context
from .canonical import Record

GROUPS = {
    "source": "semantica_dataagent.source_adapters",
    "parser": "semantica_dataagent.task_parsers",
    "executor": "semantica_dataagent.executors",
    "puller": "semantica_dataagent.pullers",
    "trace_source": "semantica_dataagent.trace_sources",
}

# Agent runners are plugins too, but they are not persisted or enabled per
# build like source/parser plugins, so they have their own discovery path.
AGENT_RUNNER_GROUP = "semantica_dataagent.agent_runners"


@dataclass(frozen=True)
class ParseResult:
    reads: list[str]
    writes: list[str]
    status: Literal["success", "failed", "unsupported"]
    reason: str | None = None
    edges: list[tuple[str, str]] | None = None
    column_edges: list[tuple[str, str, str, str]] | None = None
    column_status: str = "unsupported"
    column_reason: str | None = None


@dataclass(frozen=True)
class PullItemFailure:
    kind: str
    target: str
    reason: str


@dataclass(frozen=True)
class PullArtifact:
    ossie_models: dict[str, bytes]
    tasks: list[dict[str, Any]] = field(default_factory=list)
    dimension_values: list[dict[str, Any]] = field(default_factory=list)
    table_count: int = 0
    task_count: int = 0
    stats: dict[str, int] = field(default_factory=dict)
    failures: list[PullItemFailure] = field(default_factory=list)


@dataclass(frozen=True)
class PullContext:
    project_id: str
    instance_id: str
    parameters: dict[str, Any]
    secrets: dict[str, str] = field(default_factory=dict)
    catalog_prefix: str | None = None
    task_namespace: str | None = None

    @property
    def secret(self) -> str | None:
        if "token" in self.secrets:
            return self.secrets["token"]
        if len(self.secrets) == 1:
            return next(iter(self.secrets.values()))
        return None


class PullerPlugin(Protocol):
    id: str
    version: str
    display_name: str
    parameters_schema: ClassVar[dict]

    def pull(self, context: PullContext) -> PullArtifact: ...


@dataclass(frozen=True)
class TraceMessage:
    role: Literal["user", "assistant"]
    content: str
    timestamp: datetime | None = None


@dataclass(frozen=True)
class TraceConversation:
    trace_id: str
    project_id: str
    messages: list[TraceMessage] = field(default_factory=list)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    session_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TraceQuery:
    project_id: str
    parameters: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)
    since: datetime | None = None
    until: datetime | None = None
    limit: int = 200


class TraceSourcePlugin(Protocol):
    id: str
    version: str
    display_name: str
    parameters_schema: ClassVar[dict]

    def fetch_traces(self, query: TraceQuery) -> list[TraceConversation]: ...


def secret_parameter_keys(schema: Mapping[str, Any]) -> set[str]:
    """Return top-level parameter names declared as password/secret credentials."""
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return set()
    return {
        str(key)
        for key, prop in properties.items()
        if isinstance(prop, Mapping)
        and (prop.get("format") == "password" or prop.get("x-secret") is True)
    }


class ExecutorError(RuntimeError):
    """The configured execution platform failed."""


class ExecutorAuthenticationError(ExecutorError):
    """The caller credential was rejected."""


class QueryExecutor(Protocol):
    async def submit(self, sql: str, limit: int) -> str: ...

    async def poll(self, key: str) -> dict: ...

    async def wait(self, key: str) -> dict: ...

    async def download(self, key: str, format: str) -> tuple[bytes, str | None]: ...

    async def estimate(self, sql: str) -> dict: ...


class ExecutorPlugin(Protocol):
    def bind(self, parameters: dict, token: str) -> QueryExecutor: ...


class TaskParser(Protocol):
    task_types: list[str]

    def parse(self, task: Record, parameters: dict) -> ParseResult: ...


def discover():
    found = {}
    for category, group in GROUPS.items():
        for ep in entry_points(group=group):
            plugin = ep.load()()
            key = (category, plugin.id)
            if key in found:
                raise ValueError(f"Duplicate plugin: {category}/{plugin.id}")
            Draft202012Validator.check_schema(plugin.parameters_schema)
            found[key] = plugin
    return found


def configured(project_id=None, project_type=None):
    """Return plugins for one project, excluding incompatible source/executor plugins."""
    from .project_types import supports_project_type

    plugins = discover()
    project_type = project_type or project_context.project_type(project_id)
    saved = project_context.plugin_configs(
        project_id or project_context.DEFAULT_PROJECT_ID
    )
    result = []
    for (category, _), plugin in plugins.items():
        if category == "trace_source":
            continue
        if project_id not in (None, project_context.DEFAULT_PROJECT_ID) and (
            category not in {"source", "parser", "executor"}
        ):
            continue
        if category == "executor" and project_type == "document":
            continue
        if category == "source" and not supports_project_type(plugin, project_type):
            continue
        enabled, parameters = saved.get(
            (category, plugin.id),
            (
                (
                    category == "source"
                    and plugin.id in {"canonical", "markdown", "ossie"}
                )
                or (category == "parser" and plugin.id == "sql"),
                {},
            ),
        )
        result.append((category, plugin, enabled, parameters))
    return result


def parse_task(task, parameters_by_id=None, *, schema=None):
    parameters_by_id = parameters_by_id or {}
    for category, plugin, enabled, parameters in configured():
        if (
            category == "parser"
            and enabled
            and task.data.get("type") in plugin.task_types
        ):
            options = parameters_by_id.get(plugin.id, parameters)
            if schema is not None and plugin.id in {"sql", "dtb"}:
                options = {**options, "schema": schema}
            return plugin.parse(task, options)
    return ParseResult([], [], "unsupported", "No matching parser")
