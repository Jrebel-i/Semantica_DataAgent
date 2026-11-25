"""Structured audit logging for governance and personal-default mutations."""

from __future__ import annotations

import json
import logging

from .auth import Requester

audit_logger = logging.getLogger("semantica_dataagent.audit")


def record_audit_event(
    *,
    actor: Requester | str,
    action: str,
    target_type: str,
    target_id: str,
    details: dict[str, object] | None = None,
) -> None:
    """Record a structured audit event without secrets or upstream bodies."""
    actor_id = actor.id if isinstance(actor, Requester) else str(actor)
    payload: dict[str, object] = {
        "actor": actor_id,
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
    }
    if details:
        payload["details"] = details
    audit_logger.info(
        "audit %s",
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        extra={"audit": payload},
    )
