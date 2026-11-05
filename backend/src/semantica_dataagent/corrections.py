"""Correction proposals submitted by authenticated MCP callers."""

import difflib
import uuid
from typing import Annotated

from pydantic import Field

from . import knowledge
from .clarification import _owned_conversation
from .db import CorrectionProposal, sessions
from .mcp_context import current_conversation_id, current_requester

Text = Annotated[str, Field(min_length=1, max_length=20000, pattern=r"\S")]


def propose_correction(target: Text, patch: Text, reason: Text) -> dict:
    """Propose replacement content or a metadata comment for approval."""
    requester = current_requester()
    conversation_id = current_conversation_id()
    if conversation_id:
        _owned_conversation(conversation_id, requester.id)
    if not target.startswith(("document:", "table:", "field:")):
        raise ValueError("Unsupported correction target")
    if target not in knowledge.existing_refs([target]):
        raise ValueError("Target is not in the current knowledge generation")
    values = knowledge.settings()
    rows = knowledge.execute(
        knowledge.read_graph(values),
        "MATCH (n:SDAEntity {generation:$generation, ref:$ref}) RETURN properties(n) AS node",
        {"generation": values["knowledge_generation"], "ref": target},
    )
    if len(rows) != 1:
        raise ValueError("Correction target is ambiguous or missing")
    node = rows[0]["node"]
    if target.startswith("document:") and (
        node.get("generated") or not node.get("source_file")
    ):
        raise ValueError("Document has no editable source")
    original = node.get("content" if target.startswith("document:") else "comment", "")
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            patch.splitlines(keepends=True),
            fromfile=target,
            tofile=target,
        )
    )
    if not diff:
        raise ValueError("Correction does not change the target")
    proposal_id = uuid.uuid4().hex
    with sessions().begin() as db:
        db.add(
            CorrectionProposal(
                id=proposal_id,
                owner_id=requester.id,
                conversation_id=conversation_id,
                target=target,
                patch=patch,
                original=original,
                diff=diff,
                reason=reason,
                status="pending",
            )
        )
    return {
        "proposal_id": proposal_id,
        "status": "pending",
        "diff": diff,
        "conversation_id": conversation_id,
    }
