"""Built-in table-level SQL task parser."""

from typing import ClassVar

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from .plugins import ParseResult


def table_name(table):
    """Return catalog/database/table without the SQL alias."""
    return ".".join(part for part in (table.catalog, table.db, table.name) if part)


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
                            name = table_name(source)
                            sources.update(views.get(name, {name}))
                if isinstance(statement, exp.Create) and statement.find(
                    exp.TemporaryProperty
                ):
                    views[table_name(target)] = sources
                else:
                    reads.update(sources)
                    if isinstance(target, exp.Table):
                        destination = table_name(target)
                        writes.add(destination)
                        edges.update((source, destination) for source in sources)
            from .column_lineage import resolve_columns

            try:
                column_edges = resolve_columns(
                    task.data["sql"], dialect, parameters.get("schema", {})
                )
                column_status, column_reason = "success", None
            except (sqlglot.errors.SqlglotError, ValueError) as exc:
                column_edges, column_status, column_reason = [], "failed", str(exc)
            return ParseResult(
                sorted(reads),
                sorted(writes),
                "success",
                edges=sorted(edges),
                column_edges=column_edges,
                column_status=column_status,
                column_reason=column_reason,
            )
        except (sqlglot.errors.SqlglotError, ValueError) as exc:
            return ParseResult([], [], "failed", str(exc))
