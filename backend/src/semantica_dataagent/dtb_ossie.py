"""Deterministic extraction from DTB metadata snapshots to Apache Ossie models."""

from __future__ import annotations

import json
import re
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, ClassVar

import sqlglot
from jsonschema import Draft202012Validator
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError
from sqlglot.optimizer.scope import Scope, traverse_scope

from .canonical import Record
from .dtb_source import DtbAdapter
from .dtb_tasks import DtbTaskParser, extract_task_sql_statements
from .ossie_export import (
    ExportFormat,
    _assign_dataset_names,
    _build_field,
    serialize_ossie,
    validate_ossie_document,
    write_ossie_file,
)
from .ossie_source import OSSIE_TARGET_VERSION
from .source_directory import relative, safe, safe_name

MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$")
_TEMPORAL_COL_RE = re.compile(
    r"^(?:dt|pt|ds|day_num|bizdate|stat_date|etl_date)$"
    r"|_(?:date|day|dt|time|month)$",
    re.IGNORECASE,
)
_TEMPORAL_FUNC_NAMES = frozenset(
    {"date_sub", "date_add", "datediff", "current_date", "current_timestamp", "to_date"}
)
_MAX_UPSTREAMS_IN_DOC = 10
_MAX_DISPLAY_VALUE_CHARS = 120


@cache
def _dimension_validator() -> Draft202012Validator:
    packaged = (
        Path(__file__).resolve().parent
        / "schemas"
        / "v2"
        / "dimension_value.schema.json"
    )
    schema_path = (
        packaged
        if packaged.is_file()
        else Path(__file__).resolve().parents[3]
        / "schemas"
        / "v2"
        / "dimension_value.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def _format_dim_literal(value: str) -> str:
    single_line = value.replace("\r", " ").replace("\n", " ")
    if len(single_line) > _MAX_DISPLAY_VALUE_CHARS:
        single_line = single_line[: _MAX_DISPLAY_VALUE_CHARS - 1] + "…"
    escaped = single_line.replace("'", "''")
    return f"'{escaped}'"


def _apply_hybrid_dimensions(
    datasets: list[dict[str, Any]],
    records: list[Record],
    *,
    db_filter: str | None,
    low_cardinality_limit: int,
    sample_limit: int,
    stats: Counter[str],
) -> list[dict[str, Any]]:
    counts_by_col: dict[tuple[str, str], dict[str, Counter[str]]] = defaultdict(
        lambda: defaultdict(Counter)
    )
    raw_by_table: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

    for rec in records:
        if rec.kind != "dimension_value":
            continue
        d = rec.data
        db = str(d.get("database") or "").strip()
        tbl = str(d.get("table") or "").strip()
        col = str(d.get("field") or "").strip()
        val = d.get("value")
        if not db or not tbl or not col or not isinstance(val, str) or not val.strip():
            continue
        if db_filter is not None and db.lower() != db_filter:
            continue
        tkey = (db.lower(), tbl.lower())
        counts_by_col[tkey][col.lower()][val] += 1
        entry: dict[str, Any] = {
            "database": db,
            "table": tbl,
            "field": col,
            "value": val,
        }
        row_id = d.get("row_id")
        if isinstance(row_id, str) and row_id.strip():
            entry["row_id"] = row_id
        raw_by_table[tkey].append(entry)

    high_card_tables: set[tuple[str, str]] = set()
    for tkey, col_map in counts_by_col.items():
        if any(
            len(val_counts) > low_cardinality_limit for val_counts in col_map.values()
        ):
            high_card_tables.add(tkey)

    for ds in datasets:
        parts = _parse_qualified_table(ds["source"])
        if parts is None or parts not in counts_by_col:
            continue
        col_map = counts_by_col[parts]
        for ossie_field in ds.get("fields") or []:
            fname = str(ossie_field["name"]).lower()
            val_counts = col_map.get(fname)
            if not val_counts:
                continue
            distinct = sorted(val_counts, key=lambda v: (-val_counts[v], v))
            total = len(distinct)
            if total <= low_cardinality_limit:
                stats["low_cardinality_fields"] += 1
                literals = ", ".join(_format_dim_literal(v) for v in distinct)
                note = f"全部取值（共 {total} 个）：{literals}"
            else:
                stats["high_cardinality_fields"] += 1
                high_card_tables.add(parts)
                samples = distinct[:sample_limit]
                literals = ", ".join(_format_dim_literal(v) for v in samples)
                note = f"示例取值（共 {total} 个去重值，抽样前 {len(samples)} 个）：{literals}"

            existing = ossie_field.get("ai_context")
            if isinstance(existing, dict):
                prev = str(existing.get("instructions") or "").strip()
                existing["instructions"] = f"{prev}\n{note}" if prev else note
            elif isinstance(existing, str) and existing.strip():
                ossie_field["ai_context"] = {
                    "instructions": f"{existing.strip()}\n{note}"
                }
            else:
                ossie_field["ai_context"] = {"instructions": note}

    validator = _dimension_validator()
    exported: list[dict[str, Any]] = []
    seen_entries: set[tuple[str, str, str, str, str]] = set()
    for tkey in sorted(high_card_tables):
        rows = sorted(
            raw_by_table.get(tkey, ()),
            key=lambda r: (
                r["database"],
                r["table"],
                r.get("row_id") or "",
                r["field"],
                r["value"],
            ),
        )
        for item in rows:
            dedup = (
                item["database"],
                item["table"],
                item.get("row_id") or "",
                item["field"],
                item["value"],
            )
            if dedup in seen_entries:
                continue
            seen_entries.add(dedup)
            validator.validate(item)
            exported.append(item)

    stats["high_cardinality_tables"] = len(high_card_tables)
    stats["dimension_values"] = len(exported)
    return exported


@dataclass(frozen=True)
class DtbOssieResult:
    """Converted Apache Ossie document, serialized text, and high-cardinality dims."""

    document: dict[str, Any]
    ossie_content: str
    dimension_values: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)


def _normalize_ident(name: str) -> str:
    return name.strip().strip('`"').lower()


def _parse_qualified_table(raw: str) -> tuple[str, str] | None:
    parts = [_normalize_ident(p) for p in raw.split(".") if _normalize_ident(p)]
    if len(parts) < 2:
        return None
    db, tbl = parts[-2], parts[-1]
    if not db or not tbl or db in {"default", "<default>"}:
        return None
    return (db, tbl)


@dataclass
class _SchemaIndex:
    """Lookup tables, columns, and partition columns for deterministic resolution."""

    tables_by_key: dict[tuple[str, str], dict[str, Any]]
    by_short_name: dict[str, list[tuple[str, str]]]
    ordered_cols: dict[tuple[str, str], list[str]]
    col_map: dict[tuple[str, str], dict[str, str]]
    partition_cols: dict[tuple[str, str], set[str]]

    @classmethod
    def from_records(
        cls,
        table_records: list[Record],
        field_records: list[Record],
    ) -> _SchemaIndex:
        tables_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        by_short_name: dict[str, list[tuple[str, str]]] = defaultdict(list)
        ordered_cols: dict[tuple[str, str], list[str]] = defaultdict(list)
        col_map: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
        partition_cols: dict[tuple[str, str], set[str]] = defaultdict(set)

        for rec in table_records:
            db = str(rec.data["database"]).strip()
            name = str(rec.data["name"]).strip()
            key = (db.lower(), name.lower())
            if key not in tables_by_key:
                tables_by_key[key] = {
                    "database": db,
                    "name": name,
                    "comment": str(rec.data.get("comment") or "").strip(),
                }
                by_short_name[name.lower()].append(key)

        for rec in field_records:
            db = str(rec.data["database"]).strip()
            tbl = str(rec.data["table"]).strip()
            cname = str(rec.data["name"]).strip()
            key = (db.lower(), tbl.lower())
            if key not in tables_by_key:
                continue
            clower = cname.lower()
            if clower not in col_map[key]:
                ordered_cols[key].append(cname)
                col_map[key][clower] = cname
            if rec.data.get("partition"):
                partition_cols[key].add(clower)

        return cls(
            tables_by_key=tables_by_key,
            by_short_name=dict(by_short_name),
            ordered_cols=dict(ordered_cols),
            col_map=dict(col_map),
            partition_cols=dict(partition_cols),
        )

    def resolve_table_expr(self, node: exp.Table) -> tuple[str, str] | None:
        tbl = _normalize_ident(node.name)
        if not tbl:
            return None
        db = _normalize_ident(node.db) if node.db else ""
        if db:
            key = (db, tbl)
            return key if key in self.tables_by_key else None
        candidates = self.by_short_name.get(tbl, [])
        return candidates[0] if len(candidates) == 1 else None

    def resolve_table_name(self, qualified_or_short: str) -> tuple[str, str] | None:
        pair = _parse_qualified_table(qualified_or_short)
        if pair is not None:
            return pair if pair in self.tables_by_key else None
        short = _normalize_ident(qualified_or_short)
        candidates = self.by_short_name.get(short, [])
        return candidates[0] if len(candidates) == 1 else None

    def resolve_column(self, key: tuple[str, str], col_name: str) -> str | None:
        return self.col_map.get(key, {}).get(_normalize_ident(col_name))

    def is_partition(self, key: tuple[str, str], col_name: str) -> bool:
        return _normalize_ident(col_name) in self.partition_cols.get(key, set())

    def canonical_name(self, key: tuple[str, str]) -> str:
        t = self.tables_by_key[key]
        return f"{t['database']}.{t['name']}"


def _is_dimension_table(key: tuple[str, str]) -> bool:
    db, tbl = key
    return db == "dimensions" or tbl.startswith("dim_") or tbl == "dim"


def _flatten_and(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.And):
        return _flatten_and(node.this) + _flatten_and(node.expression)
    return [node]


def _unwrap_parens(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _build_scope_aliases(
    scope: Scope, schema: _SchemaIndex
) -> tuple[dict[str, tuple[str, str] | None], set[str]]:
    cte_names = {
        cte.alias_or_name.lower()
        for cte in (scope.ctes or [])
        if getattr(cte, "alias_or_name", None)
    }
    aliases: dict[str, tuple[str, str] | None] = {}
    for alias, src in scope.sources.items():
        key = alias.lower()
        if isinstance(src, exp.Table) and _normalize_ident(src.name) not in cte_names:
            aliases[key] = schema.resolve_table_expr(src)
        else:
            aliases[key] = None
    return aliases, cte_names


def _resolve_bare_column(
    expr: exp.Expression,
    aliases: dict[str, tuple[str, str] | None],
    schema: _SchemaIndex,
) -> tuple[tuple[str, str], str] | None:
    expr = _unwrap_parens(expr)
    if not isinstance(expr, exp.Column):
        return None
    alias = _normalize_ident(expr.table) if expr.table else ""
    if not alias or alias not in aliases:
        return None
    tkey = aliases[alias]
    if tkey is None:
        return None
    if expr.db and _normalize_ident(expr.db) != tkey[0]:
        return None
    col_name = schema.resolve_column(tkey, expr.name)
    if col_name is None:
        return None
    return (tkey, col_name)


def _extract_joins_from_query(
    query: exp.Expression,
    schema: _SchemaIndex,
    join_candidates: set[
        tuple[tuple[str, str], tuple[str, str], tuple[tuple[str, str], ...]]
    ],
) -> None:
    for scope in traverse_scope(query):
        select_expr = scope.expression
        if not isinstance(select_expr, exp.Select):
            continue
        joins = select_expr.args.get("joins") or []
        if not joins:
            continue
        aliases, _ = _build_scope_aliases(scope, schema)

        for join in joins:
            kind = str(join.kind or "").upper()
            if kind in {"CROSS", "NATURAL"}:
                continue
            on_expr = join.args.get("on")
            if on_expr is None:
                continue
            if on_expr.find(exp.Or) is not None:
                continue

            clauses = [_unwrap_parens(c) for c in _flatten_and(on_expr)]
            unsafe = False
            pairs_by_tables: dict[
                frozenset[tuple[str, str]],
                list[tuple[tuple[str, str], str, tuple[str, str], str]],
            ] = defaultdict(list)

            for clause in clauses:
                if clause.find(exp.Or) is not None:
                    unsafe = True
                    break
                referenced_aliases = {
                    _normalize_ident(col.table)
                    for col in clause.find_all(exp.Column)
                    if col.table
                }
                unqualified_cols = [
                    col for col in clause.find_all(exp.Column) if not col.table
                ]
                resolved_tables = {
                    aliases.get(a)
                    for a in referenced_aliases
                    if aliases.get(a) is not None
                }
                if len(resolved_tables) >= 2 or (
                    referenced_aliases & set(aliases)
                    and any(aliases.get(a) is None for a in referenced_aliases)
                ):
                    if not isinstance(clause, exp.EQ) or unqualified_cols:
                        unsafe = True
                        break
                    left = _resolve_bare_column(clause.this, aliases, schema)
                    right = _resolve_bare_column(clause.expression, aliases, schema)
                    if left is None or right is None or left[0] == right[0]:
                        unsafe = True
                        break
                    t_a, c_a = left
                    t_b, c_b = right
                    pairs_by_tables[frozenset((t_a, t_b))].append((t_a, c_a, t_b, c_b))
                else:
                    # Clause does not join two resolved tables (e.g. dim.flag = 'Y' filter
                    # or non-equijoin filter referencing one table). Verify no hidden subquery join.
                    if clause.find(exp.Select) is not None:
                        unsafe = True
                        break

            if unsafe:
                continue

            for tset, raw_pairs in pairs_by_tables.items():
                if len(tset) != 2:
                    continue
                t1, t2 = sorted(tset)
                aligned: list[tuple[str, str]] = []
                for ta, ca, tb, cb in raw_pairs:
                    c1, c2 = (ca, cb) if ta == t1 else (cb, ca)
                    if schema.is_partition(t1, c1) and schema.is_partition(t2, c2):
                        continue
                    aligned.append((c1, c2))
                if not aligned:
                    continue
                dedup_cols = tuple(sorted(set(aligned)))
                join_candidates.add((t1, t2, dedup_cols))


def _resolve_insert_target(
    stmt: exp.Expression,
    schema: _SchemaIndex,
) -> tuple[tuple[str, str], list[str], exp.Expression | None, exp.Expression] | None:
    if not isinstance(stmt, (exp.Insert, exp.Create)):
        return None
    target = stmt.this
    explicit_cols: list[str] = []
    if isinstance(target, exp.Schema):
        explicit_cols = [
            c.name for c in (target.expressions or []) if getattr(c, "name", None)
        ]
        target = target.this
    if not isinstance(target, exp.Table) or stmt.expression is None:
        return None
    tkey = schema.resolve_table_expr(target)
    if tkey is None:
        return None

    partition_node = stmt.args.get("partition") or target.args.get("partition")
    static_part_cols: set[str] = set()
    if isinstance(partition_node, exp.Partition):
        for expr in partition_node.expressions or []:
            if isinstance(expr, exp.EQ) and isinstance(expr.this, exp.Column):
                static_part_cols.add(_normalize_ident(expr.this.name))

    all_cols = schema.ordered_cols.get(tkey, [])
    if explicit_cols:
        target_cols = explicit_cols
    else:
        non_static = [c for c in all_cols if c.lower() not in static_part_cols]
        target_cols = non_static
    return tkey, target_cols, partition_node, stmt.expression


def _match_projection_columns(
    select_expr: exp.Select,
    tkey: tuple[str, str],
    target_cols: list[str],
    schema: _SchemaIndex,
    is_ctas: bool,
) -> dict[tuple[str, str], str]:
    """Map `(source_alias_lower_or_empty, source_col_lower)` -> target physical column."""
    projections = select_expr.selects
    all_cols = schema.ordered_cols.get(tkey, [])
    mapped_targets: list[str] | None = None
    if not is_ctas and len(projections) == len(target_cols):
        mapped_targets = target_cols
    elif not is_ctas and len(projections) == len(all_cols):
        mapped_targets = all_cols
    elif is_ctas:
        resolved: list[str] = []
        for proj in projections:
            alias = proj.alias_or_name
            col = schema.resolve_column(tkey, alias) if alias else None
            if col is None:
                return {}
            resolved.append(col)
        mapped_targets = resolved
    else:
        return {}

    result: dict[tuple[str, str], str] = {}
    for proj, dst_col in zip(projections, mapped_targets, strict=True):
        verified_dst = schema.resolve_column(tkey, dst_col)
        if verified_dst is None:
            continue
        inner = proj.this if isinstance(proj, exp.Alias) else proj
        inner = _unwrap_parens(inner)
        if isinstance(inner, exp.Column):
            src_alias = _normalize_ident(inner.table) if inner.table else ""
            src_name = _normalize_ident(inner.name)
            result[(src_alias, src_name)] = verified_dst
            if ("", src_name) not in result:
                result[("", src_name)] = verified_dst
    return result


def _lookup_projected_target(
    col_expr: exp.Expression,
    proj_map: dict[tuple[str, str], str],
) -> str | None:
    col_expr = _unwrap_parens(col_expr)
    if not isinstance(col_expr, exp.Column):
        return None
    alias = _normalize_ident(col_expr.table) if col_expr.table else ""
    cname = _normalize_ident(col_expr.name)
    if alias and (alias, cname) in proj_map:
        return proj_map[(alias, cname)]
    if not alias:
        return proj_map.get(("", cname))
    return None


def _is_row_number_eq_one(expr: exp.Expression) -> exp.Window | str | None:
    """Return Window if `ROW_NUMBER() OVER (...) = 1`, or column name if `rn = 1`."""
    expr = _unwrap_parens(expr)
    if not isinstance(expr, exp.EQ):
        return None
    left, right = _unwrap_parens(expr.this), _unwrap_parens(expr.expression)
    if isinstance(left, exp.Literal) and not left.is_string and left.name == "1":
        target = right
    elif isinstance(right, exp.Literal) and not right.is_string and right.name == "1":
        target = left
    else:
        return None
    if isinstance(target, exp.Window) and isinstance(target.this, exp.RowNumber):
        return target
    if isinstance(target, exp.Column):
        return _normalize_ident(target.name)
    return None


def _infer_primary_key_from_insert(
    stmt: exp.Insert | exp.Create,
    tkey: tuple[str, str],
    target_cols: list[str],
    query: exp.Expression,
    schema: _SchemaIndex,
) -> tuple[str, ...] | None:
    query = _unwrap_parens(query)
    if not isinstance(query, exp.Select):
        return None

    is_ctas = isinstance(stmt, exp.Create)
    proj_map = _match_projection_columns(
        query, tkey, target_cols, schema, is_ctas=is_ctas
    )
    if not proj_map:
        return None

    part_cols = [
        c for c in schema.ordered_cols.get(tkey, []) if schema.is_partition(tkey, c)
    ]

    def finalize(unique_cols: list[str]) -> tuple[str, ...] | None:
        if not unique_cols or len(set(unique_cols)) != len(unique_cols):
            return None
        combined = list(part_cols)
        for col in unique_cols:
            if col not in combined:
                combined.append(col)
        return tuple(combined)

    # Case 1: Top-level GROUP BY without ROLLUP / CUBE / GROUPING SETS
    group = query.args.get("group")
    if group is not None:
        if (
            group.args.get("grouping_sets")
            or group.args.get("cube")
            or group.args.get("rollup")
            or not group.expressions
        ):
            return None
        mapped_group: list[str] = []
        for gexpr in group.expressions:
            dst = _lookup_projected_target(gexpr, proj_map)
            if dst is None:
                return None
            mapped_group.append(dst)
        return finalize(mapped_group)

    # Case 2a: Top-level QUALIFY ROW_NUMBER() OVER (PARTITION BY ...) = 1
    qualify = query.args.get("qualify")
    if qualify is not None:
        for clause in _flatten_and(qualify.this):
            matched = _is_row_number_eq_one(clause)
            if isinstance(matched, exp.Window):
                pcols = matched.args.get("partition_by") or []
                if not pcols:
                    return None
                mapped_p: list[str] = []
                for pexpr in pcols:
                    dst = _lookup_projected_target(pexpr, proj_map)
                    if dst is None:
                        return None
                    mapped_p.append(dst)
                return finalize(mapped_p)

    # Case 2b: Subquery with ROW_NUMBER() OVER (...) AS rn filtered in outer WHERE rn = 1
    from_node = query.args.get("from_") or query.args.get("from")
    joins = query.args.get("joins") or []
    where_node = query.args.get("where")
    if (
        from_node is not None
        and not joins
        and where_node is not None
        and isinstance(from_node.this, exp.Subquery)
        and isinstance(_unwrap_parens(from_node.this.this), exp.Select)
    ):
        sub_select = _unwrap_parens(from_node.this.this)
        rn_names: set[str] = set()
        for clause in _flatten_and(where_node.this):
            matched = _is_row_number_eq_one(clause)
            if isinstance(matched, str):
                rn_names.add(matched)
        if not rn_names:
            return None

        sub_windows: dict[str, exp.Window] = {}
        sub_proj_by_alias: dict[str, exp.Expression] = {}
        for proj in sub_select.selects:
            alias = _normalize_ident(proj.alias_or_name) if proj.alias_or_name else ""
            inner = _unwrap_parens(proj.this if isinstance(proj, exp.Alias) else proj)
            if (
                isinstance(inner, exp.Window)
                and isinstance(inner.this, exp.RowNumber)
                and alias
            ):
                sub_windows[alias] = inner
            elif alias:
                sub_proj_by_alias[alias] = inner

        for rn_name in rn_names:
            win = sub_windows.get(rn_name)
            if win is None:
                continue
            pcols = win.args.get("partition_by") or []
            if not pcols:
                continue
            mapped_sub_p: list[str] = []
            valid = True
            for pexpr in pcols:
                pexpr = _unwrap_parens(pexpr)
                if not isinstance(pexpr, exp.Column):
                    valid = False
                    break
                p_name = _normalize_ident(pexpr.name)
                p_alias = _normalize_ident(pexpr.table) if pexpr.table else ""
                matched_outer: str | None = None
                for (outer_alias, outer_col), dst in proj_map.items():
                    # Match either direct subquery pass-through `uid` or alias matching source col
                    sub_inner = sub_proj_by_alias.get(outer_col)
                    if sub_inner is not None and isinstance(sub_inner, exp.Column):
                        si_name = _normalize_ident(sub_inner.name)
                        si_alias = (
                            _normalize_ident(sub_inner.table) if sub_inner.table else ""
                        )
                        if si_name == p_name and (
                            not p_alias or not si_alias or p_alias == si_alias
                        ):
                            matched_outer = dst
                            break
                    elif outer_col == p_name and (
                        not outer_alias or not p_alias or outer_alias == p_alias
                    ):
                        matched_outer = dst
                        break
                if matched_outer is None:
                    valid = False
                    break
                mapped_sub_p.append(matched_outer)
            if valid and mapped_sub_p:
                return finalize(mapped_sub_p)

    return None


def _query_has_temporal_or_partition_filter(
    query: exp.Expression, partition_cols: set[str]
) -> bool:
    for where in query.find_all(exp.Where):
        for col in where.find_all(exp.Column):
            cname = _normalize_ident(col.name)
            if cname in partition_cols or _TEMPORAL_COL_RE.search(cname):
                return True
        for func in where.find_all(
            exp.Anonymous, exp.DateAdd, exp.DateSub, exp.DateDiff
        ):
            name = (
                func.name.lower()
                if isinstance(func, exp.Anonymous)
                else type(func).__name__.lower()
            )
            if name in _TEMPORAL_FUNC_NAMES:
                return True
    return False


def _merge_onself_target(task: Record, schema: _SchemaIndex) -> tuple[str, str] | None:
    params = task.data.get("parameters")
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except json.JSONDecodeError:
            params = None
    if isinstance(params, dict):
        nested = params.get("params")
        raw_table = nested.get("table") if isinstance(nested, dict) else None
        if not raw_table:
            raw_table = params.get("table")
        if isinstance(raw_table, str):
            return schema.resolve_table_name(raw_table)
    return None


def _strategy_text(code: str) -> str:
    return {
        "merge_onself": "分区策略：merge_onself 合并更新",
        "incremental": "分区策略：增量追加分区（INSERT INTO，查询请按分区范围过滤）",
        "snapshot": "分区策略：全量快照分区（INSERT OVERWRITE，查询最新状态请限定 MAX(分区列)）",
        "overwrite_partition": "分区策略：INSERT OVERWRITE 分区覆盖（请结合业务时间列确认快照或增量口径）",
    }[code]


def _pick_relationship_direction(
    t1: tuple[str, str],
    t2: tuple[str, str],
    cols: tuple[tuple[str, str], ...],
    primary_keys: dict[tuple[str, str], tuple[str, ...]],
    schema: _SchemaIndex,
) -> tuple[tuple[str, str], tuple[str, str], list[str], list[str]]:
    c1_set = {c1 for c1, _ in cols}
    c2_set = {c2 for _, c2 in cols}
    pk1 = set(primary_keys.get(t1, ()))
    pk2 = set(primary_keys.get(t2, ()))

    def is_pk_match(col_set: set[str], pk_set: set[str], tkey: tuple[str, str]) -> bool:
        non_part_pk = {c for c in pk_set if not schema.is_partition(tkey, c)}
        return bool(col_set) and (
            col_set == pk_set or (non_part_pk and col_set == non_part_pk)
        )

    pk1_hit = is_pk_match(c1_set, pk1, t1)
    pk2_hit = is_pk_match(c2_set, pk2, t2)

    swap = False
    if pk2_hit and not pk1_hit:
        swap = False
    elif pk1_hit and not pk2_hit:
        swap = True
    elif _is_dimension_table(t2) and not _is_dimension_table(t1):
        swap = False
    elif _is_dimension_table(t1) and not _is_dimension_table(t2):
        swap = True
    else:
        swap = t2 < t1

    if swap:
        return t2, t1, [c2 for _, c2 in cols], [c1 for c1, _ in cols]
    return t1, t2, [c1 for c1, _ in cols], [c2 for _, c2 in cols]


def convert_dtb_to_ossie(
    source_dir: Path,
    *,
    snapshot_path: str = "dtb",
    snapshot: str | None = None,
    latest: bool = True,
    dimensions_path: str = "dimensions",
    dimension_tables: list[str] | None = None,
    dimension_row_limit: int = 50000,
    model_name: str = "dtb_semantic_model",
    format: ExportFormat = "yaml",
    low_cardinality_limit: int = 20,
    sample_limit: int = 5,
    database: str | None = None,
) -> DtbOssieResult:
    """Convert a DTB snapshot directory into a validated Apache Ossie model + dim values."""
    if not isinstance(model_name, str) or not MODEL_NAME_RE.fullmatch(model_name):
        raise ValueError(
            "Invalid model_name: must match ^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$"
        )
    if low_cardinality_limit < 1:
        raise ValueError("low_cardinality_limit must be >= 1")
    if sample_limit < 1:
        raise ValueError("sample_limit must be >= 1")
    if dimension_row_limit < 1:
        raise ValueError("dimension_row_limit must be >= 1")
    if format not in ("yaml", "json"):
        raise ValueError(f"Unsupported Ossie format: {format!r}")

    adapter_params: dict[str, Any] = {
        "snapshot_path": snapshot_path,
        "latest": latest,
        "dimensions_path": dimensions_path,
        "dimension_row_limit": dimension_row_limit,
    }
    if snapshot is not None:
        adapter_params["snapshot"] = snapshot
    if dimension_tables is not None:
        adapter_params["dimension_tables"] = dimension_tables

    read_result = DtbAdapter().read(Path(source_dir), adapter_params)
    if (
        read_result.stats.get("table_rows_skipped", 0) > 0
        or read_result.stats.get("task_rows_unreadable", 0) > 0
    ):
        raise ValueError("Invalid DTB metadata rows")

    db_filter = (
        database.strip().lower()
        if isinstance(database, str) and database.strip()
        else None
    )
    table_records = [
        r
        for r in read_result.records
        if r.kind == "table"
        and (db_filter is None or r.data["database"].lower() == db_filter)
    ]
    field_records = [
        r
        for r in read_result.records
        if r.kind == "field"
        and (db_filter is None or r.data["database"].lower() == db_filter)
    ]
    if not table_records:
        if db_filter is not None:
            raise ValueError(
                f"No physical tables found in DTB snapshot for database {database!r}"
            )
        raise ValueError("No physical tables found in DTB snapshot")

    schema = _SchemaIndex.from_records(table_records, field_records)

    stats: Counter[str] = Counter()
    upstreams_by_target: dict[tuple[str, str], set[str]] = defaultdict(set)
    for rec in read_result.records:
        if rec.kind != "lineage":
            continue
        dst = schema.resolve_table_name(str(rec.data.get("downstream") or ""))
        src = str(rec.data.get("upstream") or "").strip()
        if (
            dst is not None
            and src
            and src.lower() != schema.canonical_name(dst).lower()
        ):
            upstreams_by_target[dst].add(src)

    join_candidates: set[
        tuple[tuple[str, str], tuple[str, str], tuple[tuple[str, str], ...]]
    ] = set()
    pk_candidates_by_target: dict[tuple[str, str], list[tuple[str, ...] | None]] = (
        defaultdict(list)
    )
    strategies_by_target: dict[tuple[str, str], set[str]] = defaultdict(set)
    sql_parser = DtbTaskParser()

    for task in read_result.records:
        if task.kind != "task":
            continue
        ttype = str(task.data.get("type") or "").strip().lower()
        if ttype == "merge_onself":
            m_target = _merge_onself_target(task, schema)
            if m_target is not None:
                strategies_by_target[m_target].add("merge_onself")
            continue

        # Also harvest shell basic/temp view upstreams via DtbTaskParser when SQL parsing misses them
        if ttype == "shell":
            parsed_shell = sql_parser.parse(task, {})
            for src_name, dst_name in parsed_shell.edges or []:
                dst = schema.resolve_table_name(dst_name)
                if (
                    dst is not None
                    and src_name.lower() != schema.canonical_name(dst).lower()
                ):
                    upstreams_by_target[dst].add(src_name)

        statements = extract_task_sql_statements(task)
        for sql_text, dialect in statements:
            try:
                parsed_stmts = sqlglot.parse(
                    sql_text, read=dialect, error_level=ErrorLevel.RAISE
                )
            except (SqlglotError, ValueError, IndexError, TypeError, KeyError):
                stats["tasks_sql_unparseable"] += 1
                continue
            stats["tasks_sql_parsed"] += 1

            for stmt in parsed_stmts:
                if stmt is None or isinstance(stmt, exp.Command):
                    continue
                _extract_joins_from_query(stmt, schema, join_candidates)

                insert_info = _resolve_insert_target(stmt, schema)
                if insert_info is not None:
                    tkey, target_cols, partition_node, query = insert_info
                    # Direct SQL upstream tables
                    dst_name_lower = schema.canonical_name(tkey).lower()
                    for scope in traverse_scope(query):
                        cte_names = {
                            c.alias_or_name.lower()
                            for c in (scope.ctes or [])
                            if getattr(c, "alias_or_name", None)
                        }
                        for src_node in scope.sources.values():
                            if (
                                isinstance(src_node, exp.Table)
                                and _normalize_ident(src_node.name) not in cte_names
                            ):
                                resolved = schema.resolve_table_expr(src_node)
                                if resolved is not None:
                                    cand = schema.canonical_name(resolved)
                                else:
                                    parts = [
                                        _normalize_ident(p)
                                        for p in (src_node.db, src_node.name)
                                        if p
                                    ]
                                    cand = ".".join(parts) if len(parts) == 2 else ""
                                if cand and cand.lower() != dst_name_lower:
                                    upstreams_by_target[tkey].add(cand)

                    # Partition strategy for partitioned target tables
                    if schema.partition_cols.get(tkey):
                        if isinstance(stmt, exp.Create):
                            strategies_by_target[tkey].add("snapshot")
                        elif isinstance(stmt, exp.Insert):
                            if not stmt.args.get("overwrite"):
                                strategies_by_target[tkey].add("incremental")
                            elif (
                                partition_node is None
                                or not _query_has_temporal_or_partition_filter(
                                    query, schema.partition_cols[tkey]
                                )
                            ):
                                strategies_by_target[tkey].add("snapshot")
                            else:
                                strategies_by_target[tkey].add("overwrite_partition")

                    # Candidate primary key inference
                    inferred_pk = _infer_primary_key_from_insert(
                        stmt, tkey, target_cols, query, schema
                    )
                    pk_candidates_by_target[tkey].append(inferred_pk)

    resolved_pks: dict[tuple[str, str], tuple[str, ...]] = {}
    for tkey, cands in pk_candidates_by_target.items():
        if cands and all(c is not None for c in cands) and len(set(cands)) == 1:
            resolved_pks[tkey] = cands[0]  # type: ignore[assignment]
            stats["primary_keys"] += 1

    # Map datasets via ossie_export shared naming rules
    sorted_keys_3 = [
        ("", schema.tables_by_key[k]["database"], schema.tables_by_key[k]["name"])
        for k in sorted(schema.tables_by_key)
    ]
    dataset_names_3 = _assign_dataset_names(sorted_keys_3)
    dataset_names = {
        (k[1].lower(), k[2].lower()): name for k, name in dataset_names_3.items()
    }

    fields_by_table: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    seen_fields: set[tuple[tuple[str, str], str]] = set()
    for rec in field_records:
        d = rec.data
        tkey = (str(d["database"]).lower(), str(d["table"]).lower())
        cname = str(d["name"])
        if tkey not in schema.tables_by_key or (tkey, cname.lower()) in seen_fields:
            continue
        seen_fields.add((tkey, cname.lower()))
        fields_by_table[tkey].append(d)

    datasets: list[dict[str, Any]] = []
    for tkey in sorted(schema.tables_by_key):
        tmeta = schema.tables_by_key[tkey]
        ds_name = dataset_names[tkey]
        parts = (tmeta["database"], tmeta["name"])
        raw_fields = sorted(fields_by_table.get(tkey, []), key=lambda x: str(x["name"]))
        ossie_fields = [_build_field(f, ds_name, parts) for f in raw_fields]

        ds: dict[str, Any] = {
            "name": ds_name,
            "source": f"{tmeta['database']}.{tmeta['name']}",
        }
        if tmeta["comment"]:
            ds["description"] = tmeta["comment"]
        if tkey in resolved_pks:
            field_name_set = {f["name"] for f in ossie_fields}
            pk = list(resolved_pks[tkey])
            if pk and all(col in field_name_set for col in pk):
                ds["primary_key"] = pk

        inst_lines: list[str] = []
        if tkey in strategies_by_target and schema.partition_cols.get(tkey):
            strat_list = [_strategy_text(c) for c in sorted(strategies_by_target[tkey])]
            inst_lines.extend(strat_list)
        upstreams = sorted(upstreams_by_target.get(tkey, ()))
        if upstreams:
            shown = ", ".join(upstreams[:_MAX_UPSTREAMS_IN_DOC])
            suffix = (
                f"（共 {len(upstreams)} 张，展示前 {_MAX_UPSTREAMS_IN_DOC} 张）"
                if len(upstreams) > _MAX_UPSTREAMS_IN_DOC
                else ""
            )
            inst_lines.append(f"直接上游表{suffix}：{shown}")
        if inst_lines:
            ds["ai_context"] = {"instructions": "\n".join(inst_lines)}

        if ossie_fields:
            ds["fields"] = ossie_fields
        datasets.append(ds)

    # Deduplicate and build relationships
    oriented_rels: dict[
        tuple[tuple[str, str], tuple[str, str], tuple[tuple[str, str], ...]],
        dict[str, Any],
    ] = {}
    for t1, t2, aligned_cols in sorted(join_candidates):
        from_key, to_key, from_cols, to_cols = _pick_relationship_direction(
            t1, t2, aligned_cols, resolved_pks, schema
        )
        col_sig = tuple(zip(from_cols, to_cols, strict=True))
        dedup_key = (from_key, to_key, col_sig)
        if dedup_key in oriented_rels:
            continue
        from_ds = dataset_names[from_key]
        to_ds = dataset_names[to_key]
        col_part = "__".join(from_cols)
        oriented_rels[dedup_key] = {
            "name": f"{from_ds}__{to_ds}__{col_part}",
            "from": from_ds,
            "to": to_ds,
            "from_columns": from_cols,
            "to_columns": to_cols,
        }

    # Resolve any relationship name collisions deterministically
    rels_list: list[dict[str, Any]] = []
    used_rel_names: set[str] = set()
    rel_name_counts = Counter(item["name"] for item in oriented_rels.values())
    for key in sorted(oriented_rels):
        rel = dict(oriented_rels[key])
        base_name = rel["name"]
        if rel_name_counts[base_name] > 1 or base_name in used_rel_names:
            suffix = 2
            while f"{base_name}__{suffix}" in used_rel_names:
                suffix += 1
            rel["name"] = f"{base_name}__{suffix}"
        used_rel_names.add(rel["name"])
        rels_list.append(rel)

    dimension_values = _apply_hybrid_dimensions(
        datasets,
        read_result.records,
        db_filter=db_filter,
        low_cardinality_limit=low_cardinality_limit,
        sample_limit=sample_limit,
        stats=stats,
    )

    document: dict[str, Any] = {
        "version": OSSIE_TARGET_VERSION,
        "name": model_name,
        "datasets": datasets,
    }
    if rels_list:
        document["relationships"] = rels_list

    validate_ossie_document(document)
    ossie_content = serialize_ossie(document, format)
    stats["datasets"] = len(datasets)
    stats["relationships"] = len(rels_list)
    return DtbOssieResult(
        document=document,
        ossie_content=ossie_content,
        dimension_values=dimension_values,
        stats=dict(stats),
    )


def write_dtb_ossie_output(
    result: DtbOssieResult,
    output_dir: Path,
    *,
    ossie_path: str = "ossie",
    model_name: str = "dtb_semantic_model",
    format: ExportFormat = "yaml",
    dimension_filename: str = "dimension_values.json",
) -> dict[str, Path]:
    """Atomically write converted Ossie model and optional dimension_values.json."""
    if not isinstance(model_name, str) or not MODEL_NAME_RE.fullmatch(model_name):
        raise ValueError(
            "Invalid model_name: must match ^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$"
        )
    if format not in ("yaml", "json"):
        raise ValueError(f"Unsupported Ossie format: {format!r}")

    out_root = Path(output_dir)
    if out_root.is_symlink():
        raise ValueError("output_dir cannot be a symlink")
    out_root.mkdir(parents=True, exist_ok=True)
    out_resolved = out_root.resolve()

    rel_ossie = safe_name(ossie_path)
    rel_dim = relative(dimension_filename)
    if any(part.startswith(".") for part in rel_dim.parts):
        raise ValueError("dimension_filename must not use hidden segments")
    ext = "json" if format == "json" else "yaml"
    ossie_target = safe(out_root, rel_ossie / f"{model_name}.{ext}")
    if not ossie_target.parent.resolve().is_relative_to(out_resolved):
        raise ValueError("Ossie output path escapes output directory")
    dim_target = safe(out_root, rel_dim)
    if not dim_target.parent.resolve().is_relative_to(out_resolved):
        raise ValueError("Dimension output path escapes output directory")
    write_ossie_file(ossie_target, result.ossie_content)

    written: dict[str, Path] = {"ossie": ossie_target}
    if result.dimension_values:
        write_ossie_file(
            dim_target,
            json.dumps(result.dimension_values, ensure_ascii=False, indent=2) + "\n",
        )
        written["dimension_values"] = dim_target
    return written


class DtbOssiePuller:
    """Pull DTB metadata and dimensions and convert them into Ossie outputs."""

    id = "ossie"
    version = "1"
    display_name = "DTB 拉取为 Ossie"
    parameters_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "format": "uri", "title": "DTB 服务地址"},
            "token": {"type": "string", "format": "password", "title": "DTB 访问凭据"},
            "interval_seconds": {
                "type": "integer",
                "minimum": 0,
                "title": "自动拉取间隔（秒）",
            },
            "layer": {
                "type": "string",
                "enum": ["dimensions"],
                "title": "拉取层级",
            },
            "max_tables": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "title": "最大拉取表数",
            },
            "model_name": {
                "type": "string",
                "pattern": r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$",
                "default": "dtb_semantic_model",
                "title": "语义模型名称",
            },
            "low_cardinality_limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "default": 20,
                "title": "低基数内联阈值",
            },
            "sample_limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 5,
                "title": "高基数抽样数量",
            },
        },
        "additionalProperties": False,
    }

    def fetch(
        self,
        client,
        base_url: str,
        token: str,
        parameters: dict[str, Any] | None = None,
        *,
        ossie_path: str = "ossie",
    ) -> tuple[dict[str, bytes], dict[str, Any]]:
        """Pull DTB snapshot & dimensions and convert them to Ossie + dimension files."""
        from .dtb_puller import DtbPuller

        params = parameters or {}
        model_name = str(params.get("model_name") or "dtb_semantic_model")
        low_limit = int(params.get("low_cardinality_limit", 20))
        sample_limit = int(params.get("sample_limit", 5))
        rel_ossie = safe_name(ossie_path).as_posix()

        dtb_scope = {k: params[k] for k in ("layer", "max_tables") if k in params}
        raw_files, dtb_counts = DtbPuller().fetch(
            client, base_url, token, dtb_scope or None
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, content in raw_files.items():
                dest = root / "dtb" / "snapshot" / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(content)
            converted = convert_dtb_to_ossie(
                root,
                snapshot_path="dtb",
                dimensions_path="dtb/snapshot/dimensions",
                model_name=model_name,
                format="yaml",
                low_cardinality_limit=low_limit,
                sample_limit=sample_limit,
            )

        out_files: dict[str, bytes] = {
            f"{rel_ossie}/{model_name}.yaml": converted.ossie_content.encode("utf-8")
        }
        if converted.dimension_values:
            out_files["dimension_values.json"] = (
                json.dumps(converted.dimension_values, ensure_ascii=False, indent=2)
                + "\n"
            ).encode("utf-8")

        counts = {
            **dtb_counts,
            "datasets": converted.stats.get("datasets", 0),
            "relationships": converted.stats.get("relationships", 0),
            "dimension_values": converted.stats.get("dimension_values", 0),
        }
        return out_files, counts
