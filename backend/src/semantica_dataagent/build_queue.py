"""Run persisted builds in parallel across projects, one at a time per project."""

import json
import logging
import threading
import time
from datetime import UTC, datetime

from sqlalchemy import func, or_, update

from .db import Build, sessions

TERMINAL = {"succeeded", "failed", "cancelled"}


def _project(row):
    return row.project_id or "default"


def active_build(
    db,
    project_id,
    *,
    build_type=None,
    triggered_by=None,
    correction_proposal_id=None,
):
    """Return an active build matching a trigger, used to merge duplicate triggers."""
    query = db.query(Build).filter(
        func.coalesce(Build.project_id, "default") == project_id,
        Build.status.in_(("queued", "running")),
    )
    if build_type is not None:
        query = query.filter(Build.build_type == build_type)
    if triggered_by is not None:
        query = query.filter(Build.triggered_by == triggered_by)
    if correction_proposal_id is not None:
        query = query.filter(Build.correction_proposal_id == correction_proposal_id)
    return query.order_by(Build.id).first()


class BuildQueue:
    def __init__(self, limit=None):
        self._condition = threading.Condition()
        self._thread = None
        self._workers = set()
        self._stopping = False
        self._wake = False
        self._limit = limit
        self._running = {}
        self._excluded = frozenset()

    def start(self):
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            self._wake = True
            self._thread = threading.Thread(
                target=self._run, name="dataagent-build-queue", daemon=True
            )
            self._thread.start()

    def enqueue(self):
        with self._condition:
            self._wake = True
            self._condition.notify_all()

    def stop(self, timeout=5):
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        deadline = time.monotonic() + timeout
        while True:
            with self._condition:
                remaining = [
                    worker
                    for worker in self._workers
                    if worker is not threading.current_thread()
                ]
            if not remaining:
                return
            wait = deadline - time.monotonic()
            if wait <= 0:
                return
            remaining[0].join(timeout=wait)

    def _concurrency_limit(self):
        if self._limit is not None:
            return max(1, int(self._limit))
        try:
            from . import project_context

            return max(
                1,
                int(
                    project_context.current_project().values.get(
                        "build_concurrency", "2"
                    )
                ),
            )
        except Exception:  # noqa: BLE001 - fall back to the documented default
            return 2

    def _run(self):
        while True:
            with self._condition:
                if self._stopping:
                    return
                if len(self._running) >= self._concurrency_limit():
                    self._condition.wait(timeout=1)
                    continue
                self._excluded = frozenset(self._running)
            try:
                build_id = self._claim()
            except Exception:  # noqa: BLE001 - a transient claim failure must not stop the dispatcher
                with self._condition:
                    if self._stopping:
                        return
                    self._condition.wait(timeout=1)
                continue
            if build_id is None:
                with self._condition:
                    while not self._stopping and not self._wake:
                        self._condition.wait(timeout=1)
                    self._wake = False
                    if self._stopping:
                        return
                continue
            with sessions()() as db:
                row = db.get(Build, build_id)
                project_id = _project(row) if row is not None else "default"
            with self._condition:
                self._running[project_id] = build_id
                worker = threading.Thread(
                    target=self._work,
                    args=(build_id, project_id),
                    name=f"dataagent-build-{project_id}",
                    daemon=True,
                )
                self._workers.add(worker)
                worker.start()
                if self._stopping:
                    return

    def _work(self, build_id, project_id):
        try:
            from .knowledge import run_build

            run_build(build_id)
        except Exception as exc:  # noqa: BLE001 - keep the worker alive
            self._fail(build_id, str(exc))
        finally:
            current = threading.current_thread()
            with self._condition:
                if self._running.get(project_id) == build_id:
                    self._running.pop(project_id, None)
                self._workers.discard(current)
                self._wake = True
                self._condition.notify_all()

    def _claim(self):
        with sessions().begin() as db:
            rows = (
                db.query(Build)
                .filter(Build.status == "queued")
                .order_by(Build.id)
                .all()
            )
            for row in rows:
                if _project(row) in self._excluded:
                    continue
                row.status = "running"
                row.started_at = datetime.now(UTC).isoformat()
                row.finished_at = None
                row.error = None
                db.flush()
                return row.id
            return None

    @staticmethod
    def _fail(build_id, error):
        with sessions().begin() as db:
            row = db.get(Build, build_id)
            if row is not None and row.status == "running":
                row.status = "failed"
                row.error = error
                row.finished_at = datetime.now(UTC).isoformat()


build_queue = BuildQueue()


def add_queued_build(
    db,
    build_type,
    triggered_by,
    correction_proposal_id=None,
    project_id="default",
):
    now = datetime.now(UTC).isoformat()
    row = Build(
        project_id=project_id,
        status="queued",
        started_at=now,
        counts="{}",
        build_type=build_type,
        triggered_by=triggered_by,
        correction_proposal_id=correction_proposal_id,
        logs=json.dumps(
            [
                {
                    "timestamp": now,
                    "stage": "queue",
                    "level": "info",
                    "message": f"{build_type} build queued",
                }
            ]
        ),
    )
    db.add(row)
    db.flush()
    return row.id


def enqueue_build(
    build_type,
    triggered_by,
    queue=None,
    *,
    project_id="default",
    notify=True,
    dedupe=False,
):
    with sessions().begin() as db:
        if dedupe:
            existing = active_build(
                db,
                project_id,
                build_type=build_type,
                triggered_by=triggered_by,
            )
            if existing is not None:
                return existing.id
        build_id = add_queued_build(db, build_type, triggered_by, project_id=project_id)
    if notify:
        (queue or build_queue).enqueue()
    return build_id


def recover_interrupted_builds():
    """Fail interrupted builds and cancel queued non-correction builds per project."""
    with sessions()() as db:
        project_ids = sorted(
            {row[0] or "default" for row in db.query(Build.project_id).distinct().all()}
        )
    for project_id in project_ids:
        try:
            with sessions().begin() as db:
                db.execute(
                    update(Build)
                    .where(
                        func.coalesce(Build.project_id, "default") == project_id,
                        Build.status == "running",
                    )
                    .values(
                        status="failed",
                        error="Build interrupted by application shutdown",
                        finished_at=datetime.now(UTC).isoformat(),
                    )
                )
                db.execute(
                    update(Build)
                    .where(
                        func.coalesce(Build.project_id, "default") == project_id,
                        Build.status == "queued",
                        Build.correction_proposal_id.is_(None),
                        or_(
                            Build.triggered_by.is_(None),
                            Build.triggered_by != "upload",
                        ),
                    )
                    .values(
                        status="cancelled",
                        error=(
                            "Build cancelled because the application restarted "
                            "before it started"
                        ),
                        finished_at=datetime.now(UTC).isoformat(),
                    )
                )
        except Exception:
            logging.getLogger(__name__).exception(
                "Could not recover interrupted builds for project %s", project_id
            )
