"""SQL validation against a batch snapshot of the current knowledge generation."""

import re
from collections import deque
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Literal

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, ParseError
from sqlglot.optimizer.scope import traverse_scope

from . import knowledge


@dataclass
class Snapshot:
    tables: list[dict]
    edges: list[dict]
    documents: list[dict]

    def resolve(self, name):
        name = name.lower()
        return [
            t
            for t in self.tables
            if (f"{t['database']}.{t['name']}" if "." in name else t["name"]).lower()
            == name
        ]


def load_snapshot():
    values = knowledge.settings()
    store = knowledge.read_graph(values)
    params = {"generation": values["knowledge_generation"]}
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
    docs = knowledge.execute(
        store,
        """MATCH (c:SDAChunk {generation:$generation}) RETURN c.ref AS ref, c.content AS content""",
        params,
    )
    return Snapshot(
        [{**r["table"], "fields": r["fields"]} for r in tables],
        [r["edge"] for r in edges],
        docs,
    )


def full(t):
    return f"{t['database']}.{t['name']}"


def field_names(t):
    return {f["name"].lower(): f for f in t["fields"]}


def evidence(a, b, left_key=None, right_key=None, snapshot=None):
    found = []
    for e in snapshot.edges:
        if {e["upstream"].lower(), e["downstream"].lower()} == {
            full(a).lower(),
            full(b).lower(),
        }:
            found.append(
                {
                    "type": "lineage",
                    "ref": e.get("ref") or f"lineage:{e['upstream']}:{e['downstream']}",
                    "parse_status": e.get("parse_status"),
                    "evidence": e.get("evidence"),
                }
            )
    common = set(field_names(a)) & set(field_names(b))
    keys = {k for k in common if k == "id" or k.endswith(("_id", "_key"))}
    if left_key is not None:
        keys &= (
            {left_key.lower()}
            if left_key.lower() == (right_key or "").lower()
            else set()
        )
    for key in sorted(keys):
        found.append(
            {
                "type": "common_key",
                "key": key,
                "refs": [
                    field_names(a)[key].get("ref", a["ref"]),
                    field_names(b)[key].get("ref", b["ref"]),
                ],
            }
        )
    if left_key and right_key:
        for doc in snapshot.documents:
            content = doc["content"].lower()
            left = re.escape(f"{full(a)}.{left_key}".lower())
            right = re.escape(f"{full(b)}.{right_key}".lower())
            if re.search(
                rf"(?<![\w.])(?:{left}\s*=\s*{right}|{right}\s*=\s*{left})(?![\w.])",
                content,
            ):
                found.append({"type": "document", "ref": doc["ref"]})
    return found


def check_sql(sql, dialect, snapshot):
    if dialect not in ("hive", "spark", "doris"):
        raise ValueError("Unsupported dialect")
    result = {
        "status": "issues",
        "syntax": {"status": "pass"},
        "tables": [],
        "fields": [],
        "joins": [],
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
            name = ".".join(x for x in (table.db, table.name) if x)
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
            if status == "error":
                item["suggestions"] = get_close_matches(
                    column.name.lower(),
                    sorted({n for s in known for n in field_names(s)}),
                    n=3,
                    cutoff=0.5,
                )
            result["fields"].append(item)
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
            result["joins"].append(
                {
                    "condition": condition.sql(dialect=dialect)
                    if condition
                    else join.sql(dialect=dialect),
                    "status": "supported" if supports else "unsupported",
                    "evidence": supports,
                    "suggestion": None
                    if supports
                    else "Use find_join_path to find evidence.",
                }
            )
    if all(x["status"] == "pass" for x in result["tables"] + result["fields"]) and all(
        x["status"] == "supported" for x in result["joins"]
    ):
        result["status"] = "pass"
    return result


def join_path(tables, snapshot):
    if not 2 <= len(tables) <= 4:
        raise ValueError("Provide two to four tables")
    matches = [snapshot.resolve(name) for name in tables]
    if any(len(x) != 1 for x in matches):
        return {"status": "no_path", "reason": "Table missing or ambiguous"}
    targets = [full(x[0]) for x in matches]
    by_name = {full(t): t for t in snapshot.tables}
    graph = {n: [] for n in by_name}
    for i, a in enumerate(snapshot.tables):
        for b in snapshot.tables[i + 1 :]:
            proofs = evidence(a, b, snapshot=snapshot)
            if proofs:
                graph[full(a)].append((full(b), proofs))
                graph[full(b)].append((full(a), proofs))
    connected = {targets[0]}
    steps = []
    for target in targets[1:]:
        queue = deque([(name, []) for name in connected])
        seen = set(connected)
        path = None
        while queue:
            current, route = queue.popleft()
            if current == target:
                path = route
                break
            for neighbor, proofs in graph[current]:
                if neighbor not in seen:
                    seen.add(neighbor)
                    queue.append(
                        (
                            neighbor,
                            route
                            + [
                                {
                                    "from": current,
                                    "to": neighbor,
                                    "keys": [
                                        p["key"]
                                        for p in proofs
                                        if p["type"] == "common_key"
                                    ],
                                    "evidence": proofs,
                                }
                            ],
                        )
                    )
        if path is None:
            return {"status": "no_path", "reason": f"No connection to {target}"}
        steps.extend(path)
        connected.update(step["to"] for step in path)
    return {"status": "found", "steps": steps}


def validate_sql(sql: str, dialect: Literal["hive", "spark", "doris"] = "hive") -> dict:
    return check_sql(sql, dialect, load_snapshot())


def find_join_path(tables: list[str]) -> dict:
    return join_path(tables, load_snapshot())
