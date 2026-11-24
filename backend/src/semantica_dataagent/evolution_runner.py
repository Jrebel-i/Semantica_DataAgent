"""Single-hypothesis Evolver loop, startup recovery, and proposal emission (#296)."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError

from . import corrections, project_context
from .api_time import as_utc as _as_utc
from .db import (
    CorrectionProposal,
    EvolutionCheckpoint,
    EvolutionRound,
    EvolutionRun,
    sessions,
)

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
    """Reconcile or abort leftover running evolution runs."""
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

    return recovered


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
    """Stub evolution executor until M3 replaces it with TraceSource + LLM induction."""
    with sessions()() as db:
        run_row = db.get(EvolutionRun, run_id)
        if run_row is None:
            raise LookupError(f"Evolution run {run_id} not found")
        project_id = run_row.project_id

    try:
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
        raise LookupError(f"Evolution run {run_id} not found")
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
