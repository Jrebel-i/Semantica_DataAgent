"""SQL validation against a batch snapshot of the current knowledge generation."""

import os
import re
from collections import defaultdict
from dataclasses import dataclass
from difflib import get_close_matches
from heapq import heappop, heappush
from typing import Literal

import sqlglot
from sqlalchemy import func, select
from sqlglot import exp
from sqlglot.errors import ErrorLevel, ParseError
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
        for edge in self.edges:
            a, b = edge["upstream"].lower(), edge["downstream"].lower()
            if a in self.by_name and b in self.by_name and a != b:
                self.neighbors[a].add(b)
                self.neighbors[b].add(a)
                self.edge_pairs[frozenset((a, b))].append(edge)

    def resolve(self, name):
        name = name.lower()
        parts = name.split(".")
        if len(parts) in (2, 3):
            metadata, reason, candidates = resolve_table(
                exp.to_table(name, dialect="doris"), self._lookup
            )
            if metadata is not None:
                return [metadata]
            return candidates if reason == "ambiguous" else []
        return self.by_short.get(name, [])

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
    )
    if not version[1] or version[1][1] != "running":
        _snapshot_cache = (key, result)
    return result


def full(t):
    return ".".join(
        part for part in (t.get("catalog"), t.get("database"), t.get("name")) if part
    )


def field_names(t):
    return {f["name"].lower(): f for f in t["fields"]}


def evidence(a, b, left_key=None, right_key=None, snapshot=None, documents=()):
    aname, bname = full(a).lower(), full(b).lower()
    found = []
    for edge in snapshot.edge_pairs.get(frozenset((aname, bname)), []):
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
            matches = snapshot.resolve(name)
            item = {
                "name": name,
                "status": "pass"
                if len(matches) == 1
                else "ambiguous"
                if matches
                else "error",
            }
            if len(matches) == 1:
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
    for scope in traverse_scope(tree):
        sources = {}
        for alias, source in scope.sources.items():
            if isinstance(source, exp.Table):
                sources[alias.lower()] = resolved.get(id(source))
            else:
                # A derived source has known names only for explicit projections.
                names = source.expression.named_selects
                sources[alias.lower()] = {
                    "fields": [{"name": n} for n in names if n != "*"],
                    "derived_unknown": "*" in names,
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
            matching = [s for s in known if column.name.lower() in field_names(s)]
            uncertain = len(known) != len(candidates) or any(
                s.get("derived_unknown") for s in known
            )
            status = (
                "unknown"
                if uncertain
                else "pass"
                if len(matching) == 1
                else "ambiguous"
                if len(matching) > 1
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
        for join in scope.expression.args.get("joins") or []:
            condition = join.args.get("on")
            supports = []
            if condition and not condition.find(exp.Or):
                for eq in condition.find_all(exp.EQ):
                    l, r = eq.left, eq.right
                    if (
                        isinstance(l, exp.Column)
                        and isinstance(r, exp.Column)
                        and join.this.alias_or_name.lower()
                        in {l.table.lower(), r.table.lower()}
                    ):
                        a, b = (
                            sources.get(l.table.lower()),
                            sources.get(r.table.lower()),
                        )
                        if (
                            a
                            and b
                            and a is not b
                            and "database" in a
                            and "database" in b
                        ):
                            supports.extend(evidence(a, b, l.name, r.name, snapshot))
                            pending_documents.append(
                                (len(result["joins"]), a, b, l.name, r.name)
                            )
            join_item = {
                "condition": condition.sql(dialect=dialect)
                if condition
                else join.sql(dialect=dialect),
                "status": "supported"
                if any(p["strength"] == "strong" for p in supports)
                else "weak"
                if supports
                else "unsupported",
                "evidence": supports,
                "suggestion": None
                if any(p["strength"] == "strong" for p in supports)
                else "Weak or missing evidence; use find_join_path and verify the join.",
            }
            result["joins"].append(join_item)
    candidates = [
        (i, a, b, l, r)
        for i, a, b, l, r in pending_documents
        if result["joins"][i]["status"] != "supported"
    ]
    documents = snapshot.documents_for(
        {(full(a).lower(), full(b).lower()) for _, a, b, _, _ in candidates}
    )
    for i, a, b, l, r in candidates:
        proofs = [
            p
            for p in evidence(a, b, l, r, snapshot, documents)
            if p["type"] == "document"
        ]
        if proofs:
            result["joins"][i]["evidence"].extend(proofs)
            result["joins"][i]["status"] = "supported"
            result["joins"][i]["suggestion"] = None
    for join_item in result["joins"]:
        if join_item["status"] != "supported":
            result["issues"].append(
                {
                    "kind": "join",
                    "table": None,
                    "condition": join_item["condition"],
                    "message": (
                        "JOIN 只有弱证据"
                        if join_item["status"] == "weak"
                        else "JOIN 缺少证据"
                    ),
                    "suggestion": join_item["suggestion"],
                }
            )
    if partition_tables is not None:
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
                strong = bool(snapshot.edge_pairs.get(frozenset((current, neighbor))))
                cost = (weak_count + (not strong), distance + 1)
                if neighbor not in best or cost < best[neighbor]:
                    heappush(queue, (*cost, neighbor, route + [(current, neighbor)]))
        if path is None:
            return {"status": "no_path", "reason": f"No connection to {target}"}
        steps.extend(path)
        connected.update(b for _, b in path)
    # Documents can strengthen a selected path but do not introduce graph edges.
    weak_steps = {
        (a, b) for a, b in steps if not snapshot.edge_pairs.get(frozenset((a, b)))
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
        result.append(
            {
                "from": full(left),
                "to": full(right),
                "keys": [p["key"] for p in proofs if p["type"] == "common_key"],
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
