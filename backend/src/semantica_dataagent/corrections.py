"""Correction proposals submitted via HTTP/MCP with L1 gate and Git recovery."""

from __future__ import annotations

import difflib
import json
import logging
import os
import tempfile
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import text

from . import knowledge, project_context
from .api_time import UtcDatetime
from .auth import Requester
from .auth import current_requester as rest_requester
from .build_queue import active_build
from .clarification import _owned_conversation
from .db import (
    Conversation,
    CorrectionProposal,
    KnowledgeGap,
    MetadataOverride,
    Token,
    sessions,
)
from .knowledge_synthesizer import (
    LLMUnavailableError,
    markdown_root_for_project,
    unified_diff_text,
)
from .mcp_context import bind_mcp_context, current_conversation_id, current_requester
from .project_api import admin_project_scope, project_scope
from .project_context import add_queued_build
from .semantic_overrides import (
    OVERRIDE_RELATIVE_PATH,
    GateReport,
    GateValidationError,
    dump_overrides_yaml,
    legacy_target_to_patch,
    load_overrides_file,
    merge_overrides_doc,
    migrate_legacy_metadata_overrides,
    normalize_structured_patch,
    override_file_path,
    run_l1_gate,
    validate_markdown_relpath,
)
from .source_directory import git, relative, repository, safe

logger = logging.getLogger(__name__)

Text = Annotated[str, Field(min_length=1, max_length=20000, pattern=r"\S")]
TargetKind = Literal["document", "override", "ossie"]
ProposalSource = Literal["manual", "feedback", "mcp", "evolution", "discovery"]
DestinationKind = Literal[
    "historical_ticket", "knowledge_doc", "semantic_override", "ossie_model"
]

_TRUSTED_EVOLUTION_SOURCE: ContextVar[bool] = ContextVar(
    "trusted_evolution_source", default=False
)
_TRUSTED_DISCOVERY_SOURCE: ContextVar[bool] = ContextVar(
    "trusted_discovery_source", default=False
)
_TRUSTED_FEEDBACK_SOURCE: ContextVar[bool] = ContextVar(
    "trusted_feedback_source", default=False
)


@contextmanager
def trusted_evolution_source():
    """Authorize internal ``evolution_runner`` calls to emit ``source='evolution'`` proposals."""
    token = _TRUSTED_EVOLUTION_SOURCE.set(True)
    try:
        yield
    finally:
        _TRUSTED_EVOLUTION_SOURCE.reset(token)


@contextmanager
def trusted_discovery_source():
    """Authorize internal ``discovery_runner`` calls to emit ``source='discovery'`` proposals."""
    token = _TRUSTED_DISCOVERY_SOURCE.set(True)
    try:
        yield
    finally:
        _TRUSTED_DISCOVERY_SOURCE.reset(token)


@contextmanager
def trusted_feedback_source():
    """Authorize internal feedback curation calls to emit ``source='feedback'`` proposals."""
    token = _TRUSTED_FEEDBACK_SOURCE.set(True)
    try:
        yield
    finally:
        _TRUSTED_FEEDBACK_SOURCE.reset(token)


def _validate_doc_relpath(target: str, project_id: str) -> str:
    if not target.startswith("document:"):
        raise ValueError("Document target must start with document:")
    md_root = markdown_root_for_project(project_id)
    return validate_markdown_relpath(
        target[len("document:") :],
        markdown_root=md_root,
        require_category_subdir=True,
    )


def _with_placeholder_gate(report: GateReport, target: str, patch: str) -> GateReport:
    """Reject historical-ticket drafts that still carry unresolved placeholders."""
    from .historical_tickets import historical_ticket_placeholder_errors

    errors = historical_ticket_placeholder_errors(target, patch)
    if not errors:
        return report
    return GateReport(
        passed=False,
        checks=[
            *report.checks,
            {
                "name": "historical_ticket_placeholders",
                "passed": False,
                "details": errors,
            },
        ],
        errors=[*report.errors, *errors],
    )


def _evolution_doc_relpath(target: str) -> str:
    return _validate_doc_relpath(target, project_context.DEFAULT_PROJECT_ID)


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


def _revert_journal_path(root: Path, proposal_id: str) -> Path:
    return root / ".git" / f"dataagent-revert-{proposal_id}.json"


def _write_json_file(path: Path, payload: dict[str, Any], prefix: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=prefix)
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


def _read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _write_journal(root: Path, proposal_id: str, payload: dict[str, Any]) -> None:
    _write_json_file(_journal_path(root, proposal_id), payload, ".correction-journal-")


def _read_journal(root: Path, proposal_id: str) -> dict[str, Any] | None:
    return _read_json_file(_journal_path(root, proposal_id))


def _remove_journal(root: Path, proposal_id: str) -> None:
    _journal_path(root, proposal_id).unlink(missing_ok=True)


def _write_revert_journal(
    root: Path, proposal_id: str, payload: dict[str, Any]
) -> None:
    _write_json_file(
        _revert_journal_path(root, proposal_id), payload, ".revert-journal-"
    )


def _read_revert_journal(root: Path, proposal_id: str) -> dict[str, Any] | None:
    return _read_json_file(_revert_journal_path(root, proposal_id))


def _remove_revert_journal(root: Path, proposal_id: str) -> None:
    _revert_journal_path(root, proposal_id).unlink(missing_ok=True)


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
    source: Literal["mcp"] = "mcp",
    target_kind: TargetKind | None = None,
    structured_patch: dict[str, Any] | str | None = None,
) -> dict:
    """Propose a correction or structured semantic override from an MCP caller."""
    if source != "mcp":
        raise ValueError("MCP corrections must use source='mcp'")
    return propose_in_project(
        project_id or project_context.DEFAULT_PROJECT_ID,
        target,
        patch,
        reason,
        source="mcp",
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
    conversation_id: str | None = None,
    eval_candidate: dict[str, Any] | None = None,
    extra_gate_report: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    proposal_id: str | None = None,
    knowledge_gap_id: str | None = None,
) -> dict:
    """Propose replacement content, a new Markdown document, or a structured semantic override inside one project."""
    requester = current_requester()
    if knowledge_gap_id is not None:
        with sessions()() as db:
            gap = db.get(KnowledgeGap, knowledge_gap_id)
            if gap is None or gap.project_id != project_id or gap.status != "confirmed":
                raise ValueError("Confirmed knowledge gap not found in project")
    resolved_conversation_id = (
        conversation_id if conversation_id is not None else current_conversation_id()
    )
    if resolved_conversation_id:
        _owned_conversation(resolved_conversation_id, requester.id)
        with sessions()() as db:
            conv = db.get(Conversation, resolved_conversation_id)
            if conv is None or conv.project_id != project_id:
                raise ValueError("Conversation not found in project")
    if not str(reason or "").strip():
        raise ValueError("Correction reason must not be blank")
    if source not in {"manual", "feedback", "mcp", "evolution", "discovery"}:
        raise ValueError(f"Unsupported correction source: {source}")
    if source == "evolution" and not _TRUSTED_EVOLUTION_SOURCE.get():
        raise ValueError(
            "Evolution proposals can only be submitted by the evolution runner"
        )
    if source == "discovery" and not _TRUSTED_DISCOVERY_SOURCE.get():
        raise ValueError(
            "Discovery proposals can only be submitted by the discovery runner"
        )
    if source == "feedback" and not _TRUSTED_FEEDBACK_SOURCE.get():
        raise ValueError(
            "Feedback proposals can only be submitted by the historical ticket curator"
        )

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
        if (
            resolved_kind == "ossie"
            and target.startswith("ossie:")
            and source == "evolution"
        ):
            if not patch_text.strip():
                raise ValueError("Correction patch must not be blank")
        elif not target.startswith(("table:", "field:")):
            raise ValueError("Unsupported correction target")
        elif not patch_text.strip():
            raise ValueError("Correction patch must not be blank")

    original = ""
    doc_source_path: str | None = None
    is_new_file = False
    if parsed_patch is None:
        if (
            resolved_kind == "ossie"
            and target.startswith("ossie:")
            and source == "evolution"
        ):
            rel_ossie = str(relative(target[len("ossie:") :]))
            if not rel_ossie.startswith("ossie/") or not rel_ossie.endswith(
                (".yaml", ".yml")
            ):
                raise ValueError("Ossie target path must live under ossie/*.yaml")
            if project.source_directory_value:
                cand = Path(project.source_directory_value) / rel_ossie
                if cand.is_file():
                    original = cand.read_text(encoding="utf-8")
        else:
            try:
                known = (
                    knowledge.existing_refs([target])
                    if project_id == project_context.DEFAULT_PROJECT_ID
                    else knowledge.existing_refs([target], project_id)
                )
            except (KeyError, knowledge.StorageUnavailable, ValueError, RuntimeError):
                if source != "evolution" and resolved_kind != "document":
                    raise
                known = set()
            if target not in known:
                if resolved_kind == "document":
                    rel_doc = _validate_doc_relpath(target, project_id)
                    doc_source_path = rel_doc
                    if project.source_directory_value:
                        cand = safe(
                            Path(project.source_directory_value), relative(rel_doc)
                        )
                        if cand.is_file():
                            original = cand.read_text(encoding="utf-8")
                        else:
                            is_new_file = True
                    else:
                        is_new_file = True
                else:
                    raise ValueError(
                        "Target is not in the current knowledge generation"
                    )
            else:
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
                doc_source_path = (
                    str(node.get("source_file"))
                    if target.startswith("document:") and node.get("source_file")
                    else None
                )
                original = node.get(
                    "content" if target.startswith("document:") else "comment", ""
                )
                if doc_source_path and project.source_directory_value:
                    cand = safe(
                        Path(project.source_directory_value), relative(doc_source_path)
                    )
                    if cand.is_file():
                        original = cand.read_text(encoding="utf-8")

    gate_report = run_l1_gate(
        target=target,
        patch=patch_text,
        reason=reason,
        structured_patch=parsed_patch,
        target_kind=resolved_kind,
        project_id=project_id,
        resolved_path=doc_source_path,
        is_new_file=is_new_file,
    )
    if not gate_report.passed:
        raise GateValidationError(gate_report)

    stored_structured_patch: dict[str, Any] | None = None
    if parsed_patch is not None:
        if not project.source_directory_value:
            raise ValueError(
                "Structured overrides require a configured source_directory"
            )
        stored_structured_patch = normalize_structured_patch(parsed_patch)
        if not patch_text.strip():
            patch_text = dump_overrides_yaml(stored_structured_patch)
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
        diff = _unified_diff(OVERRIDE_RELATIVE_PATH, original, merged_yaml)
        if not diff:
            raise ValueError("Correction does not change semantic_overrides.yaml")
    else:
        if resolved_kind == "ossie" and not project.source_directory_value:
            raise ValueError(
                "Structured overrides require a configured source_directory"
            )
        if resolved_kind in {"override", "ossie"}:
            stored_structured_patch = legacy_target_to_patch(target, patch_text)
        if is_new_file and doc_source_path:
            diff = unified_diff_text(doc_source_path, "", patch_text, is_new_file=True)
        else:
            diff = _unified_diff(target, original, patch_text)
        if not diff:
            raise ValueError("Correction does not change the target")

    policy = project_context.evolution_policy(project_id)
    if source in {"discovery", "evolution", "feedback"} or knowledge_gap_id is not None:
        # Discovery, evolution, feedback tickets, and confirmed-gap proposals MUST enter human review.
        require_review = True
    else:
        require_review = policy["correction_require_review"]
    if not require_review:
        with sessions()() as db:
            submitter_name = _token_name(db, requester.id)
        author = _validate_git_author(submitter_name)
    else:
        author = None

    gate_payload = gate_report.to_dict()
    if extra_gate_report:
        for key, value in extra_gate_report.items():
            if key == "checks" and isinstance(value, list):
                gate_payload["checks"] = [*gate_payload.get("checks", []), *value]
            elif key == "errors" and isinstance(value, list):
                gate_payload["errors"] = [*gate_payload.get("errors", []), *value]
            elif key not in {"passed"}:
                gate_payload[key] = value
    if eval_candidate:
        cleaned_candidate = {
            key: value for key, value in eval_candidate.items() if value is not None
        }
        if cleaned_candidate:
            gate_payload["eval_candidate"] = cleaned_candidate

    trigger_evidence: dict[str, Any] = {}
    if isinstance(evidence, dict):
        trigger_evidence.update(evidence)
    elif isinstance(gate_payload.get("discovery_evidence"), dict):
        trigger_evidence.update(gate_payload["discovery_evidence"])
    elif isinstance(gate_payload.get("provenance"), dict):
        trigger_evidence.update(gate_payload["provenance"])
    if knowledge_gap_id and "knowledge_gap_id" not in trigger_evidence:
        trigger_evidence["knowledge_gap_id"] = knowledge_gap_id
    if resolved_conversation_id and "conversation_id" not in trigger_evidence:
        trigger_evidence["conversation_id"] = resolved_conversation_id
    gate_payload["evidence"] = {
        "source_type": source,
        "summary_reason": reason,
        "trigger_evidence": trigger_evidence,
    }

    proposal_id = proposal_id or uuid.uuid4().hex
    with sessions().begin() as db:
        row = CorrectionProposal(
            id=proposal_id,
            project_id=project_id,
            owner_id=requester.id,
            conversation_id=resolved_conversation_id,
            knowledge_gap_id=knowledge_gap_id,
            target=target,
            patch=patch_text,
            original_patch=patch_text,
            original=original,
            diff=diff,
            reason=reason,
            status="pending",
            source=source,
            target_kind=resolved_kind,
            source_path=(
                doc_source_path
                if resolved_kind == "document"
                else (
                    OVERRIDE_RELATIVE_PATH
                    if resolved_kind == "override" and project.source_directory_value
                    else None
                )
            ),
            structured_patch=(
                json.dumps(stored_structured_patch, ensure_ascii=False)
                if stored_structured_patch is not None
                else None
            ),
            gate_report=json.dumps(gate_payload, ensure_ascii=False),
        )
        db.add(row)
        db.flush()

    if not require_review:
        assert author is not None
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
    destination_kind: DestinationKind = "knowledge_doc"
    is_new_file: bool = False
    structured_patch: dict[str, Any] | None = None
    gate_report: dict[str, Any] | None = None
    evidence: dict[str, Any] | None = None
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

    @model_validator(mode="after")
    def _derive_evidence_and_new_file(self):
        from .historical_tickets import is_historical_ticket_target

        if self.target_kind == "override":
            self.destination_kind = "semantic_override"
        elif self.target_kind == "ossie":
            self.destination_kind = "ossie_model"
        elif is_historical_ticket_target(self.target):
            self.destination_kind = "historical_ticket"
        else:
            self.destination_kind = "knowledge_doc"
        if self.target_kind == "document" and self.diff.startswith("--- /dev/null"):
            self.is_new_file = True
        if self.evidence is None:
            gate = self.gate_report or {}
            stored_ev = gate.get("evidence")
            if isinstance(stored_ev, dict):
                self.evidence = dict(stored_ev)
            else:
                trigger_ev = {}
                if isinstance(gate.get("discovery_evidence"), dict):
                    trigger_ev = dict(gate["discovery_evidence"])
                elif isinstance(gate.get("provenance"), dict):
                    trigger_ev = dict(gate["provenance"])
                self.evidence = {
                    "source_type": self.source,
                    "summary_reason": self.reason,
                    "trigger_evidence": trigger_ev,
                }
        return self


class Approval(BaseModel):
    patch: Text | None = None
    structured_patch: dict[str, Any] | None = None
    question: str | None = Field(default=None, max_length=20000)
    reference_sql: str | None = Field(default=None, max_length=20000)
    caliber: str | None = Field(default=None, max_length=5000)
    expected_tables: list[str] | None = None
    expected_fields: list[str] | None = None
    dialect: Literal["hive", "spark", "doris"] | None = None
    category: str | None = Field(default=None, min_length=1, max_length=120)
    turn_id: int | None = None


class ProposalCreate(BaseModel):
    target: Text
    patch: str = Field(default="", max_length=20000)
    reason: Text
    source: Literal["manual"] | None = None
    target_kind: TargetKind | None = None
    structured_patch: dict[str, Any] | None = None
    evidence: dict[str, Any] | None = None
    conversation_id: str | None = None
    knowledge_gap_id: str | None = None
    turn_id: int | None = None
    question: str | None = Field(default=None, max_length=20000)
    reference_sql: str | None = Field(default=None, max_length=20000)
    caliber: str | None = Field(default=None, max_length=5000)
    expected_tables: list[str] | None = None
    expected_fields: list[str] | None = None
    dialect: Literal["hive", "spark", "doris"] | None = None
    category: str | None = Field(default=None, min_length=1, max_length=120)


from .correction_draft import (
    DraftCorrectionRequest,
    DraftCorrectionResponse,
    draft_correction_in_project,
)


@router.post("/api/v1/corrections/draft", response_model=DraftCorrectionResponse)
def draft_proposal(
    body: DraftCorrectionRequest,
    requester: Annotated[Requester, Depends(rest_requester)],
    project: Annotated[str | None, Query()] = None,
):
    try:
        return draft_correction_in_project(body, requester, query_project=project)
    except LLMUnavailableError as exc:
        return JSONResponse(
            status_code=503,
            content={
                "detail": exc.message,
                "code": exc.code,
                "fallback": "manual_edit",
                "manual_edit_recommended": True,
            },
        )


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
    eval_candidate = {
        "turn_id": body.turn_id,
        "question": body.question,
        "reference_sql": body.reference_sql,
        "caliber": body.caliber,
        "expected_tables": body.expected_tables,
        "expected_fields": body.expected_fields,
        "dialect": body.dialect,
        "category": body.category,
    }
    try:
        if body.source not in (None, "manual"):
            raise ValueError("REST callers may only submit source='manual' proposals")
        with bind_mcp_context(requester):
            return propose_in_project(
                scope.project_id,
                body.target,
                body.patch,
                body.reason,
                source="manual",
                target_kind=body.target_kind,
                structured_patch=body.structured_patch,
                conversation_id=body.conversation_id,
                knowledge_gap_id=body.knowledge_gap_id,
                evidence=body.evidence,
                eval_candidate=eval_candidate
                if any(v is not None for v in eval_candidate.values())
                else None,
            )
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None


@router.get("/api/v1/corrections", response_model=list[ProposalView])
def list_proposals(
    scope: Annotated[project_context.ProjectContext, Depends(project_scope)],
    source: Annotated[ProposalSource | None, Query()] = None,
    destination_kind: Annotated[DestinationKind | None, Query()] = None,
):
    with sessions()() as db:
        query = db.query(CorrectionProposal).filter(
            CorrectionProposal.project_id == scope.project_id
        )
        if source is not None:
            query = query.filter(CorrectionProposal.source == source)
        views = [
            ProposalView.model_validate(row)
            for row in query.order_by(CorrectionProposal.created_at.desc()).all()
        ]
        if destination_kind is not None:
            views = [v for v in views if v.destination_kind == destination_kind]
        return views


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
        is_new_doc = (row.target_kind or "document") == "document" and not row.original
        if is_new_doc and row.source_path:
            row.diff = unified_diff_text(
                row.source_path, "", row.patch, is_new_file=True
            )
        else:
            row.diff = _unified_diff(
                OVERRIDE_RELATIVE_PATH if row.structured_patch else row.target,
                row.original,
                row.patch,
            )
        row.reviewer_id = None
        row.reviewer_name = None


@dataclass(frozen=True)
class _ApplyingSnapshot:
    id: str
    repo_dir: str | None
    source_path: str | None
    original: str
    patch: str
    target_kind: str
    reviewer_id: str | None


def _recover_git_applying_row(row_snapshot: _ApplyingSnapshot) -> None:
    proposal_id = row_snapshot.id
    directory = row_snapshot.repo_dir
    source_path = row_snapshot.source_path
    old = row_snapshot.original
    patch = row_snapshot.patch
    target_kind = row_snapshot.target_kind

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
            is_human = row_snapshot.reviewer_id is not None
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
            _ApplyingSnapshot(
                id=r.id,
                repo_dir=r.source_directory,
                source_path=r.source_path,
                original=r.original,
                patch=r.patch,
                target_kind=r.target_kind or "document",
                reviewer_id=r.reviewer_id,
            )
            for r in query.all()
        ]
    for snap in snapshots:
        try:
            _recover_git_applying_row(snap)
        except Exception:
            logger.exception("Could not recover correction %s", snap.id)


def _load_structured_from_row(
    row: CorrectionProposal,
    patch_text: str,
    structured_override: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if structured_override is not None:
        return normalize_structured_patch(structured_override)
    if row.structured_patch:
        existing = json.loads(row.structured_patch)
        if patch_text == row.patch:
            return existing
        try:
            loaded = yaml.safe_load(patch_text)
        except yaml.YAMLError as exc:
            raise ValueError(
                f"Edited structured patch is not valid YAML/JSON: {exc}"
            ) from exc
        if isinstance(loaded, dict):
            return normalize_structured_patch(loaded)
        if row.target.startswith(("table:", "field:")) and _is_plain_legacy_comment(
            row.target, existing
        ):
            converted = legacy_target_to_patch(row.target, patch_text)
            if converted is not None:
                return converted
        raise ValueError(
            "Edited patch for a structured override must be valid YAML/JSON matching semantic_overrides schema"
        )
    return legacy_target_to_patch(row.target, patch_text)


def _is_plain_legacy_comment(target: str, structured: dict[str, Any] | None) -> bool:
    if not target.startswith(("table:", "field:")) or structured is None:
        return False
    if structured.get("relationships") or structured.get("metrics"):
        return False
    tables = structured.get("tables") or {}
    fields = structured.get("fields") or {}
    if target.startswith("table:"):
        if fields or len(tables) != 1:
            return False
        entry = next(iter(tables.values()))
        return (
            "comment" in entry
            and not entry.get("synonyms")
            and not entry.get("instructions")
        )
    if tables or len(fields) != 1:
        return False
    entry = next(iter(fields.values()))
    return (
        "comment" in entry
        and not entry.get("synonyms")
        and not entry.get("instructions")
    )


def _merged_gate_report_json(
    existing_json: str | None,
    gate_report,
    approval_eval: dict[str, Any] | None = None,
) -> str:
    """Serialize L1 gate_report while preserving L2 gate, provenance, and eval_candidate metadata."""
    payload = gate_report.to_dict()
    merged_candidate: dict[str, Any] = {}
    if existing_json:
        try:
            existing = json.loads(existing_json)
            if isinstance(existing, dict):
                if isinstance(existing.get("eval_candidate"), dict):
                    merged_candidate.update(existing["eval_candidate"])
                for preserved_key in (
                    "l1_gate",
                    "l2_gate",
                    "provenance",
                    "discovery_evidence",
                    "evidence",
                ):
                    if preserved_key in existing:
                        payload[preserved_key] = existing[preserved_key]
                existing_checks = existing.get("checks")
                if isinstance(existing_checks, list):
                    seen_names = {
                        c.get("name")
                        for c in payload.get("checks", [])
                        if isinstance(c, dict)
                    }
                    for chk in existing_checks:
                        if (
                            isinstance(chk, dict)
                            and chk.get("name")
                            and chk["name"] not in seen_names
                        ):
                            payload.setdefault("checks", []).append(chk)
                            seen_names.add(chk["name"])
        except ValueError:
            pass
    if approval_eval:
        for key, value in approval_eval.items():
            if value is not None:
                merged_candidate[key] = value
    if merged_candidate:
        payload["eval_candidate"] = merged_candidate
    return json.dumps(payload, ensure_ascii=False)


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
    approval_eval: dict[str, Any] | None = None,
) -> ProposalView:
    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = _pending(db, proposal_id, project_id)
        effective_patch = patch_override if patch_override is not None else row.patch
        try:
            effective_structured = _load_structured_from_row(
                row, effective_patch, structured_override
            )
            if patch_override is None and structured_override is not None:
                effective_patch = dump_overrides_yaml(effective_structured)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if (
            not auto_apply
            and row.target_kind == "document"
            and effective_patch == row.original
        ):
            raise HTTPException(422, "Correction does not change the target")
        row_target = row.target
        row_reason = row.reason
        row_project_id = row.project_id
        row_source = row.source or "manual"
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
    gate_report = _with_placeholder_gate(gate_report, row_target, effective_patch)
    if not gate_report.passed:
        raise HTTPException(422, str(GateValidationError(gate_report)))

    values = _settings(row_project_id)
    project = project_context.from_values(values, row_project_id)

    if row_kind != "document" and not project.source_directory_value:
        if (
            structured_override is not None
            or row_kind == "ossie"
            or not _is_plain_legacy_comment(row_target, effective_structured)
        ):
            raise HTTPException(
                422, "Structured overrides require a configured source_directory"
            )
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
            row.gate_report = _merged_gate_report_json(
                row.gate_report, gate_report, approval_eval
            )
            row.status = "auto_applied" if auto_apply else "approved"
            row.reviewer_id = reviewer_id
            row.reviewer_name = reviewer_name
            if not auto_apply:
                row.reviewed_at = datetime.now(UTC)
            db.flush()
            result = ProposalView.model_validate(row)
        return result

    if row_kind != "document" and effective_structured is not None:
        migrate_legacy_metadata_overrides(row_project_id)

    doc_source_file: str | None = None
    if row_kind == "document":
        try:
            nodes = knowledge.execute(
                knowledge.read_graph(values),
                "MATCH (n:SDADocument {generation:$generation, ref:$ref}) RETURN properties(n) AS node",
                {"generation": project.generation, "ref": row_target},
            )
        except (KeyError, knowledge.StorageUnavailable, ValueError, RuntimeError):
            nodes = []
        if (
            len(nodes) == 1
            and nodes[0]["node"].get("source_file")
            and not nodes[0]["node"].get("generated")
        ):
            doc_source_file = nodes[0]["node"]["source_file"]
        elif row_target.startswith("document:"):
            try:
                candidate_rel = _validate_doc_relpath(row_target, row_project_id)
            except ValueError as exc:
                raise HTTPException(409, "Document no longer exists") from exc
            if row_source == "evolution":
                doc_source_file = candidate_rel
            else:
                with sessions()() as db:
                    snap = db.get(CorrectionProposal, proposal_id)
                    orig_empty = snap is not None and snap.original == ""
                if orig_empty or (
                    project.source_directory_value
                    and (Path(project.source_directory_value) / candidate_rel).is_file()
                ):
                    doc_source_file = candidate_rel
                else:
                    raise HTTPException(409, "Document no longer exists")
        else:
            raise HTTPException(409, "Document no longer exists")
    elif (
        row_kind == "ossie"
        and effective_structured is None
        and row_target.startswith("ossie:")
    ):
        doc_source_file = str(relative(row_target[len("ossie:") :]))

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
            doc_source_file if doc_source_file is not None else OVERRIDE_RELATIVE_PATH
        )
        row.patch = effective_patch
        if effective_structured is not None:
            row.structured_patch = json.dumps(effective_structured, ensure_ascii=False)
        row.gate_report = _merged_gate_report_json(
            row.gate_report, gate_report, approval_eval
        )
        is_new_doc = row_kind == "document" and row.original == ""
        row.diff = unified_diff_text(
            row.source_path, row.original, effective_patch, is_new_file=is_new_doc
        )
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

            if row_kind == "document" or (
                row_kind == "ossie" and effective_structured is None
            ):
                before_existed = path.is_file()
                before_content = (
                    path.read_text(encoding="utf-8") if before_existed else ""
                )
                if before_content != expected_original:
                    raise ValueError("Document changed since proposal")
                new_content = effective_patch
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

    approval_eval = (
        {
            "question": body.question,
            "reference_sql": body.reference_sql,
            "caliber": body.caliber,
            "expected_tables": body.expected_tables,
            "expected_fields": body.expected_fields,
            "dialect": body.dialect,
            "category": body.category,
            "turn_id": body.turn_id,
        }
        if body
        else None
    )
    return _apply_proposal(
        proposal_id,
        reviewer_id=requester.id,
        reviewer_name=reviewer_name,
        author_name=reviewer_name,
        patch_override=body.patch if body else None,
        structured_override=body.structured_patch if body else None,
        auto_apply=False,
        project_id=project_id,
        approval_eval=approval_eval
        if approval_eval and any(v is not None for v in approval_eval.values())
        else None,
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


def _restore_reverting_row(proposal_id: str, prior_status: str) -> None:
    with sessions().begin() as db:
        row = db.get(CorrectionProposal, proposal_id)
        if row is not None and row.status == "reverting":
            row.status = prior_status


def _finalize_revert(
    proposal_id: str, revert_sha: str, actor_name: str
) -> ProposalView:
    source_dir_val: str | None = None
    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(CorrectionProposal, proposal_id)
        if row is None or row.status != "reverting":
            raise ValueError("Proposal is not reverting")
        source_dir_val = row.source_directory
        row.status = "reverted"
        row.revert_commit_sha = revert_sha
        row.reverted_at = datetime.now(UTC)
        if (
            active_build(db, row.project_id, correction_proposal_id=f"revert:{row.id}")
            is None
        ):
            add_queued_build(
                db,
                "incremental",
                actor_name,
                correction_proposal_id=f"revert:{row.id}",
                project_id=row.project_id,
            )
        db.flush()
        result = ProposalView.model_validate(row)
    if source_dir_val:
        _remove_revert_journal(Path(source_dir_val), proposal_id)
    project_context.wake_build_queue()
    return result


def _find_revert_commit(root: Path, commit_sha: str) -> str | None:
    needle = f"This reverts commit {commit_sha}."
    try:
        commits = (
            git(
                root,
                "log",
                "--all",
                "--format=%H",
                "--fixed-strings",
                "--grep",
                needle,
            )
            .decode()
            .splitlines()
        )
    except ValueError:
        return None
    matches = [
        sha
        for sha in commits
        if needle in git(root, "show", "-s", "--format=%B", sha).decode()
    ]
    if len(matches) > 1:
        raise ValueError("Multiple revert commits for one correction")
    return matches[0] if matches else None


@dataclass(frozen=True)
class _RevertingSnapshot:
    id: str
    repo_dir: str | None
    commit_sha: str | None
    reviewer_id: str | None
    reviewer_name: str | None


def _recover_git_reverting_row(snap: _RevertingSnapshot) -> None:
    if not snap.repo_dir:
        prior = "approved" if snap.reviewer_id is not None else "auto_applied"
        _restore_reverting_row(snap.id, prior)
        return
    with repository(snap.repo_dir) as root:
        journal = _read_revert_journal(root, snap.id)
        prior_status = (
            str(journal["prior_status"])
            if journal and journal.get("prior_status") in {"approved", "auto_applied"}
            else ("approved" if snap.reviewer_id is not None else "auto_applied")
        )
        commit_sha = (
            str(journal["commit_sha"])
            if journal and journal.get("commit_sha")
            else (snap.commit_sha or "")
        )
        actor_name = (
            str(journal["actor_name"])
            if journal and journal.get("actor_name")
            else (snap.reviewer_name or "system")
        )
        matched_sha = _find_revert_commit(root, commit_sha) if commit_sha else None
        if matched_sha:
            _finalize_revert(snap.id, matched_sha, actor_name)
            return
        _abort_revert_if_active(root)
        _remove_revert_journal(root, snap.id)
        _restore_reverting_row(snap.id, prior_status)


def recover_reverting(only_id: str | None = None) -> None:
    """Recover correction reverts interrupted around git revert or DB finalization."""
    with sessions()() as db:
        query = db.query(CorrectionProposal).filter_by(status="reverting")
        if only_id is not None:
            query = query.filter_by(id=only_id)
        snapshots = [
            _RevertingSnapshot(
                id=r.id,
                repo_dir=r.source_directory,
                commit_sha=r.commit_sha,
                reviewer_id=r.reviewer_id,
                reviewer_name=r.reviewer_name,
            )
            for r in query.all()
        ]
    for snap in snapshots:
        try:
            _recover_git_reverting_row(snap)
        except Exception:
            logger.exception("Could not recover revert for correction %s", snap.id)


@router.post("/api/v1/corrections/{proposal_id}/revert", response_model=ProposalView)
def revert(
    proposal_id: str,
    scope: Annotated[project_context.ProjectContext, Depends(admin_project_scope)],
    requester: Annotated[object, Depends(rest_requester)],
):
    project_id = scope.project_id
    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
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
        try:
            _validate_git_author(actor_name)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        prior_status = row.status
        commit_sha = row.commit_sha
        source_dir = row.source_directory
        row.status = "reverting"

    attempted_revert = False
    try:
        with repository(source_dir) as root:
            if git(root, "diff", "--cached", "--name-only"):
                raise HTTPException(409, "Source repository has staged changes")
            pre_revert_head = git(root, "rev-parse", "HEAD").decode().strip()
            _write_revert_journal(
                root,
                proposal_id,
                {
                    "proposal_id": proposal_id,
                    "prior_status": prior_status,
                    "commit_sha": commit_sha,
                    "pre_revert_head": pre_revert_head,
                    "actor_name": actor_name,
                },
            )
            attempted_revert = True
            try:
                git(root, "revert", "--no-edit", commit_sha, author=actor_name)
            except ValueError as exc:
                _abort_revert_if_active(root)
                _remove_revert_journal(root, proposal_id)
                _restore_reverting_row(proposal_id, prior_status)
                raise HTTPException(409, f"Git revert failed: {exc}") from exc
            revert_sha = git(root, "rev-parse", "HEAD").decode().strip()
            return _finalize_revert(proposal_id, revert_sha, actor_name)
    except HTTPException:
        if not attempted_revert:
            _restore_reverting_row(proposal_id, prior_status)
        raise
    except (OSError, ValueError) as exc:
        if attempted_revert:
            recover_reverting(proposal_id)
        else:
            _restore_reverting_row(proposal_id, prior_status)
        raise HTTPException(409, str(exc)) from exc
