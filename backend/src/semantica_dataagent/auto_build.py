"""Persist debounced auto-build requests and serve project source status."""

from __future__ import annotations

import logging
import math
import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, text

from . import project_context
from .build_queue import active_build, has_successful_full_build
from .db import Build, ProjectPendingBuild, ProjectSetting, Setting, sessions

LOGGER = logging.getLogger(__name__)

DEBOUNCE_KEY = "auto_build_debounce_seconds"
AUTO_BUILD_KEY = "auto_build"
DEFAULT_DEBOUNCE_SECONDS = 30.0
MAX_ERROR_CHARS = 300


def debounce_seconds(db=None) -> float:
    """Return the global debounce window, falling back to 30 seconds."""
    if db is None:
        with sessions()() as own:
            return debounce_seconds(own)
    row = db.get(Setting, DEBOUNCE_KEY)
    try:
        value = float(row.value) if row is not None and row.value else 0.0
    except (TypeError, ValueError):
        return DEFAULT_DEBOUNCE_SECONDS
    if not math.isfinite(value):
        return DEFAULT_DEBOUNCE_SECONDS
    return max(0.0, value)


def auto_build_enabled(project_id: str, db=None) -> bool:
    """Project-level switch; a missing row means enabled."""
    if db is None:
        with sessions()() as own:
            return auto_build_enabled(project_id, own)
    row = db.get(ProjectSetting, (project_id, AUTO_BUILD_KEY))
    if row is None:
        return True
    return row.value.strip().lower() not in {"false", "0", "off", "no"}


def _due_seconds(due_at: str | None, now: datetime) -> int | None:
    if not due_at:
        return None
    try:
        due = datetime.fromisoformat(due_at)
    except (TypeError, ValueError):
        return None
    if due.tzinfo is None:
        due = due.replace(tzinfo=UTC)
    return max(0, math.ceil((due - now).total_seconds()))


def request_upload_build(project_id: str, requested_by: str) -> bool:
    """Register or extend this project's pending build window.

    The request lives in the database, so a restart inside the window still
    builds once the deadline passes.
    """
    if project_context.project_is_frozen(project_id):
        return False
    now = datetime.now(UTC)
    with sessions().begin() as db:
        if not auto_build_enabled(project_id, db):
            return False
        due = now + timedelta(seconds=debounce_seconds(db))
        row = db.get(ProjectPendingBuild, project_id)
        if row is None:
            db.add(
                ProjectPendingBuild(
                    project_id=project_id,
                    due_at=due.isoformat(),
                    requested_by=requested_by,
                    requested_at=now.isoformat(),
                )
            )
        else:
            row.due_at = due.isoformat()
            row.requested_by = requested_by
            row.requested_at = now.isoformat()
    pending_build_scheduler.wake()
    return True


def discard_pending_builds(project_id: str, db=None) -> None:
    """Drop this project's not-yet-due request, optionally in a caller's transaction."""
    if db is None:
        with sessions().begin() as own:
            discard_pending_builds(project_id, own)
        return
    db.query(ProjectPendingBuild).filter(
        ProjectPendingBuild.project_id == project_id
    ).delete(synchronize_session=False)


def process_due_pending_builds(now: datetime | None = None) -> list[str]:
    """Delete due requests and queue one build in one transaction.

    A project without a successful full build gets a full build; an incremental
    build would be rejected before it starts.  One already queued build still
    merges the request, while a running build gets one follow-up.
    """
    now = now or datetime.now(UTC)
    stamp = now.isoformat()
    processed: list[str] = []
    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        rows = (
            db.query(ProjectPendingBuild)
            .filter(ProjectPendingBuild.due_at <= stamp)
            .order_by(ProjectPendingBuild.due_at, ProjectPendingBuild.project_id)
            .all()
        )
        for row in rows:
            project_id = row.project_id
            db.delete(row)
            if project_context.project_is_frozen(project_id):
                continue
            if not auto_build_enabled(project_id, db):
                continue
            queued = active_build(db, project_id, status="queued")
            if queued is None:
                build_type = (
                    "incremental"
                    if has_successful_full_build(db, project_id)
                    else "full"
                )
                project_context.add_queued_build(
                    db, build_type, "upload", project_id=project_id
                )
                processed.append(project_id)
    if processed:
        project_context.wake_build_queue()
    return processed


def _source_head(project_id: str) -> str | None:
    from . import project_context
    from .source_directory import git

    value = project_context.source_directory_value(project_id)
    if not value:
        return None
    try:
        return git(value, "rev-parse", "--verify", "HEAD").decode().strip()
    except (OSError, RuntimeError, ValueError):
        return None


def source_status(project_id: str) -> dict[str, object]:
    now = datetime.now(UTC)
    with sessions()() as db:
        pending = db.get(ProjectPendingBuild, project_id)
        builds = db.query(Build).filter(
            func.coalesce(Build.project_id, "default") == project_id
        )
        latest = builds.order_by(Build.id.desc()).first()
        latest_success = (
            builds.filter(Build.status == "succeeded").order_by(Build.id.desc()).first()
        )
        running = builds.filter(Build.status == "running").first() is not None
        queued_incremental = (
            builds.filter(
                Build.status == "queued",
                Build.build_type == "incremental",
            ).first()
            is not None
        )
        queued = builds.filter(Build.status == "queued").first() is not None
        enabled = auto_build_enabled(project_id, db)
        if not enabled:
            pending = None
        remaining = _due_seconds(pending.due_at if pending else None, now)
        source_commit = latest_success.source_commit if latest_success else None
        error = None
        if latest is not None and latest.status == "failed" and latest.error:
            error = latest.error[:MAX_ERROR_CHARS]

    head = _source_head(project_id)
    if running:
        state = "building"
    elif queued_incremental or pending is not None:
        state = "pending"
    elif queued:
        state = "building"
    elif latest is None:
        state = "unknown"
    elif latest.status == "failed":
        state = "failed"
    elif source_commit is not None and head is not None and source_commit == head:
        state = "effective"
    elif latest.status == "succeeded":
        state = "pending"
    else:
        state = "unknown"
    return {
        "state": state,
        "due_in_seconds": remaining if pending is not None else None,
        "last_build_id": latest.id if latest is not None else None,
        "source_commit": source_commit,
        "auto_build": enabled,
        "error": error if state == "failed" else None,
    }


class PendingBuildScheduler:
    """Small loop that turns due persisted requests into queued builds."""

    def __init__(self, interval: float = 1.0, clock=lambda: datetime.now(UTC)):
        self.interval = interval
        self.clock = clock
        self._condition = threading.Condition()
        self._thread = None
        self._stopping = False

    def start(self):
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            self._thread = threading.Thread(
                target=self._run, name="dataagent-pending-builds", daemon=True
            )
            self._thread.start()

    def wake(self):
        with self._condition:
            self._condition.notify_all()

    def stop(self):
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

    def _run(self):
        while True:
            with self._condition:
                if self._stopping:
                    return
            try:
                process_due_pending_builds(self.clock())
            except Exception:
                LOGGER.exception("Could not process pending auto-build requests")
            with self._condition:
                if self._stopping:
                    return
                self._condition.wait(timeout=self.interval)


pending_build_scheduler = PendingBuildScheduler()
