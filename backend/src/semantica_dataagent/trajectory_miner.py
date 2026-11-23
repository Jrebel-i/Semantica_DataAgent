"""Trajectory diagnosis miner (#296): isolation, dedup/split, and 5 signal clusters."""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

import sqlglot
from sqlglot import exp

from . import eval_questions, trajectories
from .api_time import as_utc_or_now as _as_utc
from .db import Conversation, QueryExecution, Setting, Turn, sessions

_PUNCT_RE = re.compile(
    r"[\s\.,;:!?\-\"'`~@#$%^&*()_+=\[\]{}|\\/<>，。；：！？、“”‘’（）【】《》、·]+"
)
_FILLER_RE = re.compile(
    r"^(?:请问|麻烦|帮我|帮忙|请|我想知道|想问一下|查一下|查询一下|统计一下)+"
)
_TOKEN_ID_RE = re.compile(r"[A-Za-z0-9_]+")
_CORRECTION_CUE_RE = re.compile(
    r"(不对|不是|错了|有误|搞错|写错|应该用|改用|改为|换成|替换成|漏了|少写了|加上|过滤|条件|口径是|口径为|应该是|instead|wrong|should use|filter by|actually)",
    re.IGNORECASE,
)


def canonical_question(text: str | None) -> str:
    """Normalize question text for holdout exclusion and near-duplicate grouping."""
    cleaned = trajectories.scrub_text(str(text or "")).strip().casefold()
    cleaned = _FILLER_RE.sub("", cleaned)
    return _PUNCT_RE.sub("", cleaned)


def _questions_are_near_duplicate(left: str, right: str) -> bool:
    c_left = canonical_question(left)
    c_right = canonical_question(right)
    if not c_left or not c_right:
        return False
    if c_left == c_right:
        return True
    ids_left = _TOKEN_ID_RE.findall(c_left)
    ids_right = _TOKEN_ID_RE.findall(c_right)
    if ids_left != ids_right:
        return False
    return SequenceMatcher(None, c_left, c_right).ratio() >= 0.90


def _load_holdout_canonical_questions(project_id: str) -> set[str]:
    """Load ONLY canonical holdout question strings to filter them out safely."""
    _, raw_rows = eval_questions._read_raw_rows(project_id)
    out: set[str] = set()
    for row in raw_rows:
        if str(row.get("split") or "").strip().lower() == "holdout":
            q = canonical_question(row.get("question"))
            if q:
                out.add(q)
    return out


def _retention_days_if_configured(db) -> int | None:
    setting = db.get(Setting, "session_retention_days")
    if setting is None or not str(setting.value or "").strip():
        return None
    try:
        days = int(str(setting.value).strip())
        return days if days > 0 else None
    except ValueError:
        return None


def _filter_eligible_trajectories(
    project_id: str,
    rows: list[dict[str, Any]],
    *,
    holdouts: set[str],
    now: datetime,
) -> list[dict[str, Any]]:
    conv_ids = {str(r["conversation_id"]) for r in rows if r.get("conversation_id")}
    resumable_map: dict[str, bool] = {}
    retention_days: int | None = None
    with sessions()() as db:
        retention_days = _retention_days_if_configured(db)
        if conv_ids:
            for cid, resumable in (
                db.query(Conversation.id, Conversation.resumable)
                .filter(Conversation.id.in_(conv_ids))
                .all()
            ):
                resumable_map[cid] = bool(resumable)

    cutoff = (
        now - timedelta(days=retention_days) if retention_days is not None else None
    )
    eligible: list[dict[str, Any]] = []
    for row in rows:
        status = str(row.get("status") or "").lower()
        if status in {"expired", "deleted"}:
            continue
        cid = row.get("conversation_id")
        if cid and cid in resumable_map and not resumable_map[cid]:
            continue
        if cutoff is not None and _as_utc(row.get("last_activity_at")) < cutoff:
            continue
        if canonical_question(row.get("question")) in holdouts:
            continue
        eligible.append(row)
    return eligible


@dataclass(slots=True)
class TrajectoryGroup:
    """Deduplicated group of trajectories (same conversation or near-duplicate question)."""

    anchor_question: str
    canonical_key: str
    trajectories: list[dict[str, Any]]
    latest_recorded_at: datetime
    max_id: int

    @property
    def trajectory_ids(self) -> list[int]:
        return [int(r["id"]) for r in self.trajectories]


@dataclass(slots=True)
class MinedSignal:
    """Actionable signal cluster extracted exclusively from the diagnosis pool."""

    signal_kind: str
    summary: str
    evidence_trajectory_ids: list[int]
    target_hint: str | None = None
    preserve_multi_caliber: bool = False
    caliber_variants: list[dict[str, Any]] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_kind": self.signal_kind,
            "summary": self.summary,
            "evidence_trajectory_ids": list(self.evidence_trajectory_ids),
            "target_hint": self.target_hint,
            "preserve_multi_caliber": self.preserve_multi_caliber,
            "caliber_variants": list(self.caliber_variants),
            "details": dict(self.details),
        }


@dataclass(slots=True)
class MiningResult:
    """Result of mining eligible trajectories after a checkpoint."""

    project_id: str
    after_trajectory_id: int | None
    frozen_max_trajectory_id: int | None
    total_eligible_rows: int
    total_eligible_groups: int
    diagnosis_groups: list[TrajectoryGroup]
    reserve_groups: list[TrajectoryGroup]
    diagnosis_trajectory_ids: list[int]
    reserve_trajectory_ids: list[int]
    signals: list[MinedSignal]
    earliest_eligible_at: datetime | None = None

    def signal_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for sig in self.signals:
            counts[sig.signal_kind] = counts.get(sig.signal_kind, 0) + 1
        return counts

    def to_diagnosis_payload(self) -> dict[str, Any]:
        """Serialize ONLY diagnosis data — reserve trajectories & holdouts are never included."""
        return {
            "project_id": self.project_id,
            "after_trajectory_id": self.after_trajectory_id,
            "frozen_max_trajectory_id": self.frozen_max_trajectory_id,
            "total_eligible_rows": self.total_eligible_rows,
            "total_eligible_groups": self.total_eligible_groups,
            "diagnosis_trajectory_ids": list(self.diagnosis_trajectory_ids),
            "reserve_count": len(self.reserve_trajectory_ids),
            "signal_counts": self.signal_counts(),
            "signals": [s.to_dict() for s in self.signals],
        }


def count_eligible_trajectories_since(
    project_id: str,
    after_id: int | None = None,
    *,
    now: datetime | None = None,
) -> tuple[int, datetime | None, int | None]:
    """Count eligible non-holdout non-expired trajectories after checkpoint."""
    ts = _as_utc(now)
    holdouts = _load_holdout_canonical_questions(project_id)
    rows = trajectories.list_trajectories(project_id, after_id=after_id)
    eligible = _filter_eligible_trajectories(
        project_id, rows, holdouts=holdouts, now=ts
    )
    if not eligible:
        return 0, None, None
    earliest = min(_as_utc(r["recorded_at"]) for r in eligible)
    max_id = max(int(r["id"]) for r in eligible)
    return len(eligible), earliest, max_id


def _dedup_and_split_groups(
    eligible: list[dict[str, Any]],
) -> tuple[list[TrajectoryGroup], list[TrajectoryGroup], list[TrajectoryGroup]]:
    ordered = sorted(eligible, key=lambda r: (_as_utc(r["recorded_at"]), int(r["id"])))

    # 1. Group turns of the same online conversation together; local_mcp windows stay separate.
    conv_buckets: dict[str, list[dict[str, Any]]] = {}
    unit_list: list[list[dict[str, Any]]] = []
    for row in ordered:
        cid = row.get("conversation_id")
        if row.get("source_type") == "online" and cid:
            bucket = conv_buckets.get(cid)
            if bucket is None:
                bucket = []
                conv_buckets[cid] = bucket
                unit_list.append(bucket)
            bucket.append(row)
        else:
            unit_list.append([row])

    # 2. Merge near-duplicate units by anchor question while preserving chronological order.
    groups: list[TrajectoryGroup] = []
    by_canonical: dict[str, TrajectoryGroup] = {}
    for unit in unit_list:
        anchor = ""
        for item in unit:
            q = str(item.get("question") or "").strip()
            if q:
                anchor = q
                break
        canon = canonical_question(anchor)
        matched_group: TrajectoryGroup | None = None
        if canon and canon in by_canonical:
            matched_group = by_canonical[canon]
        elif anchor:
            for existing in groups:
                if _questions_are_near_duplicate(anchor, existing.anchor_question):
                    matched_group = existing
                    break

        unit_latest = max(_as_utc(r["recorded_at"]) for r in unit)
        unit_max_id = max(int(r["id"]) for r in unit)
        if matched_group is None:
            grp = TrajectoryGroup(
                anchor_question=anchor,
                canonical_key=canon or f"anon-{unit_max_id}",
                trajectories=list(unit),
                latest_recorded_at=unit_latest,
                max_id=unit_max_id,
            )
            groups.append(grp)
            if canon:
                by_canonical[canon] = grp
        else:
            matched_group.trajectories.extend(unit)
            matched_group.latest_recorded_at = max(
                matched_group.latest_recorded_at, unit_latest
            )
            matched_group.max_id = max(matched_group.max_id, unit_max_id)

    groups.sort(key=lambda g: (g.latest_recorded_at, g.max_id))
    n = len(groups)
    if n == 0:
        return [], [], []
    if n == 1:
        return groups, groups, []
    reserve_count = max(1, math.ceil(n * 0.30))
    diag_count = max(1, n - reserve_count)
    return groups, groups[:diag_count], groups[diag_count:]


def _extract_sql_tables(sql: str, dialect: str | None = None) -> list[str]:
    if not sql.strip():
        return []
    try:
        tree = sqlglot.parse_one(sql, read=dialect or None)
    except sqlglot.errors.SqlglotError:
        return []
    tables: list[str] = []
    seen: set[str] = set()
    for tbl in tree.find_all(exp.Table):
        parts = [p for p in (tbl.catalog, tbl.db, tbl.name) if p]
        if len(parts) >= 2:
            full = ".".join(parts)
            if full not in seen:
                seen.add(full)
                tables.append(full)
    return tables


def _extract_join_pairs(sql: str, dialect: str | None = None) -> list[dict[str, Any]]:
    if not sql.strip():
        return []
    try:
        tree = sqlglot.parse_one(sql, read=dialect or None)
    except sqlglot.errors.SqlglotError:
        return []
    aliases: dict[str, tuple[str | None, str, str]] = {}
    for tbl in tree.find_all(exp.Table):
        if not tbl.db or not tbl.name:
            continue
        ident = (tbl.catalog or None, tbl.db, tbl.name)
        alias = tbl.alias_or_name
        aliases[alias] = ident
        aliases[tbl.name] = ident

    pairs: list[dict[str, Any]] = []
    for join in tree.find_all(exp.Join):
        on_expr = join.args.get("on")
        if on_expr is None:
            continue
        for eq in on_expr.find_all(exp.EQ):
            left = eq.left
            right = eq.right
            if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
                continue
            ltbl = aliases.get(left.table)
            rtbl = aliases.get(right.table)
            if ltbl and rtbl and ltbl != rtbl and left.name and right.name:
                pairs.append(
                    {
                        "from_catalog": ltbl[0],
                        "from_database": ltbl[1],
                        "from_table": ltbl[2],
                        "from_columns": [left.name],
                        "to_catalog": rtbl[0],
                        "to_database": rtbl[1],
                        "to_table": rtbl[2],
                        "to_columns": [right.name],
                    }
                )
    return pairs


def _extract_where_predicates(sql: str, dialect: str | None = None) -> list[str]:
    if not sql.strip():
        return []
    try:
        tree = sqlglot.parse_one(sql, read=dialect or None)
    except sqlglot.errors.SqlglotError:
        return []
    where = tree.find(exp.Where)
    if where is None or where.this is None:
        return []
    return [where.this.sql(dialect=dialect or None)]


def _mine_search_detours(diag_rows: list[dict[str, Any]]) -> list[MinedSignal]:
    by_target: dict[str, dict[str, Any]] = {}
    for row in diag_rows:
        search_queries: list[str] = []
        for call in row.get("tool_calls") or ():
            if call.get("tool_name") == "search_knowledge":
                args = call.get("arguments") or {}
                q = str(args.get("query") or "").strip()
                if q and q not in search_queries:
                    search_queries.append(q)
        if len(search_queries) < 2:
            continue
        target_ref = None
        for ref in row.get("matched_refs") or ():
            if isinstance(ref, str) and ref.startswith(
                ("table:", "field:", "document:")
            ):
                target_ref = ref
        if target_ref is None:
            for item in row.get("final_sql") or ():
                if isinstance(item, dict) and item.get("sql"):
                    tables = _extract_sql_tables(str(item["sql"]), item.get("dialect"))
                    if tables:
                        target_ref = f"table:{tables[0]}"
                        break
        cluster_key = (
            target_ref or f"topic:{canonical_question(search_queries[0])[:40]}"
        )
        bucket = by_target.setdefault(
            cluster_key,
            {
                "target": target_ref,
                "detour_queries": [],
                "evidence_ids": [],
            },
        )
        for q in search_queries[:-1]:
            if q not in bucket["detour_queries"]:
                bucket["detour_queries"].append(q)
        if row["id"] not in bucket["evidence_ids"]:
            bucket["evidence_ids"].append(row["id"])

    signals: list[MinedSignal] = []
    for data in by_target.values():
        target = data["target"]
        synonyms = data["detour_queries"][:5]
        signals.append(
            MinedSignal(
                signal_kind="search_detour",
                summary=f"检索多词绕路（{', '.join(synonyms)}）→ 补充 {target or '业务同义词'}",
                evidence_trajectory_ids=sorted(data["evidence_ids"]),
                target_hint=target,
                details={"synonyms": synonyms, "target": target},
            )
        )
    return signals


def _mine_sql_self_repairs(diag_rows: list[dict[str, Any]]) -> list[MinedSignal]:
    signals: list[MinedSignal] = []
    for row in diag_rows:
        validations = row.get("sql_validations") or []
        if len(validations) < 2:
            continue
        first_fail = next(
            (
                v
                for v in validations
                if isinstance(v, dict) and v.get("status") != "pass"
            ),
            None,
        )
        if first_fail is None:
            continue
        fail_idx = validations.index(first_fail)
        passing = next(
            (
                v
                for v in validations[fail_idx + 1 :]
                if isinstance(v, dict) and v.get("status") == "pass"
            ),
            None,
        )
        if passing is None:
            continue

        used_task_or_lineage = any(
            call.get("tool_name")
            in {"read_task_sql", "get_lineage", "find_join_path", "describe_tables"}
            for call in (row.get("tool_calls") or ())
        )
        dialect = str(passing.get("dialect") or "doris")
        pass_sql = str(passing.get("sql") or "")
        issues = first_fail.get("issues") or []
        issue_kinds = {
            str(item.get("kind") or "").lower()
            for item in issues
            if isinstance(item, dict)
        }
        join_pairs = _extract_join_pairs(pass_sql, dialect)
        predicates = _extract_where_predicates(pass_sql, dialect)
        tables = _extract_sql_tables(pass_sql, dialect)
        target_hint = f"table:{tables[0]}" if tables else None
        signals.append(
            MinedSignal(
                signal_kind="sql_self_repair",
                summary=(
                    f"validate_sql 先报错（{', '.join(sorted(issue_kinds)) or 'issues'}）后"
                    + ("查任务 SQL " if used_task_or_lineage else "")
                    + "自愈通过"
                ),
                evidence_trajectory_ids=[int(row["id"])],
                target_hint=target_hint,
                details={
                    "issue_kinds": sorted(issue_kinds),
                    "issues": issues,
                    "join_pairs": join_pairs,
                    "where_predicates": predicates,
                    "repaired_sql": pass_sql,
                    "tables": tables,
                    "used_task_or_lineage": used_task_or_lineage,
                },
            )
        )
    return signals


def _mine_multi_turn_corrections(
    diag_groups: list[TrajectoryGroup],
) -> list[MinedSignal]:
    signals: list[MinedSignal] = []
    for grp in diag_groups:
        by_conv: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in grp.trajectories:
            cid = row.get("conversation_id")
            if cid:
                by_conv[str(cid)].append(row)
        for conv_turns in by_conv.values():
            ordered = sorted(
                conv_turns,
                key=lambda r: (r.get("turn_id") or 0, int(r["id"])),
            )
            if len(ordered) < 2:
                continue
            for idx in range(len(ordered) - 1):
                prev_t = ordered[idx]
                next_t = ordered[idx + 1]
                q_next = trajectories.scrub_text(str(next_t.get("question") or ""))
                if not _CORRECTION_CUE_RE.search(q_next):
                    continue
                next_ok = next_t.get("status") == "completed" and (
                    bool(next_t.get("final_sql"))
                    or any(
                        isinstance(v, dict) and v.get("status") == "pass"
                        for v in (next_t.get("sql_validations") or ())
                    )
                )
                if not next_ok:
                    continue
                after_sql = ""
                after_dialect = "doris"
                for blk in next_t.get("final_sql") or ():
                    if isinstance(blk, dict) and blk.get("sql"):
                        after_sql = str(blk["sql"])
                        after_dialect = str(blk.get("dialect") or "doris")
                        break
                tables = _extract_sql_tables(after_sql, after_dialect)
                predicates = _extract_where_predicates(after_sql, after_dialect)
                target_hint = f"table:{tables[0]}" if tables else None
                signals.append(
                    MinedSignal(
                        signal_kind="multi_turn_correction",
                        summary=f"多轮用户纠正：{q_next[:120]}",
                        evidence_trajectory_ids=[int(prev_t["id"]), int(next_t["id"])],
                        target_hint=target_hint,
                        details={
                            "correction_text": q_next,
                            "corrected_tables": tables,
                            "where_predicates": predicates,
                            "after_sql": after_sql,
                            "dialect": after_dialect,
                        },
                    )
                )
    return signals


def _detect_chosen_option(
    row: dict[str, Any],
    next_row: dict[str, Any] | None,
    options: list[dict[str, Any]],
) -> dict[str, Any] | None:
    search_space: list[str] = []
    if next_row is not None:
        search_space.append(str(next_row.get("question") or ""))
        for cit in next_row.get("citations") or ():
            if isinstance(cit, dict) and cit.get("ref"):
                search_space.append(str(cit["ref"]))
        for blk in next_row.get("final_sql") or ():
            if isinstance(blk, dict) and blk.get("sql"):
                search_space.append(str(blk["sql"]))
        search_space.extend(str(r) for r in (next_row.get("matched_refs") or ()))
    else:
        for cit in row.get("citations") or ():
            if isinstance(cit, dict) and cit.get("ref"):
                search_space.append(str(cit["ref"]))
        for blk in row.get("final_sql") or ():
            if isinstance(blk, dict) and blk.get("sql"):
                search_space.append(str(blk["sql"]))
    haystack = "\n".join(search_space).casefold()
    for opt in options:
        if not isinstance(opt, dict):
            continue
        ref = str(opt.get("ref") or "").strip()
        label = str(opt.get("label") or "").strip()
        opt_id = str(opt.get("id") or "").strip()
        if ref and ref.casefold() in haystack:
            return opt
        if label and label.casefold() in haystack:
            return opt
        if opt_id and opt_id.casefold() in haystack:
            return opt
    return None


def _mine_divergent_clarifications(
    diag_rows: list[dict[str, Any]],
) -> list[MinedSignal]:
    by_conv: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in diag_rows:
        if row.get("conversation_id"):
            by_conv[str(row["conversation_id"])].append(row)
    for turns in by_conv.values():
        turns.sort(key=lambda r: (r.get("turn_id") or 0, int(r["id"])))

    clusters: dict[str, dict[str, Any]] = {}
    for row in diag_rows:
        clars = row.get("clarifications") or []
        if not clars:
            continue
        next_row = None
        if row.get("conversation_id"):
            turns = by_conv[str(row["conversation_id"])]
            for i, item in enumerate(turns):
                if item["id"] == row["id"] and i + 1 < len(turns):
                    next_row = turns[i + 1]
                    break
        for clar in clars:
            if not isinstance(clar, dict):
                continue
            for q in clar.get("questions") or ():
                if not isinstance(q, dict):
                    continue
                q_prompt = str(q.get("question") or "").strip()
                options = [o for o in (q.get("options") or ()) if isinstance(o, dict)]
                if len(options) < 2:
                    continue
                chosen = _detect_chosen_option(row, next_row, options)
                if chosen is None:
                    continue
                cluster_key = canonical_question(q_prompt) or "|".join(
                    sorted(str(o.get("label") or "") for o in options)
                )
                bucket = clusters.setdefault(
                    cluster_key,
                    {
                        "prompt": q_prompt,
                        "variants": {},
                        "evidence_ids": set(),
                    },
                )
                bucket["evidence_ids"].add(int(row["id"]))
                if next_row is not None:
                    bucket["evidence_ids"].add(int(next_row["id"]))
                label = str(chosen.get("label") or chosen.get("id") or "")
                ref = str(chosen.get("ref") or "")
                vkey = f"{label}::{ref}"
                var = bucket["variants"].setdefault(
                    vkey,
                    {
                        "label": label,
                        "ref": ref,
                        "count": 0,
                        "evidence_trajectory_ids": [],
                    },
                )
                var["count"] += 1
                tid = int(next_row["id"] if next_row is not None else row["id"])
                if tid not in var["evidence_trajectory_ids"]:
                    var["evidence_trajectory_ids"].append(tid)

    signals: list[MinedSignal] = []
    for data in clusters.values():
        variants = list(data["variants"].values())
        if len(variants) < 2:
            continue
        variants.sort(key=lambda v: (-v["count"], v["label"]))
        target_hint = next((v["ref"] for v in variants if v["ref"]), None)
        signals.append(
            MinedSignal(
                signal_kind="divergent_clarification",
                summary=f"高频分化澄清「{data['prompt']}」存在 {len(variants)} 种独立口径变体",
                evidence_trajectory_ids=sorted(data["evidence_ids"]),
                target_hint=target_hint,
                preserve_multi_caliber=True,
                caliber_variants=variants,
                details={
                    "clarification_prompt": data["prompt"],
                    "preserve_multi_caliber": True,
                },
            )
        )
    return signals


def _mine_gaps_and_failures(
    project_id: str,
    diag_rows: list[dict[str, Any]],
    *,
    reserve_rows: list[dict[str, Any]],
    holdout_conv_ids: set[str],
    holdouts: set[str],
    after_at: datetime | None,
    now: datetime,
) -> list[MinedSignal]:
    signals: list[MinedSignal] = []
    diag_ids = [int(r["id"]) for r in diag_rows]
    reserve_conv_ids = {
        str(r["conversation_id"]) for r in reserve_rows if r.get("conversation_id")
    }
    reserve_questions = {
        canonical_question(r.get("question"))
        for r in reserve_rows
        if canonical_question(r.get("question"))
    }
    excluded_conv_ids = reserve_conv_ids | holdout_conv_ids

    with sessions()() as db:
        retention_days = _retention_days_if_configured(db)
        cutoff = (
            now - timedelta(days=retention_days) if retention_days is not None else None
        )
        qe_query = db.query(QueryExecution).filter(QueryExecution.status == "failed")
        if after_at is not None:
            qe_query = qe_query.filter(QueryExecution.created_at >= after_at)
        failed_qes = qe_query.order_by(QueryExecution.created_at.asc()).all()

        qe_conv_ids = {
            str(qe.conversation_id) for qe in failed_qes if qe.conversation_id
        }
        conv_map: dict[str, Conversation] = {}
        if qe_conv_ids:
            for conv in (
                db.query(Conversation).filter(Conversation.id.in_(qe_conv_ids)).all()
            ):
                conv_map[conv.id] = conv

        qe_turn_ids = {int(qe.turn_id) for qe in failed_qes if qe.turn_id is not None}
        turn_question_map: dict[int, str] = {}
        if qe_turn_ids:
            for turn_id, question in (
                db.query(Turn.id, Turn.question).filter(Turn.id.in_(qe_turn_ids)).all()
            ):
                turn_question_map[int(turn_id)] = str(question or "")

        exec_failures: list[tuple[str, str, str]] = []
        for qe in failed_qes:
            if not qe.plan_data:
                continue
            try:
                plan = json.loads(qe.plan_data)
            except ValueError:
                continue
            if not isinstance(plan, dict) or plan.get("project_id") != project_id:
                continue
            cid = str(qe.conversation_id) if qe.conversation_id else None
            if cid and cid in excluded_conv_ids:
                continue
            if cid and cid in conv_map:
                conv = conv_map[cid]
                if not conv.resumable:
                    continue
                if cutoff is not None and _as_utc(conv.updated_at) < cutoff:
                    continue
            plan_q = canonical_question(plan.get("question"))
            if plan_q and (plan_q in holdouts or plan_q in reserve_questions):
                continue
            if qe.turn_id is not None and int(qe.turn_id) in turn_question_map:
                turn_q = canonical_question(turn_question_map[int(qe.turn_id)])
                if turn_q in holdouts or turn_q in reserve_questions:
                    continue
            ef = plan.get("execution_failure")
            if isinstance(ef, dict):
                exec_failures.append(
                    (
                        str(ef.get("stage") or "unknown"),
                        str(ef.get("category") or "unknown"),
                        str(qe.sql or ""),
                    )
                )

    # 2. Failed trajectories and execution_failure clusters
    failed_diag = [r for r in diag_rows if r.get("status") == "failed"]
    if exec_failures or failed_diag:
        categories = sorted({cat for _, cat, _ in exec_failures})
        tables: list[str] = []
        for _, _, sql in exec_failures:
            tables.extend(_extract_sql_tables(sql))
        evidence = sorted({int(r["id"]) for r in failed_diag} or set(diag_ids[:3]))
        signals.append(
            MinedSignal(
                signal_kind="knowledge_gap_or_failure",
                summary=(
                    f"执行失败聚类（{', '.join(categories) or 'trajectory_failed'}）"
                ),
                evidence_trajectory_ids=evidence,
                target_hint=f"table:{tables[0]}" if tables else None,
                details={
                    "kind": "execution_failure",
                    "categories": categories,
                    "tables": list(dict.fromkeys(tables)),
                    "failed_count": len(failed_diag) + len(exec_failures),
                },
            )
        )
    return signals


def mine_project_trajectories(
    project_id: str,
    *,
    after_id: int | None = None,
    after_at: datetime | None = None,
    now: datetime | None = None,
) -> MiningResult:
    """Mine project trajectories after checkpoint with strict holdout/reserve isolation."""
    ts = _as_utc(now)
    holdouts = _load_holdout_canonical_questions(project_id)
    raw_rows = trajectories.list_trajectories(project_id, after_id=after_id)
    holdout_conv_ids = {
        str(r["conversation_id"])
        for r in raw_rows
        if r.get("conversation_id")
        and canonical_question(r.get("question")) in holdouts
    }
    eligible = _filter_eligible_trajectories(
        project_id, raw_rows, holdouts=holdouts, now=ts
    )

    _, diag_groups, reserve_groups = _dedup_and_split_groups(eligible)
    diag_ids = sorted(tid for grp in diag_groups for tid in grp.trajectory_ids)
    reserve_ids = sorted(tid for grp in reserve_groups for tid in grp.trajectory_ids)
    diag_rows = [row for grp in diag_groups for row in grp.trajectories]
    reserve_rows = [row for grp in reserve_groups for row in grp.trajectories]

    signals: list[MinedSignal] = []
    if diag_rows:
        signals.extend(_mine_sql_self_repairs(diag_rows))
        signals.extend(_mine_multi_turn_corrections(diag_groups))
        signals.extend(_mine_divergent_clarifications(diag_rows))
        signals.extend(_mine_search_detours(diag_rows))
    signals.extend(
        _mine_gaps_and_failures(
            project_id,
            diag_rows,
            reserve_rows=reserve_rows,
            holdout_conv_ids=holdout_conv_ids,
            holdouts=holdouts,
            after_at=after_at,
            now=ts,
        )
    )

    # Rank by signal specificity and evidence volume
    priority = {
        "sql_self_repair": 0,
        "multi_turn_correction": 1,
        "divergent_clarification": 2,
        "search_detour": 3,
        "knowledge_gap_or_failure": 4,
    }
    signals.sort(
        key=lambda s: (priority.get(s.signal_kind, 9), -len(s.evidence_trajectory_ids))
    )

    frozen_max = max((int(r["id"]) for r in eligible), default=None)
    earliest = (
        min((_as_utc(r["recorded_at"]) for r in eligible), default=None)
        if eligible
        else None
    )
    return MiningResult(
        project_id=project_id,
        after_trajectory_id=after_id,
        frozen_max_trajectory_id=frozen_max,
        total_eligible_rows=len(eligible),
        total_eligible_groups=len(diag_groups) + len(reserve_groups),
        diagnosis_groups=diag_groups,
        reserve_groups=reserve_groups,
        diagnosis_trajectory_ids=diag_ids,
        reserve_trajectory_ids=reserve_ids,
        signals=signals,
        earliest_eligible_at=earliest,
    )
