"""Dual-mode 门禁评测引擎 comparing active vs isolated 候选知识代 (#295)."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
import secrets as py_secrets
import shutil
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import Any

import httpx
import yaml

from . import (
    agent_runner,
    eval_questions,
    knowledge,
    mcp_context,
    project_context,
    provider_probe,
    sql_validation,
    trajectories,
)
from . import (
    eval as eval_mod,
)
from .agent_runner import CLARIFICATION, DONE, ERROR, TEXT_DELTA, AgentTurnRequest
from .agent_types import ProviderConfig
from .db import ProjectMember, Token, sessions

DEFAULT_MAX_EVAL_CASES = 20
MIN_CONFIRMED_REGRESSION_QUESTIONS = 5
COLD_START_RESERVE_RATIO = 0.30
DEFAULT_JUDGE_TIMEOUT_SECONDS = 60.0

logger = logging.getLogger(__name__)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _norm_question(text: str | None) -> str:
    return " ".join(str(text or "").split()).casefold()


def _run_coro(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _issue_eval_token(project_id: str, generation: str) -> tuple[str, str]:
    plaintext = py_secrets.token_urlsafe(32)
    token_id = uuid.uuid4().hex
    with sessions().begin() as db:
        row = Token(
            id=token_id,
            digest=mcp_context.digest(plaintext),
            name=f"gate-eval-{uuid.uuid4().hex[:8]}",
            role="user",
        )
        db.add(row)
        db.flush()
        if project_id != project_context.DEFAULT_PROJECT_ID:
            db.add(
                ProjectMember(project_id=project_id, token_id=token_id, role="member")
            )
    mcp_context.register_eval_token(plaintext, project_id, generation)
    return plaintext, token_id


def _revoke_eval_token(plaintext: str, token_id: str, project_id: str) -> None:
    mcp_context.unregister_eval_token(plaintext)
    with sessions().begin() as db:
        if project_id != project_context.DEFAULT_PROJECT_ID:
            member = db.get(ProjectMember, (project_id, token_id))
            if member is not None:
                db.delete(member)
        row = db.get(Token, token_id)
        if row is not None:
            db.delete(row)


def _resolve_provider_and_model(
    project_id: str,
    provider: ProviderConfig | None,
    model: str | None,
) -> tuple[ProviderConfig, str]:
    if provider is not None:
        selected_model = model or (provider.models[0] if provider.models else "default")
        return provider, selected_model
    try:
        from .ask_api import _choices
        from .db import Provider
        from .provider_api import provider_config

        _, provider_id, default_model = _choices()
        with sessions()() as db:
            row = db.get(Provider, provider_id)
            if row is not None:
                return provider_config(row), model or default_model
    except Exception:  # noqa: BLE001 - fall back to offline mock/default config
        logger.debug("Using offline fallback provider for project %s", project_id)
    selected_model = model or "default"
    return (
        ProviderConfig(
            id="gate-offline",
            name="gate-offline",
            type="openai-completions",
            base_url="http://127.0.0.1:0",
            models=[selected_model],
        ),
        selected_model,
    )


def _resolve_runner(agent: str, runner: Any | None):
    if runner is not None:
        return runner
    resolved = agent_runner.get_runner(agent)
    if resolved is None:
        raise ValueError(f"Unknown agent runner: {agent}")
    return resolved


async def _collect_single_agent_answer(
    runner: Any,
    *,
    prompt: str,
    provider: ProviderConfig,
    model: str,
    mcp_url: str,
    mcp_token: str,
    workspace: Path,
    timeout: float,
) -> tuple[list[str], str, bool, str | None]:
    conversation_id = "gate-" + uuid.uuid4().hex
    request = AgentTurnRequest(
        conversation_id=conversation_id,
        prompt=prompt,
        session_id=conversation_id,
        workspace=workspace / conversation_id,
        provider=provider,
        model=model,
        mcp_url=mcp_url,
        mcp_token=mcp_token,
    )
    sqls: list[str] = []
    text_parts: list[str] = []
    final_text = ""
    clarified = False
    error: str | None = None
    try:
        async with asyncio.timeout(timeout):
            async for event in runner.run(request):
                if event.type == TEXT_DELTA:
                    text_parts.append(str(event.data.get("text") or ""))
                elif event.type == DONE:
                    final_text = str(event.data.get("text") or "")
                    for item in event.data.get("sql") or []:
                        if isinstance(item, dict) and item.get("sql"):
                            sqls.append(str(item["sql"]))
                elif event.type == CLARIFICATION:
                    clarified = True
                elif event.type == ERROR:
                    error = str(event.data.get("message") or "agent error")
    except TimeoutError:
        error = f"agent timed out after {timeout:.0f}s"
    except Exception as exc:  # noqa: BLE001 - capture agent failures for gate scoring
        error = f"{type(exc).__name__}: {exc}"
    return sqls, final_text or "".join(text_parts), clarified, error


def _evaluate_generation_case(
    *,
    project_id: str,
    generation: str,
    question_text: str,
    dialect: str,
    runner: Any,
    provider: ProviderConfig,
    model: str,
    mcp_url: str,
    workspace: Path,
    timeout: float,
) -> dict[str, Any]:
    token_plain, token_id = _issue_eval_token(project_id, generation)
    try:
        with project_context.bind_generation(project_id, generation):
            hits = knowledge.search(question_text, "all", 5, project_id=project_id)
            sqls, answer_text, clarified, agent_error = _run_coro(
                _collect_single_agent_answer(
                    runner,
                    prompt=question_text,
                    provider=provider,
                    model=model,
                    mcp_url=mcp_url,
                    mcp_token=token_plain,
                    workspace=workspace,
                    timeout=timeout,
                )
            )
            validation: dict[str, Any] | None = None
            if sqls and not agent_error:
                validation = sql_validation.validate_sql(
                    sqls[0], dialect=dialect, project_id=project_id
                )
            elif not agent_error:
                validation = {
                    "status": "missing",
                    "issues": [{"kind": "missing_sql", "message": "未生成 SQL"}],
                }
            return {
                "hits": hits or [],
                "sqls": sqls,
                "answer_text": answer_text,
                "clarified": clarified,
                "agent_error": agent_error,
                "validation": validation,
            }
    finally:
        _revoke_eval_token(token_plain, token_id, project_id)


def _load_project_questions_safe(
    project_id: str,
) -> tuple[list[eval_mod.Question], set[str]]:
    _, raw_rows = eval_questions._read_raw_rows(project_id)
    holdout_questions = {
        _norm_question(row.get("question"))
        for row in raw_rows
        if str(row.get("split") or "").strip().lower() == "holdout"
        and row.get("question")
    }
    if not raw_rows:
        return [], holdout_questions

    path = project_context.eval_questions_path(project_id)
    try:
        return eval_mod.load_questions(path), holdout_questions
    except ValueError:
        confirmed_rows = [
            row for row in raw_rows if row.get("answer_confirmed") is True
        ]
        if not confirmed_rows:
            return [], holdout_questions
        text = yaml.safe_dump({"questions": confirmed_rows}, allow_unicode=True)
        return eval_mod.load_questions(path, text=text), holdout_questions


def _select_regression_cases(
    confirmed: Sequence[eval_mod.Question],
    target_question_ids: Sequence[str] | None,
    max_eval_cases: int,
) -> list[eval_mod.Question]:
    if max_eval_cases <= 0:
        return []
    targets = set(target_question_ids or ())
    ordered_targets = [q for q in confirmed if q.id in targets]
    others = [q for q in confirmed if q.id not in targets]
    return [*ordered_targets, *others][:max_eval_cases]


def _score_regression_case(
    question: eval_mod.Question,
    outcome: Mapping[str, Any],
) -> dict[str, Any]:
    return eval_mod.score_question(
        question,
        hits=outcome["hits"],
        validation=outcome["validation"],
        answer_sqls=outcome["sqls"],
        answer_text=outcome["answer_text"],
        mode="agent",
        answer_error=outcome["agent_error"],
    )


def _regression_passed(score: Mapping[str, Any]) -> bool:
    return not score.get("failure_reasons")


def _summarize_scores(scores: list[dict[str, Any]]) -> dict[str, float]:
    n = len(scores)
    if n == 0:
        return {
            "pass_rate": 0.0,
            "table_recall_at_5": 0.0,
            "validate_sql_pass_rate": 0.0,
            "caliber_hit_rate": 0.0,
        }
    return {
        "pass_rate": sum(1.0 for s in scores if _regression_passed(s)) / n,
        "table_recall_at_5": sum(s["table_recall_at_5"] for s in scores) / n,
        "validate_sql_pass_rate": sum(
            1.0 for s in scores if s["validate_sql"].get("valid")
        )
        / n,
        "caliber_hit_rate": sum(1.0 for s in scores if s["caliber_hit"]) / n,
    }


def _evaluate_confirmed_regression(
    *,
    project_id: str,
    active_generation: str,
    cand_generation: str,
    confirmed: list[eval_mod.Question],
    target_question_ids: Sequence[str] | None,
    target_questions: Sequence[eval_mod.Question] | None,
    max_eval_cases: int,
    runner: Any,
    provider: ProviderConfig,
    model: str,
    mcp_url: str,
    workspace: Path,
    timeout: float,
) -> dict[str, Any]:
    confirmed_ids = {item.id for item in confirmed}
    seen_extra: set[str] = set()
    extra_targets: list[eval_mod.Question] = []
    matched_confirmed_targets: list[str] = []
    for q in target_questions or ():
        if q.id in confirmed_ids:
            if q.id not in matched_confirmed_targets:
                matched_confirmed_targets.append(q.id)
        elif q.id not in seen_extra:
            seen_extra.add(q.id)
            extra_targets.append(q)

    effective_target_ids = list(target_question_ids or ())
    for qid in matched_confirmed_targets:
        if qid not in effective_target_ids:
            effective_target_ids.append(qid)

    if max_eval_cases <= 0:
        extra_targets = []
        eval_cases = []
    else:
        extra_targets = extra_targets[:max_eval_cases]
        regression_budget = max(0, max_eval_cases - len(extra_targets))
        eval_cases = _select_regression_cases(
            confirmed, effective_target_ids, regression_budget
        )

    active_scores: list[dict[str, Any]] = []
    cand_scores: list[dict[str, Any]] = []
    regressions: list[str] = []
    case_summaries: list[dict[str, Any]] = []

    for q in eval_cases:
        active_out = _evaluate_generation_case(
            project_id=project_id,
            generation=active_generation,
            question_text=q.question,
            dialect=q.dialect,
            runner=runner,
            provider=provider,
            model=model,
            mcp_url=mcp_url,
            workspace=workspace,
            timeout=timeout,
        )
        cand_out = _evaluate_generation_case(
            project_id=project_id,
            generation=cand_generation,
            question_text=q.question,
            dialect=q.dialect,
            runner=runner,
            provider=provider,
            model=model,
            mcp_url=mcp_url,
            workspace=workspace,
            timeout=timeout,
        )
        active_score = _score_regression_case(q, active_out)
        cand_score = _score_regression_case(q, cand_out)
        active_scores.append(active_score)
        cand_scores.append(cand_score)

        regressed = False
        if (
            (_regression_passed(active_score) and not _regression_passed(cand_score))
            or (active_score["caliber_hit"] and not cand_score["caliber_hit"])
            or (
                active_score["validate_sql"].get("valid")
                and not cand_score["validate_sql"].get("valid")
            )
            or (
                cand_score["table_recall_at_5"] + 1e-9
                < active_score["table_recall_at_5"]
            )
        ):
            regressed = True
            regressions.append(q.id)

        is_holdout = q.split == "holdout"
        case_summaries.append(
            {
                "id": q.id,
                "split": q.split,
                "redacted": is_holdout,
                "regressed": regressed,
                "active_passed": _regression_passed(active_score),
                "candidate_passed": _regression_passed(cand_score),
                "active_recall_at_5": active_score["table_recall_at_5"],
                "candidate_recall_at_5": cand_score["table_recall_at_5"],
            }
        )

    target_active_scores: list[dict[str, Any]] = []
    target_cand_scores: list[dict[str, Any]] = []
    for q in extra_targets:
        a_out = _evaluate_generation_case(
            project_id=project_id,
            generation=active_generation,
            question_text=q.question,
            dialect=q.dialect,
            runner=runner,
            provider=provider,
            model=model,
            mcp_url=mcp_url,
            workspace=workspace,
            timeout=timeout,
        )
        c_out = _evaluate_generation_case(
            project_id=project_id,
            generation=cand_generation,
            question_text=q.question,
            dialect=q.dialect,
            runner=runner,
            provider=provider,
            model=model,
            mcp_url=mcp_url,
            workspace=workspace,
            timeout=timeout,
        )
        a_sc = _score_regression_case(q, a_out)
        c_sc = _score_regression_case(q, c_out)
        target_active_scores.append(a_sc)
        target_cand_scores.append(c_sc)
        case_summaries.append(
            {
                "id": q.id,
                "split": q.split,
                "target": True,
                "redacted": q.split == "holdout",
                "regressed": False,
                "active_passed": _regression_passed(a_sc),
                "candidate_passed": _regression_passed(c_sc),
                "active_recall_at_5": a_sc["table_recall_at_5"],
                "candidate_recall_at_5": c_sc["table_recall_at_5"],
            }
        )

    target_id_set = set(effective_target_ids)
    matched_active = [
        active_scores[i] for i, q in enumerate(eval_cases) if q.id in target_id_set
    ]
    matched_cand = [
        cand_scores[i] for i, q in enumerate(eval_cases) if q.id in target_id_set
    ]
    if target_active_scores or matched_active:
        active_target_slice = [*matched_active, *target_active_scores]
        cand_target_slice = [*matched_cand, *target_cand_scores]
    else:
        active_target_slice = active_scores
        cand_target_slice = cand_scores

    active_metrics = _summarize_scores(active_scores)
    cand_metrics = _summarize_scores(cand_scores)
    target_active_m = _summarize_scores(active_target_slice)
    target_cand_m = _summarize_scores(cand_target_slice)

    strict_improvement = (
        target_cand_m["pass_rate"] > target_active_m["pass_rate"] + 1e-9
        or target_cand_m["caliber_hit_rate"]
        > target_active_m["caliber_hit_rate"] + 1e-9
        or target_cand_m["validate_sql_pass_rate"]
        > target_active_m["validate_sql_pass_rate"] + 1e-9
        or target_cand_m["table_recall_at_5"]
        > target_active_m["table_recall_at_5"] + 1e-9
    )
    total_evaluated = len(eval_cases) + len(extra_targets)
    passed = (total_evaluated > 0) and (not regressions) and strict_improvement
    return {
        "passed": passed,
        "mode": "confirmed_regression",
        "evaluated_cases": total_evaluated,
        "regressions": regressions,
        "strict_improvement": strict_improvement,
        "active_metrics": active_metrics,
        "candidate_metrics": cand_metrics,
        "cases": case_summaries,
    }


def _judge_prompt(
    *,
    question: str,
    option_a: Mapping[str, Any],
    option_b: Mapping[str, Any],
) -> str:
    return (
        "You are an impartial, blank-context SQL & data-analytics quality evaluator.\n"
        "Compare two anonymized candidate responses (Option A and Option B) to the user question.\n"
        "Judge strictly on SQL correctness, schema accuracy, metric caliber completeness, and clarity.\n"
        "Return ONLY valid JSON with exact keys:\n"
        '{"winner": "A" | "B" | "tie", "critical_error_a": bool, "critical_error_b": bool, "reason": string}\n\n'
        f"Question: {question}\n\n"
        f"Option A:\n"
        f"SQL: {json.dumps(option_a['sqls'], ensure_ascii=False)}\n"
        f"Validation: {json.dumps(option_a['validation'], ensure_ascii=False)}\n"
        f"Answer: {option_a['answer_text']}\n\n"
        f"Option B:\n"
        f"SQL: {json.dumps(option_b['sqls'], ensure_ascii=False)}\n"
        f"Validation: {json.dumps(option_b['validation'], ensure_ascii=False)}\n"
        f"Answer: {option_b['answer_text']}\n"
    )


def _judge_payload(provider: ProviderConfig, model: str, prompt: str) -> dict[str, Any]:
    if provider.type == "anthropic-messages":
        return {
            "model": model,
            "max_tokens": 512,
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
        }
    if provider.type == "openai-completions":
        return {
            "model": model,
            "max_completion_tokens": 512,
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
        }
    if provider.type == "openai-responses":
        return {
            "model": model,
            "max_output_tokens": 512,
            "store": False,
            "stream": False,
            "input": [{"role": "user", "content": prompt}],
        }
    return {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 512},
    }


def _call_judge_http(
    provider: ProviderConfig,
    model: str,
    prompt: str,
    timeout: float = DEFAULT_JUDGE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    url = provider_probe._url(provider, model)
    headers = provider_probe._headers(provider)
    payload = _judge_payload(provider, model, prompt)
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        resp = client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        body = resp.json()
    raw_text = provider_probe._response_text(provider, body).strip()
    cleaned = _FENCE_RE.sub("", raw_text).strip()
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise TypeError("Judge response must be a JSON object")
    return parsed


def _parse_judge_verdict(
    verdict: Mapping[str, Any], *, cand_is_a: bool
) -> tuple[str, bool]:
    winner_raw = str(verdict.get("winner") or "").strip()
    if winner_raw not in {"A", "B", "tie"}:
        raise ValueError(f"Invalid judge winner: {winner_raw!r}")

    crit_a = bool(verdict.get("critical_error_a", False))
    crit_b = bool(verdict.get("critical_error_b", False))
    generic_crit = verdict.get("critical_error")
    if isinstance(generic_crit, bool) and generic_crit:
        crit_a = True
        crit_b = True
    elif isinstance(generic_crit, str):
        token = generic_crit.strip().upper()
        if token in {"A", "BOTH"}:
            crit_a = True
        if token in {"B", "BOTH"}:
            crit_b = True

    cand_crit = crit_a if cand_is_a else crit_b
    return winner_raw, cand_crit


def _validation_issue_count(validation: Mapping[str, Any] | None) -> int:
    if not isinstance(validation, dict):
        return 1
    issues = validation.get("issues")
    issue_len = len(issues) if isinstance(issues, list) else 0
    if validation.get("status") != "pass":
        return max(1, issue_len)
    return issue_len


def _evaluate_cold_start_ab(
    *,
    project_id: str,
    active_generation: str,
    cand_generation: str,
    holdout_questions: set[str],
    diagnosis_trajectory_ids: Sequence[int] | None,
    reserve_trajectory_ids: Sequence[int] | None = None,
    max_eval_cases: int,
    runner: Any,
    provider: ProviderConfig,
    model: str,
    judge_provider: ProviderConfig | None,
    judge_model: str | None,
    judge_fn: Callable[[str], Mapping[str, Any]] | None,
    mcp_url: str,
    workspace: Path,
    timeout: float,
    seed: int | None,
) -> dict[str, Any]:
    diag_ids = set(diagnosis_trajectory_ids or ())
    reserve_id_filter = (
        set(reserve_trajectory_ids) if reserve_trajectory_ids is not None else None
    )
    all_rows = sorted(
        trajectories.list_trajectories(project_id),
        key=lambda r: (r["recorded_at"], r["id"]),
    )
    diag_questions = {
        _norm_question(row.get("question"))
        for row in all_rows
        if row["id"] in diag_ids and (row.get("question") or "").strip()
    }
    excluded_questions = holdout_questions | diag_questions

    latest_by_question: dict[str, dict[str, Any]] = {}
    for row in all_rows:
        if reserve_id_filter is not None and row["id"] not in reserve_id_filter:
            continue
        norm_q = _norm_question(row.get("question"))
        if row["id"] in diag_ids or not norm_q or norm_q in excluded_questions:
            continue
        latest_by_question.pop(norm_q, None)
        latest_by_question[norm_q] = row
    eligible = list(latest_by_question.values())
    if not eligible:
        return {
            "passed": False,
            "mode": "cold_start_ab",
            "reason": "insufficient_cold_start_trajectories",
            "evaluated_cases": 0,
            "new_validate_errors": 0,
            "judge_errors": 0,
            "E_c": 0,
            "W_c": 0,
            "W_p": 0,
            "cases": [],
        }

    if reserve_id_filter is not None:
        reserved = eligible
    else:
        reserve_count = max(1, math.ceil(len(eligible) * COLD_START_RESERVE_RATIO))
        reserved = eligible[-reserve_count:]
    if max_eval_cases > 0:
        reserved = reserved[-max_eval_cases:]
    else:
        reserved = []

    j_provider, j_model = _resolve_provider_and_model(
        project_id, judge_provider or provider, judge_model or model
    )
    rng = random.Random(seed)
    new_validate_errors = 0
    judge_errors = 0
    e_c = 0
    w_c = 0
    w_p = 0
    case_summaries: list[dict[str, Any]] = []

    for row in reserved:
        q_text = str(row["question"])
        dialect = "doris"
        for src in (row.get("sql_validations") or (), row.get("final_sql") or ()):
            if src and isinstance(src[0], dict) and src[0].get("dialect"):
                dialect = str(src[0]["dialect"])
                break

        active_out = _evaluate_generation_case(
            project_id=project_id,
            generation=active_generation,
            question_text=q_text,
            dialect=dialect,
            runner=runner,
            provider=provider,
            model=model,
            mcp_url=mcp_url,
            workspace=workspace,
            timeout=timeout,
        )
        cand_out = _evaluate_generation_case(
            project_id=project_id,
            generation=cand_generation,
            question_text=q_text,
            dialect=dialect,
            runner=runner,
            provider=provider,
            model=model,
            mcp_url=mcp_url,
            workspace=workspace,
            timeout=timeout,
        )

        active_valid = (
            isinstance(active_out["validation"], dict)
            and active_out["validation"].get("status") == "pass"
            and not active_out["agent_error"]
        )
        cand_valid = (
            isinstance(cand_out["validation"], dict)
            and cand_out["validation"].get("status") == "pass"
            and not cand_out["agent_error"]
        )
        new_sql_err = (
            (active_valid and not cand_valid)
            or (
                _validation_issue_count(cand_out["validation"])
                > _validation_issue_count(active_out["validation"])
            )
            or bool(cand_out["agent_error"])
        )
        if new_sql_err:
            new_validate_errors += 1
            e_c += 1

        cand_is_a = rng.choice([True, False])
        opt_a = cand_out if cand_is_a else active_out
        opt_b = active_out if cand_is_a else cand_out
        prompt = _judge_prompt(question=q_text, option_a=opt_a, option_b=opt_b)

        cand_slot = "A" if cand_is_a else "B"
        active_slot = "B" if cand_is_a else "A"
        winner_side = "error"
        cand_crit = False
        try:
            verdict = (
                judge_fn(prompt)
                if judge_fn is not None
                else _call_judge_http(j_provider, j_model, prompt)
            )
            winner_slot, cand_crit = _parse_judge_verdict(verdict, cand_is_a=cand_is_a)
            if cand_crit:
                e_c += 1
            if winner_slot == cand_slot:
                w_c += 1
                winner_side = "candidate"
            elif winner_slot == active_slot:
                w_p += 1
                winner_side = "active"
            else:
                winner_side = "tie"
        except Exception:  # noqa: BLE001 - fail closed on judge transport/schema failure
            judge_errors += 1
            e_c += 1

        case_summaries.append(
            {
                "trajectory_id": row["id"],
                "cand_slot": cand_slot,
                "new_validate_error": new_sql_err,
                "candidate_critical_error": cand_crit,
                "winner": winner_side,
            }
        )

    passed = (
        len(reserved) > 0
        and new_validate_errors == 0
        and judge_errors == 0
        and e_c == 0
        and w_c > w_p
    )
    return {
        "passed": passed,
        "mode": "cold_start_ab",
        "evaluated_cases": len(reserved),
        "new_validate_errors": new_validate_errors,
        "judge_errors": judge_errors,
        "E_c": e_c,
        "W_c": w_c,
        "W_p": w_p,
        "cases": case_summaries,
    }


def evaluate_candidate_gate(
    project_id: str = project_context.DEFAULT_PROJECT_ID,
    *,
    candidate_generation: str | None = None,
    overlay_files: Mapping[str, str | None] | None = None,
    candidate_source_dir: str | Path | None = None,
    target_question_ids: Sequence[str] | None = None,
    target_questions: Sequence[eval_mod.Question] | None = None,
    diagnosis_trajectory_ids: Sequence[int] | None = None,
    reserve_trajectory_ids: Sequence[int] | None = None,
    max_eval_cases: int = DEFAULT_MAX_EVAL_CASES,
    agent: str = "pi",
    runner: Any | None = None,
    provider: ProviderConfig | None = None,
    model: str | None = None,
    judge_provider: ProviderConfig | None = None,
    judge_model: str | None = None,
    judge_fn: Callable[[str], Mapping[str, Any]] | None = None,
    mcp_url: str | None = None,
    timeout: float = 120.0,
    seed: int | None = None,
) -> dict[str, Any]:
    """Run the dual-mode evolution gate against active vs candidate generations."""
    ctx = project_context.current_project(project_id)
    active_generation = ctx.generation
    resolved_runner = _resolve_runner(agent, runner)
    resolved_provider, resolved_model = _resolve_provider_and_model(
        project_id, provider, model
    )
    resolved_mcp_url = mcp_url or eval_mod.default_mcp_url()
    questions, holdout_questions = _load_project_questions_safe(project_id)
    confirmed = [q for q in questions if q.answer_confirmed]

    work_root = Path(tempfile.mkdtemp(prefix="evolution-gate-"))
    try:
        with ExitStack() as stack:
            if candidate_generation is None:
                cand = stack.enter_context(
                    knowledge.candidate_generation(
                        project_id,
                        overlay_files=overlay_files,
                        candidate_source_dir=candidate_source_dir,
                    )
                )
                cand_gen = cand.generation
            else:
                cand_gen = candidate_generation

            if len(confirmed) >= MIN_CONFIRMED_REGRESSION_QUESTIONS:
                report = _evaluate_confirmed_regression(
                    project_id=project_id,
                    active_generation=active_generation,
                    cand_generation=cand_gen,
                    confirmed=confirmed,
                    target_question_ids=target_question_ids,
                    target_questions=target_questions,
                    max_eval_cases=max_eval_cases,
                    runner=resolved_runner,
                    provider=resolved_provider,
                    model=resolved_model,
                    mcp_url=resolved_mcp_url,
                    workspace=work_root,
                    timeout=timeout,
                )
            else:
                report = _evaluate_cold_start_ab(
                    project_id=project_id,
                    active_generation=active_generation,
                    cand_generation=cand_gen,
                    holdout_questions=holdout_questions,
                    diagnosis_trajectory_ids=diagnosis_trajectory_ids,
                    reserve_trajectory_ids=reserve_trajectory_ids,
                    max_eval_cases=max_eval_cases,
                    runner=resolved_runner,
                    provider=resolved_provider,
                    model=resolved_model,
                    judge_provider=judge_provider,
                    judge_model=judge_model,
                    judge_fn=judge_fn,
                    mcp_url=resolved_mcp_url,
                    workspace=work_root,
                    timeout=timeout,
                    seed=seed,
                )
            report["project_id"] = project_id
            report["active_generation"] = active_generation
            report["candidate_generation"] = cand_gen
            return report
    finally:
        with suppress(OSError):
            shutil.rmtree(work_root)
