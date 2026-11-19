"""Async knowledge discovery run orchestrator, proposal convergence, and startup recovery (#304)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlglot
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError
from sqlglot import exp

from . import (
    corrections,
    discovery_executor,
    eval_questions,
    fk_probing,
    knowledge,
    project_context,
    query_history_miner,
    semantic_overrides,
)
from .api_time import as_utc as _as_utc
from .auth import Requester
from .db import (
    CorrectionProposal,
    DiscoveryCandidate,
    DiscoveryRun,
    Project,
    Token,
    sessions,
)
from .discovery_executor import (
    DiscoveryAuthenticationError,
    DiscoveryExecutionError,
)
from .fk_probing import FkCandidate, FkProbeOutcome
from .mcp_context import bind_mcp_context
from .query_history_miner import (
    FilterRuleCandidateSignal,
    MetricCandidateSignal,
    QueryHistoryMiningResult,
    QueryTemplateCluster,
    scrub_sensitive_text,
)
from .semantic_overrides import GateValidationError

logger = logging.getLogger(__name__)

MAX_METRIC_CANDIDATES_PER_RUN = 25
MAX_FILTER_CANDIDATES_PER_RUN = 25
MAX_EVAL_DRAFTS_PER_RUN = 20
_SUPPORTED_OVERRIDE_DIALECTS = frozenset(
    {"hive", "spark", "doris", "OSSIE_SQL_2026", "ANSI_SQL"}
)
_SUPPORTED_EVAL_DIALECTS = frozenset({"hive", "spark", "doris"})
_DATE_COL_HINT_RE = re.compile(
    r"^(?:dt|ds|pt|p_date|stat_date|biz_date|log_date|date|day|month|year|"
    r".*_(?:dt|ds|date|time|at|timestamp))$",
    re.IGNORECASE,
)


class DiscoveryConflictError(RuntimeError):
    """Raised when a project already has a running discovery run."""


class DiscoveryFrozenError(RuntimeError):
    """Raised when a project is archived or being deleted."""


class DiscoveryUnsupportedProjectError(RuntimeError):
    """Raised when a non-warehouse project requests a discovery run."""


def _loads_json(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def _scrub_error_summary(message: str | None, *, limit: int = 300) -> str | None:
    if not message:
        return None
    cleaned = scrub_sensitive_text(str(message)).strip()
    return cleaned[:limit] or None


def serialize_candidate(row: DiscoveryCandidate) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "project_id": row.project_id,
        "candidate_kind": row.candidate_kind,
        "target": row.target,
        "evidence": _loads_json(row.evidence_json, {}),
        "decision": row.decision,
        "reason": row.reason,
        "proposal_id": row.proposal_id,
        "eval_question_id": row.eval_question_id,
        "created_at": _as_utc(row.created_at),
    }


def serialize_run(
    row: DiscoveryRun,
    candidates: Sequence[DiscoveryCandidate] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": row.id,
        "project_id": row.project_id,
        "status": row.status,
        "mine_query_history": bool(row.mine_query_history),
        "probe_relationships": bool(row.probe_relationships),
        "seed_eval_drafts": bool(row.seed_eval_drafts),
        "config_snapshot": _loads_json(row.config_snapshot, {}),
        "queries_fetched": int(row.queries_fetched or 0),
        "templates_clustered": int(row.templates_clustered or 0),
        "candidates_recalled": int(row.candidates_recalled or 0),
        "candidates_probed": int(row.candidates_probed or 0),
        "candidates_passed": int(row.candidates_passed or 0),
        "candidates_skipped": int(row.candidates_skipped or 0),
        "candidates_failed": int(row.candidates_failed or 0),
        "proposals_created": int(row.proposals_created or 0),
        "eval_drafts_created": int(row.eval_drafts_created or 0),
        "error": row.error,
        "created_by": row.created_by,
        "started_at": _as_utc(row.started_at),
        "finished_at": _as_utc(row.finished_at),
        "created_at": _as_utc(row.created_at),
    }
    if candidates is not None:
        payload["candidates"] = [serialize_candidate(c) for c in candidates]
    return payload


class DiscoveryDispatcher:
    """Fork-safe background thread dispatcher for manual discovery runs."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._workers: set[threading.Thread] = set()
        self._stopping = False

    def fork_child(self) -> None:
        """Reset inherited locks and parent thread handles after os.fork()."""
        self._condition = threading.Condition()
        self._workers = set()
        self._stopping = False

    def start(self) -> None:
        """Allow dispatching runs after a previous stop() (e.g. app restart/tests)."""
        with self._condition:
            self._stopping = False

    def dispatch_run(self, run_id: str, owner_id: str) -> None:
        """Spawn a tracked daemon worker thread for an already-reserved run."""
        with self._condition:
            if self._stopping:
                return
            worker = threading.Thread(
                target=self._run_worker,
                args=(run_id, owner_id),
                name=f"dataagent-discovery-{run_id[:8]}",
                daemon=True,
            )
            self._workers.add(worker)
            worker.start()

    def _run_worker(self, run_id: str, owner_id: str) -> None:
        current = threading.current_thread()
        try:
            execute_discovery_run(run_id, owner_id=owner_id)
        except Exception:
            logger.exception("Discovery worker failed for run %s", run_id)
        finally:
            with self._condition:
                self._workers.discard(current)
                self._condition.notify_all()

    def stop(self, timeout: float = 5.0) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
            workers = list(self._workers)
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(timeout=timeout)

    def wait_idle(self, timeout: float = 10.0) -> bool:
        with self._condition:
            return self._condition.wait_for(lambda: not self._workers, timeout=timeout)


discovery_dispatcher = DiscoveryDispatcher()


def _reset_after_fork() -> None:
    discovery_dispatcher.fork_child()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


def recover_interrupted_discovery_runs() -> int:
    """Mark leftover running discovery runs as incomplete on application startup."""
    now = datetime.now(UTC)
    with sessions().begin() as db:
        result = db.execute(
            update(DiscoveryRun)
            .where(DiscoveryRun.status == "running")
            .values(
                status="incomplete",
                error="Discovery run interrupted by application restart",
                finished_at=now,
            )
        )
        return int(result.rowcount or 0)


def create_discovery_run(
    project_id: str,
    *,
    owner_id: str,
    mine_query_history: bool = True,
    probe_relationships: bool = True,
    seed_eval_drafts: bool = False,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate preconditions and reserve a single running discovery_runs row."""
    # Pre-flight personal DTB token (400) & executor availability (503)
    discovery_executor.require_personal_dtb_token(owner_id)
    discovery_executor.resolve_executor_plugin()

    now = datetime.now(UTC)
    run_id = uuid.uuid4().hex

    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        project = db.get(Project, project_id)
        if project is None:
            raise DiscoveryFrozenError("项目不存在")
        if project.status in project_context.FROZEN_STATUSES:
            raise DiscoveryFrozenError(project_context.ARCHIVED_DETAIL)
        if project.type != "warehouse":
            raise DiscoveryUnsupportedProjectError("文档型项目不支持知识发现运行")

        existing = (
            db.query(DiscoveryRun.id)
            .filter(
                DiscoveryRun.project_id == project_id,
                DiscoveryRun.status == "running",
            )
            .first()
        )
        if existing is not None:
            raise DiscoveryConflictError(
                "该项目已有正在执行的知识发现运行，请等待完成后重试"
            )

        effective_cfg = dict(project_context.discovery_config(project_id, db))
        if overrides:
            for key, val in overrides.items():
                if val is not None and key in effective_cfg:
                    effective_cfg[key] = val

        row = DiscoveryRun(
            id=run_id,
            project_id=project_id,
            status="running",
            mine_query_history=bool(mine_query_history),
            probe_relationships=bool(probe_relationships),
            seed_eval_drafts=bool(seed_eval_drafts),
            config_snapshot=json.dumps(effective_cfg, ensure_ascii=False),
            created_by=owner_id,
            started_at=now,
            finished_at=None,
            created_at=now,
        )
        db.add(row)
        try:
            db.flush()
        except IntegrityError as exc:
            raise DiscoveryConflictError(
                "该项目已有正在执行的知识发现运行，请等待完成后重试"
            ) from exc
        return serialize_run(row)


def _record_candidate(
    *,
    run_id: str,
    project_id: str,
    candidate_kind: str,
    target: str,
    evidence: Mapping[str, Any],
    decision: str,
    reason: str | None = None,
    proposal_id: str | None = None,
    eval_question_id: str | None = None,
) -> None:
    with sessions().begin() as db:
        db.add(
            DiscoveryCandidate(
                id=uuid.uuid4().hex,
                run_id=run_id,
                project_id=project_id,
                candidate_kind=candidate_kind,
                target=target[:500],
                evidence_json=json.dumps(dict(evidence), ensure_ascii=False),
                decision=decision,
                reason=_scrub_error_summary(reason, limit=300),
                proposal_id=proposal_id,
                eval_question_id=eval_question_id,
                created_at=datetime.now(UTC),
            )
        )


def _update_run_fields(run_id: str, **values: Any) -> None:
    with sessions().begin() as db:
        row = db.get(DiscoveryRun, run_id)
        if row is None:
            return
        for k, v in values.items():
            setattr(row, k, v)


def _requester_for_owner(owner_id: str) -> Requester:
    with sessions()() as db:
        tok = db.query(Token).filter(Token.id == owner_id).first()
        if tok is not None:
            return Requester(id=tok.id, role=tok.role, token="")
    return Requester(id=owner_id, role="admin", token="")


def _norm_rel_pair(
    from_table: str,
    from_cols: Sequence[str],
    to_table: str,
    to_cols: Sequence[str],
) -> frozenset[tuple[tuple[str, str], tuple[str, str]]]:
    ft = from_table.strip().lower()
    tt = to_table.strip().lower()
    return frozenset(
        ((ft, fc.strip().lower()), (tt, tc.strip().lower()))
        if (ft, fc.strip().lower()) <= (tt, tc.strip().lower())
        else ((tt, tc.strip().lower()), (ft, fc.strip().lower()))
        for fc, tc in zip(from_cols, to_cols, strict=True)
    )


def _rel_entry_pair(entry: Mapping[str, Any]) -> frozenset | None:
    from_db = str(entry.get("from_database") or "").strip()
    from_tbl = str(entry.get("from_table") or "").strip()
    to_db = str(entry.get("to_database") or "").strip()
    to_tbl = str(entry.get("to_table") or "").strip()
    from_cols = list(entry.get("from_columns") or ())
    to_cols = list(entry.get("to_columns") or ())
    if not from_db or not from_tbl or not to_db or not to_tbl:
        return None
    if not from_cols or len(from_cols) != len(to_cols):
        return None
    ft = ".".join(p for p in (entry.get("from_catalog"), from_db, from_tbl) if p)
    tt = ".".join(p for p in (entry.get("to_catalog"), to_db, to_tbl) if p)
    return _norm_rel_pair(ft, from_cols, tt, to_cols)


def _norm_metric_sig(
    entry: Mapping[str, Any],
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    expr = " ".join(str(entry.get("expression") or "").strip().lower().split())
    filters = tuple(
        sorted(
            " ".join(str(f).strip().lower().split())
            for f in (entry.get("filters") or ())
        )
    )
    tables: list[str] = []
    if entry.get("table"):
        tables.append(str(entry["table"]).strip().lower())
    for t in entry.get("tables") or ():
        tables.append(str(t).strip().lower())
    return expr, filters, tuple(sorted(set(tables)))


def _build_dedup_state(
    project_id: str,
    tables: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Collect active overrides, existing RELATES_TO edges, and open pending proposals."""
    existing_rels: set[frozenset] = set()
    for meta in tables:
        for rel in meta.get("relationships") or ():
            if not isinstance(rel, Mapping):
                continue
            ft = str(rel.get("from_table") or "").strip()
            tt = str(rel.get("to_table") or "").strip()
            fc = list(rel.get("from_columns") or ())
            tc = list(rel.get("to_columns") or ())
            if ft and tt and fc and len(fc) == len(tc):
                existing_rels.add(_norm_rel_pair(ft, fc, tt, tc))

    existing_metric_keys: set[str] = set()
    existing_metric_sigs: set[tuple[str, tuple[str, ...], tuple[str, ...]]] = set()
    existing_field_instructions: dict[str, str] = {}
    existing_fields_raw: dict[str, dict[str, Any]] = {}

    src_dir = project_context.source_directory_value(project_id)
    if src_dir:
        src_path = Path(src_dir)
        if src_path.is_dir():
            try:
                doc, _ = semantic_overrides.load_overrides_file(src_path)
            except Exception:  # noqa: BLE001
                doc = semantic_overrides.empty_overrides_doc()
            for rel_entry in (doc.get("relationships") or {}).values():
                if isinstance(rel_entry, Mapping):
                    pair = _rel_entry_pair(rel_entry)
                    if pair is not None:
                        existing_rels.add(pair)
            for mkey, mentry in (doc.get("metrics") or {}).items():
                if isinstance(mentry, Mapping):
                    existing_metric_keys.add(str(mkey).lower())
                    existing_metric_sigs.add(_norm_metric_sig(mentry))
            for fkey, fentry in (doc.get("fields") or {}).items():
                if isinstance(fentry, Mapping):
                    existing_fields_raw[str(fkey).lower()] = dict(fentry)
                    inst = str(fentry.get("instructions") or "").strip()
                    if inst:
                        existing_field_instructions[str(fkey).lower()] = inst

    with sessions()() as db:
        open_props = (
            db.query(CorrectionProposal)
            .filter(
                CorrectionProposal.project_id == project_id,
                CorrectionProposal.status.in_(("pending", "applying")),
            )
            .all()
        )
        for prop in open_props:
            sp = _loads_json(prop.structured_patch, None)
            if not isinstance(sp, Mapping):
                continue
            for rel_entry in (sp.get("relationships") or {}).values():
                if isinstance(rel_entry, Mapping):
                    pair = _rel_entry_pair(rel_entry)
                    if pair is not None:
                        existing_rels.add(pair)
            for mkey, mentry in (sp.get("metrics") or {}).items():
                if isinstance(mentry, Mapping):
                    existing_metric_keys.add(str(mkey).lower())
                    existing_metric_sigs.add(_norm_metric_sig(mentry))
            for fkey, fentry in (sp.get("fields") or {}).items():
                if isinstance(fentry, Mapping):
                    inst = str(fentry.get("instructions") or "").strip()
                    if inst:
                        prev = existing_field_instructions.get(str(fkey).lower(), "")
                        existing_field_instructions[str(fkey).lower()] = (
                            f"{prev}\n{inst}".strip() if prev else inst
                        )

    return {
        "relationships": existing_rels,
        "metric_keys": existing_metric_keys,
        "metric_sigs": existing_metric_sigs,
        "field_instructions": existing_field_instructions,
        "fields_raw": existing_fields_raw,
    }


def _relationship_structured_patch(
    cand: FkCandidate, outcome: FkProbeOutcome
) -> tuple[str, dict[str, Any]]:
    rel_key = cand.relationship_key()
    entry: dict[str, Any] = {
        "name": rel_key,
        "from_database": cand.from_database,
        "from_table": cand.from_table_name,
        "from_columns": list(cand.from_columns),
        "to_database": cand.to_database,
        "to_table": cand.to_table_name,
        "to_columns": list(cand.to_columns),
        "description": (
            f"知识发现探活核验通过的候选外键关系："
            f"{cand.from_table}({', '.join(cand.from_columns)}) -> "
            f"{cand.to_table}({', '.join(cand.to_columns)})，"
            f"采样包含度 {outcome.overlap_ratio:.2%}"
            if outcome.overlap_ratio is not None
            else f"知识发现探活核验通过的候选外键关系：{cand.from_table} -> {cand.to_table}"
        ),
    }
    if cand.from_catalog:
        entry["from_catalog"] = cand.from_catalog
    if cand.to_catalog:
        entry["to_catalog"] = cand.to_catalog
    return rel_key, {"version": 1, "relationships": {rel_key: entry}}


def _metric_structured_patch(
    metric: MetricCandidateSignal,
) -> dict[str, Any]:
    dialect = (
        metric.dialect if metric.dialect in _SUPPORTED_OVERRIDE_DIALECTS else "ANSI_SQL"
    )
    entry: dict[str, Any] = {
        "name": metric.name,
        "description": metric.description,
        "expression": metric.expression,
        "dialect": dialect,
    }
    if metric.tables:
        entry["tables"] = list(metric.tables)
    if metric.filters:
        entry["filters"] = list(metric.filters)
    if metric.boundary_diff:
        entry["instructions"] = f"口径边界差异：{metric.boundary_diff}"
    return {"version": 1, "metrics": {metric.key: entry}}


def _filter_rule_structured_patch(
    rule: FilterRuleCandidateSignal,
    existing_fields_raw: Mapping[str, dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    field_key = f"{rule.table}.{rule.column}"
    base = dict(existing_fields_raw.get(field_key.lower(), {}))
    existing_inst = str(base.get("instructions") or "").strip()
    combined_inst = (
        f"{existing_inst}\n{rule.instruction}" if existing_inst else rule.instruction
    )
    entry: dict[str, Any] = {
        "database": rule.database,
        "table": rule.table_name,
        "name": rule.column,
        "instructions": combined_inst,
    }
    if rule.catalog:
        entry["catalog"] = rule.catalog
    if base.get("comment") is not None:
        entry["comment"] = base["comment"]
    if base.get("synonyms"):
        entry["synonyms"] = list(base["synonyms"])
    return field_key, {"version": 1, "fields": {field_key: entry}}


def _render_synthetic_eval_sql(
    template: QueryTemplateCluster,
    tables: Sequence[Mapping[str, Any]],
) -> tuple[str, str]:
    """Replace scrubbed ? placeholders with synthetic non-business constants for validate_sql."""
    dialect = (
        template.dialect if template.dialect in _SUPPORTED_EVAL_DIALECTS else "doris"
    )
    partition_cols: set[str] = set()
    numeric_cols: set[str] = set()
    for meta in tables:
        for fld in meta.get("fields") or ():
            if not isinstance(fld, Mapping):
                continue
            fname = str(fld.get("name") or "").strip().lower()
            if not fname:
                continue
            if fld.get("partition") is True:
                partition_cols.add(fname)
            ftype = str(fld.get("type") or fld.get("raw_type") or "").lower()
            if any(
                tok in ftype
                for tok in ("int", "bigint", "decimal", "double", "float", "numeric")
            ):
                numeric_cols.add(fname)

    try:
        tree = sqlglot.parse_one(template.template_sql, read=dialect)
    except Exception:  # noqa: BLE001
        return template.template_sql, dialect

    def _replace(node: exp.Expression) -> exp.Expression:
        if not isinstance(node, exp.Placeholder):
            return node
        parent = node.parent
        col_name = ""
        if parent is not None:
            lhs = getattr(parent, "this", None)
            if isinstance(lhs, exp.Column) and lhs.name:
                col_name = lhs.name.lower()
            elif isinstance(parent, exp.Between) and isinstance(
                parent.this, exp.Column
            ):
                col_name = parent.this.name.lower()
        if col_name and (
            col_name in partition_cols or bool(_DATE_COL_HINT_RE.fullmatch(col_name))
        ):
            return exp.Literal.string("2025-01-01")
        if col_name and col_name in numeric_cols:
            return exp.Literal.number(1)
        if isinstance(parent, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Limit)):
            return exp.Literal.number(1)
        return exp.Literal.string("__PLACEHOLDER__")

    rendered = tree.copy().transform(_replace).sql(dialect=dialect)
    return rendered, dialect


def _build_eval_draft_question(template: QueryTemplateCluster) -> tuple[str, str]:
    tables_desc = "、".join(template.tables[:4]) if template.tables else "业务表"
    short_fp = template.fingerprint[:8]
    question = f"请统计 {tables_desc} 的高频查询口径指标（历史模板 {short_fp}）"
    caliber = (
        f"【待人工确认口径】来自查询历史高频脱敏模板（执行 {template.execution_count} 次，"
        f"去重调用方 {template.distinct_callers} 人），占位常量已脱敏，请管理员核验后确认。"
    )
    return question, caliber


async def _execute_discovery_run_async(run_id: str, owner_id: str) -> None:
    with sessions()() as db:
        row = db.get(DiscoveryRun, run_id)
        if row is None or row.status != "running":
            return
        project_id = row.project_id
        mine_query_history_flag = bool(row.mine_query_history)
        probe_relationships_flag = bool(row.probe_relationships)
        seed_eval_drafts_flag = bool(row.seed_eval_drafts)
        cfg = _loads_json(row.config_snapshot, {})

    actor_requester = _requester_for_owner(owner_id)
    queries_fetched = 0
    templates_clustered = 0
    candidates_recalled = 0
    candidates_probed = 0
    candidates_passed = 0
    candidates_skipped = 0
    candidates_failed = 0
    proposals_created = 0
    eval_drafts_created = 0
    had_partial_error = False
    partial_reasons: list[str] = []

    try:
        runtime = discovery_executor.resolve_discovery_dtb_runtime(owner_id)
    except DiscoveryExecutionError as exc:
        _update_run_fields(
            run_id,
            status="failed",
            error=_scrub_error_summary(exc.detail),
            finished_at=datetime.now(UTC),
        )
        return

    try:
        tables = (
            knowledge.describe(summary=False)
            if project_id in (None, project_context.DEFAULT_PROJECT_ID)
            else knowledge.describe(summary=False, project_id=project_id)
        )
    except Exception:
        logger.debug(
            "Knowledge describe failed for discovery run %s (%s)",
            run_id,
            project_id,
            exc_info=True,
        )
        tables = []

    # Phase A: Query history fetch & mining
    mining_result: QueryHistoryMiningResult | None = None
    if mine_query_history_flag or seed_eval_drafts_flag:
        try:
            fetched = await discovery_executor.fetch_query_history(
                owner_id=owner_id,
                config=cfg,
                runtime=runtime,
            )
            queries_fetched = fetched.raw_row_count
            mining_result = query_history_miner.mine_query_history(
                fetched.records,
                tables=tables,
                project_id=project_id,
                min_frequency=int(cfg.get("min_frequency", 3)),
                provider_id=cfg.get("provider_id"),
                model=cfg.get("model"),
            )
            templates_clustered = len(mining_result.templates)
            _update_run_fields(
                run_id,
                queries_fetched=queries_fetched,
                templates_clustered=templates_clustered,
            )
        except DiscoveryAuthenticationError as exc:
            _update_run_fields(
                run_id,
                status="failed",
                queries_fetched=queries_fetched,
                templates_clustered=templates_clustered,
                error=_scrub_error_summary(exc.detail),
                finished_at=datetime.now(UTC),
            )
            return
        except DiscoveryExecutionError as exc:
            had_partial_error = True
            partial_reasons.append(exc.detail)
            if not probe_relationships_flag:
                _update_run_fields(
                    run_id,
                    status="failed",
                    error=_scrub_error_summary(exc.detail),
                    finished_at=datetime.now(UTC),
                )
                return

    # Phase B: Three-stage FK recall & bounded partition-pruned data probing
    probe_outcomes: list[FkProbeOutcome] = []
    max_fk = int(cfg.get("max_fk_candidates", 20))
    if probe_relationships_flag and max_fk > 0:
        recalled = fk_probing.recall_fk_candidates(
            tables=tables,
            join_pairs=(mining_result.join_pairs if mining_result else ()),
            project_id=project_id,
            max_fk_candidates=max_fk,
        )
        candidates_recalled = len(recalled)
        _update_run_fields(run_id, candidates_recalled=candidates_recalled)
        try:
            probe_outcomes = await fk_probing.probe_fk_candidates(
                candidates=recalled,
                tables=tables,
                owner_id=owner_id,
                project_id=project_id,
                sample_limit=int(cfg.get("sample_limit", 1000)),
                min_inclusion_ratio=float(cfg.get("min_inclusion_ratio", 0.8)),
                probe_timeout_seconds=float(cfg.get("probe_timeout_seconds", 30)),
                probe_concurrency=int(cfg.get("probe_concurrency", 2)),
                runtime=runtime,
            )
        except DiscoveryAuthenticationError as exc:
            _update_run_fields(
                run_id,
                status="failed",
                queries_fetched=queries_fetched,
                templates_clustered=templates_clustered,
                candidates_recalled=candidates_recalled,
                error=_scrub_error_summary(exc.detail),
                finished_at=datetime.now(UTC),
            )
            return

        for outcome in probe_outcomes:
            if outcome.decision == "skipped":
                candidates_skipped += 1
            else:
                candidates_probed += 1
                if outcome.decision == "passed":
                    candidates_passed += 1
                elif outcome.decision == "failed":
                    candidates_failed += 1
                    had_partial_error = True
                    if outcome.reason:
                        partial_reasons.append(outcome.reason)

        _update_run_fields(
            run_id,
            candidates_probed=candidates_probed,
            candidates_passed=candidates_passed,
            candidates_skipped=candidates_skipped,
            candidates_failed=candidates_failed,
        )

    # Phase C: Converge passed/mined candidates into forced-pending source="discovery" proposals
    dedup = _build_dedup_state(project_id, tables)

    for outcome in probe_outcomes:
        cand = outcome.candidate
        evidence = outcome.to_evidence()
        evidence["run_id"] = run_id
        evidence["candidate_kind"] = "relationship"
        target_ref = cand.target_ref()

        if outcome.decision != "passed":
            _record_candidate(
                run_id=run_id,
                project_id=project_id,
                candidate_kind="relationship",
                target=target_ref,
                evidence=evidence,
                decision=outcome.decision,
                reason=outcome.reason,
            )
            continue

        pair_key = cand.normalized_pair_key()
        if pair_key in dedup["relationships"]:
            _record_candidate(
                run_id=run_id,
                project_id=project_id,
                candidate_kind="relationship",
                target=target_ref,
                evidence=evidence,
                decision="deduplicated",
                reason="already_exists_or_pending",
            )
            continue

        _, structured_patch = _relationship_structured_patch(cand, outcome)
        reason_text = (
            f"冷启动知识发现探活核验通过的外键关系：{cand.from_table}({', '.join(cand.from_columns)}) "
            f"-> {cand.to_table}({', '.join(cand.to_columns)})，"
            f"采样包含度 {outcome.overlap_ratio:.2%}（样本 {outcome.sample_distinct_count}，"
            f"命中 {outcome.matched_count}，JOIN 存在性校验通过）。"
        )
        try:
            with (
                bind_mcp_context(actor_requester),
                corrections.trusted_discovery_source(),
            ):
                created = corrections.propose_in_project(
                    project_id,
                    target=target_ref,
                    patch="",
                    reason=reason_text,
                    source="discovery",
                    target_kind="override",
                    structured_patch=structured_patch,
                    extra_gate_report={"discovery_evidence": evidence},
                )
        except GateValidationError as exc:
            _record_candidate(
                run_id=run_id,
                project_id=project_id,
                candidate_kind="relationship",
                target=target_ref,
                evidence=evidence,
                decision="rejected",
                reason=str(exc),
            )
        except ValueError as exc:
            msg = str(exc)
            dec = "deduplicated" if "does not change" in msg else "failed"
            if dec == "failed":
                had_partial_error = True
                partial_reasons.append(msg)
            _record_candidate(
                run_id=run_id,
                project_id=project_id,
                candidate_kind="relationship",
                target=target_ref,
                evidence=evidence,
                decision=dec,
                reason=msg,
            )
        else:
            dedup["relationships"].add(pair_key)
            proposals_created += 1
            _record_candidate(
                run_id=run_id,
                project_id=project_id,
                candidate_kind="relationship",
                target=target_ref,
                evidence=evidence,
                decision="proposed",
                proposal_id=created["proposal_id"],
            )

    if mine_query_history_flag and mining_result is not None:
        # Metrics & caliber variants
        for metric in mining_result.metrics[:MAX_METRIC_CANDIDATES_PER_RUN]:
            target_ref = f"metric:{metric.key}"
            evidence = {
                "run_id": run_id,
                "candidate_kind": "metric",
                "topic": metric.topic,
                "expression": metric.expression,
                "filters": list(metric.filters),
                "tables": list(metric.tables),
                "query_history_frequency": metric.execution_count,
                "distinct_callers": metric.distinct_callers,
                "has_conflicting_variants": metric.has_conflicting_variants,
                "boundary_diff": metric.boundary_diff,
                "probing_status": "not_applicable",
            }
            patch_payload = _metric_structured_patch(metric)
            metric_entry = patch_payload["metrics"][metric.key]
            msig = _norm_metric_sig(metric_entry)
            if (
                metric.key.lower() in dedup["metric_keys"]
                or msig in dedup["metric_sigs"]
            ):
                _record_candidate(
                    run_id=run_id,
                    project_id=project_id,
                    candidate_kind="metric",
                    target=target_ref,
                    evidence=evidence,
                    decision="deduplicated",
                    reason="already_exists_or_pending",
                )
                continue

            reason_text = (
                f"查询历史挖掘发现高频指标口径「{metric.name}」（出现 {metric.execution_count} 次，"
                f"去重调用方 {metric.distinct_callers} 人）：`{metric.expression}`"
            )
            try:
                with (
                    bind_mcp_context(actor_requester),
                    corrections.trusted_discovery_source(),
                ):
                    created = corrections.propose_in_project(
                        project_id,
                        target=target_ref,
                        patch="",
                        reason=reason_text,
                        source="discovery",
                        target_kind="override",
                        structured_patch=patch_payload,
                        extra_gate_report={"discovery_evidence": evidence},
                    )
            except GateValidationError as exc:
                _record_candidate(
                    run_id=run_id,
                    project_id=project_id,
                    candidate_kind="metric",
                    target=target_ref,
                    evidence=evidence,
                    decision="rejected",
                    reason=str(exc),
                )
            except ValueError as exc:
                msg = str(exc)
                dec = "deduplicated" if "does not change" in msg else "failed"
                if dec == "failed":
                    had_partial_error = True
                    partial_reasons.append(msg)
                _record_candidate(
                    run_id=run_id,
                    project_id=project_id,
                    candidate_kind="metric",
                    target=target_ref,
                    evidence=evidence,
                    decision=dec,
                    reason=msg,
                )
            else:
                dedup["metric_keys"].add(metric.key.lower())
                dedup["metric_sigs"].add(msig)
                proposals_created += 1
                _record_candidate(
                    run_id=run_id,
                    project_id=project_id,
                    candidate_kind="metric",
                    target=target_ref,
                    evidence=evidence,
                    decision="proposed",
                    proposal_id=created["proposal_id"],
                )

        # Default WHERE filter rules
        for rule in mining_result.filter_rules[:MAX_FILTER_CANDIDATES_PER_RUN]:
            field_key = f"{rule.table}.{rule.column}"
            target_ref = f"field:{field_key}"
            evidence = {
                "run_id": run_id,
                "candidate_kind": "filter_rule",
                "table": rule.table,
                "column": rule.column,
                "predicate": rule.predicate,
                "query_history_frequency": rule.execution_count,
                "distinct_callers": rule.distinct_callers,
                "template_count": rule.template_count,
                "co_occurrence_ratio": rule.co_occurrence_ratio,
                "probing_status": "not_applicable",
            }
            existing_inst = dedup["field_instructions"].get(field_key.lower(), "")
            if (
                rule.predicate.lower() in existing_inst.lower()
                or rule.instruction.lower() in existing_inst.lower()
            ):
                _record_candidate(
                    run_id=run_id,
                    project_id=project_id,
                    candidate_kind="filter_rule",
                    target=target_ref,
                    evidence=evidence,
                    decision="deduplicated",
                    reason="already_exists_or_pending",
                )
                continue

            _, patch_payload = _filter_rule_structured_patch(rule, dedup["fields_raw"])
            reason_text = (
                f"查询历史挖掘发现高频非分区过滤规则：{rule.table}.{rule.column} "
                f"（`{rule.predicate}`，出现 {rule.execution_count} 次，覆盖 {rule.template_count} 类模板）"
            )
            try:
                with (
                    bind_mcp_context(actor_requester),
                    corrections.trusted_discovery_source(),
                ):
                    created = corrections.propose_in_project(
                        project_id,
                        target=target_ref,
                        patch="",
                        reason=reason_text,
                        source="discovery",
                        target_kind="override",
                        structured_patch=patch_payload,
                        extra_gate_report={"discovery_evidence": evidence},
                    )
            except GateValidationError as exc:
                _record_candidate(
                    run_id=run_id,
                    project_id=project_id,
                    candidate_kind="filter_rule",
                    target=target_ref,
                    evidence=evidence,
                    decision="rejected",
                    reason=str(exc),
                )
            except ValueError as exc:
                msg = str(exc)
                dec = "deduplicated" if "does not change" in msg else "failed"
                if dec == "failed":
                    had_partial_error = True
                    partial_reasons.append(msg)
                _record_candidate(
                    run_id=run_id,
                    project_id=project_id,
                    candidate_kind="filter_rule",
                    target=target_ref,
                    evidence=evidence,
                    decision=dec,
                    reason=msg,
                )
            else:
                dedup["field_instructions"][field_key.lower()] = (
                    f"{existing_inst}\n{rule.instruction}".strip()
                    if existing_inst
                    else rule.instruction
                )
                proposals_created += 1
                _record_candidate(
                    run_id=run_id,
                    project_id=project_id,
                    candidate_kind="filter_rule",
                    target=target_ref,
                    evidence=evidence,
                    decision="proposed",
                    proposal_id=created["proposal_id"],
                )

    # Phase D: Optional cold-start eval question draft seeding (OFF by default)
    if seed_eval_drafts_flag and mining_result is not None:
        for tpl in mining_result.templates[:MAX_EVAL_DRAFTS_PER_RUN]:
            target_ref = f"eval_draft:{tpl.fingerprint[:12]}"
            candidate_sql, eval_dialect = _render_synthetic_eval_sql(tpl, tables)
            question, caliber = _build_eval_draft_question(tpl)
            evidence = {
                "run_id": run_id,
                "candidate_kind": "eval_draft",
                "fingerprint": tpl.fingerprint,
                "tables": list(tpl.tables),
                "dialect": eval_dialect,
                "query_history_frequency": tpl.execution_count,
                "distinct_callers": tpl.distinct_callers,
                "probing_status": "not_applicable",
            }
            ok, err = eval_questions.validate_sql_if_available(
                candidate_sql, eval_dialect, project_id
            )
            if not ok:
                _record_candidate(
                    run_id=run_id,
                    project_id=project_id,
                    candidate_kind="eval_draft",
                    target=target_ref,
                    evidence=evidence,
                    decision="rejected",
                    reason=f"validate_sql_failed: {err or 'invalid_sql'}",
                )
                continue

            dup = eval_questions.find_duplicate_eval_question(
                project_id,
                question=question,
                reference_sql=candidate_sql,
                dialect=eval_dialect,
                template_fingerprint=tpl.fingerprint,
            )
            if dup is not None:
                _record_candidate(
                    run_id=run_id,
                    project_id=project_id,
                    candidate_kind="eval_draft",
                    target=target_ref,
                    evidence=evidence,
                    decision="deduplicated",
                    reason="duplicate_eval_question",
                    eval_question_id=str(dup.get("id") or "") or None,
                )
                continue

            try:
                saved = eval_questions.upsert_eval_question(
                    project_id,
                    question=question,
                    caliber=caliber,
                    reference_sql=candidate_sql,
                    notes=f"discovery_run:{run_id}; template_fingerprint:{tpl.fingerprint}",
                    dialect=eval_dialect,
                    split="dev",
                    answer_confirmed=False,
                    source="discovery",
                    created_by=owner_id,
                )
            except ValueError as exc:
                _record_candidate(
                    run_id=run_id,
                    project_id=project_id,
                    candidate_kind="eval_draft",
                    target=target_ref,
                    evidence=evidence,
                    decision="rejected",
                    reason=str(exc),
                )
            else:
                eval_drafts_created += 1
                _record_candidate(
                    run_id=run_id,
                    project_id=project_id,
                    candidate_kind="eval_draft",
                    target=target_ref,
                    evidence=evidence,
                    decision="proposed",
                    eval_question_id=str(saved["id"]),
                )

    has_any_valid_progress = (
        proposals_created > 0
        or eval_drafts_created > 0
        or candidates_passed > 0
        or candidates_skipped > 0
        or (candidates_probed > candidates_failed)
        or templates_clustered > 0
    )
    if had_partial_error:
        final_status = "partial" if has_any_valid_progress else "failed"
        final_error = _scrub_error_summary("; ".join(dict.fromkeys(partial_reasons)))
    else:
        final_status = "completed"
        final_error = None

    _update_run_fields(
        run_id,
        status=final_status,
        queries_fetched=queries_fetched,
        templates_clustered=templates_clustered,
        candidates_recalled=candidates_recalled,
        candidates_probed=candidates_probed,
        candidates_passed=candidates_passed,
        candidates_skipped=candidates_skipped,
        candidates_failed=candidates_failed,
        proposals_created=proposals_created,
        eval_drafts_created=eval_drafts_created,
        error=final_error,
        finished_at=datetime.now(UTC),
    )


def execute_discovery_run(run_id: str, *, owner_id: str) -> None:
    """Synchronous entry point executed by the background worker thread."""
    try:
        asyncio.run(_execute_discovery_run_async(run_id, owner_id))
    except Exception:
        logger.exception("Unhandled error in discovery run %s", run_id)
        _update_run_fields(
            run_id,
            status="failed",
            error="知识发现运行发生内部异常",
            finished_at=datetime.now(UTC),
        )
