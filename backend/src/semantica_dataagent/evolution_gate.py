"""Lightweight L1 static validation & anti-duplication gate for ADR-0013 evolution proposals.

Obsolete Phase-8 L2 candidate-generation A/B gating has been retired in Phase 11
(ADR-0013); standalone evaluation question sets remain managed independently in
``eval_questions.py``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from . import knowledge_synthesizer, semantic_overrides
from .agent_types import ProviderConfig

DEFAULT_JUDGE_TIMEOUT_SECONDS = 60.0


def _judge_payload(provider: ProviderConfig, model: str, prompt: str) -> dict[str, Any]:
    """Build provider-specific non-streaming JSON chat payload for LLM helper calls."""
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


def validate_evolution_candidate(
    project_id: str,
    *,
    target: str,
    target_kind: knowledge_synthesizer.TargetKind,
    resolved_path: str,
    patch: str,
    structured_patch: dict[str, Any] | None,
    reason: str,
    original: str = "",
    is_new_file: bool = False,
    existing_overrides: Mapping[str, Any] | None = None,
    existing_docs: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Run deterministic L1 static validation and anti-duplication checks."""
    l1_report = semantic_overrides.run_l1_gate(
        target=target,
        patch=patch,
        reason=reason,
        structured_patch=structured_patch,
        target_kind=target_kind,
        project_id=project_id,
        resolved_path=resolved_path,
        is_new_file=is_new_file,
    )
    report = l1_report.to_dict()
    dup = knowledge_synthesizer.check_duplicate_candidate(
        project_id,
        target=target,
        target_kind=target_kind,
        resolved_path=resolved_path,
        patch=patch,
        structured_patch=structured_patch,
        reason=reason,
        original=original,
        is_new_file=is_new_file,
        existing_overrides=existing_overrides,
        existing_docs=existing_docs,
    )
    report.setdefault("checks", []).append(
        {
            "name": "anti_duplication",
            "passed": not dup.is_duplicate,
            "details": [dup.reason] if dup.is_duplicate and dup.reason else [],
        }
    )
    report["is_duplicate"] = dup.is_duplicate
    report["duplicate_of"] = dup.duplicate_of
    report["duplicate_reason"] = dup.reason
    return report
