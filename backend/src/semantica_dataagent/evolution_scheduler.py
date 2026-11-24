"""Fork-safe wall-clock / 5-field Cron scheduler for per-project knowledge evolution."""

from __future__ import annotations

import logging
import os
import threading
from datetime import UTC, datetime

from . import evolution_runner, project_context, scheduler
from .api_time import as_utc
from .db import EvolutionRun, Project, ProjectEvolutionConfig, sessions

LOGGER = logging.getLogger(__name__)
DEFAULT_POLL_INTERVAL_SECONDS = 15.0


def is_project_due(config: ProjectEvolutionConfig, now_utc: datetime) -> bool:
    """Return True iff an enabled project's cron slot lies in (anchor, now_utc]."""
    return scheduler.is_instance_due(config, now_utc)


def next_run_at_for_config(
    config: ProjectEvolutionConfig | None, *, now: datetime | None = None
) -> datetime | None:
    """Compute the next scheduled UTC run timestamp for a project evolution config."""
    if config is None or not config.enabled:
        return None
    cron_expr = (config.schedule_cron or "").strip()
    if not cron_expr:
        return None
    now_utc = as_utc(now if now is not None else datetime.now(UTC))
    tz_name = (config.timezone or "").strip() or "Asia/Shanghai"
    try:
        if is_project_due(config, now_utc):
            return scheduler.latest_scheduled_slot(cron_expr, tz_name, now_utc)
        return scheduler.next_scheduled_slot(cron_expr, tz_name, now_utc)
    except ValueError:
        return None


def should_trigger_project(project_id: str, *, now: datetime | None = None) -> bool:
    """Evaluate whether a project is active and due for scheduled evolution."""
    now_utc = as_utc(now if now is not None else datetime.now(UTC))
    with sessions()() as db:
        proj = db.get(Project, project_id)
        if proj is None or proj.status != project_context.ACTIVE_STATUS:
            return False
        cfg = db.get(ProjectEvolutionConfig, project_id)
        if cfg is None:
            return False
        try:
            return is_project_due(cfg, now_utc)
        except ValueError:
            return False


class EvolutionScheduler:
    """Fork-safe background wall-clock/Cron scheduler for ProjectEvolutionConfig."""

    def __init__(
        self,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        clock=lambda: datetime.now(UTC),
    ):
        self._poll_interval = max(0.05, float(poll_interval))
        self._clock = clock
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

    def tick_once(
        self, *, now: datetime | None = None, dispatch: bool = True
    ) -> list[str]:
        """Scan enabled projects on wall-clock/Cron schedule and launch due runs."""
        now_utc = as_utc(now if now is not None else self._clock())
        with sessions()() as db:
            candidates = (
                db.query(ProjectEvolutionConfig, Project.status)
                .join(Project, Project.id == ProjectEvolutionConfig.project_id)
                .filter(
                    ProjectEvolutionConfig.enabled.is_(True),
                    ProjectEvolutionConfig.schedule_cron.is_not(None),
                    ProjectEvolutionConfig.schedule_cron != "",
                )
                .order_by(ProjectEvolutionConfig.project_id.asc())
                .all()
            )

        started: list[str] = []
        for cfg, project_status in candidates:
            pid = cfg.project_id
            if (
                project_status != project_context.ACTIVE_STATUS
                or project_context.project_is_frozen(pid)
            ):
                LOGGER.debug("Skipping scheduled evolution for frozen project %s", pid)
                continue
            try:
                if not is_project_due(cfg, now_utc):
                    continue
            except ValueError:
                LOGGER.warning("Invalid evolution schedule on project %s", pid)
                continue

            claimed = False
            with sessions().begin() as db:
                fresh = db.get(ProjectEvolutionConfig, pid)
                active = (
                    db.query(EvolutionRun.id)
                    .filter(
                        EvolutionRun.project_id == pid,
                        EvolutionRun.status == "running",
                    )
                    .first()
                )
                if (
                    fresh is not None
                    and active is None
                    and is_project_due(fresh, now_utc)
                ):
                    claimed = True
            if not claimed:
                continue

            try:
                run = evolution_runner.create_evolution_run(
                    pid,
                    trigger_type="scheduler",
                    created_by="scheduler",
                    claim_scheduled_at=now_utc,
                )
            except (
                evolution_runner.EvolutionConflictError,
                evolution_runner.EvolutionFrozenError,
            ):
                continue
            except Exception:
                LOGGER.exception(
                    "Auto-evolution run creation failed for project %s", pid
                )
                continue

            if dispatch:
                self.dispatch_run(run["id"])
            started.append(run["id"])
        return started

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._stopping:
                    return
            try:
                self.tick_once()
            except Exception:
                LOGGER.exception("EvolutionScheduler tick failed")
            with self._condition:
                if self._stopping:
                    return
                self._condition.wait(timeout=self._poll_interval)


evolution_scheduler = EvolutionScheduler()


def _reset_after_fork() -> None:
    evolution_scheduler.fork_child()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)
