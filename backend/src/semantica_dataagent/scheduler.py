"""Schedule incremental builds on the same persisted queue as manual builds."""

import threading
import time

from . import project_context


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
    def __init__(self, action=None):
        self._action = action or (
            lambda: project_context.enqueue_build("incremental", "scheduler")
        )
        self._condition = threading.Condition()
        self._schedule = Schedule()
        self._thread = None
        self._stopping = False

    def start(self, interval):
        with self._condition:
            if self._thread and self._thread.is_alive():
                return
            self._stopping = False
            self._schedule.configure(interval)
            self._thread = threading.Thread(
                target=self._run, name="dataagent-scheduler", daemon=True
            )
            self._thread.start()

    def configure(self, interval):
        with self._condition:
            self._schedule.configure(interval)
            self._condition.notify_all()

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
                if not self._schedule.due():
                    self._condition.wait(self._schedule.remaining())
                    continue
            try:
                self._action()
            except Exception:
                import logging

                logging.getLogger(__name__).exception(
                    "Could not enqueue scheduled build"
                )


scheduler = Scheduler()


def _scheduled_pull():
    from .pull_service import run_pull

    run_pull()


pull_scheduler = Scheduler(_scheduled_pull)
