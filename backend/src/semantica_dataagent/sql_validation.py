"""SQL validation against a batch snapshot of the current knowledge generation."""

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import get_close_matches
from heapq import heappop, heappush
from typing import Literal

import sqlglot
from sqlalchemy import func, select
from sqlglot import exp
from sqlglot.errors import ErrorLevel, OptimizeError, ParseError
from sqlglot.optimizer.scope import traverse_scope

from . import knowledge, project_context
from .db import Build, sessions
from .table_catalog import resolve_table, table_lookup

# Keys shared above both limits do not identify a relationship.
MAX_KEY_FRACTION = 0.05
MIN_COMMON_KEY_TABLES = 5


@dataclass
class Snapshot:
    tables: list[dict]
    edges: list[dict]
    documents: list[dict]
    document_loader: object = None
    relationships: list[dict] = field(default_factory=list)

    def __post_init__(self):
        self.by_name = {full(t).lower(): t for t in self.tables}
        self._lookup = table_lookup(self.tables)
        self.by_short = defaultdict(list)
        self.fields = {}
        self.keys = defaultdict(set)
        for table in self.tables:
            name = full(table).lower()
            self.by_short[table["name"].lower()].append(table)
            fields = field_names(table)
            self.fields[name] = fields
            for key in fields:
                if key != "id" and key.endswith(("_id", "_key")):
                    self.keys[key].add(name)
        self.keys = {
            k: v
            for k, v in self.keys.items()
            if len(v) <= max(MIN_COMMON_KEY_TABLES, len(self.tables) * MAX_KEY_FRACTION)
        }
        self.neighbors = defaultdict(set)
        self.edge_pairs = defaultdict(list)
        self.relationship_pairs = defaultdict(list)
        for edge in self.edges:
            a, b = edge["upstream"].lower(), edge["downstream"].lower()
            if a in self.by_name and b in self.by_name and a != b:
                self.neighbors[a].add(b)
                self.neighbors[b].add(a)
                self.edge_pairs[frozenset((a, b))].append(edge)
        for rel in self.relationships:
            a = _relationship_table_name(rel, "from").lower()
            b = _relationship_table_name(rel, "to").lower()
            if a in self.by_name and b in self.by_name and a != b:
                self.neighbors[a].add(b)
                self.neighbors[b].add(a)
                self.relationship_pairs[frozenset((a, b))].append(rel)

    def resolution(self, name):
        """Return ``(matches, reason)``; ``reason`` is None only for a full match."""
        name = name.lower()
        parts = name.split(".")
        if len(parts) in (2, 3):
            metadata, reason, candidates = resolve_table(
                exp.to_table(name, dialect="doris"), self._lookup
            )
            if metadata is not None:
                return [metadata], None
            if reason in {"ambiguous", "unqualified"}:
                return candidates, reason
            return [], reason
        short = self.by_short.get(name, [])
        if len(short) == 1:
            return short, None
        return short, "ambiguous" if short else "missing"

    def resolve(self, name):
        matches, reason = self.resolution(name)
        return [] if reason == "unqualified" else matches

    def documents_for(self, pairs):
        if self.document_loader and pairs:
            return self.document_loader(pairs)
        return self.documents


_snapshot_cache = None


def _build_version(project_id):
    with sessions()() as db:
        latest = db.execute(
            select(Build.id, Build.status)
            .where(Build.project_id == project_id)
            .order_by(Build.id.desc())
            .limit(1)
        ).first()
        success = db.scalar(
            select(func.max(Build.id)).where(
                Build.status == "succeeded", Build.project_id == project_id
            )
        )
    return (success, tuple(latest) if latest else None)


def load_snapshot(project_id: str | None = None):
    global _snapshot_cache
    values = (
        knowledge.settings()
        if project_id in (None, project_context.DEFAULT_PROJECT_ID)
        else knowledge.settings(project_id)
    )
    project = project_context.from_values(values)
    version = _build_version(project.project_id)
    key = (
        os.environ["DATAAGENT_DB_PATH"],
        values["neo4j_uri"],
        values["neo4j_user"],
        values["neo4j_password"],
        project.project_id,
        project.generation,
        version,
    )
    if (
        _snapshot_cache
        and _snapshot_cache[0] == key
        and (not version[1] or version[1][1] != "running")
    ):
        return _snapshot_cache[1]
    store = knowledge.read_graph(values)
    params = {"generation": project.generation}
    tables = knowledge.execute(
        store,
        """MATCH (t:SDATable {generation:$generation}) OPTIONAL MATCH (t)-[:HAS_FIELD]->(f:SDAField {generation:$generation}) RETURN properties(t) AS table, collect(properties(f)) AS fields""",
        params,
    )
    edges = knowledge.execute(
        store,
        """MATCH (:SDALineageTable {generation:$generation})-[r:DEPENDS_ON]->(:SDALineageTable {generation:$generation}) RETURN properties(r) AS edge""",
        params,
    )
    relationships = knowledge.execute(
        store,
        """MATCH (:SDATable {generation:$generation})-[r:RELATES_TO]->(:SDATable {generation:$generation}) RETURN properties(r) AS relationship""",
        params,
    )

    def documents(pairs):
        return knowledge.execute(
            store,
            """MATCH (c:SDAChunk {generation:$generation}) WHERE any(pair IN $pairs WHERE toLower(c.content) CONTAINS pair[0] AND toLower(c.content) CONTAINS pair[1]) RETURN c.ref AS ref, c.content AS content""",
            {**params, "pairs": [list(p) for p in pairs]},
        )

    result = Snapshot(
        [{**r["table"], "fields": r["fields"]} for r in tables],
        [r["edge"] for r in edges],
        [],
        documents,
        [r["relationship"] for r in relationships],
    )
    if not version[1] or version[1][1] != "running":
        _snapshot_cache = (key, result)
    return result


def _relationship_table_name(rel, side):
    if f"{side}_database" in rel:
        return ".".join(
            part
            for part in (
                rel.get(f"{side}_catalog"),
                rel[f"{side}_database"],
                rel[f"{side}_table"],
            )
            if part
        )
    return rel[f"{side}_table"]


def _relationship_ref(rel):
    return rel.get("ref") or (
        f"relationship:{_relationship_table_name(rel, 'from')}:"
        f"{_relationship_table_name(rel, 'to')}:{rel['name']}"
    )


def _relationship_evidence(rel):
    return {
        "type": "relationship",
        "strength": "strong",
        "name": rel["name"],
        "ref": _relationship_ref(rel),
        "from_columns": list(rel.get("from_columns") or ()),
        "to_columns": list(rel.get("to_columns") or ()),
    }


def _relationship_required_pairs(rel):
    from_name = _relationship_table_name(rel, "from").lower()
    to_name = _relationship_table_name(rel, "to").lower()
    from_cols = rel.get("from_columns") or ()
    to_cols = rel.get("to_columns") or ()
    if not from_cols or len(from_cols) != len(to_cols):
        return None
    return {
        frozenset(((from_name, fc.lower()), (to_name, tc.lower())))
        for fc, tc in zip(from_cols, to_cols, strict=True)
    }


def _relationship_step_keys(rel, left_name):
    from_name = _relationship_table_name(rel, "from").lower()
    from_cols = rel.get("from_columns") or ()
    to_cols = rel.get("to_columns") or ()
    if from_name == left_name:
        left_cols, right_cols = from_cols, to_cols
    else:
        left_cols, right_cols = to_cols, from_cols
    return [
        lcol if lcol.lower() == rcol.lower() else f"{lcol}={rcol}"
        for lcol, rcol in zip(left_cols, right_cols, strict=True)
    ]


def _join_column_source(col, sources):
    source = sources.get(col.table.lower())
    if source is None:
        return None, None
    if "database" not in source:
        if col.db or col.catalog or source.get("derived_unknown"):
            return None, None
        matches = [f for f in source["fields"] if f["name"].lower() == col.name.lower()]
        if len(matches) != 1:
            return None, None
        return matches[0].get("_physical_table"), matches[0].get("_physical_column")
    if col.db and col.db.lower() != source["database"].lower():
        return None, None
    if (
        col.catalog
        and source.get("catalog")
        and col.catalog.lower() != source["catalog"].lower()
    ):
        return None, None
    return source, col.name


def _qualify_join_column(column, aliases, sources):
    if column.table or any(
        sources.get(alias) is None or sources[alias].get("derived_unknown")
        for alias in aliases
    ):
        return column
    matches = [
        alias for alias in aliases if column.name.lower() in field_names(sources[alias])
    ]
    return exp.column(column.name, table=matches[0]) if len(matches) == 1 else column


def _merged_using_sources(scope, sources):
    """Identify USING columns that denote one output across known inputs."""
    from_clause = scope.expression.args.get("from_")
    left = (
        {from_clause.this.alias_or_name.lower()}
        if from_clause and from_clause.this
        else set()
    )
    merged = {}
    for join in scope.expression.args.get("joins") or []:
        right = join.this.alias_or_name.lower()
        for identifier in join.args.get("using") or []:
            name = identifier.name.lower()
            candidates = [
                alias
                for alias in left
                if sources.get(alias) is not None
                and name in field_names(sources[alias])
            ]
            unknown = any(
                sources.get(alias) is None or sources[alias].get("derived_unknown")
                for alias in left | {right}
            )
            unique_fields = all(
                sum(f["name"].lower() == name for f in sources[alias]["fields"]) == 1
                for alias in candidates
                + ([right] if sources.get(right) is not None else [])
            )
            if (
                not unknown
                and unique_fields
                and candidates
                and name in field_names(sources[right])
                and (len(candidates) == 1 or set(candidates) == merged.get(name))
            ):
                merged[name] = set(candidates) | {right}
            else:
                merged.pop(name, None)
        left.add(right)
    return merged


def full(t):
    return ".".join(
        part for part in (t.get("catalog"), t.get("database"), t.get("name")) if part
    )


def field_names(t):
    return {f["name"].lower(): f for f in t["fields"]}


def evidence(a, b, left_key=None, right_key=None, snapshot=None, documents=()):
    aname, bname = full(a).lower(), full(b).lower()
    found = []
    pair_key = frozenset((aname, bname))
    for edge in snapshot.edge_pairs.get(pair_key, []):
        found.append(
            {
                "type": "lineage",
                "strength": "strong",
                "ref": edge.get("ref")
                or f"lineage:{edge['upstream']}:{edge['downstream']}",
                "parse_status": edge.get("parse_status"),
                "evidence": edge.get("evidence"),
            }
        )
    if left_key is None and right_key is None:
        for rel in snapshot.relationship_pairs.get(pair_key, ()):
            found.append(_relationship_evidence(rel))
    common = (
        set(snapshot.fields[aname]) & set(snapshot.fields[bname]) & snapshot.keys.keys()
    )
    if left_key is not None:
        common &= (
            {left_key.lower()}
            if left_key.lower() == (right_key or "").lower()
            else set()
        )
    for key in sorted(common):
        found.append(
            {
                "type": "common_key",
                "strength": "weak",
                "key": key,
                "refs": [
                    snapshot.fields[aname][key].get("ref", a["ref"]),
                    snapshot.fields[bname][key].get("ref", b["ref"]),
                ],
            }
        )
    if left_key and right_key:
        left = re.escape(f"{aname}.{left_key}".lower())
        right = re.escape(f"{bname}.{right_key}".lower())
        for doc in documents:
            if re.search(
                rf"(?<![\w.])(?:{left}\s*=\s*{right}|{right}\s*=\s*{left})(?![\w.])",
                doc["content"].lower(),
            ):
                found.append(
                    {"type": "document", "strength": "strong", "ref": doc["ref"]}
                )
    return found


def check_sql(sql, dialect, snapshot, partition_tables=None):
    if dialect not in ("hive", "spark", "doris"):
        raise ValueError("Unsupported dialect")
    result = {
        "status": "issues",
        "syntax": {"status": "pass"},
        "tables": [],
        "fields": [],
        "joins": [],
        "issues": [],
    }
    try:
        statements = sqlglot.parse(sql, read=dialect, error_level=ErrorLevel.RAISE)
        if len(statements) != 1 or statements[0] is None:
            raise ValueError("Expected one SQL statement")
        tree = statements[0]
    except (ParseError, ValueError) as exc:
        details = exc.errors[0] if isinstance(exc, ParseError) and exc.errors else {}
        result["syntax"] = {
            "status": "error",
            "message": str(exc),
            "line": details.get("line"),
            "column": details.get("col"),
        }
        result["issues"].append(
            {
                "kind": "syntax",
                "message": str(exc),
                "suggestion": "修正 SQL 语法后重新调用 validate_sql。",
            }
        )
        return result
    resolved = {}
    for table in tree.find_all(exp.Table):
        # CTE references are represented as tables but are resolved by scopes below.
        if any(
            table is s
            for scope in traverse_scope(tree)
            for s in scope.sources.values()
            if isinstance(s, exp.Table)
        ):
            name = ".".join(x for x in (table.catalog, table.db, table.name) if x)
            matches, reason = snapshot.resolution(name)
            item = {
                "name": name,
                "status": "pass"
                if reason is None
                else "ambiguous"
                if reason == "ambiguous"
                else "error",
            }
            if reason is None:
                resolved[id(table)] = matches[0]
                item["ref"] = matches[0]["ref"]
            else:
                item["suggestions"] = (
                    [full(t) for t in matches]
                    if matches
                    else get_close_matches(
                        name.lower(),
                        [full(t) for t in snapshot.tables],
                        n=3,
                        cutoff=0.5,
                    )
                )
            result["tables"].append(item)
            if item["status"] != "pass":
                result["issues"].append(
                    {
                        "kind": "table",
                        "table": name,
                        "message": (
                            f"表名不唯一: {name}"
                            if item["status"] == "ambiguous"
                            else f"表缺少目录限定: {name}"
                            if reason == "unqualified"
                            else f"表不存在: {name}"
                        ),
                        "suggestion": (
                            "使用完整表名: " + ", ".join(item["suggestions"])
                            if item["suggestions"]
                            else "使用 describe_tables 确认表名。"
                        ),
                    }
                )
    pending_documents = []
    scope_outputs = {}
    invalid_scope = False
    for scope in traverse_scope(tree):
        sources = {}
        try:
            selected_sources = scope.selected_sources
        except OptimizeError as exc:
            invalid_scope = True
            result["issues"].append(
                {
                    "kind": "table",
                    "message": str(exc),
                    "suggestion": "为每个输入使用不同的表别名。",
                }
            )
            scope_outputs[id(scope)] = {"fields": [], "derived_unknown": True}
            continue
        for alias, (_, source) in selected_sources.items():
            if isinstance(source, exp.Table):
                sources[alias.lower()] = resolved.get(id(source))
            else:
                sources[alias.lower()] = scope_outputs.get(
                    id(source), {"fields": [], "derived_unknown": True}
                )
        merged_columns = _merged_using_sources(scope, sources)
        output_fields = []
        output_unknown = False
        for projection in scope.expression.selects:
            if isinstance(projection, exp.Star):
                expanded = list(sources.items())
            elif isinstance(projection, exp.Column) and projection.is_star:
                alias = projection.table.lower()
                expanded = [(alias, sources.get(alias))]
            else:
                if projection.alias_or_name:
                    output_fields.append(
                        {"name": projection.alias_or_name, "_explicit_projection": True}
                    )
                continue
            seen_merged = set()
            for alias, source in expanded:
                if source is None or source.get("derived_unknown"):
                    output_unknown = True
                if source is not None:
                    for field in source["fields"]:
                        name = field["name"].lower()
                        if isinstance(
                            projection, exp.Star
                        ) and alias in merged_columns.get(name, set()):
                            if name in seen_merged:
                                continue
                            seen_merged.add(name)
                        if "database" in source:
                            field = {
                                **field,
                                "_physical_table": source,
                                "_physical_column": field["name"],
                            }
                        if isinstance(
                            projection, exp.Star
                        ) and alias in merged_columns.get(name, set()):
                            field = {
                                k: v
                                for k, v in field.items()
                                if k not in ("_physical_table", "_physical_column")
                            }
                        output_fields.append(field)
        if output_unknown:
            # A propagated star cannot guarantee uniqueness of its input's names.
            explicit_names = {
                projection.alias_or_name.lower()
                for projection in scope.expression.selects
                if not isinstance(projection, exp.Star)
                and not (isinstance(projection, exp.Column) and projection.is_star)
                and projection.alias_or_name
            }
            output_fields = [
                {
                    **field,
                    "_explicit_projection": field["name"].lower() in explicit_names
                    and field.get("_explicit_projection", False),
                }
                for field in output_fields
            ]
        if scope.union_scopes:
            # Set operations expose the names of the first input.
            first = scope_outputs.get(
                id(scope.union_scopes[0]), {"fields": [], "derived_unknown": True}
            )
            scope_outputs[id(scope)] = {
                "fields": [
                    {
                        k: v
                        for k, v in f.items()
                        if k not in ("_physical_table", "_physical_column")
                    }
                    for f in first["fields"]
                ],
                "derived_unknown": first["derived_unknown"],
            }
        else:
            scope_outputs[id(scope)] = {
                "fields": output_fields,
                "derived_unknown": output_unknown,
            }
        if scope.outer_columns:
            output = scope_outputs[id(scope)]
            names = scope.outer_columns
            fields = output["fields"]
            scope_outputs[id(scope)] = {
                "fields": [
                    {
                        **{
                            k: v
                            for k, v in field.items()
                            if k not in ("_physical_table", "_physical_column")
                        },
                        "name": names[index] if index < len(names) else field["name"],
                    }
                    for index, field in enumerate(fields)
                ]
                + [{"name": name} for name in names[len(fields) :]],
                "derived_unknown": output["derived_unknown"],
            }
        for column in scope.columns:
            if column.is_star:
                continue
            candidates = (
                [sources.get(column.table.lower())]
                if column.table
                else list(sources.values())
            )
            known = [s for s in candidates if s is not None]
            matching = [
                field
                for source in known
                for field in source["fields"]
                if field["name"].lower() == column.name.lower()
            ]
            uncertain = len(known) != len(candidates) or any(
                s.get("derived_unknown") for s in known
            )
            matching_count = len(matching)
            if not column.table:
                merged = merged_columns.get(column.name.lower(), set())
                matching_count -= max(len(merged) - 1, 0)
            # An unknown star in another source can still introduce a collision.
            explicit_certain = len(candidates) == 1 and len(known) == 1
            explicit_count = sum(
                bool(field.get("_explicit_projection")) for field in matching
            )
            status = (
                "ambiguous"
                if matching_count > 1
                else "pass"
                if matching_count == 1
                and (not uncertain or (explicit_certain and explicit_count == 1))
                else "unknown"
                if uncertain
                else "error"
            )
            item = {"name": column.sql(dialect=dialect), "status": status}
            if column.table:
                item["table"] = column.table
            if status == "error":
                item["suggestions"] = get_close_matches(
                    column.name.lower(),
                    sorted({n for s in known for n in field_names(s)}),
                    n=3,
                    cutoff=0.5,
                )
            result["fields"].append(item)
            if status != "pass":
                suggestions = item.get("suggestions") or []
                result["issues"].append(
                    {
                        "kind": "field",
                        "table": column.table or None,
                        "field": column.name,
                        "condition": column.sql(dialect=dialect),
                        "message": (
                            f"字段不唯一: {item['name']}"
                            if status == "ambiguous"
                            else f"无法确认字段: {item['name']}"
                            if status == "unknown"
                            else f"字段不存在: {item['name']}"
                        ),
                        "suggestion": (
                            "改用已确认字段: " + ", ".join(suggestions)
                            if suggestions
                            else "使用 describe_tables 确认字段名。"
                        ),
                    }
                )
        left_aliases = set()
        from_clause = scope.expression.args.get("from_")
        if from_clause and from_clause.this:
            left_aliases.add(from_clause.this.alias_or_name.lower())
        merged_using = {}
        for join in scope.expression.args.get("joins") or []:
            condition = join.args.get("on")
            right_alias = join.this.alias_or_name.lower()
            pairs = []
            if condition and not condition.find(exp.Or):
                for eq in condition.find_all(exp.EQ):
                    if eq.find_ancestor(exp.Not) is not None:
                        continue
                    l, r = eq.left, eq.right
                    if isinstance(l, exp.Column) and isinstance(r, exp.Column):
                        aliases = left_aliases | {right_alias}
                        l = _qualify_join_column(l, aliases, sources)
                        r = _qualify_join_column(r, aliases, sources)
                        if (
                            l.table.lower() == right_alias
                            and r.table.lower() in left_aliases
                        ):
                            l, r = r, l
                        if (
                            l.table.lower() in left_aliases
                            and r.table.lower() == right_alias
                        ):
                            pairs.append((l, r, len(pairs)))
            for identifier in join.args.get("using") or []:
                name = identifier.name
                matches = [
                    alias
                    for alias in left_aliases
                    if sources.get(alias) is not None
                    and name.lower() in field_names(sources[alias])
                ]
                uncertain = any(
                    sources.get(alias) is None or sources[alias].get("derived_unknown")
                    for alias in left_aliases
                )
                right = sources.get(right_alias)
                if (
                    (
                        len(matches) == 1
                        or set(matches) == merged_using.get(name.lower())
                    )
                    and bool(matches)
                    and not uncertain
                    and right is not None
                    and not right.get("derived_unknown")
                    and all(
                        sum(
                            f["name"].lower() == name.lower()
                            for f in sources[alias]["fields"]
                        )
                        == 1
                        for alias in matches + [right_alias]
                    )
                    and name.lower() in field_names(right)
                ):
                    group = len(pairs)
                    pairs.extend(
                        (
                            exp.column(name, table=alias),
                            exp.column(name, table=right_alias),
                            group,
                        )
                        for alias in matches
                    )
                    merged_using[name.lower()] = set(matches) | {right_alias}
                else:
                    pairs.append(
                        (None, exp.column(name, table=right_alias), len(pairs))
                    )
                    merged_using.pop(name.lower(), None)
            pair_records = []
            observed_pairs = defaultdict(set)
            for l, r, group in pairs:
                a, left_key = (
                    _join_column_source(l, sources) if l is not None else (None, None)
                )
                b, right_key = _join_column_source(r, sources)
                label = f"{l.sql(dialect=dialect) if l is not None else 'USING'} = {r.sql(dialect=dialect)}"
                proofs = []
                pair = None
                if a and b and left_key and right_key and a is not b:
                    aname, bname = full(a).lower(), full(b).lower()
                    pair_key = frozenset((aname, bname))
                    pair = frozenset(
                        ((aname, left_key.lower()), (bname, right_key.lower()))
                    )
                    observed_pairs[pair_key].add(pair)
                    proofs = evidence(a, b, left_key, right_key, snapshot)
                    pending_documents.append(
                        (pair_records, len(pair_records), a, b, left_key, right_key)
                    )
                pair_records.append(
                    {"label": label, "proofs": proofs, "pair": pair, "group": group}
                )
            for pair_key, observed in observed_pairs.items():
                for rel in snapshot.relationship_pairs.get(pair_key, ()):
                    required = _relationship_required_pairs(rel)
                    if required is not None and required <= observed:
                        for record in pair_records:
                            if record["pair"] in required:
                                record["proofs"].append(_relationship_evidence(rel))
            result["joins"].append(
                {
                    "condition": condition.sql(dialect=dialect)
                    if condition
                    else join.sql(dialect=dialect),
                    "pairs": pair_records,
                }
            )
            left_aliases.add(right_alias)
    candidates = [
        item
        for item in pending_documents
        if not any(
            proof["strength"] == "strong"
            for record in item[0]
            if record["group"] == item[0][item[1]]["group"]
            for proof in record["proofs"]
        )
    ]
    documents = snapshot.documents_for(
        {(full(a).lower(), full(b).lower()) for _, _, a, b, _, _ in candidates}
    )
    for records, index, a, b, l, r in candidates:
        records[index]["proofs"].extend(
            p
            for p in evidence(a, b, l, r, snapshot, documents)
            if p["type"] == "document"
        )
    for join_item in result["joins"]:
        records = join_item.pop("pairs")
        supports = [proof for record in records for proof in record["proofs"]]
        groups = defaultdict(list)
        for record in records:
            groups[record["group"]].append(record)
        missing = [
            alternatives[0]["label"]
            for alternatives in groups.values()
            if not any(
                proof["strength"] == "strong"
                for record in alternatives
                for proof in record["proofs"]
            )
        ]
        supported = bool(records) and not missing
        join_item.update(
            {
                "status": "supported"
                if supported
                else "weak"
                if supports
                else "unsupported",
                "evidence": supports,
                "suggestion": None
                if supported
                else "Weak or missing evidence; use find_join_path and verify the join.",
            }
        )
        if not supported:
            result["issues"].append(
                {
                    "kind": "join",
                    "table": None,
                    "condition": join_item["condition"],
                    "message": ("JOIN 缺少证据: " + ", ".join(missing))
                    if missing
                    else "JOIN 缺少证据",
                    "suggestion": join_item["suggestion"],
                }
            )
    if partition_tables is not None and not invalid_scope:
        from .query_api import _partition_issues

        _, _, problems = _partition_issues(tree, partition_tables)
        result["issues"].extend(issue for _, issue in problems)
    if (
        not result["issues"]
        and all(x["status"] == "pass" for x in result["tables"] + result["fields"])
        and all(x["status"] == "supported" for x in result["joins"])
    ):
        result["status"] = "pass"
    return result


def join_path(tables, snapshot=None):
    if not 2 <= len(tables) <= 4:
        raise ValueError("Provide two to four tables")
    if snapshot is None:
        snapshot = load_snapshot()
    matches = [snapshot.resolve(name) for name in tables]
    if any(len(x) != 1 for x in matches):
        return {"status": "no_path", "reason": "Table missing or ambiguous"}
    targets = [full(x[0]).lower() for x in matches]
    connected = {targets[0]}
    steps = []
    for target in targets[1:]:
        queue = [(0, 0, name, []) for name in sorted(connected)]
        best = {}
        path = None
        while queue:
            weak_count, distance, current, route = heappop(queue)
            if current in best and best[current] <= (weak_count, distance):
                continue
            best[current] = (weak_count, distance)
            if current == target:
                path = route
                break
            neighbors = set(snapshot.neighbors[current])
            for key in snapshot.fields[current]:
                neighbors.update(snapshot.keys.get(key, ()))
            neighbors.discard(current)
            for neighbor in sorted(neighbors):
                pair = frozenset((current, neighbor))
                strong = bool(
                    snapshot.edge_pairs.get(pair)
                    or snapshot.relationship_pairs.get(pair)
                )
                cost = (weak_count + (not strong), distance + 1)
                if neighbor not in best or cost < best[neighbor]:
                    heappush(queue, (*cost, neighbor, route + [(current, neighbor)]))
        if path is None:
            return {"status": "no_path", "reason": f"No connection to {target}"}
        steps.extend(path)
        connected.update(b for _, b in path)
    # Documents can strengthen a selected path but do not introduce graph edges.
    weak_steps = {
        (a, b)
        for a, b in steps
        if not (
            snapshot.edge_pairs.get(frozenset((a, b)))
            or snapshot.relationship_pairs.get(frozenset((a, b)))
        )
    }
    documents = snapshot.documents_for(weak_steps)
    result = []
    for a, b in steps:
        left, right = snapshot.by_name[a], snapshot.by_name[b]
        proofs = evidence(left, right, snapshot=snapshot)
        for key in (
            (set(snapshot.fields[a]) & set(snapshot.fields[b]))
            if (a, b) in weak_steps and documents
            else ()
        ):
            proofs.extend(
                p
                for p in evidence(left, right, key, key, snapshot, documents)
                if p["type"] == "document"
            )
        strength = (
            "strong" if any(p["strength"] == "strong" for p in proofs) else "weak"
        )
        rel_keys = [
            key
            for rel in snapshot.relationship_pairs.get(frozenset((a, b)), ())
            for key in _relationship_step_keys(rel, a)
        ]
        common_keys = [p["key"] for p in proofs if p["type"] == "common_key"]
        keys = []
        seen_keys = set()
        for key in [*rel_keys, *common_keys]:
            lowered = key.lower()
            if lowered not in seen_keys:
                seen_keys.add(lowered)
                keys.append(key)
        result.append(
            {
                "from": full(left),
                "to": full(right),
                "keys": keys,
                "strength": strength,
                "evidence": proofs,
            }
        )
    return {
        "status": "found",
        "strength": "weak"
        if any(s["strength"] == "weak" for s in result)
        else "strong",
        "steps": result,
    }


def validate_sql(
    sql: str,
    dialect: Literal["hive", "spark", "doris"] = "hive",
    project_id: str | None = None,
) -> dict:
    """Validate SQL against tables, fields, joins and partition rules.

    Returns the existing status/table/field/join shape plus an ``issues`` list.
    Each issue names the problem and the fix: ``kind`` is one of syntax, table,
    field, join, partition or partition_metadata, with ``table``, ``field`` or
    ``condition`` where applicable, plus ``message`` and ``suggestion``.
    """
    snapshot = (
        load_snapshot()
        if project_id in (None, project_context.DEFAULT_PROJECT_ID)
        else load_snapshot(project_id)
    )
    return check_sql(sql, dialect, snapshot, partition_tables=snapshot.tables)


def find_join_path(tables: list[str], project_id: str | None = None) -> dict:
    if not 2 <= len(tables) <= 4:
        raise ValueError("Provide two to four tables")
    snapshot = (
        load_snapshot()
        if project_id in (None, project_context.DEFAULT_PROJECT_ID)
        else load_snapshot(project_id)
    )
    return join_path(tables, snapshot)
