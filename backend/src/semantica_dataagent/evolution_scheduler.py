"""Fork-safe per-project autonomous evolution trigger scheduler and async runner."""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime

from . import evolution_runner, project_context
from .db import Project, sessions

LOGGER = logging.getLogger(__name__)
DEFAULT_POLL_INTERVAL_SECONDS = 60.0


def should_trigger_project(project_id: str, *, now: datetime | None = None) -> bool:
    """Evaluate whether a project satisfies auto-evolution trigger thresholds."""
    return False


class EvolutionScheduler:
    """Fork-safe background scheduler evaluating per-project evolution triggers."""

    def __init__(self, poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS):
        self._poll_interval = max(1.0, float(poll_interval))
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._workers: set[threading.Thread] = set()
        self._stopping = False

    def fork_child(self) -> None:
        """Reset inherited locks and parent thread handles after os.fork()."""
        self._condition = threading.Condition()
        self._thread = None
        self._workers = set()
        self._stopping = False

    def start(self) -> None:
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            self._thread = threading.Thread(
                target=self._run, name="dataagent-evolution-scheduler", daemon=True
            )
            self._thread.start()

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def stop(self, timeout: float = 5.0) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
            thread = self._thread
            workers = list(self._workers)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(timeout=timeout)

    def dispatch_run(self, run_id: str) -> None:
        """Spawn a tracked worker thread to execute an already-reserved run."""
        with self._condition:
            if self._stopping:
                return
            worker = threading.Thread(
                target=self._run_worker,
                args=(run_id,),
                name=f"dataagent-evolution-{run_id[:8]}",
                daemon=True,
            )
            self._workers.add(worker)
            worker.start()

    def _run_worker(self, run_id: str) -> None:
        current = threading.current_thread()
        try:
            evolution_runner.execute_evolution_run(run_id)
        except Exception:
            LOGGER.exception("Evolution worker failed for run %s", run_id)
        finally:
            with self._condition:
                self._workers.discard(current)
                self._condition.notify_all()

    def tick_once(self, *, now: datetime | None = None) -> list[str]:
        """Evaluate every active project once and launch due evolution runs."""
        with sessions()() as db:
            project_ids = [
                row.id
                for row in db.query(Project.id)
                .filter(Project.status == project_context.ACTIVE_STATUS)
                .order_by(Project.id.asc())
                .all()
            ]
        started: list[str] = []
        for pid in project_ids:
            try:
                if not should_trigger_project(pid, now=now):
                    continue
                run = evolution_runner.create_evolution_run(
                    pid, trigger_type="scheduler", created_by="scheduler"
                )
                self.dispatch_run(run["id"])
                started.append(run["id"])
            except (
                evolution_runner.EvolutionConflictError,
                evolution_runner.EvolutionFrozenError,
            ):
                continue
            except Exception:
                LOGGER.exception(
                    "Auto-evolution trigger check failed for project %s", pid
                )
        return started

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._stopping:
                    return
                self._condition.wait(timeout=self._poll_interval)
                if self._stopping:
                    return
            self.tick_once()


evolution_scheduler = EvolutionScheduler()


def _reset_after_fork() -> None:
    evolution_scheduler.fork_child()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)
