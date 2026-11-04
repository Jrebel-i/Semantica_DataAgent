"""Run persisted builds one at a time in submission order."""

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
            build_id = self._claim()
            if build_id is None:
                with self._condition:
                    while not self._stopping and not self._wake:
                        self._condition.wait(timeout=1)
                    self._wake = False
                    if self._stopping:
                        return
                continue
            from .knowledge import run_build

            try:
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
