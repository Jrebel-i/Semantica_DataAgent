"""Correction proposals submitted via HTTP/MCP with L1 gate and Git recovery."""

from __future__ import annotations

import difflib
import json
import logging
import os
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

from . import knowledge, project_context
from .api_time import UtcDatetime
from .auth import Requester
from .auth import current_requester as rest_requester
from .build_queue import active_build
from .clarification import _owned_conversation
from .db import CorrectionProposal, MetadataOverride, Token, sessions
from .mcp_context import bind_mcp_context, current_conversation_id, current_requester
from .project_api import admin_project_scope, project_scope
from .project_context import add_queued_build
from .semantic_overrides import (
    OVERRIDE_RELATIVE_PATH,
    GateValidationError,
    dump_overrides_yaml,
    legacy_target_to_patch,
    load_overrides_file,
    merge_overrides_doc,
    migrate_legacy_metadata_overrides,
    normalize_structured_patch,
    override_file_path,
    run_l1_gate,
)
from .source_directory import git, relative, repository, safe

logger = logging.getLogger(__name__)

Text = Annotated[str, Field(min_length=1, max_length=20000, pattern=r"\S")]
TargetKind = Literal["document", "override", "ossie"]
ProposalSource = Literal["manual", "mcp", "evolution"]


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = (path.stat().st_mode & 0o777) if path.exists() else 0o644
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


def _journal_path(root: Path, proposal_id: str) -> Path:
    return root / ".git" / f"dataagent-correction-{proposal_id}.json"


def _write_journal(root: Path, proposal_id: str, payload: dict[str, Any]) -> None:
    path = _journal_path(root, proposal_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".correction-journal-")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _read_journal(root: Path, proposal_id: str) -> dict[str, Any] | None:
    path = _journal_path(root, proposal_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _remove_journal(root: Path, proposal_id: str) -> None:
    _journal_path(root, proposal_id).unlink(missing_ok=True)


def _validate_git_author(name: str) -> str:
    if any(c in name for c in "<>\n\r\x00") or name != name.strip() or not name:
        raise ValueError("Invalid git author")
    return name


def _token_name(db, token_id: str) -> str:
    row = db.query(Token).filter_by(id=token_id).one_or_none()
    if row is not None and row.name:
        return row.name
    return token_id


def _settings(project_id):
    if project_id in (None, project_context.DEFAULT_PROJECT_ID):
        return knowledge.settings()
    return knowledge.settings(project_id)


def _parse_structured_patch_arg(
    structured_patch: dict[str, Any] | str | None,
) -> dict[str, Any] | None:
    if structured_patch is None:
        return None
    if isinstance(structured_patch, str):
        stripped = structured_patch.strip()
        if not stripped:
            return None
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"structured_patch is not valid JSON: {exc}") from exc
        if isinstance(parsed, dict):
            return parsed
        raise ValueError("structured_patch JSON must decode to an object")
    if isinstance(structured_patch, dict):
        return structured_patch
    raise ValueError("structured_patch must be a mapping or JSON object string")


def _resolve_target_kind(
    target: str,
    requested_kind: str | None,
    structured_patch: dict[str, Any] | None,
) -> TargetKind:
    if requested_kind is not None:
        if requested_kind not in {"document", "override", "ossie"}:
            raise ValueError(f"Unsupported target_kind: {requested_kind}")
        if requested_kind == "document" and structured_patch is not None:
            raise ValueError("Document corrections cannot carry structured_patch")
        return requested_kind  # type: ignore[return-value]
    if structured_patch is not None or target.startswith(("table:", "field:")):
        return "override"
    return "document"


def _unified_diff(target: str, old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=target,
            tofile=target,
        )
    )


def _unstage_path(root: Path, source_path: str) -> None:
    has_head = False
    try:
        git(root, "rev-parse", "--verify", "HEAD")
        has_head = True
    except ValueError:
        has_head = False
    if has_head:
        try:
            git(root, "cat-file", "-e", f"HEAD:{source_path}")
            git(root, "reset", "--quiet", "HEAD", "--", source_path)
            return
        except ValueError:
            pass
    git(root, "rm", "-r", "-f", "--cached", "--ignore-unmatch", "--", source_path)


def propose_correction(
    target: Text,
    patch: str = "",
    reason: Text = "",
    project_id: str = "default",
    *,
    source: ProposalSource = "mcp",
    target_kind: TargetKind | None = None,
    structured_patch: dict[str, Any] | str | None = None,
) -> dict:
    """Propose a correction or structured semantic override from an MCP caller."""
    return propose_in_project(
        project_id or project_context.DEFAULT_PROJECT_ID,
        target,
        patch,
        reason,
        source=source,
        target_kind=target_kind,
        structured_patch=structured_patch,
    )


def propose_in_project(
    project_id: str,
    target: Text,
    patch: str = "",
    reason: Text = "",
    *,
    source: ProposalSource = "mcp",
    target_kind: TargetKind | None = None,
    structured_patch: dict[str, Any] | str | None = None,
) -> dict:
    """Propose replacement content or a structured semantic override inside one project."""
    requester = current_requester()
    conversation_id = current_conversation_id()
    if conversation_id:
        _owned_conversation(conversation_id, requester.id)
    if not str(reason or "").strip():
        raise ValueError("Correction reason must not be blank")
    if source not in {"manual", "mcp", "evolution"}:
        raise ValueError(f"Unsupported correction source: {source}")

    parsed_patch = _parse_structured_patch_arg(structured_patch)
    resolved_kind = _resolve_target_kind(target, target_kind, parsed_patch)
    patch_text = str(patch or "")

    values = _settings(project_id)
    project = project_context.from_values(values, project_id)

    if resolved_kind == "document":
        if not target.startswith("document:"):
            raise ValueError("Unsupported correction target")
        if not patch_text.strip():
            raise ValueError("Correction patch must not be blank")
    elif parsed_patch is None:
        if not target.startswith(("table:", "field:")):
            raise ValueError("Unsupported correction target")
        if not patch_text.strip():
            raise ValueError("Correction patch must not be blank")

    original = ""
    if parsed_patch is None:
        known = (
            knowledge.existing_refs([target])
            if project_id == project_context.DEFAULT_PROJECT_ID
            else knowledge.existing_refs([target], project_id)
        )
        if target not in known:
            raise ValueError("Target is not in the current knowledge generation")
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
        original = node.get(
            "content" if target.startswith("document:") else "comment", ""
        )

    gate_report = run_l1_gate(
        target=target,
        patch=patch_text,
        reason=reason,
        structured_patch=parsed_patch,
        target_kind=resolved_kind,
        project_id=project_id,
    )
    if not gate_report.passed:
        raise GateValidationError(gate_report)

    stored_structured_patch: dict[str, Any] | None = None
    if parsed_patch is not None:
        stored_structured_patch = normalize_structured_patch(parsed_patch)
        if not patch_text.strip():
            patch_text = dump_overrides_yaml(stored_structured_patch)
        if project.source_directory_value:
            src_root = Path(project.source_directory_value)
            yaml_path = override_file_path(src_root)
            if yaml_path.is_file():
                original = yaml_path.read_text(encoding="utf-8")
            base_doc, _ = (
                load_overrides_file(src_root)
                if src_root.is_dir()
                else ({"version": 1}, None)
            )
            merged_yaml = dump_overrides_yaml(
                merge_overrides_doc(base_doc, stored_structured_patch)
            )
        else:
            merged_yaml = dump_overrides_yaml(stored_structured_patch)
        diff = _unified_diff(OVERRIDE_RELATIVE_PATH, original, merged_yaml)
        if not diff:
            raise ValueError("Correction does not change semantic_overrides.yaml")
    else:
        if resolved_kind in {"override", "ossie"}:
            stored_structured_patch = legacy_target_to_patch(target, patch_text)
        diff = _unified_diff(target, original, patch_text)
        if not diff:
            raise ValueError("Correction does not change the target")

    proposal_id = uuid.uuid4().hex
    with sessions().begin() as db:
        row = CorrectionProposal(
            id=proposal_id,
            project_id=project_id,
            owner_id=requester.id,
            conversation_id=conversation_id,
            target=target,
            patch=patch_text,
            original_patch=patch_text,
            original=original,
            diff=diff,
            reason=reason,
            status="pending",
            source=source,
            target_kind=resolved_kind,
            structured_patch=(
                json.dumps(stored_structured_patch, ensure_ascii=False)
                if stored_structured_patch is not None
                else None
            ),
            gate_report=json.dumps(gate_report.to_dict(), ensure_ascii=False),
        )
        db.add(row)
        db.flush()
        submitter_name = _token_name(db, requester.id)

    policy = project_context.evolution_policy(project_id)
    require_review = (
        policy["evolution_require_review"]
        if source == "evolution"
        else policy["correction_require_review"]
    )
    if not require_review:
        author = _validate_git_author(submitter_name)
        view = _apply_proposal(
            proposal_id,
            reviewer_id=None,
            reviewer_name=None,
            author_name=author,
            patch_override=None,
            structured_override=None,
            auto_apply=True,
        )
        return {**view.model_dump(mode="json"), "proposal_id": proposal_id}

    with sessions()() as db:
        view = ProposalView.model_validate(db.get(CorrectionProposal, proposal_id))
    return {**view.model_dump(mode="json"), "proposal_id": proposal_id}


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
    source: str = "manual"
    target_kind: str = "document"
    structured_patch: dict[str, Any] | None = None
    gate_report: dict[str, Any] | None = None
    reviewer_id: str | None
    reviewed_at: UtcDatetime | None
    reverted_at: UtcDatetime | None = None
    revert_commit_sha: str | None = None
    created_at: UtcDatetime
    model_config = {"from_attributes": True}

    @field_validator("structured_patch", "gate_report", mode="before")
    @classmethod
    def _parse_json_field(cls, value: Any) -> dict[str, Any] | None:
        if value is None or isinstance(value, dict):
            return value
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            return json.loads(stripped)
        return value


class Approval(BaseModel):
    patch: Text | None = None
    structured_patch: dict[str, Any] | None = None


class ProposalCreate(BaseModel):
    target: Text
    patch: str = Field(default="", max_length=20000)
    reason: Text
    source: ProposalSource | None = None
    target_kind: TargetKind | None = None
    structured_patch: dict[str, Any] | None = None


@router.post("/api/v1/corrections", status_code=201)
def create_proposal(
    body: ProposalCreate,
    requester: Annotated[Requester, Depends(rest_requester)],
    scope: Annotated[project_context.ProjectContext, Depends(project_scope)],
):
    if body.structured_patch is None and not body.patch.strip():
        raise HTTPException(
            422, "patch must not be blank when structured_patch is omitted"
        )
    try:
        with bind_mcp_context(requester):
            return propose_in_project(
                scope.project_id,
                body.target,
                body.patch,
                body.reason,
                source=body.source or "manual",
                target_kind=body.target_kind,
                structured_patch=body.structured_patch,
            )
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None


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
    source_dir_val: str | None = None
    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(CorrectionProposal, proposal_id)
        if row is None or row.status != "applying":
            raise ValueError("Proposal is not applying")
        source_dir_val = row.source_directory
        is_human_review = row.reviewer_id is not None
        row.commit_sha = commit_sha
        row.status = "approved" if is_human_review else "auto_applied"
        if is_human_review:
            row.reviewed_at = datetime.now(UTC)
        triggered_by = row.reviewer_name or _token_name(db, row.owner_id)
        if active_build(db, row.project_id, correction_proposal_id=row.id) is None:
            add_queued_build(
                db,
                "incremental",
                triggered_by,
                correction_proposal_id=row.id,
                project_id=row.project_id,
            )
        db.flush()
        result = ProposalView.model_validate(row)
    if source_dir_val:
        _remove_journal(Path(source_dir_val), proposal_id)
    project_context.wake_build_queue()
    return result


def _rollback_applying_row(proposal_id: str) -> None:
    with sessions().begin() as db:
        row = db.get(CorrectionProposal, proposal_id)
        if row is None or row.status != "applying":
            return
        row.status = "pending"
        if row.original_patch is not None:
            row.patch = row.original_patch
        row.diff = _unified_diff(
            OVERRIDE_RELATIVE_PATH if row.structured_patch else row.target,
            row.original,
            row.patch,
        )
        row.reviewer_id = None
        row.reviewer_name = None


def _recover_git_applying_row(row_snapshot: dict[str, Any]) -> None:
    proposal_id = row_snapshot["id"]
    directory = row_snapshot["source_directory"]
    source_path = row_snapshot["source_path"]
    old = row_snapshot["original"]
    patch = row_snapshot["patch"]
    target_kind = row_snapshot["target_kind"]

    with repository(directory) as root:
        journal = _read_journal(root, proposal_id)
        if journal is not None:
            source_path = journal["source_path"]
            before_existed = bool(journal["before_existed"])
            before_content = str(journal["before_content"])
            after_content = str(journal["after_content"])
            message = str(journal["commit_message"])
        else:
            before_existed = True
            before_content = old
            after_content = patch
            is_human = row_snapshot["reviewer_id"] is not None
            message = (
                f"Approve correction {proposal_id}"
                if is_human
                else f"Auto-apply correction {proposal_id}"
            )

        path = safe(root, relative(source_path))
        try:
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
        except ValueError:
            commits = []
        matches = [
            sha
            for sha in commits
            if git(root, "show", "-s", "--format=%s", sha).decode().strip() == message
        ]
        if len(matches) > 1:
            raise ValueError("Multiple commits for one correction")
        if matches:
            committed = git(root, "show", f"{matches[0]}:{source_path}").decode()
            if (
                target_kind == "document" or journal is not None
            ) and committed != after_content:
                raise ValueError("Correction commit content mismatch")
            _finalize(proposal_id, matches[0])
            return

        current = path.read_text(encoding="utf-8") if path.exists() else ""
        staged_raw = (
            git(root, "diff", "--cached", "--name-only", "-z").decode().split("\0")
        )
        staged = [item for item in staged_raw if item]
        if current == before_content and (before_existed or not path.exists()):
            staged = []
        elif any(item != source_path for item in staged):
            raise ValueError("Unrelated staged changes prevent correction recovery")
        if staged:
            if (target_kind == "document" or journal is not None) and git(
                root, "show", f":{source_path}"
            ).decode() != after_content:
                raise ValueError("Staged document differs from correction")
            _unstage_path(root, source_path)

        if target_kind in {"override", "ossie"} and journal is None:
            # Killed in precheck before journal creation: working tree untouched
            _rollback_applying_row(proposal_id)
            return

        if current == after_content:
            if before_existed:
                _atomic_write(path, before_content)
            elif path.exists():
                path.unlink()
        elif current != before_content or (before_existed and not path.exists()):
            raise ValueError("Document changed during correction recovery")

        _remove_journal(root, proposal_id)
        _rollback_applying_row(proposal_id)


def recover_applying(only_id: str | None = None):
    """Recover document or semantic override approvals interrupted around git commit."""
    with sessions()() as db:
        query = db.query(CorrectionProposal).filter_by(status="applying")
        if only_id is not None:
            query = query.filter_by(id=only_id)
        snapshots = [
            {
                "id": r.id,
                "source_directory": r.source_directory,
                "source_path": r.source_path,
                "original": r.original,
                "patch": r.patch,
                "target_kind": r.target_kind or "document",
                "reviewer_id": r.reviewer_id,
            }
            for r in query.all()
        ]
    for snap in snapshots:
        try:
            _recover_git_applying_row(snap)
        except Exception:
            logger.exception("Could not recover correction %s", snap["id"])


def _load_structured_from_row(
    row: CorrectionProposal,
    patch_text: str,
    structured_override: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if structured_override is not None:
        return normalize_structured_patch(structured_override)
    if row.structured_patch:
        parsed = json.loads(row.structured_patch)
        if (
            row.target.startswith(("table:", "field:"))
            and patch_text != row.original_patch
        ):
            converted = legacy_target_to_patch(row.target, patch_text)
            if converted is not None:
                return converted
        return parsed
    return legacy_target_to_patch(row.target, patch_text)


def _apply_proposal(
    proposal_id: str,
    *,
    reviewer_id: str | None,
    reviewer_name: str | None,
    author_name: str,
    patch_override: str | None,
    structured_override: dict[str, Any] | None,
    auto_apply: bool,
    project_id: str | None = None,
) -> ProposalView:
    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = _pending(db, proposal_id, project_id)
        effective_patch = patch_override if patch_override is not None else row.patch
        effective_structured = _load_structured_from_row(
            row, effective_patch, structured_override
        )
        if patch_override is None and structured_override is not None:
            effective_patch = dump_overrides_yaml(effective_structured)
        if (
            not auto_apply
            and row.target_kind == "document"
            and effective_patch == row.original
        ):
            raise HTTPException(422, "Correction does not change the target")
        row_target = row.target
        row_reason = row.reason
        row_project_id = row.project_id
        row_kind = row.target_kind or (
            "override"
            if (
                effective_structured is not None
                or row_target.startswith(("table:", "field:"))
            )
            else "document"
        )

    gate_report = run_l1_gate(
        target=row_target,
        patch=effective_patch,
        reason=row_reason,
        structured_patch=effective_structured,
        target_kind=row_kind,
        project_id=row_project_id,
    )
    if not gate_report.passed:
        raise HTTPException(422, str(GateValidationError(gate_report)))

    values = _settings(row_project_id)
    project = project_context.from_values(values, row_project_id)

    if row_kind != "document" and not project.source_directory_value:
        # Legacy DB-only metadata override without source_directory configured
        with sessions().begin() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            row = _pending(db, proposal_id, project_id)
            override = db.get(MetadataOverride, (row.project_id, row.target))
            if override is None:
                db.add(
                    MetadataOverride(
                        project_id=row.project_id,
                        target=row.target,
                        comment=effective_patch,
                    )
                )
            else:
                override.comment = effective_patch
            row.patch = effective_patch
            row.diff = _unified_diff(row.target, row.original, effective_patch)
            row.gate_report = json.dumps(gate_report.to_dict(), ensure_ascii=False)
            row.status = "auto_applied" if auto_apply else "approved"
            row.reviewer_id = reviewer_id
            row.reviewer_name = reviewer_name
            if not auto_apply:
                row.reviewed_at = datetime.now(UTC)
            db.flush()
            return ProposalView.model_validate(row)

    if row_kind != "document":
        migrate_legacy_metadata_overrides(row_project_id)

    doc_source_file: str | None = None
    if row_kind == "document":
        nodes = knowledge.execute(
            knowledge.read_graph(values),
            "MATCH (n:SDADocument {generation:$generation, ref:$ref}) RETURN properties(n) AS node",
            {"generation": project.generation, "ref": row_target},
        )
        if len(nodes) != 1 or not nodes[0]["node"].get("source_file"):
            raise HTTPException(409, "Document no longer exists")
        doc_source_file = nodes[0]["node"]["source_file"]

    commit_message = (
        f"Auto-apply correction {proposal_id}"
        if auto_apply
        else f"Approve correction {proposal_id}"
    )

    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = _pending(db, proposal_id, project_id)
        row.source_directory = project.source_directory_value
        row.source_path = (
            doc_source_file if row_kind == "document" else OVERRIDE_RELATIVE_PATH
        )
        row.patch = effective_patch
        if effective_structured is not None:
            row.structured_patch = json.dumps(effective_structured, ensure_ascii=False)
        row.gate_report = json.dumps(gate_report.to_dict(), ensure_ascii=False)
        row.diff = _unified_diff(row.source_path, row.original, effective_patch)
        row.status = "applying"
        row.reviewer_id = reviewer_id
        row.reviewer_name = reviewer_name
        src_dir = row.source_directory
        src_path = row.source_path
        expected_original = row.original

    attempted_write = False
    try:
        with repository(src_dir) as root:
            path = safe(root, relative(src_path))
            if git(root, "diff", "--cached", "--name-only"):
                raise ValueError("Source repository has staged changes")

            if row_kind == "document":
                if path.read_text(encoding="utf-8") != expected_original:
                    raise ValueError("Document changed since proposal")
                new_content = effective_patch
                before_existed = True
                before_content = expected_original
            else:
                if effective_structured is None:
                    raise ValueError("Missing structured_patch for override correction")
                before_existed = path.is_file()
                before_content = (
                    path.read_text(encoding="utf-8") if before_existed else ""
                )
                base_doc, _ = load_overrides_file(root)
                merged_doc = merge_overrides_doc(base_doc, effective_structured)
                new_content = dump_overrides_yaml(merged_doc)
                if new_content == before_content:
                    raise ValueError(
                        "Correction does not change semantic_overrides.yaml"
                    )
                with sessions().begin() as db:
                    row = db.get(CorrectionProposal, proposal_id)
                    if row is not None and row.status == "applying":
                        row.original = before_content
                        row.patch = new_content
                        row.diff = _unified_diff(src_path, before_content, new_content)

            _write_journal(
                root,
                proposal_id,
                {
                    "proposal_id": proposal_id,
                    "source_path": src_path,
                    "before_existed": before_existed,
                    "before_content": before_content,
                    "after_content": new_content,
                    "commit_message": commit_message,
                    "author_name": author_name,
                },
            )
            attempted_write = True
            _atomic_write(path, new_content)
            git(root, "add", "--", src_path)
            git(
                root,
                "commit",
                "--quiet",
                "-m",
                commit_message,
                author=author_name,
            )
            sha = git(root, "rev-parse", "HEAD").decode().strip()
            return _finalize(proposal_id, sha)
    except (OSError, ValueError) as exc:
        if attempted_write:
            recover_applying(proposal_id)
        else:
            _rollback_applying_row(proposal_id)
        status_code = 422 if isinstance(exc, GateValidationError) else 409
        raise HTTPException(status_code, str(exc)) from exc


@router.post("/api/v1/corrections/{proposal_id}/approve", response_model=ProposalView)
def approve(
    proposal_id: str,
    scope: Annotated[project_context.ProjectContext, Depends(admin_project_scope)],
    requester: Annotated[object, Depends(rest_requester)],
    body: Approval | None = None,
):
    project_id = scope.project_id
    with sessions()() as db:
        reviewer_name = db.query(Token).filter_by(id=requester.id).one().name
    try:
        _validate_git_author(reviewer_name)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None

    return _apply_proposal(
        proposal_id,
        reviewer_id=requester.id,
        reviewer_name=reviewer_name,
        author_name=reviewer_name,
        patch_override=body.patch if body else None,
        structured_override=body.structured_patch if body else None,
        auto_apply=False,
        project_id=project_id,
    )


def _abort_revert_if_active(root: Path) -> None:
    if (root / ".git" / "REVERT_HEAD").exists():
        git(root, "revert", "--abort")
        return
    try:
        unmerged = git(root, "diff", "--name-only", "--diff-filter=U").strip()
    except ValueError:
        unmerged = b""
    if unmerged:
        git(root, "revert", "--abort")


@router.post("/api/v1/corrections/{proposal_id}/revert", response_model=ProposalView)
def revert(
    proposal_id: str,
    scope: Annotated[project_context.ProjectContext, Depends(admin_project_scope)],
    requester: Annotated[object, Depends(rest_requester)],
):
    project_id = scope.project_id
    with sessions()() as db:
        actor_name = _token_name(db, requester.id)
        row = db.get(CorrectionProposal, proposal_id)
        if row is None or row.project_id != project_id:
            raise HTTPException(404, "Proposal not found")
        if row.status not in {"approved", "auto_applied"}:
            raise HTTPException(
                409, f"Proposal cannot be reverted from status {row.status}"
            )
        if not row.commit_sha or not row.source_directory:
            raise HTTPException(409, "Proposal has no Git commit to revert")
        commit_sha = row.commit_sha
        source_dir = row.source_directory

    try:
        _validate_git_author(actor_name)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None

    try:
        with repository(source_dir) as root:
            with sessions()() as db:
                current_row = db.get(CorrectionProposal, proposal_id)
                if current_row is None or current_row.project_id != project_id:
                    raise HTTPException(404, "Proposal not found")
                if current_row.status not in {"approved", "auto_applied"}:
                    raise HTTPException(
                        409,
                        f"Proposal cannot be reverted from status {current_row.status}",
                    )
            if git(root, "diff", "--cached", "--name-only"):
                raise HTTPException(409, "Source repository has staged changes")
            try:
                git(root, "revert", "--no-edit", commit_sha, author=actor_name)
            except ValueError as exc:
                _abort_revert_if_active(root)
                raise HTTPException(409, f"Git revert failed: {exc}") from exc
            revert_sha = git(root, "rev-parse", "HEAD").decode().strip()
    except HTTPException:
        raise
    except (OSError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc

    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(CorrectionProposal, proposal_id)
        if row is None or row.project_id != project_id:
            raise HTTPException(404, "Proposal not found")
        if row.status not in {"approved", "auto_applied"}:
            raise HTTPException(
                409, f"Proposal cannot be reverted from status {row.status}"
            )
        row.status = "reverted"
        row.revert_commit_sha = revert_sha
        row.reverted_at = datetime.now(UTC)
        add_queued_build(
            db,
            "incremental",
            actor_name,
            correction_proposal_id=None,
            project_id=row.project_id,
        )
        db.flush()
        result = ProposalView.model_validate(row)
    project_context.wake_build_queue()
    return result
