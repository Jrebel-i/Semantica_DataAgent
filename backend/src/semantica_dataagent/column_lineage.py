"""Resolve SQL output projections to their physical source columns."""

import sqlglot
from sqlglot import exp
from sqlglot.lineage import lineage
from sqlglot.optimizer.qualify import qualify

from .sql_tasks import table_name


def resolve_columns(sql, dialect, tables):
    schema = {}
    for name, columns in tables.items():
        node = schema
        parts = name.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = {column: "UNKNOWN" for column in columns}
    edges = set()
    found = False
    for statement in sqlglot.parse(
        sql, read=dialect, error_level=sqlglot.ErrorLevel.RAISE
    ):
        if not isinstance(statement, (exp.Insert, exp.Create)):
            continue
        target = statement.this
        explicit = []
        if isinstance(target, exp.Schema):
            explicit = [column.name for column in target.expressions]
            target = target.this
        if not isinstance(target, exp.Table) or not statement.expression:
            continue
        if isinstance(statement, exp.Create) and statement.find(exp.TemporaryProperty):
            continue
        found = True
        destination = table_name(target)
        query = qualify(
            statement.expression.copy(),
            dialect=dialect,
            schema=schema,
            validate_qualify_columns=True,
        )
        projections = query.selects
        if any(
            p.is_star or p.find(exp.Star) and isinstance(p.this, exp.Column)
            for p in projections
        ):
            raise ValueError("Cannot expand SELECT * without source field metadata")
        partition = target.args.get("partition")
        static = (
            {
                e.this.name.lower()
                for e in partition.expressions
                if isinstance(e, exp.EQ)
            }
            if partition
            else set()
        )
        target_columns = explicit or [
            c for c in tables.get(destination, []) if c.lower() not in static
        ]
        if not target_columns:
            target_columns = [p.alias_or_name for p in projections]
        if len(target_columns) != len(projections) or any(
            not c for c in target_columns
        ):
            raise ValueError(f"Cannot map projections to fields of {destination}")
        for projection, target_column in zip(projections, target_columns, strict=True):
            name = projection.alias_or_name
            if not name:
                raise ValueError("Projection has no name")
            root = lineage(name, query, schema=schema, dialect=dialect)
            for node in root.walk():
                if isinstance(node.expression, exp.Table):
                    source_column = sqlglot.parse_one(
                        node.name, into=exp.Column, read=dialect
                    ).name
                    edges.add(
                        (
                            table_name(node.expression),
                            source_column,
                            destination,
                            target_column,
                        )
                    )
                elif not node.downstream and isinstance(
                    node.expression, exp.Placeholder
                ):
                    raise ValueError(f"Cannot resolve source for {name}")
    if not found:
        raise ValueError("No output SQL statement recognized")
    return sorted(edges)
