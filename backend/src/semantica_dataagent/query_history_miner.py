"""Offline query-history scrubbing, AST fingerprinting, clustering, and signal mining (#303)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from . import evolution_gate, knowledge, provider_probe, table_catalog, trajectories
from .db import Provider, sessions
from .discovery_executor import QueryHistoryRecord
from .provider_api import provider_config

logger = logging.getLogger(__name__)

_SQL_DIALECTS = ("doris", "hive", "spark", "presto", "")
_RECOGNIZED_DIALECTS = frozenset({"doris", "hive", "spark", "presto"})
_FORBIDDEN_QUERY_NODES = tuple(
    node
    for node in (
        exp.DML,
        exp.DDL,
        exp.Command,
        exp.Into,
        exp.Lock,
        getattr(exp, "Transaction", None),
        getattr(exp, "Commit", None),
        getattr(exp, "Rollback", None),
        getattr(exp, "Pragma", None),
    )
    if node is not None
)

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_CN_ID_CARD_RE = re.compile(
    r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"
)
_CN_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_LONG_DIGITS_RE = re.compile(r"(?<!\d)\d{11,}(?!\d)")

_TIME_COL_NAME_RE = re.compile(
    r"^(?:dt|ds|pt|p_date|stat_date|biz_date|log_date|date|day|month|year|"
    r".*_(?:dt|ds|date|time|at|timestamp))$",
    re.IGNORECASE,
)
_DATE_TYPE_HINTS = ("date", "time", "timestamp")
_RANGE_PREDICATES = (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between)
_SAFE_ALIAS_RE = re.compile(r"^[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]{0,63}$")


def scrub_sensitive_text(value: str) -> str:
    """Scrub credentials, prompt injections, emails, phones, ID cards, and long numeric PII."""
    cleaned = trajectories.scrub_text(str(value or ""))
    cleaned = _EMAIL_RE.sub("[REDACTED_EMAIL]", cleaned)
    cleaned = _CN_ID_CARD_RE.sub("[REDACTED_ID]", cleaned)
    cleaned = _CN_PHONE_RE.sub("[REDACTED_PHONE]", cleaned)
    cleaned = _LONG_DIGITS_RE.sub("[REDACTED_NUMBER]", cleaned)
    return cleaned


def _parse_readonly_query(
    sql: str, preferred_dialect: str | None = None
) -> tuple[exp.Query | None, str]:
    """Parse one read-only SELECT/WITH/UNION statement, returning (tree, dialect)."""
    if not isinstance(sql, str) or not sql.strip():
        return None, "doris"
    norm_pref = (preferred_dialect or "").strip().lower()
    dialects: list[str] = []
    if norm_pref in _RECOGNIZED_DIALECTS:
        dialects.append(norm_pref)
    for d in _SQL_DIALECTS:
        if d not in dialects:
            dialects.append(d)

    for dialect in dialects:
        try:
            statements = sqlglot.parse(sql, read=dialect or None)
        except sqlglot.errors.SqlglotError:
            continue
        except ValueError:
            continue
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            return None, dialect or "doris"
        tree = statements[0]
        if any(isinstance(node, _FORBIDDEN_QUERY_NODES) for node in tree.walk()):
            return None, dialect or "doris"
        return tree, dialect or "doris"
    return None, "doris"


def _scrub_identifier_name(name: str) -> str:
    scrubbed = scrub_sensitive_text(name)
    if scrubbed != name:
        return "redacted_alias"
    return name


def _scrub_ast_node(node: exp.Expression) -> exp.Expression:
    node.comments = None
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
        return exp.Placeholder()
    if isinstance(node, exp.Literal):
        return exp.Placeholder()
    if isinstance(node, (exp.HexString, exp.BitString, exp.National)):
        return exp.Placeholder()
    if isinstance(node, exp.In):
        exprs = node.expressions
        if exprs and all(
            isinstance(item, (exp.Placeholder, exp.Literal))
            or (isinstance(item, exp.Neg) and isinstance(item.this, exp.Literal))
            for item in exprs
        ):
            node.set("expressions", [exp.Placeholder()])
    if isinstance(node, exp.Alias):
        raw_alias = node.alias
        if raw_alias:
            cleaned = _scrub_identifier_name(raw_alias)
            if cleaned != raw_alias:
                node.set("alias", exp.to_identifier(cleaned))
    if isinstance(node, exp.TableAlias):
        raw_alias = node.name
        if raw_alias:
            cleaned = _scrub_identifier_name(raw_alias)
            if cleaned != raw_alias:
                node.set("this", exp.to_identifier(cleaned))
    return node


def scrub_sql_ast(tree: exp.Expression) -> exp.Expression:
    """Return a deep-copied AST with comments stripped and all literals replaced by placeholders."""
    copied = tree.copy()
    transformed = copied.transform(_scrub_ast_node)
    for node in transformed.walk():
        node.comments = None
    return transformed


def scrub_and_fingerprint_sql(
    sql: str, *, dialect: str | None = None
) -> tuple[str, str, exp.Query | None, str]:
    """Parse, scrub comments/literals/secrets/PII, and return (template_sql, fingerprint, raw_tree, dialect)."""
    raw_tree, used_dialect = _parse_readonly_query(sql, preferred_dialect=dialect)
    if raw_tree is None:
        return "", "", None, used_dialect
    scrubbed_tree = scrub_sql_ast(raw_tree)
    try:
        template_sql = scrubbed_tree.sql(dialect="doris", comments=False)
    except Exception:  # noqa: BLE001 - fallback to generic serialization
        template_sql = scrubbed_tree.sql(comments=False)
    template_sql = scrub_sensitive_text(template_sql)
    fingerprint = hashlib.sha256(template_sql.encode("utf-8")).hexdigest()
    return template_sql, fingerprint, raw_tree, used_dialect


@dataclass(frozen=True)
class QueryTemplateCluster:
    """Cluster of structurally equivalent scrubbed queries above min_frequency."""

    fingerprint: str
    template_sql: str
    tables: list[str]
    execution_count: int
    distinct_callers: int
    last_executed_at: datetime | None = None
    dialect: str = "doris"

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "template_sql": self.template_sql,
            "tables": list(self.tables),
            "execution_count": self.execution_count,
            "distinct_callers": self.distinct_callers,
            "last_executed_at": (
                self.last_executed_at.isoformat()
                if self.last_executed_at is not None
                else None
            ),
            "dialect": self.dialect,
        }


@dataclass(frozen=True)
class JoinPairSignal:
    """Frequent equi-join column pair extracted from query history."""

    from_table: str
    from_catalog: str | None
    from_database: str
    from_table_name: str
    from_columns: list[str]
    to_table: str
    to_catalog: str | None
    to_database: str
    to_table_name: str
    to_columns: list[str]
    execution_count: int
    distinct_callers: int
    source_fingerprints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_table": self.from_table,
            "from_catalog": self.from_catalog,
            "from_database": self.from_database,
            "from_table_name": self.from_table_name,
            "from_columns": list(self.from_columns),
            "to_table": self.to_table,
            "to_catalog": self.to_catalog,
            "to_database": self.to_database,
            "to_table_name": self.to_table_name,
            "to_columns": list(self.to_columns),
            "execution_count": self.execution_count,
            "distinct_callers": self.distinct_callers,
            "source_fingerprints": list(self.source_fingerprints),
        }


@dataclass
class MetricCandidateSignal:
    """Frequent aggregation/metric expression candidate, preserving conflicting caliber variants."""

    key: str
    topic: str
    name: str
    description: str
    expression: str
    filters: list[str]
    tables: list[str]
    dialect: str
    execution_count: int
    distinct_callers: int
    variant_index: int = 1
    has_conflicting_variants: bool = False
    boundary_diff: str | None = None
    source_fingerprints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "topic": self.topic,
            "name": self.name,
            "description": self.description,
            "expression": self.expression,
            "filters": list(self.filters),
            "tables": list(self.tables),
            "dialect": self.dialect,
            "execution_count": self.execution_count,
            "distinct_callers": self.distinct_callers,
            "variant_index": self.variant_index,
            "has_conflicting_variants": self.has_conflicting_variants,
            "boundary_diff": self.boundary_diff,
            "source_fingerprints": list(self.source_fingerprints),
        }


@dataclass
class FilterRuleCandidateSignal:
    """Frequent non-partition default WHERE filter rule on a physical table."""

    table: str
    catalog: str | None
    database: str
    table_name: str
    column: str
    predicate: str
    instruction: str
    execution_count: int
    template_count: int
    table_execution_count: int
    co_occurrence_ratio: float
    distinct_callers: int
    source_fingerprints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "catalog": self.catalog,
            "database": self.database,
            "table_name": self.table_name,
            "column": self.column,
            "predicate": self.predicate,
            "instruction": self.instruction,
            "execution_count": self.execution_count,
            "template_count": self.template_count,
            "table_execution_count": self.table_execution_count,
            "co_occurrence_ratio": self.co_occurrence_ratio,
            "distinct_callers": self.distinct_callers,
            "source_fingerprints": list(self.source_fingerprints),
        }


@dataclass(frozen=True)
class QueryHistoryMiningResult:
    """Complete structured output of offline query-history mining."""

    templates: list[QueryTemplateCluster]
    join_pairs: list[JoinPairSignal]
    metrics: list[MetricCandidateSignal]
    filter_rules: list[FilterRuleCandidateSignal]
    stats: dict[str, int]
    llm_used: bool = False
    llm_fallback_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "templates": [t.to_dict() for t in self.templates],
            "join_pairs": [j.to_dict() for j in self.join_pairs],
            "metrics": [m.to_dict() for m in self.metrics],
            "filter_rules": [f.to_dict() for f in self.filter_rules],
            "stats": dict(self.stats),
            "llm_used": self.llm_used,
            "llm_fallback_reason": self.llm_fallback_reason,
        }


@dataclass(frozen=True)
class _TableInfo:
    canonical_name: str
    catalog: str | None
    database: str
    name: str
    fields_by_lower: dict[str, dict[str, Any]]
    partition_cols: frozenset[str]


def _canonical_table_name(metadata: Mapping[str, Any]) -> str:
    catalog = str(metadata.get("catalog") or "").strip()
    database = str(metadata.get("database") or "").strip()
    name = str(metadata.get("name") or "").strip()
    return ".".join(part for part in (catalog, database, name) if part)


def _build_table_index(
    tables: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[int, _TableInfo]]:
    lookup = table_catalog.table_lookup(tables)
    by_id: dict[int, _TableInfo] = {}
    for meta in tables:
        if not isinstance(meta, Mapping):
            continue
        db_name = str(meta.get("database") or "").strip()
        tbl_name = str(meta.get("name") or "").strip()
        if not db_name or not tbl_name:
            continue
        cat = str(meta.get("catalog") or "").strip() or None
        fields_by_lower: dict[str, dict[str, Any]] = {}
        partition_cols: set[str] = set()
        for f in meta.get("fields") or ():
            if not isinstance(f, Mapping):
                continue
            fname = str(f.get("name") or "").strip()
            if not fname:
                continue
            fields_by_lower[fname.lower()] = dict(f)
            if f.get("partition") is True:
                partition_cols.add(fname.lower())
        by_id[id(meta)] = _TableInfo(
            canonical_name=_canonical_table_name(meta),
            catalog=cat,
            database=db_name,
            name=tbl_name,
            fields_by_lower=fields_by_lower,
            partition_cols=frozenset(partition_cols),
        )
    return lookup, by_id


def _match_history_table(
    table_node: exp.Table,
    lookup: dict[str, Any],
    by_id: dict[int, _TableInfo],
    record_db: str | None,
) -> _TableInfo | None:
    if not isinstance(table_node.this, exp.Identifier):
        return None
    meta, reason, candidates = table_catalog.resolve_table(table_node, lookup)
    if meta is not None:
        return by_id.get(id(meta))
    if reason == "unqualified" and len(candidates) == 1:
        return by_id.get(id(candidates[0]))
    if not table_node.db and record_db:
        qualified = exp.Table(
            this=table_node.this.copy(),
            db=exp.to_identifier(record_db),
        )
        meta2, reason2, candidates2 = table_catalog.resolve_table(qualified, lookup)
        if meta2 is not None:
            return by_id.get(id(meta2))
        if reason2 == "unqualified" and len(candidates2) == 1:
            return by_id.get(id(candidates2[0]))
    return None


def _resolve_scope_tables(
    scope,
    lookup: dict[str, Any],
    by_id: dict[int, _TableInfo],
    record_db: str | None,
) -> dict[str, _TableInfo]:
    alias_map: dict[str, _TableInfo] = {}
    for alias, (_, source) in scope.selected_sources.items():
        if not isinstance(source, exp.Table):
            continue
        info = _match_history_table(source, lookup, by_id, record_db)
        if info is not None:
            alias_map[alias.lower()] = info
            alias_map.setdefault(source.name.lower(), info)
    return alias_map


def _parse_timestamp(raw: str | None) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00") if text.endswith("Z") else text
    for candidate in (normalized, normalized.replace(" ", "T")):
        try:
            dt = datetime.fromisoformat(candidate)
            return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
        except ValueError:
            continue
    return None


def _conjunctive_terms(expr: exp.Expression | None) -> list[exp.Expression]:
    terms: list[exp.Expression] = []

    def collect(node: exp.Expression | None) -> None:
        if node is None:
            return
        if isinstance(node, exp.Paren):
            collect(node.this)
        elif isinstance(node, exp.And):
            collect(node.this)
            collect(node.expression)
        elif not isinstance(node, (exp.Or, exp.Not)):
            terms.append(node)

    collect(expr)
    return terms


def _resolve_column(
    col: exp.Column, alias_map: Mapping[str, _TableInfo]
) -> tuple[str, _TableInfo, str, dict[str, Any]] | None:
    col_name = (col.name or "").strip()
    if not col_name or isinstance(col.this, exp.Star):
        return None
    col_lower = col_name.lower()
    if col.table:
        alias = col.table.lower()
        info = alias_map.get(alias)
        if info is None:
            return None
        field_meta = info.fields_by_lower.get(col_lower)
        if field_meta is None:
            return None
        return alias, info, str(field_meta["name"]), field_meta
    unique_infos = {
        info.canonical_name: (alias, info) for alias, info in alias_map.items()
    }
    matches = [
        (
            alias,
            info,
            str(info.fields_by_lower[col_lower]["name"]),
            info.fields_by_lower[col_lower],
        )
        for alias, info in unique_infos.values()
        if col_lower in info.fields_by_lower
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _is_partition_or_time_filter(
    term: exp.Expression,
    info: _TableInfo,
    col_name: str,
    field_meta: Mapping[str, Any],
) -> bool:
    col_lower = col_name.lower()
    if col_lower in info.partition_cols:
        return True
    raw_type = str(field_meta.get("type") or field_meta.get("raw_type") or "").lower()
    is_date_type = any(hint in raw_type for hint in _DATE_TYPE_HINTS)
    is_time_name = bool(_TIME_COL_NAME_RE.fullmatch(col_lower))
    if isinstance(term, _RANGE_PREDICATES) and (is_date_type or is_time_name):
        return True
    if is_date_type and isinstance(term, (exp.EQ, exp.In)):
        return True
    return bool(is_time_name and isinstance(term, (exp.EQ, exp.In)))


def _normalize_single_table_predicate(
    term: exp.Expression,
    col_name: str,
) -> str | None:
    """Return scrubbed SQL for a single-table predicate using the canonical unqualified column."""
    scrubbed = scrub_sql_ast(term)
    for col in scrubbed.find_all(exp.Column):
        col.set("table", None)
        col.set("db", None)
        col.set("catalog", None)
        if col.name.lower() == col_name.lower():
            col.set("this", exp.to_identifier(col_name))
    try:
        rendered = scrubbed.sql(dialect="doris", comments=False)
    except Exception:  # noqa: BLE001
        return None
    return scrub_sensitive_text(rendered)


def _predicate_value_digest(raw_term: exp.Expression) -> str | None:
    """Compute an in-memory-only hash of literal values in an equality/IN business predicate."""
    if not isinstance(raw_term, (exp.EQ, exp.NEQ, exp.In)):
        return None
    literals: list[str] = []
    if isinstance(raw_term, exp.In):
        for item in raw_term.expressions:
            if isinstance(item, exp.Literal):
                literals.append(str(item.this))
            elif isinstance(item, exp.Neg) and isinstance(item.this, exp.Literal):
                literals.append(f"-{item.this.this}")
    else:
        rhs = raw_term.expression
        lhs = raw_term.this
        lit = rhs if isinstance(rhs, (exp.Literal, exp.Neg)) else lhs
        if isinstance(lit, exp.Literal):
            literals.append(str(lit.this))
        elif isinstance(lit, exp.Neg) and isinstance(lit.this, exp.Literal):
            literals.append(f"-{lit.this.this}")
    if not literals:
        return None
    joined = "|".join(sorted(literals))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _extract_single_table_filters(
    select_expr: exp.Expression,
    alias_map: Mapping[str, _TableInfo],
) -> list[tuple[_TableInfo, str, str, str | None]]:
    """Extract (table_info, col_name, scrubbed_predicate, raw_value_digest) from WHERE."""
    where = select_expr.args.get("where")
    if where is None:
        return []
    extracted: list[tuple[_TableInfo, str, str, str | None]] = []
    for term in _conjunctive_terms(where.this):
        cols = list(term.find_all(exp.Column))
        if not cols:
            continue
        resolved = [_resolve_column(c, alias_map) for c in cols]
        if any(r is None for r in resolved):
            continue
        table_names = {r[1].canonical_name for r in resolved if r is not None}
        if len(table_names) != 1:
            continue
        # Require a predicate comparing column against literal/NULL/list (not col = col).
        if (
            isinstance(term, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE))
            and isinstance(term.this, exp.Column)
            and isinstance(term.expression, exp.Column)
        ):
            continue
        _, info, col_name, field_meta = resolved[0]
        if _is_partition_or_time_filter(term, info, col_name, field_meta):
            continue
        scrubbed_pred = _normalize_single_table_predicate(term, col_name)
        if not scrubbed_pred:
            continue
        val_hash = _predicate_value_digest(term)
        extracted.append((info, col_name, scrubbed_pred, val_hash))
    return extracted


def _is_partition_join_pair(
    left_info: _TableInfo,
    left_col: str,
    right_info: _TableInfo,
    right_col: str,
) -> bool:
    left_is_part = left_col.lower() in left_info.partition_cols
    right_is_part = right_col.lower() in right_info.partition_cols
    if left_is_part and right_is_part:
        return True
    return bool(
        (left_is_part or right_is_part) and left_col.lower() == right_col.lower()
    )


def _canonicalize_join_direction(
    left_info: _TableInfo,
    left_cols: list[str],
    right_info: _TableInfo,
    right_cols: list[str],
) -> tuple[_TableInfo, list[str], _TableInfo, list[str]]:
    """Order (from, to) deterministically, preferring child(*_id) -> parent(id)."""
    left_is_pk = len(left_cols) == 1 and left_cols[0].lower() == "id"
    right_is_pk = len(right_cols) == 1 and right_cols[0].lower() == "id"
    if left_is_pk and not right_is_pk:
        return right_info, right_cols, left_info, left_cols
    if right_is_pk and not left_is_pk:
        return left_info, left_cols, right_info, right_cols

    left_key = (left_info.canonical_name, tuple(left_cols))
    right_key = (right_info.canonical_name, tuple(right_cols))
    if right_key < left_key:
        return right_info, right_cols, left_info, left_cols
    return left_info, left_cols, right_info, right_cols


def _extract_scope_join_pairs(
    scope,
    alias_map: Mapping[str, _TableInfo],
) -> list[tuple[_TableInfo, list[str], _TableInfo, list[str]]]:
    """Extract non-partition, non-trivial equi-join column pairs from JOIN ON & WHERE."""
    predicates: list[exp.Expression] = []
    for join in scope.expression.args.get("joins") or ():
        on_expr = join.args.get("on")
        if on_expr is not None:
            predicates.extend(_conjunctive_terms(on_expr))
    where = scope.expression.args.get("where")
    if where is not None:
        predicates.extend(_conjunctive_terms(where.this))

    grouped: dict[
        tuple[str, str],
        tuple[_TableInfo, _TableInfo, list[tuple[str, str]]],
    ] = {}

    for term in predicates:
        if not isinstance(term, exp.EQ):
            continue
        if not isinstance(term.this, exp.Column) or not isinstance(
            term.expression, exp.Column
        ):
            continue
        left = _resolve_column(term.this, alias_map)
        right = _resolve_column(term.expression, alias_map)
        if left is None or right is None:
            continue
        l_alias, l_info, l_col, _ = left
        r_alias, r_info, r_col, _ = right
        if (
            l_info.canonical_name == r_info.canonical_name
            and l_col.lower() == r_col.lower()
        ):
            continue
        if _is_partition_join_pair(l_info, l_col, r_info, r_col):
            continue

        if (r_alias, r_info.canonical_name) < (l_alias, l_info.canonical_name):
            l_alias, l_info, l_col, r_alias, r_info, r_col = (
                r_alias,
                r_info,
                r_col,
                l_alias,
                l_info,
                l_col,
            )
        pair_key = (l_alias, r_alias)
        if pair_key not in grouped:
            grouped[pair_key] = (l_info, r_info, [])
        col_pair = (l_col, r_col)
        if col_pair not in grouped[pair_key][2]:
            grouped[pair_key][2].append(col_pair)

    results: list[tuple[_TableInfo, list[str], _TableInfo, list[str]]] = []
    for l_info, r_info, col_pairs in grouped.values():
        if not col_pairs:
            continue
        col_pairs_sorted = sorted(
            col_pairs, key=lambda pair: (pair[0].lower(), pair[1].lower())
        )
        l_cols = [p[0] for p in col_pairs_sorted]
        r_cols = [p[1] for p in col_pairs_sorted]
        results.append(_canonicalize_join_direction(l_info, l_cols, r_info, r_cols))
    return results


def _normalize_metric_expression(
    expr: exp.Expression,
    alias_map: Mapping[str, _TableInfo],
) -> tuple[str, list[str]]:
    """Scrub a metric aggregation expression and return (scrubbed_sql, referenced_tables)."""
    target = expr.this if isinstance(expr, exp.Alias) else expr
    scrubbed = scrub_sql_ast(target)
    referenced: set[str] = set()
    unique_tables = {info.canonical_name for info in alias_map.values()}
    single_table = len(unique_tables) == 1

    for col in scrubbed.find_all(exp.Column):
        if isinstance(col.this, exp.Star):
            continue
        resolved = _resolve_column(col, alias_map)
        if resolved is not None:
            _, info, canonical_col, _ = resolved
            referenced.add(info.canonical_name)
            col.set("this", exp.to_identifier(canonical_col))
            if single_table:
                col.set("table", None)
                col.set("db", None)
                col.set("catalog", None)
            else:
                col.set("table", exp.to_identifier(info.name))
                col.set("db", None)
                col.set("catalog", None)
    if not referenced and unique_tables:
        referenced.update(unique_tables)
    try:
        rendered = scrubbed.sql(dialect="doris", comments=False)
    except Exception:  # noqa: BLE001
        rendered = scrubbed.sql(comments=False)
    return scrub_sensitive_text(rendered), sorted(referenced)


def _derive_metric_topic(
    proj: exp.Expression,
    expr_sql: str,
    tables: Sequence[str],
) -> str:
    if isinstance(proj, exp.Alias) and proj.alias:
        cleaned = _scrub_identifier_name(proj.alias).strip()
        if cleaned and cleaned != "redacted_alias":
            return cleaned.lower()
    primary = tables[0].split(".")[-1] if tables else "metric"
    agg = proj.find(exp.AggFunc)
    agg_name = type(agg).__name__.lower() if agg is not None else "agg"
    cols = [
        c.name.lower()
        for c in proj.find_all(exp.Column)
        if c.name and not isinstance(c.this, exp.Star)
    ]
    suffix = "_".join(dict.fromkeys(cols[:2])) or "all"
    base = f"{primary}__{agg_name}_{suffix}"
    if _SAFE_ALIAS_RE.fullmatch(base):
        return base
    return f"metric_{hashlib.sha256(expr_sql.encode('utf-8')).hexdigest()[:8]}"


def _default_metric_name(topic: str, variant_index: int, total_variants: int) -> str:
    base = topic.replace("__", " / ")
    if total_variants > 1:
        return f"{base}（口径变体 {variant_index}）"
    return base


def _build_boundary_diff(
    current_expr: str,
    current_filters: tuple[str, ...],
    siblings: Sequence[tuple[str, tuple[str, ...], str]],
    current_cohort: str,
) -> str:
    parts: list[str] = []
    other_exprs = sorted({e for e, _, _ in siblings if e != current_expr})
    if other_exprs:
        parts.append(
            f"聚合计算式存在差异：当前变体为 `{current_expr}`，其他变体包含 "
            + "、".join(f"`{e}`" for e in other_exprs[:3])
        )
    other_filter_sets = sorted({f for _, f, _ in siblings if f != current_filters})
    if other_filter_sets:
        curr_desc = (
            " AND ".join(current_filters) if current_filters else "无固定业务过滤"
        )
        other_descs = [
            " AND ".join(fs) if fs else "无固定业务过滤" for fs in other_filter_sets[:3]
        ]
        parts.append(
            f"业务过滤条件存在差异：当前变体为 [{curr_desc}]，其他变体包含 "
            + "、".join(f"[{d}]" for d in other_descs)
        )
    if not parts and any(c != current_cohort for _, _, c in siblings):
        parts.append(
            "同列业务过滤取值存在多个高频互斥分支（字面量已脱敏），需人工核验并保留独立口径边界"
        )
    return "；".join(parts)


@dataclass
class _ClusterAccumulator:
    fingerprint: str
    template_sql: str
    dialect: str
    tables: set[str] = field(default_factory=set)
    execution_count: int = 0
    callers: set[str] = field(default_factory=set)
    last_executed_at: datetime | None = None
    # Kept strictly in memory during mining, never serialized or returned:
    sample_tree: exp.Query | None = None
    sample_record_db: str | None = None
    raw_filter_signatures: list[tuple[tuple[str, ...], tuple[tuple[str, str], ...]]] = (
        field(default_factory=list)
    )


def _resolve_discovery_llm_provider(
    provider_id: str | None,
    model: str | None,
    override_provider: Any | None = None,
) -> tuple[Any | None, str | None]:
    if override_provider is not None:
        chosen = model or (
            override_provider.models[0]
            if getattr(override_provider, "models", None)
            else None
        )
        return override_provider, chosen
    if provider_id:
        with sessions()() as db:
            row = db.get(Provider, provider_id)
            if row is None:
                return None, model
            cfg = provider_config(row)
            chosen = model or (cfg.models[0] if cfg.models else None)
            return cfg, chosen
    try:
        from .ask_api import _choices

        _, default_pid, default_model = _choices()
        with sessions()() as db:
            row = db.get(Provider, default_pid)
            if row is not None:
                cfg = provider_config(row)
                return cfg, model or default_model
    except Exception:
        logger.debug(
            "Default assistant provider unavailable for discovery naming fallback",
            exc_info=True,
        )
    return None, model


def _enrich_candidates_with_llm(
    *,
    provider: Any,
    model: str,
    metrics: list[MetricCandidateSignal],
    filter_rules: list[FilterRuleCandidateSignal],
    tables: Sequence[Mapping[str, Any]],
    timeout_seconds: float,
) -> tuple[bool, str | None]:
    """Optionally refine Chinese names/descriptions using strictly scrubbed metadata & templates."""
    if not metrics and not filter_rules:
        return False, "no_candidates"

    table_summary = [
        {
            "table": _canonical_table_name(t),
            "comment": scrub_sensitive_text(str(t.get("comment") or ""))[:120],
            "fields": [
                str(f.get("name") or "")
                for f in (t.get("fields") or ())[:30]
                if isinstance(f, Mapping) and f.get("name")
            ],
        }
        for t in tables[:30]
        if isinstance(t, Mapping)
    ]
    metric_summary = [
        {
            "key": m.key,
            "topic": m.topic,
            "expression": m.expression,
            "filters": m.filters,
            "tables": m.tables,
            "has_conflicting_variants": m.has_conflicting_variants,
            "boundary_diff": m.boundary_diff,
            "execution_count": m.execution_count,
        }
        for m in metrics[:25]
    ]
    filter_summary = [
        {
            "table": f.table,
            "column": f.column,
            "predicate": f.predicate,
            "execution_count": f.execution_count,
            "template_count": f.template_count,
        }
        for f in filter_rules[:25]
    ]

    prompt = (
        "你是企业数仓知识发现语义命名助手。\n"
        "请根据已脱敏的高频查询结构（严禁编造具体字面量取值）为候选指标与表级过滤规则生成简洁准确的中文名称与业务说明。\n"
        "重要约束：\n"
        "1. 当 has_conflicting_variants 为 true 时，必须为每个变体 key 保留独立名称与边界差异说明，严禁合并或覆盖。\n"
        "2. 返回纯 JSON 对象：\n"
        '{"metrics": {"<key>": {"name": "中文指标名", "description": "中文口径说明"}}, '
        '"filter_rules": {"<table>.<column>|<predicate>": "中文过滤规则说明"}}\n\n'
        f"Tables: {json.dumps(table_summary, ensure_ascii=False)}\n"
        f"Metrics: {json.dumps(metric_summary, ensure_ascii=False)}\n"
        f"FilterRules: {json.dumps(filter_summary, ensure_ascii=False)}\n"
    )
    url = provider_probe._url(provider, model)
    headers = provider_probe._headers(provider)
    payload = evolution_gate._judge_payload(provider, model, prompt)
    for token_key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        if token_key in payload:
            payload[token_key] = 1200

    try:
        with httpx.Client(timeout=timeout_seconds, trust_env=False) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            body = resp.json()
        raw_text = provider_probe._response_text(provider, body).strip()
        parsed = provider_probe.parse_json_object(raw_text)
    except Exception:
        logger.debug(
            "Discovery LLM naming failed; falling back to deterministic labels",
            exc_info=True,
        )
        return False, "llm_call_failed"

    llm_metrics = parsed.get("metrics")
    if isinstance(llm_metrics, Mapping):
        for m in metrics:
            entry = llm_metrics.get(m.key)
            if not isinstance(entry, Mapping):
                continue
            raw_name = entry.get("name")
            if isinstance(raw_name, str) and raw_name.strip():
                m.name = scrub_sensitive_text(raw_name.strip())[:120]
            raw_desc = entry.get("description")
            if isinstance(raw_desc, str) and raw_desc.strip():
                cleaned_desc = scrub_sensitive_text(raw_desc.strip())[:500]
                if (
                    m.has_conflicting_variants
                    and m.boundary_diff
                    and m.boundary_diff not in cleaned_desc
                ):
                    cleaned_desc = f"{cleaned_desc}（边界差异：{m.boundary_diff}）"
                m.description = cleaned_desc

    llm_filters = parsed.get("filter_rules")
    if isinstance(llm_filters, Mapping):
        for f in filter_rules:
            rule_key = f"{f.table}.{f.column}|{f.predicate}"
            raw_inst = llm_filters.get(rule_key)
            if isinstance(raw_inst, str) and raw_inst.strip():
                f.instruction = scrub_sensitive_text(raw_inst.strip())[:400]

    return True, None


def _coerce_record(item: QueryHistoryRecord | Mapping[str, Any]) -> QueryHistoryRecord:
    if isinstance(item, QueryHistoryRecord):
        return item
    return QueryHistoryRecord(
        sql=str(item.get("sql") or ""),
        status=str(item["status"]) if item.get("status") is not None else None,
        executed_at=(
            str(item["executed_at"]) if item.get("executed_at") is not None else None
        ),
        database=str(item["database"]) if item.get("database") is not None else None,
        caller=str(item["caller"]) if item.get("caller") is not None else None,
        dialect=str(item["dialect"]) if item.get("dialect") is not None else None,
    )


def mine_query_history(
    records: Sequence[QueryHistoryRecord | Mapping[str, Any]],
    *,
    tables: Sequence[Mapping[str, Any]] | None = None,
    project_id: str = "default",
    min_frequency: int = 3,
    provider_id: str | None = None,
    model: str | None = None,
    enable_llm: bool = True,
    llm_timeout_seconds: float = 30.0,
    override_provider: Any | None = None,
) -> QueryHistoryMiningResult:
    """Scrub, filter to current-generation tables, cluster by AST fingerprint, and mine signals."""
    if tables is None:
        tables = (
            knowledge.describe(summary=False)
            if project_id in (None, "default")
            else knowledge.describe(summary=False, project_id=project_id)
        )
    lookup, by_id = _build_table_index(tables)

    stats = {
        "total_records": len(records),
        "skipped_unparseable": 0,
        "skipped_non_project_tables": 0,
        "retained_records": 0,
        "total_clusters": 0,
        "filtered_low_frequency_templates": 0,
        "retained_templates": 0,
    }

    accumulators: dict[str, _ClusterAccumulator] = {}

    for raw_item in records:
        rec = _coerce_record(raw_item)
        template_sql, fingerprint, raw_tree, used_dialect = scrub_and_fingerprint_sql(
            rec.sql, dialect=rec.dialect
        )
        if raw_tree is None or not fingerprint:
            stats["skipped_unparseable"] += 1
            continue

        resolved_tables: set[str] = set()
        try:
            scopes = list(traverse_scope(raw_tree))
        except Exception:  # noqa: BLE001
            stats["skipped_unparseable"] += 1
            continue

        for scope in scopes:
            alias_map = _resolve_scope_tables(scope, lookup, by_id, rec.database)
            for info in alias_map.values():
                resolved_tables.add(info.canonical_name)

        if not resolved_tables:
            stats["skipped_non_project_tables"] += 1
            continue

        stats["retained_records"] += 1
        acc = accumulators.get(fingerprint)
        if acc is None:
            acc = _ClusterAccumulator(
                fingerprint=fingerprint,
                template_sql=template_sql,
                dialect=used_dialect,
                sample_tree=raw_tree,
                sample_record_db=rec.database,
            )
            accumulators[fingerprint] = acc

        acc.execution_count += 1
        acc.tables.update(resolved_tables)
        if rec.caller:
            caller_key = hashlib.sha256(
                rec.caller.strip().lower().encode("utf-8")
            ).hexdigest()[:16]
            acc.callers.add(caller_key)
        ts = _parse_timestamp(rec.executed_at)
        if ts is not None and (
            acc.last_executed_at is None or ts > acc.last_executed_at
        ):
            acc.last_executed_at = ts

        # Record in-memory-only hashed value signatures of top-level business filters
        # to detect high-frequency conflicting constant branches without storing raw values.
        root_select = raw_tree if isinstance(raw_tree, exp.Select) else None
        if root_select is not None and scopes:
            root_scope = next(
                (s for s in scopes if s.expression is root_select), scopes[-1]
            )
            root_aliases = _resolve_scope_tables(
                root_scope, lookup, by_id, rec.database
            )
            flts = _extract_single_table_filters(root_select, root_aliases)
            scrubbed_key = tuple(
                sorted({f"{info.canonical_name}:{pred}" for info, _, pred, _ in flts})
            )
            val_sig = tuple(
                sorted(
                    {
                        (f"{info.canonical_name}:{col.lower()}:{pred}", vhash)
                        for info, col, pred, vhash in flts
                        if vhash is not None
                    }
                )
            )
            if val_sig:
                acc.raw_filter_signatures.append((scrubbed_key, val_sig))

    stats["total_clusters"] = len(accumulators)
    threshold = max(1, int(min_frequency))
    retained_accs = [
        acc for acc in accumulators.values() if acc.execution_count >= threshold
    ]
    stats["filtered_low_frequency_templates"] = len(accumulators) - len(retained_accs)
    stats["retained_templates"] = len(retained_accs)

    retained_accs.sort(
        key=lambda a: (-a.execution_count, -len(a.callers), a.fingerprint)
    )

    templates = [
        QueryTemplateCluster(
            fingerprint=acc.fingerprint,
            template_sql=acc.template_sql,
            tables=sorted(acc.tables),
            execution_count=acc.execution_count,
            distinct_callers=len(acc.callers),
            last_executed_at=acc.last_executed_at,
            dialect=acc.dialect,
        )
        for acc in retained_accs
    ]

    # 1. Extract frequent equi-join pairs, 2. metrics (+ variants), 3. default WHERE filters
    join_agg: dict[
        tuple[str, tuple[str, ...], str, tuple[str, ...]],
        dict[str, Any],
    ] = {}
    filter_agg: dict[tuple[str, str, str], dict[str, Any]] = {}
    table_exec_totals: dict[str, int] = defaultdict(int)
    metric_agg: dict[tuple[str, str, tuple[str, ...], str], dict[str, Any]] = {}

    for acc in retained_accs:
        if acc.sample_tree is None:
            continue
        for tname in acc.tables:
            table_exec_totals[tname] += acc.execution_count

        scopes = list(traverse_scope(acc.sample_tree))
        seen_joins_in_template: set[
            tuple[str, tuple[str, ...], str, tuple[str, ...]]
        ] = set()
        seen_filters_in_template: set[tuple[str, str, str]] = set()

        for scope in scopes:
            alias_map = _resolve_scope_tables(
                scope, lookup, by_id, acc.sample_record_db
            )
            if not alias_map:
                continue

            # (a) Equi-join pairs
            for f_info, f_cols, t_info, t_cols in _extract_scope_join_pairs(
                scope, alias_map
            ):
                jkey = (
                    f_info.canonical_name,
                    tuple(f_cols),
                    t_info.canonical_name,
                    tuple(t_cols),
                )
                if jkey in seen_joins_in_template:
                    continue
                seen_joins_in_template.add(jkey)
                slot = join_agg.setdefault(
                    jkey,
                    {
                        "from_info": f_info,
                        "from_cols": f_cols,
                        "to_info": t_info,
                        "to_cols": t_cols,
                        "execution_count": 0,
                        "callers": set(),
                        "fingerprints": [],
                    },
                )
                slot["execution_count"] += acc.execution_count
                slot["callers"].update(acc.callers)
                slot["fingerprints"].append(acc.fingerprint)

            # (c) Non-partition default WHERE filters
            for info, col_name, pred_sql, _ in _extract_single_table_filters(
                scope.expression, alias_map
            ):
                fkey = (info.canonical_name, col_name, pred_sql)
                if fkey in seen_filters_in_template:
                    continue
                seen_filters_in_template.add(fkey)
                fslot = filter_agg.setdefault(
                    fkey,
                    {
                        "info": info,
                        "column": col_name,
                        "predicate": pred_sql,
                        "execution_count": 0,
                        "template_count": 0,
                        "callers": set(),
                        "fingerprints": [],
                    },
                )
                fslot["execution_count"] += acc.execution_count
                fslot["template_count"] += 1
                fslot["callers"].update(acc.callers)
                fslot["fingerprints"].append(acc.fingerprint)

        # (b) Aggregation / metric expressions on top-level SELECT(s)
        top_selects: list[exp.Select] = []
        if isinstance(acc.sample_tree, exp.Select):
            top_selects.append(acc.sample_tree)
        elif isinstance(acc.sample_tree, exp.Union):
            for branch in acc.sample_tree.find_all(exp.Select):
                if branch.parent is acc.sample_tree:
                    top_selects.append(branch)

        # Detect whether this template has >=2 distinct high-frequency constant-value cohorts
        # on the same business filter columns (e.g. status='paid' x4 vs status='completed' x4).
        cohort_counts: dict[tuple[tuple[str, str], ...], int] = defaultdict(int)
        for _, vsig in acc.raw_filter_signatures:
            cohort_counts[vsig] += 1
        qualifying_cohorts = [
            (vsig, cnt) for vsig, cnt in cohort_counts.items() if cnt >= threshold
        ]
        if len(qualifying_cohorts) >= 2:
            cohorts_to_emit = [
                (
                    hashlib.sha256(str(vsig).encode("utf-8")).hexdigest()[:8],
                    cnt,
                )
                for vsig, cnt in sorted(
                    qualifying_cohorts, key=lambda pair: (-pair[1], pair[0])
                )
            ]
        else:
            cohorts_to_emit = [("", acc.execution_count)]

        for sel in top_selects:
            sel_scope = next((s for s in scopes if s.expression is sel), None)
            if sel_scope is None:
                continue
            alias_map = _resolve_scope_tables(
                sel_scope, lookup, by_id, acc.sample_record_db
            )
            if not alias_map:
                continue
            extracted_sel_filters = _extract_single_table_filters(sel, alias_map)
            sel_filters = tuple(
                sorted({pred for _, _, pred, _ in extracted_sel_filters})
            )
            filter_tables = {
                info.canonical_name for info, _, _, _ in extracted_sel_filters
            }
            for proj in sel.expressions:
                if proj.find(exp.AggFunc) is None:
                    continue
                expr_sql, ref_tables = _normalize_metric_expression(proj, alias_map)
                if not ref_tables:
                    continue
                combined_tables = sorted(set(ref_tables) | filter_tables)
                topic = _derive_metric_topic(proj, expr_sql, ref_tables)
                for cohort_id, cohort_exec_count in cohorts_to_emit:
                    mkey = (topic, expr_sql, sel_filters, cohort_id)
                    mslot = metric_agg.setdefault(
                        mkey,
                        {
                            "topic": topic,
                            "expression": expr_sql,
                            "filters": sel_filters,
                            "cohort_id": cohort_id,
                            "tables": set(),
                            "dialect": acc.dialect,
                            "execution_count": 0,
                            "callers": set(),
                            "fingerprints": [],
                        },
                    )
                    mslot["tables"].update(combined_tables)
                    mslot["execution_count"] += cohort_exec_count
                    mslot["callers"].update(acc.callers)
                    if acc.fingerprint not in mslot["fingerprints"]:
                        mslot["fingerprints"].append(acc.fingerprint)

    join_pairs = [
        JoinPairSignal(
            from_table=slot["from_info"].canonical_name,
            from_catalog=slot["from_info"].catalog,
            from_database=slot["from_info"].database,
            from_table_name=slot["from_info"].name,
            from_columns=list(slot["from_cols"]),
            to_table=slot["to_info"].canonical_name,
            to_catalog=slot["to_info"].catalog,
            to_database=slot["to_info"].database,
            to_table_name=slot["to_info"].name,
            to_columns=list(slot["to_cols"]),
            execution_count=slot["execution_count"],
            distinct_callers=len(slot["callers"]),
            source_fingerprints=sorted(slot["fingerprints"]),
        )
        for slot in join_agg.values()
        if slot["execution_count"] >= threshold
    ]
    join_pairs.sort(
        key=lambda j: (
            -j.execution_count,
            -j.distinct_callers,
            j.from_table,
            j.to_table,
            tuple(j.from_columns),
        )
    )

    # Group metrics by topic and preserve all conflicting caliber variants independently.
    by_topic: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for slot in metric_agg.values():
        if slot["execution_count"] >= threshold:
            by_topic[slot["topic"]].append(slot)

    metrics: list[MetricCandidateSignal] = []
    for topic, variants in by_topic.items():
        variants.sort(
            key=lambda s: (
                -s["execution_count"],
                -len(s["callers"]),
                s["expression"],
                s["filters"],
                s["cohort_id"],
            )
        )
        has_conflict = len(variants) > 1
        sibling_specs = [
            (v["expression"], v["filters"], v["cohort_id"]) for v in variants
        ]
        for idx, slot in enumerate(variants, start=1):
            key = f"{topic}__v{idx}" if has_conflict else topic
            name = _default_metric_name(topic, idx, len(variants))
            boundary_diff = (
                _build_boundary_diff(
                    slot["expression"],
                    slot["filters"],
                    sibling_specs,
                    slot["cohort_id"],
                )
                if has_conflict
                else None
            )
            filter_desc = (
                "；默认过滤：" + " AND ".join(slot["filters"])
                if slot["filters"]
                else ""
            )
            base_desc = (
                f"来自查询历史高频聚合表达式 `{slot['expression']}`"
                f"（出现 {slot['execution_count']} 次）{filter_desc}"
            )
            if boundary_diff:
                base_desc = f"{base_desc}。口径边界差异：{boundary_diff}"
            metrics.append(
                MetricCandidateSignal(
                    key=key,
                    topic=topic,
                    name=name,
                    description=base_desc,
                    expression=slot["expression"],
                    filters=list(slot["filters"]),
                    tables=sorted(slot["tables"]),
                    dialect=slot["dialect"],
                    execution_count=slot["execution_count"],
                    distinct_callers=len(slot["callers"]),
                    variant_index=idx,
                    has_conflicting_variants=has_conflict,
                    boundary_diff=boundary_diff,
                    source_fingerprints=sorted(slot["fingerprints"]),
                )
            )
    metrics.sort(key=lambda m: (-m.execution_count, -m.distinct_callers, m.key))

    filter_rules: list[FilterRuleCandidateSignal] = []
    for slot in filter_agg.values():
        if slot["execution_count"] < threshold:
            continue
        info: _TableInfo = slot["info"]
        total_for_table = max(
            slot["execution_count"], table_exec_totals[info.canonical_name]
        )
        ratio = round(slot["execution_count"] / total_for_table, 4)
        instruction = (
            f"查询表 {info.canonical_name} 时高频使用非分区过滤条件 `{slot['predicate']}`"
            f"（出现 {slot['execution_count']} 次，覆盖 {slot['template_count']} 类模板），"
            "请核实是否为默认业务口径约束。"
        )
        filter_rules.append(
            FilterRuleCandidateSignal(
                table=info.canonical_name,
                catalog=info.catalog,
                database=info.database,
                table_name=info.name,
                column=slot["column"],
                predicate=slot["predicate"],
                instruction=instruction,
                execution_count=slot["execution_count"],
                template_count=slot["template_count"],
                table_execution_count=total_for_table,
                co_occurrence_ratio=ratio,
                distinct_callers=len(slot["callers"]),
                source_fingerprints=sorted(slot["fingerprints"]),
            )
        )
    filter_rules.sort(
        key=lambda f: (
            -f.execution_count,
            -f.template_count,
            f.table,
            f.column,
            f.predicate,
        )
    )

    llm_used = False
    llm_fallback_reason: str | None = None
    if enable_llm and (metrics or filter_rules):
        resolved_provider, resolved_model = _resolve_discovery_llm_provider(
            provider_id, model, override_provider=override_provider
        )
        if resolved_provider is not None and resolved_model:
            llm_used, llm_fallback_reason = _enrich_candidates_with_llm(
                provider=resolved_provider,
                model=resolved_model,
                metrics=metrics,
                filter_rules=filter_rules,
                tables=tables,
                timeout_seconds=llm_timeout_seconds,
            )
        else:
            llm_fallback_reason = "no_provider_configured"
    elif not enable_llm:
        llm_fallback_reason = "llm_disabled"

    return QueryHistoryMiningResult(
        templates=templates,
        join_pairs=join_pairs,
        metrics=metrics,
        filter_rules=filter_rules,
        stats=stats,
        llm_used=llm_used,
        llm_fallback_reason=llm_fallback_reason,
    )
