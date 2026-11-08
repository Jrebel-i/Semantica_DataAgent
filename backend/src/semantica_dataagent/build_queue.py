"""Run persisted builds one at a time in submission order."""

import json
import threading
from datetime import UTC, datetime

from .db import Build, sessions


class BuildQueue:
    def __init__(self):
        self._condition = threading.Condition()
        self._thread = None
        self._stopping = False
        self._wake = False

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
            self._condition.notify()

    def stop(self, timeout=5):
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def _run(self):
        while True:
            with self._condition:
                if self._stopping:
                    return
            try:
                build_id = self._claim()
            except Exception:  # noqa: BLE001 - a transient claim failure must not stop the worker
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
            try:
                from .knowledge import run_build

                run_build(build_id)
            except Exception as exc:  # noqa: BLE001 - keep the worker alive
                self._fail(build_id, str(exc))

    def _claim(self):
        with sessions().begin() as db:
            row = (
                db.query(Build)
                .filter(Build.status == "queued")
                .order_by(Build.id)
                .first()
            )
            if row is None:
                return None
            row.status = "running"
            row.started_at = datetime.now(UTC).isoformat()
            row.finished_at = None
            row.error = None
            return row.id

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
    build_type, triggered_by, queue=None, *, project_id="default", notify=True
):
    with sessions().begin() as db:
        build_id = add_queued_build(db, build_type, triggered_by, project_id=project_id)
    if notify:
        (queue or build_queue).enqueue()
    return build_id
