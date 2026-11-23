"""Three-stage candidate foreign-key recall and partition-pruned data probing engine (#304)."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlglot import exp

from . import discovery_executor, knowledge, project_context, sample_api
from .discovery_executor import (
    DiscoveryAuthenticationError,
    DiscoveryExecutionError,
    DiscoverySqlValidationError,
    build_column_node,
    build_table_node,
    ensure_readonly_select_sql,
    execute_readonly_dtb_query,
)
from .query_history_miner import JoinPairSignal

logger = logging.getLogger(__name__)

_TABLE_PREFIX_RE = re.compile(
    r"^(?:ods|dwd|dws|dim|ads|dwm|dwb|fact|stg|tmp|raw|t|tb|tbl)_+",
    re.IGNORECASE,
)
_DIM_TABLE_RE = re.compile(r"^(?:dim_|d_)", re.IGNORECASE)
_KEY_SUFFIX_RE = re.compile(
    r"^(.+?)_(id|key|code|no|uid|uuid|guid|num|sku|sn)$",
    re.IGNORECASE,
)
_PRIMARY_KEY_NAMES = frozenset({"id", "code", "key", "no", "uid", "uuid", "guid"})
_TRIVIAL_DIM_VALUES = frozenset(
    {"0", "1", "true", "false", "y", "n", "yes", "no", "t", "f"}
)

_INTEGER_TYPE_TOKENS = (
    "tinyint",
    "smallint",
    "mediumint",
    "int",
    "integer",
    "bigint",
    "largeint",
    "long",
    "int32",
    "int64",
    "uint",
    "short",
    "byte",
)
_STRING_TYPE_TOKENS = (
    "varchar",
    "nvarchar",
    "char",
    "nchar",
    "string",
    "text",
    "clob",
)
_DECIMAL_TYPE_TOKENS = ("decimal", "numeric", "float", "double", "real", "number")
_DATE_TYPE_TOKENS = ("date", "datetime", "timestamp", "time")
_BOOL_TYPE_TOKENS = ("bool", "boolean")

_TOKEN_SPLIT_RE = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]{2,}")
_STOP_TOKENS = frozenset(
    {
        "id",
        "key",
        "code",
        "no",
        "num",
        "the",
        "of",
        "for",
        "and",
        "or",
        "is",
        "字段",
        "编码",
        "编号",
        "主键",
        "标识",
        "信息",
        "名称",
    }
)


@dataclass(frozen=True)
class _PhysicalTable:
    canonical_name: str
    catalog: str | None
    database: str
    name: str
    raw_metadata: dict[str, Any]
    fields: list[dict[str, Any]]
    fields_by_lower: dict[str, dict[str, Any]]
    partition_cols: frozenset[str]
    normalized_entity: str
    is_dim_table: bool


@dataclass
class FkCandidate:
    """Candidate foreign-key relationship produced by Stage 1 local recall."""

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
    recall_sources: list[str] = field(default_factory=list)
    recall_score: float = 0.0
    query_history_frequency: int = 0
    distinct_callers: int = 0
    name_similarity_score: float = 0.0
    dim_overlap_ratio: float = 0.0

    def relationship_key(self) -> str:
        """Deterministic key suitable for overrides/semantic_overrides.yaml relationships."""
        from_short = f"{self.from_database}_{self.from_table_name}"
        to_short = f"{self.to_database}_{self.to_table_name}"
        cols_part = "_".join(self.from_columns)
        raw = f"fk_{from_short}__{to_short}__{cols_part}"
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw)[:120]

    def target_ref(self) -> str:
        return (
            f"relationship:{self.from_table}:{self.to_table}:{self.relationship_key()}"
        )

    def normalized_pair_key(self) -> frozenset[tuple[tuple[str, str], tuple[str, str]]]:
        return frozenset(
            (
                (self.from_table.lower(), fc.lower()),
                (self.to_table.lower(), tc.lower()),
            )
            if (self.from_table.lower(), fc.lower())
            <= (self.to_table.lower(), tc.lower())
            else (
                (self.to_table.lower(), tc.lower()),
                (self.from_table.lower(), fc.lower()),
            )
            for fc, tc in zip(self.from_columns, self.to_columns, strict=True)
        )


@dataclass(frozen=True)
class FkProbeOutcome:
    """Aggregated Stage 2/3 probing outcome; contains ZERO raw business fact rows."""

    candidate: FkCandidate
    decision: str  # passed | skipped | rejected | failed
    probing_status: str
    reason: str | None = None
    sample_distinct_count: int | None = None
    matched_count: int | None = None
    overlap_ratio: float | None = None
    join_matched: bool | None = None
    probe_partitions: dict[str, dict[str, str]] = field(default_factory=dict)

    def to_evidence(self) -> dict[str, Any]:
        """Serialize strictly aggregated metrics & partition labels (no fact values)."""
        return {
            "from_table": self.candidate.from_table,
            "from_columns": list(self.candidate.from_columns),
            "to_table": self.candidate.to_table,
            "to_columns": list(self.candidate.to_columns),
            "recall_sources": list(self.candidate.recall_sources),
            "recall_score": round(self.candidate.recall_score, 4),
            "query_history_frequency": self.candidate.query_history_frequency,
            "distinct_callers": self.candidate.distinct_callers,
            "sample_distinct_count": self.sample_distinct_count,
            "matched_count": self.matched_count,
            "overlap_ratio": self.overlap_ratio,
            "join_matched": self.join_matched,
            "probe_partitions": {
                tbl: dict(parts) for tbl, parts in self.probe_partitions.items()
            },
            "probing_status": self.probing_status,
        }


def _canonical_table_name(meta: Mapping[str, Any]) -> str:
    cat = str(meta.get("catalog") or "").strip()
    db = str(meta.get("database") or "").strip()
    name = str(meta.get("name") or "").strip()
    return ".".join(part for part in (cat, db, name) if part)


def _singularize(word: str) -> str:
    low = word.lower().strip("_")
    if len(low) > 4 and low.endswith("ies"):
        return low[:-3] + "y"
    if len(low) > 3 and low.endswith(("ses", "xes", "ches", "shes")):
        return low[:-2]
    if len(low) > 2 and low.endswith("s") and not low.endswith(("ss", "us", "is")):
        return low[:-1]
    return low


def _normalize_table_entity(table_name: str) -> str:
    stripped = _TABLE_PREFIX_RE.sub("", table_name.strip().lower())
    parts = [p for p in stripped.split("_") if p]
    if not parts:
        return table_name.strip().lower()
    parts[-1] = _singularize(parts[-1])
    return "_".join(parts)


def _classify_sql_type(field_meta: Mapping[str, Any]) -> str:
    raw = (
        str(field_meta.get("type") or field_meta.get("raw_type") or "").strip().lower()
    )
    if not raw:
        return "unknown"
    base = re.split(r"[\s(<]", raw, maxsplit=1)[0]
    if base in _BOOL_TYPE_TOKENS:
        return "boolean"
    if any(base == tok or base.startswith(tok) for tok in _DATE_TYPE_TOKENS):
        return "date"
    if any(base == tok or base.startswith(tok) for tok in _DECIMAL_TYPE_TOKENS):
        return "decimal"
    if any(base == tok or base.startswith(tok) for tok in _INTEGER_TYPE_TOKENS):
        return "integer"
    if any(base == tok or base.startswith(tok) for tok in _STRING_TYPE_TOKENS):
        return "string"
    return "other"


def _types_compatible(
    left_meta: Mapping[str, Any],
    right_meta: Mapping[str, Any],
    *,
    strict_key_types: bool = True,
) -> bool:
    left = _classify_sql_type(left_meta)
    right = _classify_sql_type(right_meta)
    if strict_key_types:
        allowed = {"integer", "string", "unknown"}
        if left not in allowed or right not in allowed:
            return False
    if left == "unknown" or right == "unknown":
        return True
    return left == right


def _index_physical_tables(
    tables: Sequence[Mapping[str, Any]],
) -> dict[str, _PhysicalTable]:
    indexed: dict[str, _PhysicalTable] = {}
    for meta in tables:
        if not isinstance(meta, Mapping):
            continue
        db_name = str(meta.get("database") or "").strip()
        tbl_name = str(meta.get("name") or "").strip()
        if not db_name or not tbl_name:
            continue
        cat = str(meta.get("catalog") or "").strip() or None
        canonical = _canonical_table_name(meta)
        fields: list[dict[str, Any]] = []
        fields_by_lower: dict[str, dict[str, Any]] = {}
        partition_cols: set[str] = set()
        for raw_f in meta.get("fields") or ():
            if not isinstance(raw_f, Mapping):
                continue
            fname = str(raw_f.get("name") or "").strip()
            if not fname:
                continue
            fdict = dict(raw_f)
            fields.append(fdict)
            fields_by_lower[fname.lower()] = fdict
            if fdict.get("partition") is True:
                partition_cols.add(fname.lower())
        indexed[canonical.lower()] = _PhysicalTable(
            canonical_name=canonical,
            catalog=cat,
            database=db_name,
            name=tbl_name,
            raw_metadata=dict(meta),
            fields=fields,
            fields_by_lower=fields_by_lower,
            partition_cols=frozenset(partition_cols),
            normalized_entity=_normalize_table_entity(tbl_name),
            is_dim_table=bool(_DIM_TABLE_RE.match(tbl_name)),
        )
    return indexed


def _existing_relationship_pairs(
    tables: Sequence[Mapping[str, Any]],
) -> set[frozenset[tuple[tuple[str, str], tuple[str, str]]]]:
    existing: set[frozenset[tuple[tuple[str, str], tuple[str, str]]]] = set()
    for meta in tables:
        if not isinstance(meta, Mapping):
            continue
        for rel in meta.get("relationships") or ():
            if not isinstance(rel, Mapping):
                continue
            from_t = str(rel.get("from_table") or "").strip().lower()
            to_t = str(rel.get("to_table") or "").strip().lower()
            if not from_t and rel.get("from_database") and rel.get("from_table"):
                from_t = ".".join(
                    p
                    for p in (
                        rel.get("from_catalog"),
                        rel.get("from_database"),
                        rel.get("from_table"),
                    )
                    if p
                ).lower()
            from_cols = [
                str(c).strip().lower() for c in (rel.get("from_columns") or ())
            ]
            to_cols = [str(c).strip().lower() for c in (rel.get("to_columns") or ())]
            if (
                not from_t
                or not to_t
                or not from_cols
                or len(from_cols) != len(to_cols)
            ):
                continue
            pair_set = frozenset(
                ((from_t, fc), (to_t, tc))
                if (from_t, fc) <= (to_t, tc)
                else ((to_t, tc), (from_t, fc))
                for fc, tc in zip(from_cols, to_cols, strict=True)
            )
            existing.add(pair_set)
    return existing


def _is_pure_partition_candidate(
    from_tbl: _PhysicalTable,
    from_cols: Sequence[str],
    to_tbl: _PhysicalTable,
    to_cols: Sequence[str],
) -> bool:
    if not from_cols or not to_cols:
        return True
    from_all_part = all(c.lower() in from_tbl.partition_cols for c in from_cols)
    to_all_part = all(c.lower() in to_tbl.partition_cols for c in to_cols)
    if from_all_part or to_all_part:
        return True
    for fc, tc in zip(from_cols, to_cols, strict=True):
        if (
            fc.lower() in from_tbl.partition_cols
            and tc.lower() in to_tbl.partition_cols
        ):
            return True
    return False


def _is_trivial_self_link(
    from_tbl: _PhysicalTable,
    from_cols: Sequence[str],
    to_tbl: _PhysicalTable,
    to_cols: Sequence[str],
) -> bool:
    if from_tbl.canonical_name.lower() != to_tbl.canonical_name.lower():
        return False
    return [c.lower() for c in from_cols] == [c.lower() for c in to_cols]


def _semantic_tokens(col_name: str, comment: str) -> set[str]:
    tokens: set[str] = set()
    for raw in _TOKEN_SPLIT_RE.findall(f"{col_name.replace('_', ' ')} {comment}"):
        low = _singularize(raw.lower())
        if len(low) >= 2 and low not in _STOP_TOKENS:
            tokens.add(low)
    return tokens


def _name_and_semantic_similarity(
    left_tbl: _PhysicalTable,
    left_field: Mapping[str, Any],
    right_tbl: _PhysicalTable,
    right_field: Mapping[str, Any],
) -> tuple[float, bool]:
    """Return (score, left_is_child) for a compatible column pair across two distinct tables."""
    l_name = str(left_field["name"]).lower()
    r_name = str(right_field["name"]).lower()
    l_match = _KEY_SUFFIX_RE.match(l_name)
    r_match = _KEY_SUFFIX_RE.match(r_name)

    # 1. Child table has `<parent_entity>_id` and parent table has `id` (or same suffix PK)
    if l_match and r_name in _PRIMARY_KEY_NAMES:
        l_stem = _singularize(l_match.group(1))
        if (
            l_stem == right_tbl.normalized_entity
            or right_tbl.normalized_entity.endswith(f"_{l_stem}")
            or l_stem.endswith(f"_{right_tbl.normalized_entity}")
        ):
            bonus = 0.05 if right_tbl.is_dim_table else 0.0
            return min(0.95, 0.85 + bonus), True

    if r_match and l_name in _PRIMARY_KEY_NAMES:
        r_stem = _singularize(r_match.group(1))
        if (
            r_stem == left_tbl.normalized_entity
            or left_tbl.normalized_entity.endswith(f"_{r_stem}")
            or r_stem.endswith(f"_{left_tbl.normalized_entity}")
        ):
            bonus = 0.05 if left_tbl.is_dim_table else 0.0
            return min(0.95, 0.85 + bonus), False

    # 2. Exact shared key column (e.g. user_id <-> user_id, city_code <-> city_code)
    if l_name == r_name and l_match:
        stem = _singularize(l_match.group(1))
        left_owns = (
            stem == left_tbl.normalized_entity
            or left_tbl.normalized_entity.endswith(f"_{stem}")
        )
        right_owns = (
            stem == right_tbl.normalized_entity
            or right_tbl.normalized_entity.endswith(f"_{stem}")
        )
        if right_owns and not left_owns:
            return 0.82, True
        if left_owns and not right_owns:
            return 0.82, False
        if right_tbl.is_dim_table and not left_tbl.is_dim_table:
            return 0.78, True
        if left_tbl.is_dim_table and not right_tbl.is_dim_table:
            return 0.78, False
        return 0.70, (left_tbl.canonical_name <= right_tbl.canonical_name)

    # 3. Stem + semantic/comment lexical overlap on key-like columns
    if (l_match or l_name in _PRIMARY_KEY_NAMES) and (
        r_match or r_name in _PRIMARY_KEY_NAMES
    ):
        if l_name in _PRIMARY_KEY_NAMES and r_name in _PRIMARY_KEY_NAMES:
            return 0.0, True
        l_tokens = _semantic_tokens(l_name, str(left_field.get("comment") or ""))
        r_tokens = _semantic_tokens(r_name, str(right_field.get("comment") or ""))
        if l_tokens and r_tokens:
            overlap = l_tokens & r_tokens
            jaccard = len(overlap) / len(l_tokens | r_tokens)
            if jaccard >= 0.5:
                left_is_child = not (
                    l_name in _PRIMARY_KEY_NAMES and r_name not in _PRIMARY_KEY_NAMES
                )
                if right_tbl.is_dim_table and not left_tbl.is_dim_table:
                    left_is_child = True
                elif left_tbl.is_dim_table and not right_tbl.is_dim_table:
                    left_is_child = False
                return round(0.50 + 0.25 * jaccard, 4), left_is_child

    return 0.0, True


def _load_generation_dimension_values(
    indexed_tables: Mapping[str, _PhysicalTable],
    *,
    project_id: str,
    dimension_values_loader: Callable[[str, str, str], Sequence[str]] | None = None,
) -> dict[tuple[str, str], set[str]]:
    """Zero-scan load of existing indexed dimension values for current-generation columns."""
    values_by_col: dict[tuple[str, str], set[str]] = {}

    if dimension_values_loader is None:
        # Try single batched Cypher query first when knowledge.dimension_values is unpatched.
        is_default_loader = (
            getattr(knowledge.dimension_values, "__module__", "") == knowledge.__name__
        )
        if is_default_loader:
            try:
                vals = (
                    knowledge.settings()
                    if project_id in (None, project_context.DEFAULT_PROJECT_ID)
                    else knowledge.settings(project_id)
                )
                proj = project_context.from_values(vals, project_id)
                dim_label = project_context.label("DimensionValue")
                rows = knowledge.execute(
                    knowledge.read_graph(vals),
                    f"MATCH (v:{dim_label} {{generation:$generation}}) "
                    "RETURN v.database AS database, v.table AS table, "
                    "v.field AS field, collect(DISTINCT v.value) AS values",
                    {"generation": proj.generation},
                )
                by_db_tbl: dict[tuple[str, str], list[_PhysicalTable]] = {}
                for tbl in indexed_tables.values():
                    by_db_tbl.setdefault(
                        (tbl.database.lower(), tbl.name.lower()), []
                    ).append(tbl)
                for row in rows:
                    db_l = str(row.get("database") or "").lower()
                    tb_l = str(row.get("table") or "").lower()
                    fd_l = str(row.get("field") or "").lower()
                    raw_vals = row.get("values") or []
                    cleaned = {
                        str(v).strip()
                        for v in raw_vals
                        if isinstance(v, str) and v.strip()
                    }
                    if len(cleaned) < 2:
                        continue
                    for tbl in by_db_tbl.get((db_l, tb_l), ()):
                        if (
                            fd_l in tbl.fields_by_lower
                            and fd_l not in tbl.partition_cols
                        ):
                            values_by_col[(tbl.canonical_name.lower(), fd_l)] = cleaned
                return values_by_col
            except Exception:
                logger.debug(
                    "Batched dimension_values read unavailable for project %s",
                    project_id,
                    exc_info=True,
                )

    def _fetch(db_name: str, tbl_name: str, fld_name: str) -> Sequence[str]:
        if dimension_values_loader is not None:
            return dimension_values_loader(db_name, tbl_name, fld_name)
        if project_id in (None, project_context.DEFAULT_PROJECT_ID):
            return knowledge.dimension_values(db_name, tbl_name, fld_name)
        return knowledge.dimension_values(
            db_name, tbl_name, fld_name, project_id=project_id
        )

    for tbl in indexed_tables.values():
        for field_meta in tbl.fields:
            fname = str(field_meta["name"])
            if fname.lower() in tbl.partition_cols:
                continue
            try:
                raw_vals = _fetch(tbl.database, tbl.name, fname)
            except Exception:
                logger.debug(
                    "Skipping dimension values for %s.%s.%s",
                    tbl.database,
                    tbl.name,
                    fname,
                    exc_info=True,
                )
                continue
            cleaned = {
                str(v).strip()
                for v in (raw_vals or ())
                if isinstance(v, str) and v.strip()
            }
            if len(cleaned) >= 2:
                values_by_col[(tbl.canonical_name.lower(), fname.lower())] = cleaned
    return values_by_col


def _is_trivial_value_set(values: set[str]) -> bool:
    lowered = {v.lower() for v in values}
    return bool(lowered) and lowered.issubset(_TRIVIAL_DIM_VALUES)


def recall_fk_candidates(
    *,
    tables: Sequence[Mapping[str, Any]],
    join_pairs: Sequence[JoinPairSignal] = (),
    project_id: str = "default",
    max_fk_candidates: int = 20,
    dimension_values_loader: Callable[[str, str, str], Sequence[str]] | None = None,
) -> list[FkCandidate]:
    """Stage 1 (zero-scan): three-path local candidate FK recall, dedup, exclusion, and Top-K cap."""
    if max_fk_candidates <= 0 or not tables:
        return []

    indexed = _index_physical_tables(tables)
    if not indexed:
        return []
    existing_rels = _existing_relationship_pairs(tables)

    merged: dict[frozenset[tuple[tuple[str, str], tuple[str, str]]], FkCandidate] = {}

    def _register(
        from_tbl: _PhysicalTable,
        from_cols: list[str],
        to_tbl: _PhysicalTable,
        to_cols: list[str],
        *,
        source: str,
        score_delta: float,
        history_freq: int = 0,
        distinct_callers: int = 0,
        name_sim: float = 0.0,
        dim_overlap: float = 0.0,
        prefer_orientation: bool = False,
    ) -> None:
        if len(from_cols) != len(to_cols) or not from_cols:
            return
        if _is_trivial_self_link(from_tbl, from_cols, to_tbl, to_cols):
            return
        if _is_pure_partition_candidate(from_tbl, from_cols, to_tbl, to_cols):
            return
        cand = FkCandidate(
            from_table=from_tbl.canonical_name,
            from_catalog=from_tbl.catalog,
            from_database=from_tbl.database,
            from_table_name=from_tbl.name,
            from_columns=list(from_cols),
            to_table=to_tbl.canonical_name,
            to_catalog=to_tbl.catalog,
            to_database=to_tbl.database,
            to_table_name=to_tbl.name,
            to_columns=list(to_cols),
            recall_sources=[source],
            recall_score=score_delta,
            query_history_frequency=history_freq,
            distinct_callers=distinct_callers,
            name_similarity_score=name_sim,
            dim_overlap_ratio=dim_overlap,
        )
        pkey = cand.normalized_pair_key()
        if pkey in existing_rels:
            return

        slot = merged.get(pkey)
        if slot is None:
            merged[pkey] = cand
            return
        if source not in slot.recall_sources:
            slot.recall_sources.append(source)
        slot.query_history_frequency = max(slot.query_history_frequency, history_freq)
        slot.distinct_callers = max(slot.distinct_callers, distinct_callers)
        slot.name_similarity_score = max(slot.name_similarity_score, name_sim)
        slot.dim_overlap_ratio = max(slot.dim_overlap_ratio, dim_overlap)
        # Corroborating recall paths boost confidence
        slot.recall_score = min(1.5, max(slot.recall_score, score_delta) + 0.18)
        if prefer_orientation:
            slot.from_table = from_tbl.canonical_name
            slot.from_catalog = from_tbl.catalog
            slot.from_database = from_tbl.database
            slot.from_table_name = from_tbl.name
            slot.from_columns = list(from_cols)
            slot.to_table = to_tbl.canonical_name
            slot.to_catalog = to_tbl.catalog
            slot.to_database = to_tbl.database
            slot.to_table_name = to_tbl.name
            slot.to_columns = list(to_cols)

    # Path 1: Query history frequent equi-join pairs
    for jp in join_pairs:
        from_tbl = indexed.get(jp.from_table.lower())
        to_tbl = indexed.get(jp.to_table.lower())
        if from_tbl is None or to_tbl is None:
            continue
        f_cols: list[str] = []
        t_cols: list[str] = []
        valid = True
        for fc, tc in zip(jp.from_columns, jp.to_columns, strict=True):
            f_meta = from_tbl.fields_by_lower.get(fc.lower())
            t_meta = to_tbl.fields_by_lower.get(tc.lower())
            if f_meta is None or t_meta is None:
                valid = False
                break
            f_cols.append(str(f_meta["name"]))
            t_cols.append(str(t_meta["name"]))
        if not valid:
            continue
        freq_bonus = min(0.35, 0.04 * jp.execution_count + 0.04 * jp.distinct_callers)
        _register(
            from_tbl,
            f_cols,
            to_tbl,
            t_cols,
            source="query_history",
            score_delta=round(0.65 + freq_bonus, 4),
            history_freq=jp.execution_count,
            distinct_callers=jp.distinct_callers,
        )

    # Path 2: Physical column name / compatible type / semantic similarity across distinct tables
    table_list = sorted(indexed.values(), key=lambda t: t.canonical_name.lower())
    for i, left_tbl in enumerate(table_list):
        for right_tbl in table_list[i + 1 :]:
            for lf in left_tbl.fields:
                l_col = str(lf["name"])
                if l_col.lower() in left_tbl.partition_cols:
                    continue
                for rf in right_tbl.fields:
                    r_col = str(rf["name"])
                    if r_col.lower() in right_tbl.partition_cols:
                        continue
                    if not _types_compatible(lf, rf, strict_key_types=True):
                        continue
                    sim_score, left_is_child = _name_and_semantic_similarity(
                        left_tbl, lf, right_tbl, rf
                    )
                    if sim_score <= 0.0:
                        continue
                    if left_is_child:
                        _register(
                            left_tbl,
                            [l_col],
                            right_tbl,
                            [r_col],
                            source="name_type_similarity",
                            score_delta=sim_score,
                            name_sim=sim_score,
                            prefer_orientation=True,
                        )
                    else:
                        _register(
                            right_tbl,
                            [r_col],
                            left_tbl,
                            [l_col],
                            source="name_type_similarity",
                            score_delta=sim_score,
                            name_sim=sim_score,
                            prefer_orientation=True,
                        )

    # Path 3: Zero-scan dimension value overlap in current knowledge generation
    dim_values = _load_generation_dimension_values(
        indexed,
        project_id=project_id,
        dimension_values_loader=dimension_values_loader,
    )
    dim_items = sorted(dim_values.items(), key=lambda kv: kv[0])
    for i, ((t_a_key, c_a_low), vals_a) in enumerate(dim_items):
        if _is_trivial_value_set(vals_a):
            continue
        tbl_a = indexed.get(t_a_key)
        if tbl_a is None:
            continue
        f_a = tbl_a.fields_by_lower.get(c_a_low)
        if f_a is None:
            continue
        for (t_b_key, c_b_low), vals_b in dim_items[i + 1 :]:
            if t_a_key == t_b_key or _is_trivial_value_set(vals_b):
                continue
            tbl_b = indexed.get(t_b_key)
            if tbl_b is None:
                continue
            f_b = tbl_b.fields_by_lower.get(c_b_low)
            if f_b is None:
                continue
            if not _types_compatible(f_a, f_b, strict_key_types=False):
                continue
            inter = len(vals_a & vals_b)
            if inter < 2:
                continue
            ratio_a_in_b = inter / len(vals_a)
            ratio_b_in_a = inter / len(vals_b)
            best_ratio = max(ratio_a_in_b, ratio_b_in_a)
            if best_ratio < 0.6:
                continue
            if ratio_a_in_b >= ratio_b_in_a:
                child_tbl, child_col = tbl_a, str(f_a["name"])
                parent_tbl, parent_col = tbl_b, str(f_b["name"])
            else:
                child_tbl, child_col = tbl_b, str(f_b["name"])
                parent_tbl, parent_col = tbl_a, str(f_a["name"])
            _register(
                child_tbl,
                [child_col],
                parent_tbl,
                [parent_col],
                source="dimension_overlap",
                score_delta=round(0.55 + 0.30 * best_ratio, 4),
                dim_overlap=round(best_ratio, 4),
            )

    ranked = sorted(
        merged.values(),
        key=lambda c: (
            -c.recall_score,
            -c.query_history_frequency,
            -len(c.recall_sources),
            c.from_table.lower(),
            tuple(col.lower() for col in c.from_columns),
            c.to_table.lower(),
            tuple(col.lower() for col in c.to_columns),
        ),
    )
    return ranked[:max_fk_candidates]


def _subquery(inner: exp.Query, alias: str) -> exp.Subquery:
    return exp.Subquery(
        this=inner,
        alias=exp.TableAlias(this=exp.Identifier(this=alias, quoted=True)),
    )


def _build_bounded_side_subquery(
    table_identifier: str,
    columns: Sequence[str],
    *,
    partitions: Sequence[Mapping[str, str]],
    sample_limit: int | None,
    alias: str,
) -> exp.Subquery:
    """Build a strictly bounded AST subquery with partition filters and/or LIMIT."""
    tbl_node = build_table_node(table_identifier)
    projections = [
        exp.alias_(
            build_column_node(col),
            exp.Identifier(this=f"k{idx}", quoted=True),
        )
        for idx, col in enumerate(columns)
    ]
    inner = exp.select(*projections).from_(tbl_node)
    for part in partitions:
        part_col = build_column_node(str(part["field"]))
        inner = inner.where(
            exp.EQ(
                this=part_col,
                expression=exp.Literal.string(str(part["value"])),
            )
        )
    for col in columns:
        inner = inner.where(
            exp.Not(this=exp.Is(this=build_column_node(col), expression=exp.Null()))
        )
    if sample_limit is not None:
        inner = inner.limit(sample_limit)
    return _subquery(inner, alias)


def build_inclusion_probe_sql(
    candidate: FkCandidate,
    *,
    from_partitions: Sequence[Mapping[str, str]],
    to_partitions: Sequence[Mapping[str, str]],
    sample_limit: int,
    from_is_partitioned: bool,
    to_is_partitioned: bool,
) -> str:
    """Step 1 AST SQL: aggregate inclusion count (returns ONLY 1 row of 2 integers)."""
    if from_is_partitioned and not from_partitions:
        raise DiscoverySqlValidationError("分区表缺少有效分区过滤，拒绝无分区扫描")
    if to_is_partitioned and not to_partitions:
        raise DiscoverySqlValidationError("分区表缺少有效分区过滤，拒绝无分区扫描")

    child_raw = _build_bounded_side_subquery(
        candidate.from_table,
        candidate.from_columns,
        partitions=from_partitions,
        sample_limit=sample_limit,
        alias="c_raw",
    )
    key_cols = [f"k{idx}" for idx in range(len(candidate.from_columns))]
    child_distinct = _subquery(
        exp.select(*[build_column_node(k) for k in key_cols])
        .distinct()
        .from_(child_raw),
        "c",
    )

    parent_raw = _build_bounded_side_subquery(
        candidate.to_table,
        candidate.to_columns,
        partitions=to_partitions,
        sample_limit=None if to_is_partitioned else sample_limit,
        alias="p_raw",
    )
    parent_distinct = _subquery(
        exp.select(*[build_column_node(k) for k in key_cols])
        .distinct()
        .from_(parent_raw),
        "p",
    )

    on_expr: exp.Expression | None = None
    for k in key_cols:
        eq = exp.EQ(
            this=build_column_node(k, table_alias="c"),
            expression=build_column_node(k, table_alias="p"),
        )
        on_expr = eq if on_expr is None else exp.and_(on_expr, eq)
    assert on_expr is not None

    matched_case = exp.Case(
        ifs=[
            exp.If(
                this=exp.Not(
                    this=exp.Is(
                        this=build_column_node("k0", table_alias="p"),
                        expression=exp.Null(),
                    )
                ),
                true=exp.Literal.number(1),
            )
        ],
        default=exp.Literal.number(0),
    )
    query = (
        exp.select(
            exp.alias_(
                exp.Count(this=exp.Star()),
                exp.Identifier(this="sample_distinct_count", quoted=True),
            ),
            exp.alias_(
                exp.Sum(this=matched_case),
                exp.Identifier(this="matched_count", quoted=True),
            ),
        )
        .from_(child_distinct)
        .join(parent_distinct, on=on_expr, join_type="LEFT")
        .limit(1)
    )
    sql = query.sql(dialect="doris")
    ensure_readonly_select_sql(sql, dialect="doris", require_limit=True)
    return sql


def build_join_existence_probe_sql(
    candidate: FkCandidate,
    *,
    from_partitions: Sequence[Mapping[str, str]],
    to_partitions: Sequence[Mapping[str, str]],
    sample_limit: int,
    from_is_partitioned: bool,
    to_is_partitioned: bool,
) -> str:
    """Step 2 AST SQL: bounded SELECT 1 ... INNER JOIN ... LIMIT 1 existence confirmation."""
    if from_is_partitioned and not from_partitions:
        raise DiscoverySqlValidationError("分区表缺少有效分区过滤，拒绝无分区扫描")
    if to_is_partitioned and not to_partitions:
        raise DiscoverySqlValidationError("分区表缺少有效分区过滤，拒绝无分区扫描")

    child_sub = _build_bounded_side_subquery(
        candidate.from_table,
        candidate.from_columns,
        partitions=from_partitions,
        sample_limit=sample_limit,
        alias="c",
    )
    parent_sub = _build_bounded_side_subquery(
        candidate.to_table,
        candidate.to_columns,
        partitions=to_partitions,
        sample_limit=None if to_is_partitioned else sample_limit,
        alias="p",
    )
    key_cols = [f"k{idx}" for idx in range(len(candidate.from_columns))]
    on_expr: exp.Expression | None = None
    for k in key_cols:
        eq = exp.EQ(
            this=build_column_node(k, table_alias="c"),
            expression=build_column_node(k, table_alias="p"),
        )
        on_expr = eq if on_expr is None else exp.and_(on_expr, eq)
    assert on_expr is not None

    query = (
        exp.select(
            exp.alias_(
                exp.Literal.number(1),
                exp.Identifier(this="matched", quoted=True),
            )
        )
        .from_(child_sub)
        .join(parent_sub, on=on_expr, join_type="INNER")
        .limit(1)
    )
    sql = query.sql(dialect="doris")
    ensure_readonly_select_sql(sql, dialect="doris", require_limit=True)
    return sql


async def _resolve_table_latest_partitions(
    tbl: _PhysicalTable,
    *,
    owner_id: str,
    project_id: str,
    timeout_seconds: float,
    runtime: tuple[Any, dict[str, Any], str],
    cache: dict[str, tuple[list[dict[str, str]] | None, str | None]],
    cache_lock: asyncio.Lock,
) -> tuple[list[dict[str, str]] | None, str | None]:
    """Reuse sample_api partition metadata + knowledge fallback with QueryAudit & fail-closed."""
    if not tbl.partition_cols:
        return [], None

    cache_key = tbl.canonical_name.lower()
    async with cache_lock:
        if cache_key in cache:
            return cache[cache_key]

    reason: str | None = None
    try:
        meta_sql = sample_api._metadata_partition_sql(tbl.database, tbl.name)
        sample_api._validate_metadata_sql(meta_sql, tbl.database, tbl.name)
    except Exception:  # noqa: BLE001 - fail closed if table identifier invalid
        reason = "invalid_partition_metadata_identifier"
    else:
        try:
            res = await execute_readonly_dtb_query(
                owner_id=owner_id,
                sql=meta_sql,
                limit=sample_api.PARTITION_METADATA_LIMIT,
                kind="discovery_probe",
                timeout_seconds=timeout_seconds,
                runtime=runtime,
                require_limit=False,
                project_id=project_id,
            )
        except DiscoveryAuthenticationError:
            raise
        except DiscoveryExecutionError as exc:
            reason = f"metadata_{exc.category}"
        else:
            parsed, malformed = sample_api._partitions_from_metadata(
                tbl.fields, res.columns, res.rows
            )
            if parsed is not None:
                async with cache_lock:
                    cache[cache_key] = (parsed, None)
                return parsed, None
            reason = malformed or "empty_partition_metadata"

    try:
        fallback, _knowledge_reason = sample_api._knowledge_partitions(
            tbl.raw_metadata, tbl.fields, project_id=project_id
        )
    except Exception:  # noqa: BLE001
        fallback = None
    if fallback is not None:
        async with cache_lock:
            cache[cache_key] = (fallback, None)
        return fallback, None

    async with cache_lock:
        cache[cache_key] = (None, reason or "skipped_no_partition")
    return None, reason or "skipped_no_partition"


def _parse_int_cell(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


async def probe_fk_candidates(
    *,
    candidates: Sequence[FkCandidate],
    tables: Sequence[Mapping[str, Any]],
    owner_id: str,
    project_id: str = "default",
    sample_limit: int = 1000,
    min_inclusion_ratio: float = 0.8,
    probe_timeout_seconds: float = 30.0,
    probe_concurrency: int = 2,
    runtime: tuple[Any, dict[str, Any], str] | None = None,
) -> list[FkProbeOutcome]:
    """Stage 2 & 3: bounded partition-pruned two-step probing with concurrency <= 2 and 401 abort."""
    if not candidates:
        return []

    resolved_runtime = (
        runtime
        if runtime is not None
        else discovery_executor.resolve_discovery_dtb_runtime(
            owner_id, project_id=project_id
        )
    )
    indexed = _index_physical_tables(tables)
    effective_concurrency = min(2, max(1, int(probe_concurrency)))
    effective_limit = min(5000, max(100, int(sample_limit)))
    sem = asyncio.Semaphore(effective_concurrency)
    abort_event = asyncio.Event()
    auth_errors: list[DiscoveryAuthenticationError] = []
    partition_cache: dict[str, tuple[list[dict[str, str]] | None, str | None]] = {}
    partition_lock = asyncio.Lock()

    async def _probe_one(cand: FkCandidate) -> FkProbeOutcome | None:
        if abort_event.is_set():
            return None
        from_tbl = indexed.get(cand.from_table.lower())
        to_tbl = indexed.get(cand.to_table.lower())
        if from_tbl is None or to_tbl is None:
            return FkProbeOutcome(
                candidate=cand,
                decision="skipped",
                probing_status="skipped_missing_table",
                reason="missing_table_metadata",
            )

        async with sem:
            if abort_event.is_set():
                return None

            # Partition pruning guard (fail-closed)
            try:
                from_parts, _ = await _resolve_table_latest_partitions(
                    from_tbl,
                    owner_id=owner_id,
                    project_id=project_id,
                    timeout_seconds=probe_timeout_seconds,
                    runtime=resolved_runtime,
                    cache=partition_cache,
                    cache_lock=partition_lock,
                )
                if abort_event.is_set():
                    return None
                to_parts, _ = await _resolve_table_latest_partitions(
                    to_tbl,
                    owner_id=owner_id,
                    project_id=project_id,
                    timeout_seconds=probe_timeout_seconds,
                    runtime=resolved_runtime,
                    cache=partition_cache,
                    cache_lock=partition_lock,
                )
            except DiscoveryAuthenticationError as exc:
                auth_errors.append(exc)
                abort_event.set()
                return None

            if (from_tbl.partition_cols and not from_parts) or (
                to_tbl.partition_cols and not to_parts
            ):
                return FkProbeOutcome(
                    candidate=cand,
                    decision="skipped",
                    probing_status="skipped_no_partition",
                    reason="skipped_no_partition",
                )

            probe_partitions: dict[str, dict[str, str]] = {}
            if from_parts:
                probe_partitions[from_tbl.canonical_name] = {
                    p["field"]: p["value"] for p in from_parts
                }
            if to_parts:
                probe_partitions[to_tbl.canonical_name] = {
                    p["field"]: p["value"] for p in to_parts
                }

            # Step 1: Aggregate inclusion query
            try:
                step1_sql = build_inclusion_probe_sql(
                    cand,
                    from_partitions=from_parts or (),
                    to_partitions=to_parts or (),
                    sample_limit=effective_limit,
                    from_is_partitioned=bool(from_tbl.partition_cols),
                    to_is_partitioned=bool(to_tbl.partition_cols),
                )
                step1_res = await execute_readonly_dtb_query(
                    owner_id=owner_id,
                    sql=step1_sql,
                    limit=1,
                    kind="discovery_probe",
                    timeout_seconds=probe_timeout_seconds,
                    runtime=resolved_runtime,
                    require_limit=True,
                    project_id=project_id,
                )
            except DiscoveryAuthenticationError as exc:
                auth_errors.append(exc)
                abort_event.set()
                return None
            except DiscoveryExecutionError as exc:
                return FkProbeOutcome(
                    candidate=cand,
                    decision="failed",
                    probing_status=f"failed_{exc.category}",
                    reason=f"probe_{exc.category}",
                    probe_partitions=probe_partitions,
                )

            sample_distinct = 0
            matched_count = 0
            if step1_res.rows:
                row0 = step1_res.rows[0]
                lowered_cols = [c.lower() for c in step1_res.columns]
                idx_sample = (
                    lowered_cols.index("sample_distinct_count")
                    if "sample_distinct_count" in lowered_cols
                    else 0
                )
                idx_matched = (
                    lowered_cols.index("matched_count")
                    if "matched_count" in lowered_cols
                    else (1 if len(row0) > 1 else 0)
                )
                sample_distinct = max(0, _parse_int_cell(row0[idx_sample]))
                matched_count = max(0, _parse_int_cell(row0[idx_matched]))
            # Discard step1_res rows immediately after extracting aggregate integers
            del step1_res

            if sample_distinct <= 0:
                return FkProbeOutcome(
                    candidate=cand,
                    decision="rejected",
                    probing_status="rejected_empty_sample",
                    reason="empty_sample",
                    sample_distinct_count=0,
                    matched_count=0,
                    overlap_ratio=0.0,
                    join_matched=False,
                    probe_partitions=probe_partitions,
                )

            overlap_ratio = round(min(1.0, matched_count / sample_distinct), 4)
            if overlap_ratio < min_inclusion_ratio:
                return FkProbeOutcome(
                    candidate=cand,
                    decision="rejected",
                    probing_status="rejected_low_inclusion",
                    reason=f"low_inclusion_ratio:{overlap_ratio:.4f}<{min_inclusion_ratio:.2f}",
                    sample_distinct_count=sample_distinct,
                    matched_count=matched_count,
                    overlap_ratio=overlap_ratio,
                    join_matched=False,
                    probe_partitions=probe_partitions,
                )

            if abort_event.is_set():
                return None

            # Step 2: Partition-pruned / sample-bounded SELECT 1 ... JOIN ... LIMIT 1
            try:
                step2_sql = build_join_existence_probe_sql(
                    cand,
                    from_partitions=from_parts or (),
                    to_partitions=to_parts or (),
                    sample_limit=effective_limit,
                    from_is_partitioned=bool(from_tbl.partition_cols),
                    to_is_partitioned=bool(to_tbl.partition_cols),
                )
                step2_res = await execute_readonly_dtb_query(
                    owner_id=owner_id,
                    sql=step2_sql,
                    limit=1,
                    kind="discovery_probe",
                    timeout_seconds=probe_timeout_seconds,
                    runtime=resolved_runtime,
                    require_limit=True,
                    project_id=project_id,
                )
            except DiscoveryAuthenticationError as exc:
                auth_errors.append(exc)
                abort_event.set()
                return None
            except DiscoveryExecutionError as exc:
                return FkProbeOutcome(
                    candidate=cand,
                    decision="failed",
                    probing_status=f"failed_{exc.category}",
                    reason=f"probe_{exc.category}",
                    sample_distinct_count=sample_distinct,
                    matched_count=matched_count,
                    overlap_ratio=overlap_ratio,
                    probe_partitions=probe_partitions,
                )

            join_matched = len(step2_res.rows) > 0
            del step2_res

            if not join_matched:
                return FkProbeOutcome(
                    candidate=cand,
                    decision="rejected",
                    probing_status="rejected_join_no_match",
                    reason="join_no_match",
                    sample_distinct_count=sample_distinct,
                    matched_count=matched_count,
                    overlap_ratio=overlap_ratio,
                    join_matched=False,
                    probe_partitions=probe_partitions,
                )

            return FkProbeOutcome(
                candidate=cand,
                decision="passed",
                probing_status="passed",
                reason=None,
                sample_distinct_count=sample_distinct,
                matched_count=matched_count,
                overlap_ratio=overlap_ratio,
                join_matched=True,
                probe_partitions=probe_partitions,
            )

    raw_results = await asyncio.gather(*(_probe_one(cand) for cand in candidates))
    if auth_errors:
        raise auth_errors[0]
    return [item for item in raw_results if item is not None]
