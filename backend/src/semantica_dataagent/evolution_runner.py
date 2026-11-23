"""Single-hypothesis Evolver loop, startup recovery, and proposal emission (#296)."""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError

from . import (
    corrections,
    eval_questions,
    evolution_gate,
    knowledge,
    project_context,
    provider_probe,
    semantic_overrides,
    trajectory_miner,
)
from . import eval as eval_mod
from .api_time import as_utc as _as_utc
from .auth import Requester
from .db import (
    CorrectionProposal,
    EvolutionCheckpoint,
    EvolutionRound,
    EvolutionRun,
    Provider,
    sessions,
)
from .mcp_context import bind_mcp_context
from .provider_api import provider_config
from .source_directory import relative

logger = logging.getLogger(__name__)
DEFAULT_EVOLVER_TIMEOUT_SECONDS = 60.0


class EvolutionConflictError(RuntimeError):
    """Raised when attempting to start a concurrent evolution run on one project."""


class EvolutionFrozenError(RuntimeError):
    """Raised when attempting to run evolution on an archived or deleting project."""


def _loads_json(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def serialize_round(row: EvolutionRound) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "project_id": row.project_id,
        "round_number": row.round_number,
        "signal_kind": row.signal_kind,
        "hypothesis": row.hypothesis,
        "evidence_trajectory_ids": _loads_json(row.evidence_trajectory_ids, []),
        "target": row.target,
        "target_kind": row.target_kind,
        "candidate_change": _loads_json(row.candidate_change, {}),
        "l1_gate_report": _loads_json(row.l1_gate_report, None),
        "l2_gate_report": _loads_json(row.l2_gate_report, None),
        "decision": row.decision,
        "reject_reason": row.reject_reason,
        "proposal_id": row.proposal_id,
        "started_at": _as_utc(row.started_at),
        "finished_at": _as_utc(row.finished_at),
    }


def serialize_run(
    row: EvolutionRun, rounds: Sequence[EvolutionRound] = ()
) -> dict[str, Any]:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "trigger_type": row.trigger_type,
        "status": row.status,
        "checkpoint_before_trajectory_id": row.checkpoint_before_trajectory_id,
        "checkpoint_after_trajectory_id": row.checkpoint_after_trajectory_id,
        "frozen_max_trajectory_id": row.frozen_max_trajectory_id,
        "diagnosis_trajectory_ids": _loads_json(row.diagnosis_trajectory_ids, []),
        "reserve_trajectory_ids": _loads_json(row.reserve_trajectory_ids, []),
        "signal_summary": _loads_json(row.signal_summary, {}),
        "max_rounds": row.max_rounds,
        "max_eval_cases": row.max_eval_cases,
        "rounds_completed": row.rounds_completed,
        "stop_reason": row.stop_reason,
        "error": row.error,
        "proposal_id": row.proposal_id,
        "created_by": row.created_by,
        "started_at": _as_utc(row.started_at),
        "finished_at": _as_utc(row.finished_at),
        "created_at": _as_utc(row.created_at),
        "rounds": [serialize_round(rnd) for rnd in rounds],
    }


def _find_emitted_proposal_for_run(
    db, project_id: str, run_id: str, rounds: Sequence[EvolutionRound]
) -> tuple[CorrectionProposal | None, EvolutionRound | None]:
    for rnd in reversed(rounds):
        if rnd.proposal_id:
            prop = db.get(CorrectionProposal, rnd.proposal_id)
            if prop is not None:
                return prop, rnd
    evo_props = (
        db.query(CorrectionProposal)
        .filter(
            CorrectionProposal.project_id == project_id,
            CorrectionProposal.source == "evolution",
        )
        .order_by(CorrectionProposal.created_at.desc())
        .all()
    )
    for prop in evo_props:
        gate_data = _loads_json(prop.gate_report, {})
        prov = gate_data.get("provenance") if isinstance(gate_data, dict) else None
        if isinstance(prov, dict) and prov.get("evolution_run_id") == run_id:
            round_num = prov.get("round_number")
            matched_round = next(
                (r for r in rounds if r.round_number == round_num),
                rounds[-1] if rounds else None,
            )
            return prop, matched_round
    return None, None


def _finalize_accepted_run_in_db(
    db,
    *,
    run_id: str,
    round_id: str | None,
    proposal_id: str,
    l1_json: str | None,
    l2_json: str | None,
    frozen_max_trajectory_id: int | None,
    round_number: int | None,
    accepted_at: datetime,
) -> tuple[EvolutionRun, list[EvolutionRound]]:
    if round_id is not None:
        rnd = db.get(EvolutionRound, round_id)
        if rnd is not None:
            if l1_json is not None:
                rnd.l1_gate_report = l1_json
            if l2_json is not None:
                rnd.l2_gate_report = l2_json
            rnd.decision = "accept"
            rnd.proposal_id = proposal_id
            rnd.reject_reason = None
            rnd.finished_at = accepted_at

    run = db.get(EvolutionRun, run_id)
    assert run is not None
    ckpt_row = db.get(EvolutionCheckpoint, run.project_id)
    target_ckpt_id = (
        frozen_max_trajectory_id
        if frozen_max_trajectory_id is not None
        else run.frozen_max_trajectory_id
    )
    if ckpt_row is None:
        ckpt_row = EvolutionCheckpoint(
            project_id=run.project_id,
            checkpoint_trajectory_id=target_ckpt_id,
            checkpoint_at=accepted_at,
            last_run_id=run_id,
            updated_at=accepted_at,
        )
        db.add(ckpt_row)
    else:
        if target_ckpt_id is not None:
            ckpt_row.checkpoint_trajectory_id = target_ckpt_id
        ckpt_row.checkpoint_at = accepted_at
        ckpt_row.last_run_id = run_id
        ckpt_row.updated_at = accepted_at

    run.status = "accepted"
    run.stop_reason = "accepted"
    run.error = None
    run.proposal_id = proposal_id
    run.checkpoint_after_trajectory_id = ckpt_row.checkpoint_trajectory_id
    if round_number is not None:
        run.rounds_completed = round_number
    run.finished_at = accepted_at
    rounds = (
        db.query(EvolutionRound)
        .filter(EvolutionRound.run_id == run_id)
        .order_by(EvolutionRound.round_number.asc())
        .all()
    )
    return run, rounds


def recover_interrupted_evolution_runs() -> int:
    """Reconcile or abort leftover running evolution runs and clean candidate generations.

    If a run crashed after emitting a ``CorrectionProposal``, finalize the accepted
    run/round/checkpoint consistently so restarts never duplicate proposals. Otherwise
    mark the interrupted run and unfinished rounds ``incomplete`` without advancing
    the checkpoint.
    """
    now = datetime.now(UTC)
    reconcile_items: list[tuple[str, str | None, str, str, int | None, int | None]] = []
    incomplete_ids: list[str] = []

    with sessions()() as db:
        running_runs = (
            db.query(EvolutionRun).filter(EvolutionRun.status == "running").all()
        )
        for run in running_runs:
            rounds = (
                db.query(EvolutionRound)
                .filter(EvolutionRound.run_id == run.id)
                .order_by(EvolutionRound.round_number.asc())
                .all()
            )
            prop, matched_round = _find_emitted_proposal_for_run(
                db, run.project_id, run.id, rounds
            )
            if prop is not None:
                reconcile_items.append(
                    (
                        run.id,
                        matched_round.id if matched_round else None,
                        prop.id,
                        prop.status,
                        run.frozen_max_trajectory_id,
                        matched_round.round_number if matched_round else None,
                    )
                )
            else:
                incomplete_ids.append(run.id)

    for _run_id, _rnd_id, prop_id, prop_status, _fmax, _rnum in reconcile_items:
        if prop_status == "applying":
            corrections.recover_applying(only_id=prop_id)

    recovered = 0
    with sessions().begin() as db:
        for (
            run_id,
            round_id,
            prop_id,
            _prop_status,
            frozen_max,
            round_num,
        ) in reconcile_items:
            prop = db.get(CorrectionProposal, prop_id)
            gate_data = _loads_json(prop.gate_report, {}) if prop is not None else {}
            l1_json = (
                json.dumps(gate_data["l1_gate"], ensure_ascii=False)
                if isinstance(gate_data, dict) and "l1_gate" in gate_data
                else None
            )
            l2_json = (
                json.dumps(gate_data["l2_gate"], ensure_ascii=False)
                if isinstance(gate_data, dict) and "l2_gate" in gate_data
                else None
            )
            _finalize_accepted_run_in_db(
                db,
                run_id=run_id,
                round_id=round_id,
                proposal_id=prop_id,
                l1_json=l1_json,
                l2_json=l2_json,
                frozen_max_trajectory_id=frozen_max,
                round_number=round_num,
                accepted_at=now,
            )
            recovered += 1

        if incomplete_ids:
            db.execute(
                update(EvolutionRun)
                .where(
                    EvolutionRun.id.in_(incomplete_ids),
                    EvolutionRun.status == "running",
                )
                .values(
                    status="incomplete",
                    stop_reason="interrupted",
                    error="Evolution run interrupted by application restart",
                    finished_at=now,
                )
            )
            db.execute(
                update(EvolutionRound)
                .where(
                    EvolutionRound.run_id.in_(incomplete_ids),
                    EvolutionRound.finished_at.is_(None),
                )
                .values(
                    decision="incomplete",
                    proposal_id=None,
                    reject_reason="Evolution run interrupted by application restart",
                    finished_at=now,
                )
            )
            recovered += len(incomplete_ids)

    knowledge.cleanup_candidate_generations()
    return recovered


def _resolve_configured_provider(provider_id: str | None, model: str | None):
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
            "Default assistant provider unavailable for evolution fallback",
            exc_info=True,
        )
    return None, model


def _resolve_run_provider_pair(
    override_provider: Any | None,
    override_model: str | None,
    cfg_provider_id: str | None,
    cfg_model: str | None,
) -> tuple[Any | None, str | None]:
    if override_provider is not None:
        return override_provider, override_model or cfg_model
    resolved_provider, resolved_model = _resolve_configured_provider(
        cfg_provider_id, cfg_model
    )
    return resolved_provider, override_model or resolved_model or cfg_model


def _schema_tables_for_project(project_id: str) -> list[dict[str, Any]]:
    try:
        return knowledge.describe(summary=False, project_id=project_id) or []
    except Exception:  # noqa: BLE001 - schema may be unbuilt in tests/cold projects
        return []


def _parse_table_ref(
    ref: str | None, tables_meta: list[dict[str, Any]]
) -> dict[str, str] | None:
    if not ref:
        return None
    cleaned = ref.strip()
    for prefix in ("table:", "field:"):
        cleaned = cleaned.removeprefix(prefix)
    parts = [p.strip() for p in cleaned.split(".") if p.strip()]
    if ref.startswith("field:") and len(parts) >= 3:
        parts = parts[:-1]
    if len(parts) == 3:
        cat, db_name, tbl_name = parts
        return {"catalog": cat, "database": db_name, "name": tbl_name}
    if len(parts) == 2:
        db_name, tbl_name = parts
        for item in tables_meta:
            if item.get("database") == db_name and item.get("name") == tbl_name:
                res = {"database": db_name, "name": tbl_name}
                if item.get("catalog"):
                    res["catalog"] = str(item["catalog"])
                return res
        return {"database": db_name, "name": tbl_name}
    if len(parts) == 1 and tables_meta:
        for item in tables_meta:
            if item.get("name") == parts[0]:
                res = {"database": str(item["database"]), "name": parts[0]}
                if item.get("catalog"):
                    res["catalog"] = str(item["catalog"])
                return res
    return None


def _table_key(tbl: Mapping[str, str]) -> str:
    if tbl.get("catalog"):
        return f"{tbl['catalog']}.{tbl['database']}.{tbl['name']}"
    return f"{tbl['database']}.{tbl['name']}"


def _load_editable_document(
    project_id: str, doc_ref: str
) -> tuple[str | None, str | None]:
    try:
        ctx = project_context.current_project(project_id)
        rows = knowledge.execute(
            knowledge.read_graph(ctx.values),
            "MATCH (n:SDADocument {generation:$generation, ref:$ref}) RETURN properties(n) AS node",
            {"generation": ctx.generation, "ref": doc_ref},
        )
        if len(rows) == 1 and isinstance(rows[0].get("node"), dict):
            node = rows[0]["node"]
            if not node.get("generated") and node.get("source_file"):
                return str(node["source_file"]), str(node.get("content") or "")
    except Exception:
        logger.debug(
            "Failed to load editable document %s for %s",
            doc_ref,
            project_id,
            exc_info=True,
        )
    return None, None


def _synthesize_deterministic_candidate(
    project_id: str,
    signal: trajectory_miner.MinedSignal,
    *,
    round_number: int,
    tables_meta: list[dict[str, Any]],
) -> dict[str, Any]:
    """Deterministically map one mined signal cluster to a single-hypothesis candidate."""
    ctx = project_context.current_project(project_id)
    src_root = Path(ctx.source_directory_value) if ctx.source_directory_value else None

    if signal.signal_kind == "divergent_clarification":
        variants = signal.caliber_variants or []
        prompt_text = str(
            signal.details.get("clarification_prompt") or signal.summary
        ).strip()
        variant_lines = [
            f"- 口径变体 {idx + 1}：{v.get('label')}（引用：`{v.get('ref') or '待补充'}`，出现 {v.get('count', 1)} 次）"
            for idx, v in enumerate(variants)
        ]
        # Prefer editing an existing editable document or creating under knowledge/evolution/<file>.md
        slug = re.sub(r"[^A-Za-z0-9]+", "_", prompt_text)[:24].strip("_") or "caliber"
        rel_path = f"knowledge/evolution/{slug}_variants.md"
        doc_ref = f"document:{rel_path}"
        if signal.target_hint and signal.target_hint.startswith("document:"):
            src_file, existing_text = _load_editable_document(
                project_id, signal.target_hint
            )
            if src_file:
                rel_path = src_file
                doc_ref = signal.target_hint
                base_text = existing_text or ""
                if src_root and (src_root / rel_path).is_file():
                    base_text = (src_root / rel_path).read_text(encoding="utf-8")
            elif src_root and (src_root / rel_path).is_file():
                base_text = (src_root / rel_path).read_text(encoding="utf-8")
            else:
                base_text = ""
        elif src_root and (src_root / rel_path).is_file():
            base_text = (src_root / rel_path).read_text(encoding="utf-8")
        else:
            base_text = ""

        section = "\n".join(
            [
                f"# 口径变体说明：{prompt_text}",
                "",
                "当提问未明确指定下列哪一种口径变体时，必须继续发起**澄清**让用户选择，严禁将其合并或拍扁为唯一默认值：",
                "",
                *variant_lines,
                "",
            ]
        )
        new_doc = f"{base_text.rstrip()}\n\n{section}" if base_text.strip() else section
        return {
            "hypothesis": f"澄清词「{prompt_text}」对应 {len(variants)} 种独立口径变体，需分别沉淀边界而非拍扁为单一默认值",
            "signal_kind": signal.signal_kind,
            "target": doc_ref,
            "target_kind": "document",
            "patch": new_doc,
            "structured_patch": None,
            "reason": f"沉淀「{prompt_text}」的 {len(variants)} 种独立口径变体边界并保留澄清触发条件",
        }

    if signal.signal_kind == "sql_self_repair":
        join_pairs = signal.details.get("join_pairs") or []
        if join_pairs:
            pair = join_pairs[0]
            rel_name = (
                f"{pair['from_table']}_to_{pair['to_table']}_"
                f"{'_'.join(pair['from_columns'])}"
            )
            rel_entry: dict[str, Any] = {
                "name": rel_name,
                "from_database": pair["from_database"],
                "from_table": pair["from_table"],
                "from_columns": list(pair["from_columns"]),
                "to_database": pair["to_database"],
                "to_table": pair["to_table"],
                "to_columns": list(pair["to_columns"]),
                "description": "从自愈成功的任务 SQL 提炼的显式外键连接",
                "instructions": f"关联 {pair['from_database']}.{pair['from_table']} 与 {pair['to_database']}.{pair['to_table']} 时使用该等值键",
            }
            if pair.get("from_catalog"):
                rel_entry["from_catalog"] = pair["from_catalog"]
            if pair.get("to_catalog"):
                rel_entry["to_catalog"] = pair["to_catalog"]
            structured = {"version": 1, "relationships": {rel_name: rel_entry}}
            return {
                "hypothesis": f"补充显式外键关系 {rel_name} 可消除多表连接校验失败",
                "signal_kind": signal.signal_kind,
                "target": f"relationship:{rel_name}",
                "target_kind": "override",
                "patch": "",
                "structured_patch": structured,
                "reason": f"依据自愈轨迹提炼显式外键关系 {rel_name}",
            }

        predicates = signal.details.get("where_predicates") or []
        tbl_info = _parse_table_ref(signal.target_hint, tables_meta)
        if tbl_info is None and tables_meta:
            first = tables_meta[0]
            tbl_info = {
                "database": str(first["database"]),
                "name": str(first["name"]),
                **({"catalog": str(first["catalog"])} if first.get("catalog") else {}),
            }
        if tbl_info is not None:
            tkey = _table_key(tbl_info)
            constraint = (
                f"查询时注意过滤条件：{'; '.join(predicates[:2])}"
                if predicates
                else "查询前务必先核验表分区与物理列名"
            )
            entry = {**tbl_info, "instructions": constraint}
            return {
                "hypothesis": f"为 {tkey} 补充使用约束（{constraint}）可避免 validate_sql 首轮报错",
                "signal_kind": signal.signal_kind,
                "target": f"table:{tkey}",
                "target_kind": "override",
                "patch": "",
                "structured_patch": {"version": 1, "tables": {tkey: entry}},
                "reason": f"从 SQL 自愈轨迹提炼 {tkey} 使用约束",
            }

    if signal.signal_kind == "search_detour":
        synonyms = [str(s) for s in (signal.details.get("synonyms") or []) if s]
        tbl_info = _parse_table_ref(signal.target_hint, tables_meta)
        if tbl_info is None and tables_meta:
            first = tables_meta[0]
            tbl_info = {
                "database": str(first["database"]),
                "name": str(first["name"]),
                **({"catalog": str(first["catalog"])} if first.get("catalog") else {}),
            }
        if tbl_info is not None and synonyms:
            tkey = _table_key(tbl_info)
            entry = {
                **tbl_info,
                "synonyms": synonyms[:8],
                "instructions": f"业务说法 {' / '.join(synonyms[:4])} 均指向物理表 {tkey}",
            }
            return {
                "hypothesis": f"为 {tkey} 补充业务同义词 {', '.join(synonyms[:3])} 可减少多轮检索绕路",
                "signal_kind": signal.signal_kind,
                "target": f"table:{tkey}",
                "target_kind": "override",
                "patch": "",
                "structured_patch": {"version": 1, "tables": {tkey: entry}},
                "reason": f"根据检索绕路轨迹为 {tkey} 补充业务同义词",
            }

    if signal.signal_kind == "multi_turn_correction":
        correction_text = str(signal.details.get("correction_text") or signal.summary)
        predicates = signal.details.get("where_predicates") or []
        tbl_info = _parse_table_ref(signal.target_hint, tables_meta)
        if tbl_info is not None:
            tkey = _table_key(tbl_info)
            instr = correction_text[:300]
            if predicates:
                instr = f"{instr}（参考过滤条件：{predicates[0][:150]}）"
            entry = {**tbl_info, "instructions": instr}
            return {
                "hypothesis": f"将用户多轮纠偏明确的表与过滤约束固化至 {tkey}",
                "signal_kind": signal.signal_kind,
                "target": f"table:{tkey}",
                "target_kind": "override",
                "patch": "",
                "structured_patch": {"version": 1, "tables": {tkey: entry}},
                "reason": f"沉淀多轮对话纠偏：{correction_text[:120]}",
            }

    # knowledge_gap_or_failure or document-project fallback -> editable knowledge/*.md or table override
    tbl_info = _parse_table_ref(signal.target_hint, tables_meta)
    if tbl_info is not None and ctx.project_type == "warehouse":
        tkey = _table_key(tbl_info)
        entry = {
            **tbl_info,
            "instructions": f"注意规范限定与口径：{signal.summary[:240]}",
        }
        return {
            "hypothesis": f"为 {tkey} 补充缺口说明以消除同类失败（第 {round_number} 轮）",
            "signal_kind": signal.signal_kind,
            "target": f"table:{tkey}",
            "target_kind": "override",
            "patch": "",
            "structured_patch": {"version": 1, "tables": {tkey: entry}},
            "reason": signal.summary[:200],
        }

    rel_doc = f"knowledge/evolution/gap_r{round_number}_{uuid.uuid4().hex[:6]}.md"
    content = "\n".join(
        [
            f"# 演化补充说明（第 {round_number} 轮）",
            "",
            f"- 问题信号：{signal.summary}",
            "",
        ]
    )
    return {
        "hypothesis": f"补充知识文档 {rel_doc} 填补 {signal.signal_kind} 缺口",
        "signal_kind": signal.signal_kind,
        "target": f"document:{rel_doc}",
        "target_kind": "document",
        "patch": content,
        "structured_patch": None,
        "reason": signal.summary[:200],
    }


def _preserves_all_variants(
    candidate: Mapping[str, Any], signal: trajectory_miner.MinedSignal
) -> bool:
    if (
        signal.signal_kind != "divergent_clarification"
        or len(signal.caliber_variants) < 2
    ):
        return True
    haystack = json.dumps(
        {
            "patch": candidate.get("patch"),
            "structured_patch": candidate.get("structured_patch"),
            "hypothesis": candidate.get("hypothesis"),
        },
        ensure_ascii=False,
    ).casefold()
    matched = 0
    for variant in signal.caliber_variants:
        label = str(variant.get("label") or "").strip().casefold()
        ref = str(variant.get("ref") or "").strip().casefold()
        if (label and label in haystack) or (ref and ref in haystack):
            matched += 1
    return matched >= min(2, len(signal.caliber_variants))


def _call_evolver_llm(
    provider,
    model: str,
    *,
    diagnosis_payload: Mapping[str, Any],
    signal: trajectory_miner.MinedSignal,
    tables_meta: list[dict[str, Any]],
    round_number: int,
    prior_rejections: list[dict[str, Any]],
) -> dict[str, Any] | None:
    prompt = (
        "你是企业知识库自主演化分析器（Single-Hypothesis Evolver）。\n"
        "严格遵循以下约束：\n"
        "1. 本轮只针对一个主因果假设生成一处可验证修改。\n"
        "2. 严禁将存在分化澄清（divergent_clarification）的多口径拍扁为单一默认值，必须保留全部独立口径变体。\n"
        "3. 返回纯 JSON 对象，字段：\n"
        '   {"hypothesis": str, "signal_kind": str, "target": str, "target_kind": "override"|"document"|"ossie", '
        '"reason": str, "structured_patch": dict|null, "patch": str}\n\n'
        f"Round: {round_number}\n"
        f"Focus Signal: {json.dumps(signal.to_dict(), ensure_ascii=False)}\n"
        f"Schema Tables Summary: {json.dumps([{'catalog': t.get('catalog'), 'database': t.get('database'), 'name': t.get('name'), 'fields': [f.get('name') for f in (t.get('fields') or [])[:30]]} for t in tables_meta[:40]], ensure_ascii=False)}\n"
        f"Diagnosis Summary: {json.dumps(diagnosis_payload, ensure_ascii=False)}\n"
        f"Prior Rejected Rounds: {json.dumps(prior_rejections, ensure_ascii=False)}\n"
    )
    url = provider_probe._url(provider, model)
    headers = provider_probe._headers(provider)
    payload = evolution_gate._judge_payload(provider, model, prompt)
    if "max_tokens" in payload:
        payload["max_tokens"] = 1600
    if "max_completion_tokens" in payload:
        payload["max_completion_tokens"] = 1600
    if "max_output_tokens" in payload:
        payload["max_output_tokens"] = 1600
    try:
        with httpx.Client(
            timeout=DEFAULT_EVOLVER_TIMEOUT_SECONDS, trust_env=False
        ) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            body = resp.json()
        raw_text = provider_probe._response_text(provider, body).strip()
        return provider_probe.parse_json_object(raw_text)
    except Exception:
        logger.debug("Evolver LLM call failed or returned invalid JSON", exc_info=True)
        return None


def _build_overlay_files(
    project_id: str, candidate: Mapping[str, Any]
) -> dict[str, str | None]:
    ctx = project_context.current_project(project_id)
    src_root = Path(ctx.source_directory_value) if ctx.source_directory_value else None
    target_kind = str(candidate.get("target_kind") or "override")
    target = str(candidate.get("target") or "")
    structured_patch = candidate.get("structured_patch")
    patch_text = str(candidate.get("patch") or "")

    if structured_patch is None and target_kind == "override":
        structured_patch = semantic_overrides.legacy_target_to_patch(target, patch_text)
        if structured_patch is None and patch_text.strip():
            try:
                loaded = yaml.safe_load(patch_text)
            except yaml.YAMLError:
                loaded = None
            if isinstance(loaded, dict):
                structured_patch = semantic_overrides.normalize_structured_patch(loaded)
        if structured_patch is None:
            raise ValueError("Missing structured_patch for override candidate")

    if structured_patch is not None:
        base_doc, _ = (
            semantic_overrides.load_overrides_file(src_root)
            if src_root and src_root.is_dir()
            else (semantic_overrides.empty_overrides_doc(), None)
        )
        merged = semantic_overrides.merge_overrides_doc(base_doc, structured_patch)
        return {
            semantic_overrides.OVERRIDE_RELATIVE_PATH: semantic_overrides.dump_overrides_yaml(
                merged
            )
        }
    if target_kind == "document" and target.startswith("document:"):
        doc_file, _ = _load_editable_document(project_id, target)
        rel_path = (
            relative(doc_file).as_posix()
            if doc_file
            else relative(target[len("document:") :]).as_posix()
        )
        return {rel_path: patch_text}
    if target_kind == "ossie" and target.startswith("ossie:"):
        rel_path = relative(target[len("ossie:") :]).as_posix()
        return {rel_path: patch_text}
    return {}


def _extract_trajectory_sql(row: Mapping[str, Any]) -> tuple[str, str]:
    sql = ""
    dialect = "doris"
    for item in row.get("final_sql") or ():
        if isinstance(item, dict) and str(item.get("sql") or "").strip():
            sql = str(item["sql"]).strip()
            if item.get("dialect"):
                dialect = str(item["dialect"]).lower()
    if not sql:
        for item in row.get("sql_validations") or ():
            if isinstance(item, dict) and str(item.get("sql") or "").strip():
                sql = str(item["sql"]).strip()
                if item.get("dialect"):
                    dialect = str(item["dialect"]).lower()
    if dialect not in eval_mod.DIALECTS:
        dialect = "doris"
    return sql, dialect


def _target_eval_inputs(
    project_id: str,
    signal: trajectory_miner.MinedSignal,
    mining: trajectory_miner.MiningResult,
) -> tuple[list[str], list[eval_mod.Question]]:
    _, raw_rows = eval_questions._read_raw_rows(project_id)
    confirmed_rows = [r for r in raw_rows if r.get("answer_confirmed") is True]
    holdout_canon = {
        trajectory_miner.canonical_question(r.get("question"))
        for r in raw_rows
        if str(r.get("split") or "").strip().lower() == "holdout" and r.get("question")
    }
    confirmed_by_canon: dict[str, str] = {}
    for row in confirmed_rows:
        cq = trajectory_miner.canonical_question(row.get("question"))
        if cq and row.get("id"):
            confirmed_by_canon[cq] = str(row["id"])

    hint = str(signal.target_hint or "")
    hint_table = hint[len("table:") :] if hint.startswith("table:") else ""
    matched_ids: list[str] = []
    for row in confirmed_rows:
        expected = [str(t) for t in (row.get("expected_tables") or ())]
        if hint_table and any(hint_table in exp_t for exp_t in expected):
            matched_ids.append(str(row["id"]))

    evidence_ids = {int(tid) for tid in signal.evidence_trajectory_ids}
    diag_rows_by_id: dict[int, dict[str, Any]] = {}
    group_by_tid: dict[int, trajectory_miner.TrajectoryGroup] = {}
    for grp in mining.diagnosis_groups:
        for item in grp.trajectories:
            tid = int(item["id"])
            diag_rows_by_id[tid] = item
            group_by_tid[tid] = grp

    clarify = bool(
        signal.preserve_multi_caliber or signal.signal_kind == "divergent_clarification"
    )
    seen_canon: set[str] = set()
    target_qs: list[eval_mod.Question] = []

    def _maybe_add_question(
        qid: str,
        q_text: str,
        ref_sql: str,
        dialect: str,
        matched_refs: Sequence[str],
    ) -> None:
        cleaned_q = str(q_text or "").strip()
        if not cleaned_q:
            return
        cq = trajectory_miner.canonical_question(cleaned_q)
        if not cq or cq in holdout_canon or cq in seen_canon:
            return
        if cq in confirmed_by_canon:
            cid = confirmed_by_canon[cq]
            if cid not in matched_ids:
                matched_ids.append(cid)
            seen_canon.add(cq)
            return
        seen_canon.add(cq)

        exp_tables: list[str] = []
        exp_fields: list[str] = []
        if ref_sql:
            try:
                exp_tables, exp_fields = eval_questions.extract_tables_and_fields(
                    ref_sql, dialect=dialect
                )
            except ValueError:
                exp_tables, exp_fields = [], []
        if not exp_tables:
            exp_tables = [
                str(r)[len("table:") :]
                for r in matched_refs
                if str(r).startswith("table:")
            ]
        if not exp_tables and hint_table:
            exp_tables = [hint_table]

        target_qs.append(
            eval_mod.Question(
                id=qid,
                question=cleaned_q,
                expected_tables=exp_tables,
                expected_fields=exp_fields,
                caliber=signal.summary,
                reference_sql=ref_sql,
                dialect=dialect,
                clarify=clarify,
                split="dev",
                answer_confirmed=False,
            )
        )

    for tid in signal.evidence_trajectory_ids:
        if len(target_qs) >= 5:
            break
        row = diag_rows_by_id.get(int(tid))
        if row is None:
            continue
        grp = group_by_tid.get(int(tid))
        q_text = str(row.get("question") or (grp.anchor_question if grp else "") or "")
        ref_sql, dialect = _extract_trajectory_sql(row)
        matched_refs = list(row.get("matched_refs") or ())
        if not ref_sql and grp is not None:
            for item in reversed(grp.trajectories):
                grp_sql, grp_dialect = _extract_trajectory_sql(item)
                if grp_sql:
                    ref_sql, dialect = grp_sql, grp_dialect
                    break
            if not matched_refs:
                for item in grp.trajectories:
                    matched_refs.extend(item.get("matched_refs") or ())
        _maybe_add_question(f"traj-{tid}", q_text, ref_sql, dialect, matched_refs)

    if not target_qs and signal.signal_kind == "knowledge_gap_or_failure":
        for idx, q_text in enumerate(signal.details.get("questions") or ()):
            if len(target_qs) >= 5:
                break
            _maybe_add_question(
                f"gap-{signal.evidence_trajectory_ids[0] if signal.evidence_trajectory_ids else idx}-{idx}",
                str(q_text),
                "",
                "doris",
                [],
            )

    if not target_qs and evidence_ids:
        for grp in mining.diagnosis_groups:
            if len(target_qs) >= 5:
                break
            if any(int(t["id"]) in evidence_ids for t in grp.trajectories):
                ref_sql, dialect = _extract_trajectory_sql(grp.trajectories[-1])
                _maybe_add_question(
                    f"grp-{grp.max_id}",
                    grp.anchor_question,
                    ref_sql,
                    dialect,
                    grp.trajectories[-1].get("matched_refs") or (),
                )

    return matched_ids, target_qs


def create_evolution_run(
    project_id: str,
    *,
    trigger_type: str = "manual",
    created_by: str | None = None,
    max_rounds: int | None = None,
    max_eval_cases: int | None = None,
) -> dict[str, Any]:
    """Atomically reserve one running evolution slot for ``project_id``."""
    now = datetime.now(UTC)
    cfg = project_context.evolution_config(project_id)
    eff_rounds = max_rounds if max_rounds is not None else int(cfg["max_rounds"])
    eff_cases = (
        max_eval_cases if max_eval_cases is not None else int(cfg["max_eval_cases"])
    )

    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        if project_context.project_is_frozen(project_id):
            raise EvolutionFrozenError(project_context.ARCHIVED_DETAIL)
        active = (
            db.query(EvolutionRun)
            .filter(
                EvolutionRun.project_id == project_id,
                EvolutionRun.status == "running",
            )
            .first()
        )
        if active is not None:
            raise EvolutionConflictError("该项目已有正在进行的演化运行")
        ckpt = db.get(EvolutionCheckpoint, project_id)
        run = EvolutionRun(
            id=uuid.uuid4().hex,
            project_id=project_id,
            trigger_type=trigger_type,
            status="running",
            checkpoint_before_trajectory_id=(
                ckpt.checkpoint_trajectory_id if ckpt is not None else None
            ),
            max_rounds=eff_rounds,
            max_eval_cases=eff_cases,
            rounds_completed=0,
            created_by=created_by,
            started_at=now,
            created_at=now,
        )
        db.add(run)
        try:
            db.flush()
        except IntegrityError as exc:
            raise EvolutionConflictError("该项目已有正在进行的演化运行") from exc
        return serialize_run(run, [])


def execute_evolution_run(
    run_id: str,
    *,
    runner: Any | None = None,
    evolution_provider: Any | None = None,
    evolution_model: str | None = None,
    judge_provider: Any | None = None,
    judge_model: str | None = None,
    evolver_fn: Callable[..., dict[str, Any] | None] | None = None,
    judge_fn: Callable[[str], Mapping[str, Any]] | None = None,
    mcp_url: str | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """Execute the single-hypothesis Evolver loop for an already-reserved running run."""
    with sessions()() as db:
        run_row = db.get(EvolutionRun, run_id)
        if run_row is None:
            raise LookupError(f"Evolution run {run_id} not found")
        project_id = run_row.project_id
        max_rounds = run_row.max_rounds
        max_eval_cases = run_row.max_eval_cases
        actor_id = run_row.created_by or "evolution"
        ckpt = db.get(EvolutionCheckpoint, project_id)
        ckpt_id = ckpt.checkpoint_trajectory_id if ckpt is not None else None
        # A confirmation can arrive after mining but before acceptance. Keep
        # checkpoint_at as the scheduler's acceptance time and use the prior
        # run's start as a conservative mining watermark, including recovery.
        previous_run = (
            db.get(EvolutionRun, ckpt.last_run_id)
            if ckpt and ckpt.last_run_id
            else None
        )
        ckpt_at = _as_utc(previous_run.started_at) if previous_run is not None else None

    try:
        cfg = project_context.evolution_config(project_id)
        evo_prov, evo_mdl = _resolve_run_provider_pair(
            evolution_provider,
            evolution_model,
            cfg.get("evolution_provider_id"),
            cfg.get("evolution_model"),
        )
        jdg_prov, jdg_mdl = _resolve_run_provider_pair(
            judge_provider,
            judge_model,
            cfg.get("judge_provider_id"),
            cfg.get("judge_model"),
        )

        mining = trajectory_miner.mine_project_trajectories(
            project_id, after_id=ckpt_id, after_at=ckpt_at
        )
        diagnosis_payload = mining.to_diagnosis_payload()

        with sessions().begin() as db:
            run = db.get(EvolutionRun, run_id)
            if run is None:
                raise LookupError(f"Evolution run {run_id} not found")
            run.frozen_max_trajectory_id = mining.frozen_max_trajectory_id
            run.diagnosis_trajectory_ids = json.dumps(mining.diagnosis_trajectory_ids)
            run.reserve_trajectory_ids = json.dumps(mining.reserve_trajectory_ids)
            run.signal_summary = json.dumps(
                {
                    "total_eligible_rows": mining.total_eligible_rows,
                    "total_eligible_groups": mining.total_eligible_groups,
                    "signal_counts": mining.signal_counts(),
                    "signals": [s.to_dict() for s in mining.signals],
                },
                ensure_ascii=False,
            )

        if not mining.signals:
            now = datetime.now(UTC)
            with sessions().begin() as db:
                run = db.get(EvolutionRun, run_id)
                if run is not None:
                    run.status = "incomplete"
                    run.stop_reason = "no_signals"
                    run.finished_at = now
                    rounds = (
                        db.query(EvolutionRound)
                        .filter(EvolutionRound.run_id == run_id)
                        .order_by(EvolutionRound.round_number.asc())
                        .all()
                    )
                    return serialize_run(run, rounds)

        tables_meta = _schema_tables_for_project(project_id)
        prior_rejections: list[dict[str, Any]] = []

        for round_num in range(1, max_rounds + 1):
            signal = mining.signals[(round_num - 1) % len(mining.signals)]
            rnd_started = datetime.now(UTC)

            candidate: dict[str, Any] | None = None
            if evolver_fn is not None:
                candidate = evolver_fn(
                    diagnosis_payload=diagnosis_payload,
                    signal=signal,
                    tables_meta=tables_meta,
                    round_number=round_num,
                    prior_rejections=prior_rejections,
                )
            elif evo_prov is not None and evo_mdl:
                candidate = _call_evolver_llm(
                    evo_prov,
                    evo_mdl,
                    diagnosis_payload=diagnosis_payload,
                    signal=signal,
                    tables_meta=tables_meta,
                    round_number=round_num,
                    prior_rejections=prior_rejections,
                )
            if candidate is None or not _preserves_all_variants(candidate, signal):
                candidate = _synthesize_deterministic_candidate(
                    project_id,
                    signal,
                    round_number=round_num,
                    tables_meta=tables_meta,
                )

            hypothesis = str(candidate.get("hypothesis") or signal.summary).strip()
            target = str(candidate.get("target") or "").strip()
            target_kind = str(candidate.get("target_kind") or "override").strip()
            structured_patch = candidate.get("structured_patch")
            patch_text = str(candidate.get("patch") or "")
            reason = str(candidate.get("reason") or hypothesis)[:1000]

            round_id = uuid.uuid4().hex
            with sessions().begin() as db:
                db.add(
                    EvolutionRound(
                        id=round_id,
                        run_id=run_id,
                        project_id=project_id,
                        round_number=round_num,
                        signal_kind=signal.signal_kind,
                        hypothesis=hypothesis,
                        evidence_trajectory_ids=json.dumps(
                            signal.evidence_trajectory_ids
                        ),
                        target=target,
                        target_kind=target_kind,
                        candidate_change=json.dumps(
                            {
                                "patch": patch_text,
                                "structured_patch": structured_patch,
                                "reason": reason,
                            },
                            ensure_ascii=False,
                        ),
                        decision="running",
                        started_at=rnd_started,
                    )
                )

            # L1 static validation
            l1_report = semantic_overrides.run_l1_gate(
                target=target,
                patch=patch_text,
                reason=reason,
                structured_patch=structured_patch,
                target_kind=target_kind,
                project_id=project_id,
            )
            l1_dict = l1_report.to_dict()
            if not l1_report.passed:
                reject_reason = "L1: " + "; ".join(l1_report.errors)
                rnd_finished = datetime.now(UTC)
                with sessions().begin() as db:
                    rnd = db.get(EvolutionRound, round_id)
                    if rnd is not None:
                        rnd.l1_gate_report = json.dumps(l1_dict, ensure_ascii=False)
                        rnd.decision = "reject"
                        rnd.reject_reason = reject_reason
                        rnd.finished_at = rnd_finished
                    run = db.get(EvolutionRun, run_id)
                    if run is not None:
                        run.rounds_completed = round_num
                prior_rejections.append(
                    {
                        "round_number": round_num,
                        "hypothesis": hypothesis,
                        "reason": reject_reason,
                    }
                )
                continue

            overlay_files = _build_overlay_files(project_id, candidate)
            target_q_ids, target_qs = _target_eval_inputs(project_id, signal, mining)

            try:
                l2_report = evolution_gate.evaluate_candidate_gate(
                    project_id,
                    overlay_files=overlay_files,
                    target_question_ids=target_q_ids or None,
                    target_questions=target_qs or None,
                    diagnosis_trajectory_ids=mining.diagnosis_trajectory_ids,
                    reserve_trajectory_ids=mining.reserve_trajectory_ids,
                    max_eval_cases=max_eval_cases,
                    runner=runner,
                    provider=evo_prov,
                    model=evo_mdl,
                    judge_provider=jdg_prov,
                    judge_model=jdg_mdl,
                    judge_fn=judge_fn,
                    mcp_url=mcp_url,
                    seed=seed,
                )
            except Exception:
                logger.warning(
                    "L2 candidate gate failed for run %s round %s",
                    run_id,
                    round_num,
                    exc_info=True,
                )
                l2_report = {
                    "passed": False,
                    "mode": "error",
                    "error": "Candidate gate evaluation failed",
                }

            if l2_report.get("passed") is True:
                if project_context.project_is_frozen(project_id):
                    finished = datetime.now(UTC)
                    with sessions().begin() as db:
                        run = db.get(EvolutionRun, run_id)
                        if run is not None:
                            run.status = "incomplete"
                            run.stop_reason = "project_frozen"
                            run.finished_at = finished
                        db.execute(
                            update(EvolutionRound)
                            .where(
                                EvolutionRound.run_id == run_id,
                                EvolutionRound.decision == "running",
                            )
                            .values(
                                decision="incomplete",
                                reject_reason="project_frozen",
                                finished_at=finished,
                            )
                        )
                        if run is not None:
                            rounds = (
                                db.query(EvolutionRound)
                                .filter(EvolutionRound.run_id == run_id)
                                .order_by(EvolutionRound.round_number.asc())
                                .all()
                            )
                            return serialize_run(run, rounds)
                extra_gate = {
                    "l1_gate": l1_dict,
                    "l2_gate": l2_report,
                    "provenance": {
                        "evolution_run_id": run_id,
                        "round_number": round_num,
                        "signal_kind": signal.signal_kind,
                        "hypothesis": hypothesis,
                        "evidence_trajectory_ids": list(signal.evidence_trajectory_ids),
                    },
                    "checks": [
                        {
                            "name": "l2_candidate_gate",
                            "passed": True,
                            "details": [
                                f"mode={l2_report.get('mode')}",
                                f"evaluated_cases={l2_report.get('evaluated_cases', 0)}",
                            ],
                        }
                    ],
                }
                l1_json = json.dumps(l1_dict, ensure_ascii=False)
                l2_json = json.dumps(l2_report, ensure_ascii=False)
                pre_proposal_id = uuid.uuid4().hex
                with sessions().begin() as db:
                    rnd = db.get(EvolutionRound, round_id)
                    if rnd is not None:
                        rnd.l1_gate_report = l1_json
                        rnd.l2_gate_report = l2_json
                        rnd.proposal_id = pre_proposal_id

                evo_requester = Requester(id=actor_id, role="admin", token="")
                with (
                    bind_mcp_context(evo_requester),
                    corrections.trusted_evolution_source(),
                ):
                    proposal = corrections.propose_in_project(
                        project_id,
                        target=target,
                        patch=patch_text,
                        reason=reason,
                        source="evolution",
                        target_kind=target_kind,  # type: ignore[arg-type]
                        structured_patch=structured_patch,
                        extra_gate_report=extra_gate,
                        proposal_id=pre_proposal_id,
                    )
                proposal_id = str(proposal["id"])
                accepted_at = datetime.now(UTC)
                with sessions().begin() as db:
                    run, rounds = _finalize_accepted_run_in_db(
                        db,
                        run_id=run_id,
                        round_id=round_id,
                        proposal_id=proposal_id,
                        l1_json=l1_json,
                        l2_json=l2_json,
                        frozen_max_trajectory_id=mining.frozen_max_trajectory_id,
                        round_number=round_num,
                        accepted_at=accepted_at,
                    )
                    return serialize_run(run, rounds)

            reject_reason = str(
                l2_report.get("error")
                or l2_report.get("reason")
                or f"L2 {l2_report.get('mode', 'gate')} rejected"
            )
            rnd_finished = datetime.now(UTC)
            with sessions().begin() as db:
                rnd = db.get(EvolutionRound, round_id)
                if rnd is not None:
                    rnd.l1_gate_report = json.dumps(l1_dict, ensure_ascii=False)
                    rnd.l2_gate_report = json.dumps(l2_report, ensure_ascii=False)
                    rnd.decision = "reject"
                    rnd.reject_reason = reject_reason
                    rnd.finished_at = rnd_finished
                run = db.get(EvolutionRun, run_id)
                if run is not None:
                    run.rounds_completed = round_num
            prior_rejections.append(
                {
                    "round_number": round_num,
                    "hypothesis": hypothesis,
                    "reason": reject_reason,
                }
            )

        # Budget exhausted without passing L1+L2 -> incomplete, checkpoint untouched
        finished = datetime.now(UTC)
        with sessions().begin() as db:
            run = db.get(EvolutionRun, run_id)
            assert run is not None
            run.status = "incomplete"
            run.stop_reason = "budget_exhausted"
            run.finished_at = finished
            rounds = (
                db.query(EvolutionRound)
                .filter(EvolutionRound.run_id == run_id)
                .order_by(EvolutionRound.round_number.asc())
                .all()
            )
            return serialize_run(run, rounds)
    except Exception:
        logger.exception("Evolution run %s failed unexpectedly", run_id)
        safe_error = "Evolution run failed unexpectedly"
        finished = datetime.now(UTC)
        with sessions().begin() as db:
            run = db.get(EvolutionRun, run_id)
            if run is not None and run.status == "running":
                rounds = (
                    db.query(EvolutionRound)
                    .filter(EvolutionRound.run_id == run_id)
                    .order_by(EvolutionRound.round_number.asc())
                    .all()
                )
                prop, matched_round = _find_emitted_proposal_for_run(
                    db, project_id, run_id, rounds
                )
                if prop is not None:
                    gate_data = _loads_json(prop.gate_report, {})
                    _finalize_accepted_run_in_db(
                        db,
                        run_id=run_id,
                        round_id=matched_round.id if matched_round else None,
                        proposal_id=prop.id,
                        l1_json=(
                            json.dumps(gate_data["l1_gate"], ensure_ascii=False)
                            if isinstance(gate_data, dict) and "l1_gate" in gate_data
                            else None
                        ),
                        l2_json=(
                            json.dumps(gate_data["l2_gate"], ensure_ascii=False)
                            if isinstance(gate_data, dict) and "l2_gate" in gate_data
                            else None
                        ),
                        frozen_max_trajectory_id=run.frozen_max_trajectory_id,
                        round_number=(
                            matched_round.round_number if matched_round else None
                        ),
                        accepted_at=finished,
                    )
                else:
                    run.status = "incomplete"
                    run.stop_reason = "error"
                    run.error = safe_error
                    run.finished_at = finished
                    db.execute(
                        update(EvolutionRound)
                        .where(
                            EvolutionRound.run_id == run_id,
                            EvolutionRound.finished_at.is_(None),
                        )
                        .values(
                            decision="incomplete",
                            proposal_id=None,
                            reject_reason=safe_error,
                            finished_at=finished,
                        )
                    )
        knowledge.cleanup_candidate_generations(project_id)
        raise


def run_evolution(
    project_id: str = project_context.DEFAULT_PROJECT_ID,
    *,
    trigger_type: str = "manual",
    created_by: str | None = None,
    max_rounds: int | None = None,
    max_eval_cases: int | None = None,
    runner: Any | None = None,
    evolution_provider: Any | None = None,
    evolution_model: str | None = None,
    judge_provider: Any | None = None,
    judge_model: str | None = None,
    evolver_fn: Callable[..., dict[str, Any] | None] | None = None,
    judge_fn: Callable[[str], Mapping[str, Any]] | None = None,
    mcp_url: str | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """Synchronously reserve and execute one full evolution run."""
    created = create_evolution_run(
        project_id,
        trigger_type=trigger_type,
        created_by=created_by,
        max_rounds=max_rounds,
        max_eval_cases=max_eval_cases,
    )
    return execute_evolution_run(
        created["id"],
        runner=runner,
        evolution_provider=evolution_provider,
        evolution_model=evolution_model,
        judge_provider=judge_provider,
        judge_model=judge_model,
        evolver_fn=evolver_fn,
        judge_fn=judge_fn,
        mcp_url=mcp_url,
        seed=seed,
    )
