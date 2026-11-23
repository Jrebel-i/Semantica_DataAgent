"""Schedule incremental builds and puller instances on the shared persisted queue."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import project_context
from .api_time import as_utc

DEFAULT_PROJECT_ID = "default"
LOGGER = logging.getLogger(__name__)
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_CRON_PART_RE = re.compile(r"^(\*|\d+)(?:-(\d+))?(?:/(\d+))?$")
_CRON_FIELD_BOUNDS: tuple[tuple[str, int, int, bool], ...] = (
    ("minute", 0, 59, False),
    ("hour", 0, 23, False),
    ("day", 1, 31, False),
    ("month", 1, 12, False),
    ("weekday", 0, 7, True),
)
_MAX_SLOT_LOOKBACK_DAYS = 1466


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

    def fork_child(self):
        """Drop parent-thread state inherited by a forked child (#231)."""
        self._condition = threading.Condition()
        self._schedules = {}
        self._thread = None
        self._stopping = False

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
    project_id = project_id or DEFAULT_PROJECT_ID
    if project_context.project_is_frozen(project_id):
        LOGGER.debug("Skipping scheduled pull for frozen project %s", project_id)
        return
    from .pull_service import run_pull

    run_pull(project_id)


pull_scheduler = Scheduler(_scheduled_pull)


def _expand_cron_field(
    expr: str, label: str, low: int, high: int, *, is_weekday: bool = False
) -> tuple[int, ...]:
    if not expr:
        raise ValueError(f"Cron {label} 字段不能为空")
    values: set[int] = set()
    for token in expr.split(","):
        match = _CRON_PART_RE.fullmatch(token)
        if match is None:
            raise ValueError(f"Cron {label} 字段语法无效：{token!r}")
        start_raw, end_raw, step_raw = match.groups()
        step = 1
        if step_raw is not None:
            step = int(step_raw)
            if step < 1 or step > high:
                raise ValueError(f"Cron {label} 字段步长越界：{token!r}")
        if start_raw == "*":
            if end_raw is not None:
                raise ValueError(f"Cron {label} 字段范围无效：{token!r}")
            values.update(range(low, high + 1, step))
            continue
        start = int(start_raw)
        if not low <= start <= high:
            raise ValueError(f"Cron {label} 字段数值越界：{token!r}")
        if end_raw is not None:
            end = int(end_raw)
            if not low <= end <= high or end < start:
                raise ValueError(f"Cron {label} 字段区间无效：{token!r}")
            values.update(range(start, end + 1, step))
        else:
            if step_raw is not None:
                raise ValueError(
                    f"Cron {label} 字段仅允许对 '*' 或 'a-b' 指定步长：{token!r}"
                )
            values.add(start)
    if is_weekday and 7 in values:
        values.discard(7)
        values.add(0)
    return tuple(sorted(values))


def _parse_cron_fields(
    cron_expr: str,
) -> tuple[
    tuple[int, ...],
    tuple[int, ...],
    frozenset[int],
    frozenset[int],
    frozenset[int],
]:
    cleaned = (cron_expr or "").strip()
    if not cleaned:
        raise ValueError("Cron 表达式不能为空")
    hhmm = _HHMM_RE.fullmatch(cleaned)
    if hhmm is not None:
        cleaned = f"{int(hhmm.group(2))} {int(hhmm.group(1))} * * *"
    parts = cleaned.split()
    if len(parts) != 5:
        raise ValueError("定时表达式仅支持 HH:MM 或 5 段 Cron 表达式")
    expanded = [
        _expand_cron_field(part, label, low, high, is_weekday=is_weekday)
        for part, (label, low, high, is_weekday) in zip(
            parts, _CRON_FIELD_BOUNDS, strict=True
        )
    ]
    return (
        expanded[0],
        expanded[1],
        frozenset(expanded[2]),
        frozenset(expanded[3]),
        frozenset(expanded[4]),
    )


def cron_matches(cron_expr: str, dt_local: datetime) -> bool:
    """Return True if local datetime matches the 5-field cron (or HH:MM) expression."""
    minutes, hours, days, months, weekdays = _parse_cron_fields(cron_expr)
    cron_weekday = (dt_local.weekday() + 1) % 7
    return (
        dt_local.minute in minutes
        and dt_local.hour in hours
        and dt_local.day in days
        and dt_local.month in months
        and cron_weekday in weekdays
    )


def latest_scheduled_slot(
    cron_expr: str, tz_name: str | None, now_utc: datetime
) -> datetime | None:
    """Return the latest UTC timestamp <= now_utc matching cron_expr in tz_name."""
    minutes, hours, days, months, weekdays = _parse_cron_fields(cron_expr)
    now_norm = as_utc(now_utc)
    try:
        tz = ZoneInfo((tz_name or "").strip() or "Asia/Shanghai")
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ValueError(f"无效时区：{tz_name}") from exc
    now_local = now_norm.astimezone(tz)
    current_date: date = now_local.date()

    for offset in range(_MAX_SLOT_LOOKBACK_DAYS):
        day = current_date - timedelta(days=offset)
        cron_weekday = (day.weekday() + 1) % 7
        if (
            day.month not in months
            or day.day not in days
            or cron_weekday not in weekdays
        ):
            continue
        for hour in reversed(hours):
            for minute in reversed(minutes):
                for fold in (1, 0):
                    local_dt = datetime(
                        day.year, day.month, day.day, hour, minute, tzinfo=tz, fold=fold
                    )
                    utc_dt = local_dt.astimezone(UTC)
                    roundtrip = utc_dt.astimezone(tz)
                    if (
                        roundtrip.year,
                        roundtrip.month,
                        roundtrip.day,
                        roundtrip.hour,
                        roundtrip.minute,
                    ) != (day.year, day.month, day.day, hour, minute):
                        continue
                    if utc_dt <= now_norm:
                        return utc_dt
    return None


def is_instance_due(instance, now_utc: datetime) -> bool:
    """Return True iff a scheduled slot lies strictly after anchor and <= now_utc."""
    if not getattr(instance, "enabled", False):
        return False
    cron_expr = (getattr(instance, "schedule_cron", None) or "").strip()
    if not cron_expr:
        return False
    now_norm = as_utc(now_utc)
    slot = latest_scheduled_slot(
        cron_expr, getattr(instance, "timezone", None) or "Asia/Shanghai", now_norm
    )
    if slot is None:
        return False
    anchor_raw = getattr(instance, "last_triggered_at", None) or getattr(
        instance, "created_at", None
    )
    anchor = as_utc(anchor_raw)
    if anchor is None:
        return False
    return anchor < slot <= now_norm


class PullInstanceScheduler:
    """Fork-safe wall-clock/cron scheduler for ProjectPullInstance rows."""

    def __init__(self, poll_interval: float = 15.0, clock=lambda: datetime.now(UTC)):
        self._poll_interval = max(0.05, float(poll_interval))
        self._clock = clock
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stopping = False

    def fork_child(self) -> None:
        """Drop parent-thread state inherited by a forked child (#231)."""
        self._condition = threading.Condition()
        self._thread = None
        self._stopping = False

    def start(self) -> None:
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            self._thread = threading.Thread(
                target=self._run,
                name="dataagent-instance-pull-scheduler",
                daemon=True,
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
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def tick_once(self, *, now: datetime | None = None) -> list[tuple[str, str]]:
        """Scan enabled instances on active projects and trigger due pulls once."""
        from . import pull_service
        from .db import Project, ProjectPullInstance, sessions

        now_utc = as_utc(now if now is not None else self._clock())
        with sessions()() as db:
            candidates = (
                db.query(ProjectPullInstance, Project.status)
                .join(Project, Project.id == ProjectPullInstance.project_id)
                .filter(
                    ProjectPullInstance.enabled.is_(True),
                    ProjectPullInstance.schedule_cron.is_not(None),
                    ProjectPullInstance.schedule_cron != "",
                )
                .order_by(ProjectPullInstance.project_id, ProjectPullInstance.id)
                .all()
            )
        triggered: list[tuple[str, str]] = []
        for instance, project_status in candidates:
            project_id = instance.project_id
            instance_id = instance.id
            if (
                project_status != project_context.ACTIVE_STATUS
                or project_context.project_is_frozen(project_id)
            ):
                LOGGER.debug(
                    "Skipping scheduled pull for frozen project %s instance %s",
                    project_id,
                    instance_id,
                )
                continue
            try:
                if not is_instance_due(instance, now_utc):
                    continue
            except ValueError:
                LOGGER.warning(
                    "Invalid schedule on project %s pull instance %s",
                    project_id,
                    instance_id,
                )
                continue

            claimed = False
            with sessions().begin() as db:
                fresh = db.get(ProjectPullInstance, (project_id, instance_id))
                if fresh is not None and is_instance_due(fresh, now_utc):
                    fresh.last_triggered_at = now_utc
                    claimed = True
            if not claimed:
                continue

            triggered.append((project_id, instance_id))
            try:
                pull_service.run_instance_pull(
                    project_id, instance_id, trigger_type="scheduled"
                )
            except Exception:
                LOGGER.exception(
                    "Could not execute scheduled pull for project %s instance %s",
                    project_id,
                    instance_id,
                )
        return triggered

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._stopping:
                    return
            try:
                self.tick_once()
            except Exception:
                LOGGER.exception("PullInstanceScheduler tick failed")
            with self._condition:
                if self._stopping:
                    return
                self._condition.wait(timeout=self._poll_interval)


instance_pull_scheduler = PullInstanceScheduler()


def _reset_after_fork() -> None:
    scheduler.fork_child()
    pull_scheduler.fork_child()
    instance_pull_scheduler.fork_child()


os.register_at_fork(after_in_child=_reset_after_fork)
