"""Correction proposals submitted by authenticated MCP callers."""

import difflib
import os
import tempfile
import uuid
from pathlib import Path
from typing import Annotated

from pydantic import Field

from . import knowledge, project_context
from .clarification import _owned_conversation
from .db import CorrectionProposal, sessions
from .mcp_context import bind_mcp_context, current_conversation_id, current_requester
from .project_context import add_queued_build

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


def propose_correction(
    target: Text, patch: Text, reason: Text, project_id: str = "default"
) -> dict:
    """Propose a correction in the resolved project from an MCP caller."""
    return propose_in_project(
        project_id or project_context.DEFAULT_PROJECT_ID, target, patch, reason
    )


def _settings(project_id):
    if project_id in (None, project_context.DEFAULT_PROJECT_ID):
        return knowledge.settings()
    return knowledge.settings(project_id)


def propose_in_project(
    project_id: str, target: Text, patch: Text, reason: Text
) -> dict:
    """Propose replacement content or a metadata comment inside one project."""
    requester = current_requester()
    conversation_id = current_conversation_id()
    if conversation_id:
        _owned_conversation(conversation_id, requester.id)
    if not target.startswith(("document:", "table:", "field:")):
        raise ValueError("Unsupported correction target")
    known = (
        knowledge.existing_refs([target])
        if project_id == project_context.DEFAULT_PROJECT_ID
        else knowledge.existing_refs([target], project_id)
    )
    if target not in known:
        raise ValueError("Target is not in the current knowledge generation")
    values = _settings(project_id)
    project = project_context.from_values(values, project_id)
    rows = knowledge.execute(
        knowledge.read_graph(values),
        "MATCH (n:SDAEntity {generation:$generation, ref:$ref}) RETURN properties(n) AS node",
        {"generation": project.generation, "ref": target},
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
                project_id=project_id,
                owner_id=requester.id,
                conversation_id=conversation_id,
                target=target,
                patch=patch,
                original_patch=patch,
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

from .api_time import UtcDatetime
from .auth import Requester
from .auth import current_requester as rest_requester
from .build_queue import active_build
from .db import MetadataOverride, Token
from .project_api import admin_project_scope, project_scope
from .source_directory import git, relative, repository, safe

router = APIRouter()


class ProposalView(BaseModel):
    id: str
    project_id: str
    owner_id: str
    conversation_id: str | None
    target: str
    patch: str
    original: str
    original_patch: str | None
    commit_sha: str | None
    diff: str
    reason: str
    status: str
    reviewer_id: str | None
    reviewed_at: UtcDatetime | None
    created_at: UtcDatetime
    model_config = {"from_attributes": True}


class Approval(BaseModel):
    patch: Text | None = None


class ProposalCreate(BaseModel):
    target: Text
    patch: Text
    reason: Text


@router.post("/api/v1/corrections", status_code=201)
def create_proposal(
    body: ProposalCreate,
    requester: Annotated[Requester, Depends(rest_requester)],
    scope: Annotated[project_context.ProjectContext, Depends(project_scope)],
):
    try:
        with bind_mcp_context(requester):
            return propose_in_project(
                scope.project_id, body.target, body.patch, body.reason
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/api/v1/corrections", response_model=list[ProposalView])
def list_proposals(
    scope: Annotated[project_context.ProjectContext, Depends(project_scope)],
):
    with sessions()() as db:
        return [
            ProposalView.model_validate(row)
            for row in db.query(CorrectionProposal)
            .filter(CorrectionProposal.project_id == scope.project_id)
            .order_by(CorrectionProposal.created_at.desc())
            .all()
        ]


@router.get("/api/v1/corrections/{proposal_id}", response_model=ProposalView)
def get_proposal(
    proposal_id: str,
    scope: Annotated[project_context.ProjectContext, Depends(project_scope)],
):
    with sessions()() as db:
        row = db.get(CorrectionProposal, proposal_id)
        if row is None or row.project_id != scope.project_id:
            raise HTTPException(404, "Proposal not found")
        return ProposalView.model_validate(row)


def _pending(db, proposal_id, project_id=None):
    row = db.get(CorrectionProposal, proposal_id)
    if row is None or (project_id is not None and row.project_id != project_id):
        raise HTTPException(404, "Proposal not found")
    if row.status != "pending":
        raise HTTPException(409, "Proposal already reviewed")
    return row


@router.post("/api/v1/corrections/{proposal_id}/reject", response_model=ProposalView)
def reject(
    proposal_id: str,
    scope: Annotated[project_context.ProjectContext, Depends(admin_project_scope)],
    requester: Annotated[object, Depends(rest_requester)],
):
    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = _pending(db, proposal_id, scope.project_id)
        row.status = "rejected"
        row.reviewer_id = requester.id
        row.reviewed_at = datetime.now(UTC)
        db.flush()
        return ProposalView.model_validate(row)


def _finalize(proposal_id: str, commit_sha: str) -> ProposalView:
    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(CorrectionProposal, proposal_id)
        if row is None or row.status != "applying":
            raise ValueError("Proposal is not applying")
        row.commit_sha = commit_sha
        row.status = "approved"
        row.reviewed_at = datetime.now(UTC)
        if active_build(db, row.project_id, correction_proposal_id=row.id) is None:
            add_queued_build(
                db,
                "incremental",
                row.reviewer_name,
                correction_proposal_id=row.id,
                project_id=row.project_id,
            )
        db.flush()
        result = ProposalView.model_validate(row)
    project_context.wake_build_queue()
    return result


def recover_applying(only_id: str | None = None):
    """Recover document approvals interrupted before or after the git commit."""
    with sessions()() as db:
        query = db.query(CorrectionProposal).filter_by(status="applying")
        if only_id is not None:
            query = query.filter_by(id=only_id)
        ids = [r.id for r in query.all()]
    for proposal_id in ids:
        try:
            with sessions()() as db:
                row = db.get(CorrectionProposal, proposal_id)
                directory, source_path, old, patch = (
                    row.source_directory,
                    row.source_path,
                    row.original,
                    row.patch,
                )
            with repository(directory) as root:
                path = safe(root, relative(source_path))
                message = f"Approve correction {proposal_id}"
                commits = (
                    git(
                        root,
                        "log",
                        "--all",
                        "--format=%H",
                        "--fixed-strings",
                        "--grep",
                        message,
                    )
                    .decode()
                    .splitlines()
                )
                matches = [
                    sha
                    for sha in commits
                    if git(root, "show", "-s", "--format=%s", sha).decode().strip()
                    == message
                ]
                if len(matches) > 1:
                    raise ValueError("Multiple commits for one correction")
                if matches:
                    if (
                        git(root, "show", f"{matches[0]}:{source_path}").decode()
                        != patch
                    ):
                        raise ValueError("Correction commit content mismatch")
                    _finalize(proposal_id, matches[0])
                    continue
                current = path.read_text(encoding="utf-8")
                staged = (
                    git(root, "diff", "--cached", "--name-only", "-z")
                    .decode()
                    .split("\0")
                )
                staged = [item for item in staged if item]
                if current == old:
                    staged = []
                elif any(item != source_path for item in staged):
                    raise ValueError(
                        "Unrelated staged changes prevent correction recovery"
                    )
                if staged:
                    if git(root, "show", f":{source_path}").decode() != patch:
                        raise ValueError("Staged document differs from correction")
                    git(root, "reset", "--quiet", "HEAD", "--", source_path)
                if current == patch:
                    _atomic_write(path, old)
                elif current != old:
                    raise ValueError("Document changed during correction recovery")
                with sessions().begin() as db:
                    row = db.get(CorrectionProposal, proposal_id)
                    if row.status == "applying":
                        row.status = "pending"
                        row.patch = row.original_patch
                        row.diff = "".join(
                            difflib.unified_diff(
                                row.original.splitlines(keepends=True),
                                row.patch.splitlines(keepends=True),
                                fromfile=row.target,
                                tofile=row.target,
                            )
                        )
                        row.reviewer_id = None
                        row.reviewer_name = None
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "Could not recover correction %s", proposal_id
            )


@router.post("/api/v1/corrections/{proposal_id}/approve", response_model=ProposalView)
def approve(
    proposal_id: str,
    scope: Annotated[project_context.ProjectContext, Depends(admin_project_scope)],
    requester: Annotated[object, Depends(rest_requester)],
    body: Approval | None = None,
):
    project_id = scope.project_id
    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = _pending(db, proposal_id, project_id)
        patch = body.patch if body and body.patch is not None else row.patch
        if patch == row.original:
            raise HTTPException(422, "Correction does not change the target")
        reviewer_name = db.query(Token).filter_by(id=requester.id).one().name
        if (
            any(c in reviewer_name for c in "<>\n\r\x00")
            or reviewer_name != reviewer_name.strip()
            or not reviewer_name
        ):
            raise HTTPException(422, "Invalid git author")
        if not row.target.startswith("document:"):
            override = db.get(MetadataOverride, (row.project_id, row.target))
            if override is None:
                db.add(
                    MetadataOverride(
                        project_id=row.project_id,
                        target=row.target,
                        comment=patch,
                    )
                )
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
            return ProposalView.model_validate(row)
        values = _settings(row.project_id)
        project = project_context.from_values(values, row.project_id)
        nodes = knowledge.execute(
            knowledge.read_graph(values),
            "MATCH (n:SDADocument {generation:$generation, ref:$ref}) RETURN properties(n) AS node",
            {"generation": project.generation, "ref": row.target},
        )
        if len(nodes) != 1 or not nodes[0]["node"].get("source_file"):
            raise HTTPException(409, "Document no longer exists")
        row.source_directory = project.source_directory_value
        row.source_path = nodes[0]["node"]["source_file"]
        row.patch = patch
        row.diff = "".join(
            difflib.unified_diff(
                row.original.splitlines(keepends=True),
                patch.splitlines(keepends=True),
                fromfile=row.target,
                tofile=row.target,
            )
        )
        row.status = "applying"
        row.reviewer_id = requester.id
        row.reviewer_name = reviewer_name
        source_directory, source_path, original = (
            row.source_directory,
            row.source_path,
            row.original,
        )
    attempted_write = False
    try:
        with repository(source_directory) as root:
            path = safe(root, relative(source_path))
            if path.read_text(encoding="utf-8") != original:
                raise ValueError("Document changed since proposal")
            if git(root, "diff", "--cached", "--name-only"):
                raise ValueError("Source repository has staged changes")
            attempted_write = True
            _atomic_write(path, patch)
            git(root, "add", "--", source_path)
            git(
                root,
                "commit",
                "--quiet",
                "-m",
                f"Approve correction {proposal_id}",
                author=reviewer_name,
            )
            sha = git(root, "rev-parse", "HEAD").decode().strip()
            return _finalize(proposal_id, sha)
    except (OSError, ValueError) as exc:
        if attempted_write:
            recover_applying(proposal_id)
        else:
            with sessions().begin() as db:
                row = db.get(CorrectionProposal, proposal_id)
                row.status = "pending"
                row.patch = row.original_patch
                row.diff = "".join(
                    difflib.unified_diff(
                        row.original.splitlines(keepends=True),
                        row.patch.splitlines(keepends=True),
                        fromfile=row.target,
                        tofile=row.target,
                    )
                )
                row.reviewer_id = None
                row.reviewer_name = None
        raise HTTPException(409, str(exc)) from exc
