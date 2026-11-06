"""Entry point contracts and persisted plugin configuration."""

import json
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Literal, Protocol

from jsonschema import Draft202012Validator

from .canonical import Record
from .db import PluginConfig, sessions

GROUPS = {
    "source": "semantica_dataagent.source_adapters",
    "parser": "semantica_dataagent.task_parsers",
    "executor": "semantica_dataagent.executors",
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


def configured():
    plugins = discover()
    with sessions()() as db:
        saved = {
            (r.category, r.plugin_id): (r.enabled, json.loads(r.parameters))
            for r in db.query(PluginConfig).all()
        }
    return [
        (
            category,
            plugin,
            *saved.get(
                (category, plugin.id),
                (
                    (category == "source" and plugin.id in {"canonical", "markdown"})
                    or (category == "parser" and plugin.id == "sql"),
                    {},
                ),
            ),
        )
        for (category, _), plugin in plugins.items()
    ]


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
