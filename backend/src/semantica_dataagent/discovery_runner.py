"""Async knowledge discovery run orchestrator, proposal convergence, and startup recovery (#304)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
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
    knowledge_synthesizer,
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
    QueryHistoryMiningResult,
    QueryTemplateCluster,
    scrub_sensitive_text,
)
from .semantic_overrides import GateValidationError

logger = logging.getLogger(__name__)

MAX_EVAL_DRAFTS_PER_RUN = 20
DEFAULT_TOP_TEMPLATES = 20
MAX_TOP_TEMPLATES = 100
_EMPTY_PROPOSALS_MSG = "大模型未返回可用的知识提案，请重试或切换为手工编辑"
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
    raw_snapshot = _loads_json(row.config_snapshot, {})
    snapshot = dict(raw_snapshot) if isinstance(raw_snapshot, Mapping) else {}
    raw_stats = snapshot.pop("_run_stats", {})
    run_stats = dict(raw_stats) if isinstance(raw_stats, Mapping) else {}
    enable_fk_probing = bool(row.probe_relationships)
    generate_eval_drafts = bool(row.seed_eval_drafts)
    try:
        top_templates = max(
            1,
            min(
                MAX_TOP_TEMPLATES,
                int(snapshot.get("top_templates") or DEFAULT_TOP_TEMPLATES),
            ),
        )
    except (TypeError, ValueError):
        top_templates = DEFAULT_TOP_TEMPLATES
    proposal_ids = [str(item) for item in (run_stats.get("proposal_ids") or ()) if item]

    payload: dict[str, Any] = {
        "id": row.id,
        "project_id": row.project_id,
        "status": row.status,
        "mine_query_history": bool(row.mine_query_history),
        "probe_relationships": enable_fk_probing,
        "enable_fk_probing": enable_fk_probing,
        "seed_eval_drafts": generate_eval_drafts,
        "generate_eval_drafts": generate_eval_drafts,
        "top_templates": top_templates,
        "config_snapshot": snapshot,
        "queries_fetched": int(row.queries_fetched or 0),
        "templates_clustered": int(row.templates_clustered or 0),
        "templates_synthesized": int(run_stats.get("templates_synthesized", 0) or 0),
        "candidates_recalled": int(row.candidates_recalled or 0),
        "candidates_probed": int(row.candidates_probed or 0),
        "fk_candidates_probed": int(row.candidates_probed or 0),
        "candidates_passed": int(row.candidates_passed or 0),
        "fk_candidates_verified": int(row.candidates_passed or 0),
        "candidates_skipped": int(row.candidates_skipped or 0),
        "candidates_failed": int(row.candidates_failed or 0),
        "proposals_created": int(row.proposals_created or 0),
        "duplicates_skipped": int(run_stats.get("duplicates_skipped", 0) or 0),
        "l1_rejected": int(run_stats.get("l1_rejected", 0) or 0),
        "eval_drafts_created": int(row.eval_drafts_created or 0),
        "proposal_ids": proposal_ids,
        "llm_used": bool(run_stats.get("llm_used", False)),
        "llm_error": run_stats.get("llm_error"),
        "summary": str(run_stats.get("summary") or ""),
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
    enable_fk_probing: bool | None = None,
    probe_relationships: bool | None = None,
    generate_eval_drafts: bool | None = None,
    seed_eval_drafts: bool | None = None,
    top_templates: int | None = None,
    provider_id: str | None = None,
    model: str | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate preconditions and reserve a single running discovery_runs row."""
    # Pre-flight personal DTB token (400) & executor availability (503)
    discovery_executor.require_personal_dtb_token(owner_id)
    discovery_executor.resolve_executor_plugin(project_id=project_id)

    eff_fk_probing = (
        bool(enable_fk_probing)
        if enable_fk_probing is not None
        else (bool(probe_relationships) if probe_relationships is not None else False)
    )
    eff_eval_drafts = (
        bool(generate_eval_drafts)
        if generate_eval_drafts is not None
        else (bool(seed_eval_drafts) if seed_eval_drafts is not None else False)
    )
    eff_top_templates = (
        max(1, min(MAX_TOP_TEMPLATES, int(top_templates)))
        if top_templates is not None
        else DEFAULT_TOP_TEMPLATES
    )

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
        effective_cfg["top_templates"] = eff_top_templates
        clean_pid = (
            provider_id.strip()
            if isinstance(provider_id, str) and provider_id.strip()
            else None
        )
        clean_model = (
            model.strip() if isinstance(model, str) and model.strip() else None
        )
        if clean_pid is not None:
            if clean_pid != effective_cfg.get("provider_id") and clean_model is None:
                effective_cfg["model"] = None
            effective_cfg["provider_id"] = clean_pid
        if clean_model is not None:
            effective_cfg["model"] = clean_model
        effective_cfg["_run_stats"] = {
            "templates_synthesized": 0,
            "duplicates_skipped": 0,
            "l1_rejected": 0,
            "proposal_ids": [],
            "llm_used": False,
            "llm_error": None,
            "summary": "",
        }

        row = DiscoveryRun(
            id=run_id,
            project_id=project_id,
            status="running",
            mine_query_history=bool(mine_query_history),
            probe_relationships=eff_fk_probing,
            seed_eval_drafts=eff_eval_drafts,
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


def _update_run_fields(
    run_id: str,
    *,
    run_stats: Mapping[str, Any] | None = None,
    **values: Any,
) -> None:
    with sessions().begin() as db:
        row = db.get(DiscoveryRun, run_id)
        if row is None:
            return
        for k, v in values.items():
            setattr(row, k, v)
        if run_stats:
            raw_snapshot = _loads_json(row.config_snapshot, {})
            snapshot = dict(raw_snapshot) if isinstance(raw_snapshot, Mapping) else {}
            existing_stats = snapshot.get("_run_stats")
            merged_stats = (
                dict(existing_stats) if isinstance(existing_stats, Mapping) else {}
            )
            merged_stats.update(run_stats)
            snapshot["_run_stats"] = merged_stats
            row.config_snapshot = json.dumps(snapshot, ensure_ascii=False)


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


def _build_dedup_state(
    project_id: str,
    tables: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Collect active relationship overrides, existing RELATES_TO edges, and open pending proposals."""
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

    return {"relationships": existing_rels}


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


def _fk_outcome_aggregate_evidence(
    outcome: FkProbeOutcome, *, run_id: str
) -> dict[str, Any]:
    """Build strictly aggregate FK probe metrics (zero business fact rows)."""
    evidence = outcome.to_evidence()
    evidence["run_id"] = run_id
    evidence["discovery_run_id"] = run_id
    evidence["candidate_kind"] = "relationship"
    evidence["inclusion_rate"] = outcome.overlap_ratio
    evidence["join_hit"] = outcome.join_matched
    evidence["probed_partition"] = {
        tbl: dict(parts) for tbl, parts in outcome.probe_partitions.items()
    }
    return evidence


def _build_cluster_summaries(
    top_clusters: Sequence[QueryTemplateCluster],
    mining_result: QueryHistoryMiningResult | None,
) -> list[dict[str, Any]]:
    metrics_by_fp: dict[str, list[dict[str, Any]]] = {}
    filters_by_fp: dict[str, list[dict[str, Any]]] = {}
    if mining_result is not None:
        for metric in mining_result.metrics:
            item = {
                "key": metric.key,
                "name": metric.name,
                "expression": metric.expression,
                "filters": list(metric.filters),
                "tables": list(metric.tables),
                "execution_count": metric.execution_count,
                "has_conflicting_variants": metric.has_conflicting_variants,
                "boundary_diff": metric.boundary_diff,
            }
            for fp in metric.source_fingerprints:
                metrics_by_fp.setdefault(fp, []).append(item)
        for rule in mining_result.filter_rules:
            item = {
                "table": rule.table,
                "column": rule.column,
                "predicate": rule.predicate,
                "execution_count": rule.execution_count,
                "co_occurrence_ratio": rule.co_occurrence_ratio,
            }
            for fp in rule.source_fingerprints:
                filters_by_fp.setdefault(fp, []).append(item)

    summaries: list[dict[str, Any]] = []
    for tpl in top_clusters:
        summaries.append(
            {
                "fingerprint": tpl.fingerprint,
                "template_sql": tpl.template_sql,
                "tables": list(tpl.tables),
                "execution_count": tpl.execution_count,
                "distinct_callers": tpl.distinct_callers,
                "dialect": tpl.dialect,
                "metrics": metrics_by_fp.get(tpl.fingerprint, [])[:10],
                "filters": filters_by_fp.get(tpl.fingerprint, [])[:10],
            }
        )
    return summaries


def _build_synthesis_inputs(
    *,
    run_id: str,
    top_clusters: Sequence[QueryTemplateCluster],
    total_clusters: int,
    top_n: int,
    mining_result: QueryHistoryMiningResult | None,
    passed_outcomes: Sequence[FkProbeOutcome],
    enable_fk_probing: bool,
) -> tuple[
    str,
    dict[str, Any],
    list[dict[str, str]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    cluster_summaries = _build_cluster_summaries(top_clusters, mining_result)
    fk_probe_summaries = [
        _fk_outcome_aggregate_evidence(outcome, run_id=run_id)
        for outcome in passed_outcomes
    ]
    sql_blocks = [
        {"sql": tpl.template_sql, "dialect": tpl.dialect} for tpl in top_clusters
    ]

    top_fps = {tpl.fingerprint for tpl in top_clusters}
    relevant_metrics: list[dict[str, Any]] = []
    relevant_filters: list[dict[str, Any]] = []
    if mining_result is not None:
        for m in mining_result.metrics:
            if not top_fps or any(fp in top_fps for fp in m.source_fingerprints):
                relevant_metrics.append(m.to_dict())
            if len(relevant_metrics) >= 25:
                break
        for f in mining_result.filter_rules:
            if not top_fps or any(fp in top_fps for fp in f.source_fingerprints):
                relevant_filters.append(f.to_dict())
            if len(relevant_filters) >= 25:
                break

    lines = [
        f"冷启动数仓查询历史发现（共聚类 {total_clusters} 类高频脱敏 SQL 模板，取前 {len(top_clusters)} 类进行语义归纳）："
    ]
    for idx, cl in enumerate(cluster_summaries, start=1):
        tbls = ", ".join(cl["tables"]) or "未知表"
        lines.append(
            f"{idx}. [执行 {cl['execution_count']} 次 / 调用方 {cl['distinct_callers']} 人 / 表: {tbls}] "
            f"SQL: {cl['template_sql']}"
        )
        if cl["filters"]:
            flt_txt = "；".join(
                f"{f['table']}.{f['column']}: {f['predicate']}"
                for f in cl["filters"][:5]
            )
            lines.append(f"   高频过滤: {flt_txt}")
        if cl["metrics"]:
            met_txt = "；".join(
                f"{m['key']}={m['expression']}" for m in cl["metrics"][:5]
            )
            lines.append(f"   聚合指标: {met_txt}")

    if fk_probe_summaries:
        lines.append(
            f"\n数据探活已核验通过的外键候选（共 {len(fk_probe_summaries)} 对）："
        )
        for idx, fk in enumerate(fk_probe_summaries, start=1):
            rate = fk["inclusion_rate"]
            rate_str = f"{rate:.2%}" if isinstance(rate, (int, float)) else "N/A"
            lines.append(
                f"{idx}. {fk['from_table']}({', '.join(fk['from_columns'])}) -> "
                f"{fk['to_table']}({', '.join(fk['to_columns'])}), "
                f"inclusion_rate={rate_str}, join_hit={fk['join_hit']}, "
                f"probed_partition={json.dumps(fk['probed_partition'], ensure_ascii=False)}"
            )

    lines.append(
        "\n请结合现有表字段元数据与知识文档归纳高质量知识提案："
        "单表/字段说明、默认过滤约束、外键关系与单一指标口径请路由至 overrides/semantic_overrides.yaml；"
        "跨表业务口径与查询指南请优先合并进已有最匹配 Markdown 文档，无匹配时建议新建 Markdown 文档。"
    )
    description = "\n".join(lines)
    raw_evidence = {
        "run_id": run_id,
        "discovery_run_id": run_id,
        "enable_fk_probing": enable_fk_probing,
        "top_templates": top_n,
        "templates_clustered": total_clusters,
        "templates_synthesized": len(top_clusters),
        "sql_clusters": cluster_summaries,
        "metrics_signals": relevant_metrics,
        "filter_rule_signals": relevant_filters,
        "fk_probe_evidence": fk_probe_summaries,
    }
    return (
        description,
        raw_evidence,
        sql_blocks,
        cluster_summaries,
        fk_probe_summaries,
    )


def _candidate_kind_for_proposal(
    cand: knowledge_synthesizer.SynthesizedProposal,
) -> str:
    if cand.target_kind == "document":
        return "document"
    if cand.target.startswith("relationship:"):
        return "relationship"
    if cand.target.startswith("metric:"):
        return "metric"
    if cand.target.startswith("field:"):
        return "field"
    if cand.target.startswith("table:"):
        return "table"
    return "override"


def _proposal_referenced_tables(
    cand: knowledge_synthesizer.SynthesizedProposal,
) -> set[str]:
    refs: set[str] = set()
    sp = cand.structured_patch or {}
    if isinstance(sp, Mapping):
        for key, entry in (sp.get("tables") or {}).items():
            refs.add(str(key).lower())
            if (
                isinstance(entry, Mapping)
                and entry.get("database")
                and entry.get("name")
            ):
                refs.add(f"{entry['database']}.{entry['name']}".lower())
        for entry in (sp.get("fields") or {}).values():
            if (
                isinstance(entry, Mapping)
                and entry.get("database")
                and entry.get("table")
            ):
                refs.add(f"{entry['database']}.{entry['table']}".lower())
        for entry in (sp.get("metrics") or {}).values():
            if isinstance(entry, Mapping):
                if entry.get("table"):
                    refs.add(str(entry["table"]).lower())
                for tbl in entry.get("tables") or ():
                    refs.add(str(tbl).lower())
        for entry in (sp.get("relationships") or {}).values():
            if isinstance(entry, Mapping):
                if entry.get("from_database") and entry.get("from_table"):
                    refs.add(f"{entry['from_database']}.{entry['from_table']}".lower())
                if entry.get("to_database") and entry.get("to_table"):
                    refs.add(f"{entry['to_database']}.{entry['to_table']}".lower())
    return refs


def _table_matches(ref_set: set[str], table_name: str) -> bool:
    tlow = table_name.lower()
    if tlow in ref_set:
        return True
    short = ".".join(tlow.split(".")[-2:])
    return short in ref_set or any(
        r == tlow or r == short or r.endswith(f".{short}") for r in ref_set
    )


def _enrich_proposal_evidence(
    cand: knowledge_synthesizer.SynthesizedProposal,
    *,
    run_id: str,
    enable_fk_probing: bool,
    cluster_summaries: Sequence[Mapping[str, Any]],
    passed_outcomes: Sequence[FkProbeOutcome],
) -> tuple[dict[str, Any], set[frozenset]]:
    cand_kind = _candidate_kind_for_proposal(cand)
    ref_tables = _proposal_referenced_tables(cand)
    matching_clusters = [
        cl
        for cl in cluster_summaries
        if ref_tables
        and any(_table_matches(ref_tables, str(t)) for t in (cl.get("tables") or ()))
    ]
    if not matching_clusters:
        matching_clusters = list(cluster_summaries)

    total_freq = sum(int(cl.get("execution_count", 0) or 0) for cl in matching_clusters)
    max_callers = max(
        (int(cl.get("distinct_callers", 0) or 0) for cl in matching_clusters),
        default=0,
    )
    trigger_evidence: dict[str, Any] = {
        "run_id": run_id,
        "discovery_run_id": run_id,
        "candidate_kind": cand_kind,
        "enable_fk_probing": enable_fk_probing,
        "query_history_frequency": total_freq,
        "distinct_callers": max_callers,
        "sql_clusters": [
            {
                "fingerprint": cl["fingerprint"],
                "template_sql": cl["template_sql"],
                "tables": list(cl["tables"]),
                "execution_count": cl["execution_count"],
                "distinct_callers": cl["distinct_callers"],
                "dialect": cl["dialect"],
            }
            for cl in matching_clusters[:10]
        ],
        "llm_rationale": cand.reason,
        "probing_status": "not_applicable",
    }

    covered_fk_pairs: set[frozenset] = set()
    outcome_by_pair = {
        outcome.candidate.normalized_pair_key(): outcome for outcome in passed_outcomes
    }
    matched_fk_evs: list[dict[str, Any]] = []
    if isinstance(cand.structured_patch, Mapping):
        for rel_entry in (cand.structured_patch.get("relationships") or {}).values():
            if not isinstance(rel_entry, Mapping):
                continue
            pair = _rel_entry_pair(rel_entry)
            if pair is not None and pair in outcome_by_pair:
                outcome = outcome_by_pair[pair]
                covered_fk_pairs.add(pair)
                fk_ev = _fk_outcome_aggregate_evidence(outcome, run_id=run_id)
                matched_fk_evs.append(fk_ev)
                trigger_evidence.update(
                    {
                        "from_table": fk_ev["from_table"],
                        "from_columns": fk_ev["from_columns"],
                        "to_table": fk_ev["to_table"],
                        "to_columns": fk_ev["to_columns"],
                        "sample_distinct_count": fk_ev["sample_distinct_count"],
                        "matched_count": fk_ev["matched_count"],
                        "overlap_ratio": fk_ev["overlap_ratio"],
                        "inclusion_rate": fk_ev["inclusion_rate"],
                        "join_matched": fk_ev["join_matched"],
                        "join_hit": fk_ev["join_hit"],
                        "probe_partitions": fk_ev["probe_partitions"],
                        "probed_partition": fk_ev["probed_partition"],
                        "probing_status": fk_ev["probing_status"],
                    }
                )

    if enable_fk_probing and passed_outcomes and not matched_fk_evs:
        text_haystack = f"{cand.target} {cand.patch} {cand.resolved_path}".lower()
        for outcome in passed_outcomes:
            ft = outcome.candidate.from_table.lower()
            tt = outcome.candidate.to_table.lower()
            ft_short = ".".join(ft.split(".")[-2:])
            tt_short = ".".join(tt.split(".")[-2:])
            if (
                _table_matches(ref_tables, ft)
                or _table_matches(ref_tables, tt)
                or ft_short in text_haystack
                or tt_short in text_haystack
            ):
                matched_fk_evs.append(
                    _fk_outcome_aggregate_evidence(outcome, run_id=run_id)
                )

    if matched_fk_evs:
        trigger_evidence["fk_probe_evidence"] = matched_fk_evs
    return trigger_evidence, covered_fk_pairs


def _proposal_duplicates_existing_relationship(
    cand: knowledge_synthesizer.SynthesizedProposal,
    existing_rels: set[frozenset],
) -> bool:
    if cand.target_kind != "override" or not isinstance(cand.structured_patch, Mapping):
        return False
    rels = cand.structured_patch.get("relationships")
    if not isinstance(rels, Mapping) or not rels:
        return False
    if any(cand.structured_patch.get(sec) for sec in ("tables", "fields", "metrics")):
        return False
    pairs = [
        _rel_entry_pair(entry) for entry in rels.values() if isinstance(entry, Mapping)
    ]
    valid_pairs = [p for p in pairs if p is not None]
    return bool(valid_pairs) and all(p in existing_rels for p in valid_pairs)


def _submit_synthesized_proposal(
    *,
    project_id: str,
    actor_requester: Requester,
    cand: knowledge_synthesizer.SynthesizedProposal,
    trigger_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    anti_dup_checks = [
        c
        for c in (cand.gate_report.get("checks") or ())
        if isinstance(c, Mapping) and c.get("name") == "anti_duplication"
    ]
    extra_gate = {
        "checks": anti_dup_checks,
        "discovery_evidence": dict(trigger_evidence),
    }
    eval_cand = {"reference_sql": cand.reference_sql} if cand.reference_sql else None

    def _call(target_value: str) -> dict[str, Any]:
        with (
            bind_mcp_context(actor_requester),
            corrections.trusted_discovery_source(),
        ):
            return corrections.propose_in_project(
                project_id,
                target=target_value,
                patch=cand.patch,
                reason=cand.reason,
                source="discovery",
                target_kind=cand.target_kind,
                structured_patch=cand.structured_patch,
                eval_candidate=eval_cand,
                extra_gate_report=extra_gate,
                evidence=dict(trigger_evidence),
            )

    try:
        return _call(cand.target)
    except ValueError as exc:
        if (
            cand.target_kind == "document"
            and cand.resolved_path
            and "Document target must live under" in str(exc)
        ):
            fallback_target = f"document:{cand.resolved_path}"
            if fallback_target != cand.target:
                return _call(fallback_target)
        raise


def _build_run_summary(
    *,
    templates_clustered: int,
    templates_synthesized: int,
    proposals_created: int,
    duplicates_skipped: int,
    l1_rejected: int,
    candidates_passed: int,
    eval_drafts_created: int,
    llm_error: str | None,
) -> str:
    parts: list[str] = []
    if templates_clustered > 0:
        parts.append(
            f"已聚类 {templates_clustered} 个高频 SQL 模板（归纳前 {templates_synthesized} 个）"
        )
    if candidates_passed > 0:
        parts.append(f"验证通过 {candidates_passed} 对外键关系")
    parts.append(f"生成 {proposals_created} 条待审批纠错提案")
    if duplicates_skipped:
        parts.append(f"跳过 {duplicates_skipped} 条重复提案")
    if l1_rejected:
        parts.append(f"拦截 {l1_rejected} 条 L1 校验未通过提案")
    if eval_drafts_created:
        parts.append(f"生成 {eval_drafts_created} 条待确认评测草稿")
    if llm_error:
        parts.append(f"LLM 提示：{llm_error}")
    return "，".join(parts) + "。"


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


async def _execute_discovery_run_async(
    run_id: str,
    owner_id: str,
    *,
    llm_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
) -> None:
    with sessions()() as db:
        row = db.get(DiscoveryRun, run_id)
        if row is None or row.status != "running":
            return
        project_id = row.project_id
        mine_query_history_flag = bool(row.mine_query_history)
        probe_relationships_flag = bool(row.probe_relationships)
        seed_eval_drafts_flag = bool(row.seed_eval_drafts)
        cfg = _loads_json(row.config_snapshot, {})
        if not isinstance(cfg, Mapping):
            cfg = {}

    actor_requester = _requester_for_owner(owner_id)
    try:
        top_n = max(
            1,
            min(
                MAX_TOP_TEMPLATES,
                int(cfg.get("top_templates") or DEFAULT_TOP_TEMPLATES),
            ),
        )
    except (TypeError, ValueError):
        top_n = DEFAULT_TOP_TEMPLATES

    queries_fetched = 0
    templates_clustered = 0
    templates_synthesized = 0
    candidates_recalled = 0
    candidates_probed = 0
    candidates_passed = 0
    candidates_skipped = 0
    candidates_failed = 0
    proposals_created = 0
    duplicates_skipped = 0
    l1_rejected = 0
    eval_drafts_created = 0
    proposal_ids: list[str] = []
    llm_used = False
    llm_error: str | None = None
    had_partial_error = False
    partial_reasons: list[str] = []

    try:
        runtime = discovery_executor.resolve_discovery_dtb_runtime(
            owner_id, project_id=project_id
        )
    except DiscoveryExecutionError as exc:
        err_msg = _scrub_error_summary(exc.detail)
        _update_run_fields(
            run_id,
            status="failed",
            error=err_msg,
            finished_at=datetime.now(UTC),
            run_stats={"summary": err_msg or ""},
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

    # Stage 1: Query history fetch & deterministic literal scrubbing / template clustering
    mining_result: QueryHistoryMiningResult | None = None
    top_clusters: list[QueryTemplateCluster] = []
    if mine_query_history_flag or seed_eval_drafts_flag:
        try:
            fetched = await discovery_executor.fetch_query_history(
                owner_id=owner_id,
                config=cfg,
                runtime=runtime,
                project_id=project_id,
            )
            queries_fetched = fetched.raw_row_count
            mining_result = query_history_miner.mine_query_history(
                fetched.records,
                tables=tables,
                project_id=project_id,
                min_frequency=int(cfg.get("min_frequency", 3)),
                enable_llm=False,
            )
            templates_clustered = len(mining_result.templates)
            top_clusters = (
                list(mining_result.templates[:top_n]) if mine_query_history_flag else []
            )
            templates_synthesized = len(top_clusters)
            _update_run_fields(
                run_id,
                queries_fetched=queries_fetched,
                templates_clustered=templates_clustered,
                run_stats={"templates_synthesized": templates_synthesized},
            )
        except DiscoveryAuthenticationError as exc:
            err_msg = _scrub_error_summary(exc.detail)
            _update_run_fields(
                run_id,
                status="failed",
                queries_fetched=queries_fetched,
                templates_clustered=templates_clustered,
                error=err_msg,
                finished_at=datetime.now(UTC),
                run_stats={"summary": err_msg or ""},
            )
            return
        except DiscoveryExecutionError as exc:
            had_partial_error = True
            partial_reasons.append(exc.detail)
            if not probe_relationships_flag:
                err_msg = _scrub_error_summary(exc.detail)
                _update_run_fields(
                    run_id,
                    status="failed",
                    error=err_msg,
                    finished_at=datetime.now(UTC),
                    run_stats={"summary": err_msg or ""},
                )
                return

    # Stage 1.5 (opt-in): Three-stage FK recall & bounded partition-pruned data probing
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
            err_msg = _scrub_error_summary(exc.detail)
            _update_run_fields(
                run_id,
                status="failed",
                queries_fetched=queries_fetched,
                templates_clustered=templates_clustered,
                candidates_recalled=candidates_recalled,
                error=err_msg,
                finished_at=datetime.now(UTC),
                run_stats={
                    "templates_synthesized": templates_synthesized,
                    "summary": err_msg or "",
                },
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

    # Record non-passed FK probe outcomes immediately; retain passed outcomes for Stage 2
    dedup = _build_dedup_state(project_id, tables)
    passed_outcomes: list[FkProbeOutcome] = []
    for outcome in probe_outcomes:
        if outcome.decision == "passed":
            passed_outcomes.append(outcome)
            continue
        cand = outcome.candidate
        evidence = _fk_outcome_aggregate_evidence(outcome, run_id=run_id)
        _record_candidate(
            run_id=run_id,
            project_id=project_id,
            candidate_kind="relationship",
            target=cand.target_ref(),
            evidence=evidence,
            decision=outcome.decision,
            reason=outcome.reason,
        )

    # Stage 2: Two-stage LLM business summarization via unified knowledge_synthesizer
    covered_fk_pairs: set[frozenset] = set()
    should_synthesize = bool(mine_query_history_flag and top_clusters)
    if not should_synthesize and passed_outcomes:
        has_llm = llm_fn is not None or (
            knowledge_synthesizer.resolve_llm_provider(
                project_id,
                source_type="discovery",
                provider_id=cfg.get("provider_id"),
                model=cfg.get("model"),
            )
            is not None
        )
        should_synthesize = has_llm

    if should_synthesize:
        description, raw_evidence, sql_blocks, cluster_summaries, _ = (
            _build_synthesis_inputs(
                run_id=run_id,
                top_clusters=top_clusters,
                total_clusters=templates_clustered,
                top_n=top_n,
                mining_result=mining_result,
                passed_outcomes=passed_outcomes,
                enable_fk_probing=probe_relationships_flag,
            )
        )
        synthesized: list[knowledge_synthesizer.SynthesizedProposal] = []
        llm_failed_explicitly = False
        try:
            synthesized = knowledge_synthesizer.synthesize_proposals(
                project_id=project_id,
                source_type="discovery",
                description=description,
                raw_evidence=raw_evidence,
                sql_blocks=sql_blocks,
                provider_id=cfg.get("provider_id"),
                model=cfg.get("model"),
                allow_multi=True,
                skip_duplicates=False,
                llm_fn=llm_fn,
            )
            llm_used = True
        except knowledge_synthesizer.LLMUnavailableError as exc:
            if exc.message == _EMPTY_PROPOSALS_MSG:
                llm_used = True
                synthesized = []
            else:
                llm_used = False
                llm_failed_explicitly = True
                llm_error = _scrub_error_summary(str(exc)) or "大模型不可用"

        if llm_failed_explicitly:
            if not probe_relationships_flag:
                final_err = llm_error or "大模型不可用，冷启动知识总结失败"
                _update_run_fields(
                    run_id,
                    status="failed",
                    queries_fetched=queries_fetched,
                    templates_clustered=templates_clustered,
                    candidates_recalled=candidates_recalled,
                    candidates_probed=candidates_probed,
                    candidates_passed=candidates_passed,
                    candidates_skipped=candidates_skipped,
                    candidates_failed=candidates_failed,
                    proposals_created=0,
                    eval_drafts_created=0,
                    error=final_err,
                    finished_at=datetime.now(UTC),
                    run_stats={
                        "templates_synthesized": templates_synthesized,
                        "duplicates_skipped": 0,
                        "l1_rejected": 0,
                        "proposal_ids": [],
                        "llm_used": False,
                        "llm_error": final_err,
                        "summary": final_err,
                    },
                )
                return
            had_partial_error = True
            partial_reasons.append(
                f"大模型归纳不可用（已保留验证通过的外键探活证据）：{llm_error}"
            )
        else:
            for cand in synthesized:
                cand_kind = _candidate_kind_for_proposal(cand)
                trigger_evidence, matched_fk_pairs = _enrich_proposal_evidence(
                    cand,
                    run_id=run_id,
                    enable_fk_probing=probe_relationships_flag,
                    cluster_summaries=cluster_summaries,
                    passed_outcomes=passed_outcomes,
                )
                if cand.is_duplicate or _proposal_duplicates_existing_relationship(
                    cand, dedup["relationships"]
                ):
                    duplicates_skipped += 1
                    _record_candidate(
                        run_id=run_id,
                        project_id=project_id,
                        candidate_kind=cand_kind,
                        target=cand.target,
                        evidence=trigger_evidence,
                        decision="deduplicated",
                        reason=cand.duplicate_reason or "already_exists_or_pending",
                    )
                    covered_fk_pairs.update(
                        p for p in matched_fk_pairs if p in dedup["relationships"]
                    )
                    continue

                if not cand.gate_report.get("passed", False):
                    l1_rejected += 1
                    err_text = (
                        "; ".join(
                            str(e) for e in (cand.gate_report.get("errors") or ())
                        )
                        or "l1_validation_failed"
                    )
                    _record_candidate(
                        run_id=run_id,
                        project_id=project_id,
                        candidate_kind=cand_kind,
                        target=cand.target,
                        evidence=trigger_evidence,
                        decision="rejected",
                        reason=err_text,
                    )
                    continue

                try:
                    created = _submit_synthesized_proposal(
                        project_id=project_id,
                        actor_requester=actor_requester,
                        cand=cand,
                        trigger_evidence=trigger_evidence,
                    )
                except GateValidationError as exc:
                    l1_rejected += 1
                    _record_candidate(
                        run_id=run_id,
                        project_id=project_id,
                        candidate_kind=cand_kind,
                        target=cand.target,
                        evidence=trigger_evidence,
                        decision="rejected",
                        reason=str(exc),
                    )
                except ValueError as exc:
                    msg = str(exc)
                    if "does not change" in msg:
                        duplicates_skipped += 1
                        covered_fk_pairs.update(
                            p for p in matched_fk_pairs if p in dedup["relationships"]
                        )
                        _record_candidate(
                            run_id=run_id,
                            project_id=project_id,
                            candidate_kind=cand_kind,
                            target=cand.target,
                            evidence=trigger_evidence,
                            decision="deduplicated",
                            reason=msg,
                        )
                    else:
                        had_partial_error = True
                        partial_reasons.append(msg)
                        _record_candidate(
                            run_id=run_id,
                            project_id=project_id,
                            candidate_kind=cand_kind,
                            target=cand.target,
                            evidence=trigger_evidence,
                            decision="failed",
                            reason=msg,
                        )
                else:
                    proposals_created += 1
                    prop_id = str(created["proposal_id"])
                    proposal_ids.append(prop_id)
                    covered_fk_pairs.update(matched_fk_pairs)
                    if isinstance(cand.structured_patch, Mapping):
                        for rel_entry in (
                            cand.structured_patch.get("relationships") or {}
                        ).values():
                            if isinstance(rel_entry, Mapping):
                                pair = _rel_entry_pair(rel_entry)
                                if pair is not None:
                                    dedup["relationships"].add(pair)
                    _record_candidate(
                        run_id=run_id,
                        project_id=project_id,
                        candidate_kind=cand_kind,
                        target=str(created.get("target") or cand.target),
                        evidence=trigger_evidence,
                        decision="proposed",
                        proposal_id=prop_id,
                    )

    # Persist any verified FK probe relationships not already covered by LLM synthesis
    for outcome in passed_outcomes:
        cand = outcome.candidate
        pair_key = cand.normalized_pair_key()
        if pair_key in covered_fk_pairs:
            continue
        evidence = _fk_outcome_aggregate_evidence(outcome, run_id=run_id)
        target_ref = cand.target_ref()
        if pair_key in dedup["relationships"]:
            duplicates_skipped += 1
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
        evidence["llm_rationale"] = reason_text
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
                    evidence=evidence,
                )
        except GateValidationError as exc:
            l1_rejected += 1
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
            if "does not change" in msg:
                duplicates_skipped += 1
                dec = "deduplicated"
            else:
                dec = "failed"
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
            prop_id = str(created["proposal_id"])
            proposal_ids.append(prop_id)
            _record_candidate(
                run_id=run_id,
                project_id=project_id,
                candidate_kind="relationship",
                target=target_ref,
                evidence=evidence,
                decision="proposed",
                proposal_id=prop_id,
            )

    # Stage 3: Optional cold-start historical ticket proposal seeding (OFF by default)
    if seed_eval_drafts_flag and mining_result is not None:
        from . import historical_tickets

        for tpl in mining_result.templates[:MAX_EVAL_DRAFTS_PER_RUN]:
            target_ref = f"historical_ticket:{tpl.fingerprint[:12]}"
            candidate_sql, eval_dialect = _render_synthetic_eval_sql(tpl, tables)
            question, caliber = _build_eval_draft_question(tpl)
            evidence = {
                "run_id": run_id,
                "candidate_kind": "historical_ticket",
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
                    candidate_kind="historical_ticket",
                    target=target_ref,
                    evidence=evidence,
                    decision="rejected",
                    reason=f"validate_sql_failed: {err or 'invalid_sql'}",
                )
                continue

            decision, prop_id, err_reason, ticket_ev = (
                historical_tickets.propose_ticket_from_discovery(
                    project_id,
                    run_id=run_id,
                    tpl=tpl,
                    candidate_sql=candidate_sql,
                    eval_dialect=eval_dialect,
                    fallback_title=question,
                    fallback_caliber=caliber,
                    requester=actor_requester,
                )
            )
            merged_ev = {**evidence, **(ticket_ev or {})}
            if decision == "proposed" and prop_id:
                eval_drafts_created += 1
                proposals_created += 1
                proposal_ids.append(prop_id)
            elif decision == "deduplicated":
                duplicates_skipped += 1
            elif decision == "rejected":
                l1_rejected += 1
            _record_candidate(
                run_id=run_id,
                project_id=project_id,
                candidate_kind="historical_ticket",
                target=target_ref,
                evidence=merged_ev,
                decision=decision,
                reason=err_reason,
                proposal_id=prop_id,
            )

    has_any_valid_progress = (
        proposals_created > 0
        or eval_drafts_created > 0
        or candidates_passed > 0
        or candidates_skipped > 0
        or (candidates_probed > candidates_failed)
        or (templates_clustered > 0 and not llm_error)
    )
    if had_partial_error:
        final_status = "partial" if has_any_valid_progress else "failed"
        final_error = _scrub_error_summary("; ".join(dict.fromkeys(partial_reasons)))
    else:
        final_status = "completed"
        final_error = None

    summary_text = _build_run_summary(
        templates_clustered=templates_clustered,
        templates_synthesized=templates_synthesized,
        proposals_created=proposals_created,
        duplicates_skipped=duplicates_skipped,
        l1_rejected=l1_rejected,
        candidates_passed=candidates_passed,
        eval_drafts_created=eval_drafts_created,
        llm_error=llm_error,
    )

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
        run_stats={
            "templates_synthesized": templates_synthesized,
            "duplicates_skipped": duplicates_skipped,
            "l1_rejected": l1_rejected,
            "proposal_ids": proposal_ids,
            "llm_used": llm_used,
            "llm_error": llm_error,
            "summary": summary_text,
        },
    )


def execute_discovery_run(
    run_id: str,
    *,
    owner_id: str,
    llm_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
) -> None:
    """Synchronous entry point executed by the background worker thread."""
    try:
        asyncio.run(_execute_discovery_run_async(run_id, owner_id, llm_fn=llm_fn))
    except Exception:
        logger.exception("Unhandled error in discovery run %s", run_id)
        _update_run_fields(
            run_id,
            status="failed",
            error="知识发现运行发生内部异常",
            finished_at=datetime.now(UTC),
            run_stats={"summary": "知识发现运行发生内部异常"},
        )
