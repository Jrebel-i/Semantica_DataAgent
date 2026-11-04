"""Built-in table-level SQL task parser."""

from typing import ClassVar

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from .plugins import ParseResult


class SQLTaskParser:
    id = "sql"
    version = "1.0.0"
    display_name = "SQL"
    parameters_schema: ClassVar[dict] = {
        "type": "object",
        "additionalProperties": False,
    }
    task_types: ClassVar[list[str]] = ["sql", "hive", "spark", "doris"]

    def parse(self, task, parameters):
        dialect = task.data.get("dialect", task.data.get("type", "hive"))
        if dialect == "sql":
            dialect = "hive"
        views = {}
        reads, writes, edges = set(), set(), set()
        try:
            statements = sqlglot.parse(
                task.data["sql"], read=dialect, error_level=sqlglot.ErrorLevel.RAISE
            )
            for statement in statements:
                if statement is None:
                    continue
                if isinstance(statement, exp.Command):
                    raise ValueError("Unsupported SQL statement")  # noqa: TRY004
                target = (
                    statement.this
                    if isinstance(statement, (exp.Insert, exp.Create))
                    else None
                )
                query = statement.expression if target is not None else statement
                sources = set()
                for scope in traverse_scope(query):
                    for source in scope.sources.values():
                        if isinstance(source, exp.Table):
                            name = source.sql(dialect=dialect, identify=False)
                            sources.update(views.get(name, {name}))
                if isinstance(statement, exp.Create) and statement.find(
                    exp.TemporaryProperty
                ):
                    views[target.sql(dialect=dialect)] = sources
                else:
                    reads.update(sources)
                    if isinstance(target, exp.Table):
                        destination = ".".join(part.name for part in target.parts)
                        writes.add(destination)
                        edges.update((source, destination) for source in sources)
            return ParseResult(
                sorted(reads), sorted(writes), "success", edges=sorted(edges)
            )
        except (sqlglot.errors.SqlglotError, ValueError) as exc:
            return ParseResult([], [], "failed", str(exc))
