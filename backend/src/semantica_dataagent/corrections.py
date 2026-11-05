"""Correction proposals submitted by authenticated MCP callers."""

import difflib
import os
import tempfile
import uuid
from pathlib import Path
from typing import Annotated

from pydantic import Field

from . import knowledge
from .clarification import _owned_conversation
from .db import CorrectionProposal, sessions
from .mcp_context import current_conversation_id, current_requester

Text = Annotated[str, Field(min_length=1, max_length=20000, pattern=r"\S")]


def _atomic_write(path: Path, content: str) -> None:
    mode = path.stat().st_mode & 0o777
    name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            name = stream.name
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        name = None
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if name is not None:
            Path(name).unlink(missing_ok=True)


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


from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from .auth import admin
from .auth import current_requester as rest_requester
from .build_queue import enqueue_build
from .db import MetadataOverride, Token
from .source_directory import git, relative, repository, safe

router = APIRouter()


class ProposalView(BaseModel):
    id: str
    owner_id: str
    conversation_id: str | None
    target: str
    patch: str
    original: str
    diff: str
    reason: str
    status: str
    reviewer_id: str | None
    reviewed_at: datetime | None
    created_at: datetime
    model_config = {"from_attributes": True}


class Approval(BaseModel):
    patch: Text | None = None


@router.get("/api/v1/corrections", response_model=list[ProposalView])
def list_proposals(_admin: Annotated[str, Depends(admin)]):
    with sessions()() as db:
        return [
            ProposalView.model_validate(row)
            for row in db.query(CorrectionProposal)
            .order_by(CorrectionProposal.created_at.desc())
            .all()
        ]


@router.get("/api/v1/corrections/{proposal_id}", response_model=ProposalView)
def get_proposal(proposal_id: str, _admin: Annotated[str, Depends(admin)]):
    with sessions()() as db:
        row = db.get(CorrectionProposal, proposal_id)
        if row is None:
            raise HTTPException(404, "Proposal not found")
        return ProposalView.model_validate(row)


def _pending(db, proposal_id):
    row = db.get(CorrectionProposal, proposal_id)
    if row is None:
        raise HTTPException(404, "Proposal not found")
    if row.status != "pending":
        raise HTTPException(409, "Proposal already reviewed")
    return row


@router.post("/api/v1/corrections/{proposal_id}/reject", response_model=ProposalView)
def reject(
    proposal_id: str,
    _admin: Annotated[str, Depends(admin)],
    requester: Annotated[object, Depends(rest_requester)],
):
    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = _pending(db, proposal_id)
        row.status = "rejected"
        row.reviewer_id = requester.id
        row.reviewed_at = datetime.now(UTC)
        db.flush()
        return ProposalView.model_validate(row)


@router.post("/api/v1/corrections/{proposal_id}/approve", response_model=ProposalView)
def approve(
    proposal_id: str,
    _admin: Annotated[str, Depends(admin)],
    requester: Annotated[object, Depends(rest_requester)],
    body: Approval | None = None,
):
    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = _pending(db, proposal_id)
        patch = body.patch if body and body.patch is not None else row.patch
        token = db.query(Token).filter_by(id=requester.id).one()
        reviewer_name = token.name
        if row.target.startswith("document:"):
            values = knowledge.settings()
            nodes = knowledge.execute(
                knowledge.read_graph(values),
                "MATCH (n:SDADocument {generation:$generation, ref:$ref}) RETURN properties(n) AS node",
                {"generation": values["knowledge_generation"], "ref": row.target},
            )
            if len(nodes) != 1 or not nodes[0]["node"].get("source_file"):
                raise HTTPException(409, "Document no longer exists")
            try:
                with repository(values["source_directory"]) as root:
                    path = safe(root, relative(nodes[0]["node"]["source_file"]))
                    original = path.read_text(encoding="utf-8")
                    if original != row.original:
                        raise ValueError("Document changed since proposal")
                    if git(root, "diff", "--cached", "--name-only"):
                        raise ValueError("Source repository has staged changes")
                    if (
                        any(c in token.name for c in "<>\n\r\x00")
                        or token.name != token.name.strip()
                        or not token.name
                    ):
                        raise ValueError("Invalid git author")
                    try:
                        _atomic_write(path, patch)
                        git(root, "add", "--", str(path.relative_to(root)))
                        git(
                            root,
                            "commit",
                            "--quiet",
                            "-m",
                            f"Approve correction {proposal_id}",
                            author=token.name,
                        )
                    except Exception:
                        _atomic_write(path, original)
                        git(
                            root,
                            "reset",
                            "--quiet",
                            "HEAD",
                            "--",
                            str(path.relative_to(root)),
                        )
                        raise
            except (OSError, ValueError) as exc:
                raise HTTPException(409, str(exc)) from exc
        else:
            override = db.get(MetadataOverride, row.target)
            if override is None:
                db.add(MetadataOverride(target=row.target, comment=patch))
            else:
                override.comment = patch
        row.patch = patch
        row.diff = "".join(
            difflib.unified_diff(
                row.original.splitlines(keepends=True),
                patch.splitlines(keepends=True),
                fromfile=row.target,
                tofile=row.target,
            )
        )
        row.status = "approved"
        row.reviewer_id = requester.id
        row.reviewed_at = datetime.now(UTC)
        db.flush()
        result = ProposalView.model_validate(row)
    if result.target.startswith("document:"):
        enqueue_build("incremental", reviewer_name)
    return result
