"""Schedule incremental builds per project on the shared persisted queue."""

import logging
import threading
import time

from . import project_context

DEFAULT_PROJECT_ID = "default"


class Schedule:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.interval = 0
        self.deadline = None

    def configure(self, interval):
        self.interval = interval
        self.deadline = self.clock() + interval if interval else None

    def due(self):
        if self.deadline is None or self.clock() < self.deadline:
            return False
        self.deadline = self.clock() + self.interval
        return True

    def remaining(self):
        return None if self.deadline is None else max(0, self.deadline - self.clock())


class Scheduler:
    def __init__(self, action=None, clock=time.monotonic):
        self._action = action or self._scheduled_build
        self._clock = clock
        self._condition = threading.Condition()
        self._schedules = {}
        self._thread = None
        self._stopping = False

    @staticmethod
    def _scheduled_build(project_id):
        if project_context.project_is_frozen(project_id):
            return
        project_context.enqueue_build(
            "incremental", "scheduler", project_id=project_id, dedupe=True
        )

    def start(self, interval, project_id=DEFAULT_PROJECT_ID):
        with self._condition:
            self._configure(project_id, interval)
            if self._thread and self._thread.is_alive():
                return
            self._stopping = False
            self._thread = threading.Thread(
                target=self._run, name="dataagent-scheduler", daemon=True
            )
            self._thread.start()

    def configure(self, interval, project_id=DEFAULT_PROJECT_ID):
        with self._condition:
            self._configure(project_id, interval)
            self._condition.notify_all()

    def remove(self, project_id):
        with self._condition:
            self._schedules.pop(project_id, None)
            self._condition.notify_all()

    def _configure(self, project_id, interval):
        schedule = self._schedules.get(project_id)
        if schedule is None:
            schedule = Schedule(self._clock)
            self._schedules[project_id] = schedule
        schedule.configure(interval)

    def due_projects(self):
        with self._condition:
            return [
                project_id
                for project_id, schedule in sorted(self._schedules.items())
                if schedule.due()
            ]

    def stop(self):
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)

    def _run(self):
        while True:
            with self._condition:
                if self._stopping:
                    return
                due = [
                    project_id
                    for project_id, schedule in self._schedules.items()
                    if schedule.due()
                ]
                if not due:
                    remaining = [
                        schedule.remaining()
                        for schedule in self._schedules.values()
                        if schedule.remaining() is not None
                    ]
                    self._condition.wait(min(remaining) if remaining else None)
                    continue
            for project_id in due:
                try:
                    self._action(project_id)
                except Exception:
                    logging.getLogger(__name__).exception(
                        "Could not enqueue scheduled build for project %s", project_id
                    )


scheduler = Scheduler()


def _scheduled_pull(project_id=None):
    from .pull_service import run_pull

    run_pull()


pull_scheduler = Scheduler(_scheduled_pull)
