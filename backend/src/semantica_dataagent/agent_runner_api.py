"""Structured Agent Runner registry and live readiness status (#328)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from .agent_runner import get_runner
from .api_time import UtcDatetime
from .audit_log import record_audit_event
from .auth import Requester, admin, current_requester
from .db import AgentRunnerRecord, sessions

router = APIRouter()

RunnerStatusOverride = Literal["pending_build", "building", "image_deployed", "failed"]
RunnerLiveStatus = Literal[
    "available",
    "pending_build",
    "building",
    "image_deployed",
    "adapter_missing",
    "failed",
]

_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
_NPM_PACKAGE_PATTERN = r"^(?:@[a-z0-9-~][a-z0-9-._~]*/)?[a-z0-9-~][a-z0-9-._~]*$"
_PINNED_VERSION_PATTERN = r"^\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?$"
_EXECUTABLE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


class AgentRunnerCreate(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=_ID_PATTERN)
    display_name: str = Field(min_length=1, max_length=200, pattern=r"\S")
    npm_package: str = Field(min_length=1, max_length=200, pattern=_NPM_PACKAGE_PATTERN)
    package_version: str = Field(
        min_length=1, max_length=64, pattern=_PINNED_VERSION_PATTERN
    )
    executable: str = Field(min_length=1, max_length=64, pattern=_EXECUTABLE_PATTERN)
    adapter_id: str = Field(min_length=1, max_length=64, pattern=_ID_PATTERN)
    status_override: RunnerStatusOverride | None = None


class AgentRunnerUpdate(BaseModel):
    display_name: str | None = Field(
        default=None, min_length=1, max_length=200, pattern=r"\S"
    )
    npm_package: str | None = Field(
        default=None, min_length=1, max_length=200, pattern=_NPM_PACKAGE_PATTERN
    )
    package_version: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=_PINNED_VERSION_PATTERN
    )
    executable: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=_EXECUTABLE_PATTERN
    )
    adapter_id: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=_ID_PATTERN
    )
    status_override: RunnerStatusOverride | None = None


class AgentRunnerView(BaseModel):
    id: str
    display_name: str
    npm_package: str
    package_version: str
    executable: str
    adapter_id: str
    status_override: str | None
    builtin: bool
    cli_deployed: bool
    adapter_registered: bool
    available: bool
    status: RunnerLiveStatus
    created_at: UtcDatetime
    updated_at: UtcDatetime


def runner_status_info(
    row: AgentRunnerRecord,
) -> tuple[bool, bool, bool, RunnerLiveStatus]:
    """Compute (cli_deployed, adapter_registered, available, status) without shell exec."""
    runner = get_runner(row.adapter_id)
    adapter_registered = runner is not None
    override = row.status_override

    if override == "failed":
        return False, adapter_registered, False, "failed"
    if override == "building":
        return False, adapter_registered, False, "building"
    if override == "pending_build":
        return False, adapter_registered, False, "pending_build"
    if override == "image_deployed":
        if not adapter_registered:
            return True, False, False, "adapter_missing"
        if runner.available():
            return True, True, True, "available"
        return True, True, False, "image_deployed"

    if row.builtin:
        if not adapter_registered:
            return False, False, False, "adapter_missing"
        cli_ready = bool(runner.available())
        if cli_ready:
            return True, True, True, "available"
        return False, True, False, "pending_build"

    return False, adapter_registered, False, "pending_build"


def is_runner_available(db, runner_id: str) -> bool:
    """Return True if the runner has both deployed CLI and registered adapter."""
    row = db.get(AgentRunnerRecord, runner_id)
    if row is not None:
        _, _, available, _ = runner_status_info(row)
        return available
    runner = get_runner(runner_id)
    return bool(runner is not None and runner.available())


def runner_view(row: AgentRunnerRecord) -> AgentRunnerView:
    cli_deployed, adapter_registered, available, status = runner_status_info(row)
    return AgentRunnerView(
        id=row.id,
        display_name=row.display_name,
        npm_package=row.npm_package,
        package_version=row.package_version,
        executable=row.executable,
        adapter_id=row.adapter_id,
        status_override=row.status_override,
        builtin=bool(row.builtin),
        cli_deployed=cli_deployed,
        adapter_registered=adapter_registered,
        available=available,
        status=status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/api/v1/agent-runners", response_model=list[AgentRunnerView])
def list_agent_runners(_: Annotated[str, Depends(admin)]):
    with sessions()() as db:
        rows = (
            db.query(AgentRunnerRecord)
            .order_by(AgentRunnerRecord.builtin.desc(), AgentRunnerRecord.id)
            .all()
        )
        return [runner_view(row) for row in rows]


@router.post("/api/v1/agent-runners", response_model=AgentRunnerView, status_code=201)
def create_agent_runner(
    body: AgentRunnerCreate,
    _: Annotated[str, Depends(admin)],
    actor: Annotated[Requester, Depends(current_requester)],
):
    now = datetime.now(UTC)
    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        if db.get(AgentRunnerRecord, body.id) is not None:
            raise HTTPException(409, f"Runner already exists: {body.id}")
        row = AgentRunnerRecord(
            id=body.id,
            display_name=body.display_name.strip(),
            npm_package=body.npm_package.strip(),
            package_version=body.package_version.strip(),
            executable=body.executable.strip(),
            adapter_id=body.adapter_id.strip(),
            status_override=body.status_override,
            builtin=False,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        db.flush()
        result = runner_view(row)
    record_audit_event(
        actor=actor,
        action="agent_runner.register",
        target_type="agent_runner",
        target_id=result.id,
        details={
            "adapter_id": result.adapter_id,
            "package_version": result.package_version,
        },
    )
    return result


@router.put("/api/v1/agent-runners/{runner_id}", response_model=AgentRunnerView)
def update_agent_runner(
    runner_id: str,
    body: AgentRunnerUpdate,
    _: Annotated[str, Depends(admin)],
    actor: Annotated[Requester, Depends(current_requester)],
):
    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(AgentRunnerRecord, runner_id)
        if row is None:
            raise HTTPException(404, "Agent runner not found")
        if body.display_name is not None:
            row.display_name = body.display_name.strip()
        if body.npm_package is not None:
            row.npm_package = body.npm_package.strip()
        if body.package_version is not None:
            row.package_version = body.package_version.strip()
        if body.executable is not None:
            row.executable = body.executable.strip()
        if body.adapter_id is not None:
            row.adapter_id = body.adapter_id.strip()
        if "status_override" in body.model_fields_set:
            row.status_override = body.status_override
        row.updated_at = datetime.now(UTC)
        db.flush()
        result = runner_view(row)
    record_audit_event(
        actor=actor,
        action="agent_runner.update",
        target_type="agent_runner",
        target_id=result.id,
        details={"fields": sorted(body.model_fields_set)},
    )
    return result
