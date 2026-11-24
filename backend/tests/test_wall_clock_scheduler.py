"""Tests for wall-clock/cron PullInstanceScheduler (Seam 4)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from zoneinfo import ZoneInfo

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


@pytest.fixture
def db_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "sched.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    from semantica_dataagent.cli import initialize

    token = initialize()
    return token


def test_cron_matches_variants_and_sunday_zero_or_seven():
    from semantica_dataagent.scheduler import cron_matches

    # HH:MM shorthand
    assert cron_matches("02:30", datetime(2026, 6, 15, 2, 30))
    assert not cron_matches("02:30", datetime(2026, 6, 15, 2, 31))

    # */step, ranges, stepped ranges, comma lists
    expr = "*/15 9-17/2 1,15 1-6 1-5"
    # 2026-06-15 is Monday (weekday=1)
    assert cron_matches(expr, datetime(2026, 6, 15, 9, 0))
    assert cron_matches(expr, datetime(2026, 6, 15, 11, 45))
    assert not cron_matches(expr, datetime(2026, 6, 15, 10, 0))
    assert not cron_matches(expr, datetime(2026, 6, 15, 9, 10))
    assert not cron_matches(expr, datetime(2026, 6, 14, 9, 0))  # Sunday

    # Sunday as 0 or 7 (2026-06-14 is Sunday)
    sunday = datetime(2026, 6, 14, 3, 15)
    assert cron_matches("15 3 * * 0", sunday)
    assert cron_matches("15 3 * * 7", sunday)
    assert not cron_matches("15 3 * * 1", sunday)


def test_latest_scheduled_slot_respects_timezone_and_cross_midnight():
    from semantica_dataagent.scheduler import latest_scheduled_slot

    # 00:30 Asia/Shanghai == 16:30 UTC previous calendar day
    now_after = datetime(2026, 6, 15, 16, 31, tzinfo=UTC)  # 2026-06-16 00:31 +08:00
    assert latest_scheduled_slot("30 0 * * *", "Asia/Shanghai", now_after) == datetime(
        2026, 6, 15, 16, 30, tzinfo=UTC
    )

    now_before = datetime(2026, 6, 15, 16, 29, tzinfo=UTC)  # 2026-06-16 00:29 +08:00
    assert latest_scheduled_slot("30 0 * * *", "Asia/Shanghai", now_before) == datetime(
        2026, 6, 14, 16, 30, tzinfo=UTC
    )

    # Same cron "0 2 * * *" in UTC vs Asia/Shanghai differs by 8 hours
    now = datetime(2026, 6, 15, 3, 0, tzinfo=UTC)
    assert latest_scheduled_slot("0 2 * * *", "UTC", now) == datetime(
        2026, 6, 15, 2, 0, tzinfo=UTC
    )
    assert latest_scheduled_slot("0 2 * * *", "Asia/Shanghai", now) == datetime(
        2026, 6, 14, 18, 0, tzinfo=UTC
    )

    # DST spring-forward skipped hour in America/New_York (2026-03-08 02:30 does not exist)
    dst_check = datetime(2026, 3, 8, 10, 0, tzinfo=UTC)  # 06:00 EDT
    slot = latest_scheduled_slot("30 2 * * *", "America/New_York", dst_check)
    # Must fall back to previous valid day 2026-03-07 02:30 EST (07:30 UTC)
    assert slot == datetime(
        2026, 3, 7, 2, 30, tzinfo=ZoneInfo("America/New_York")
    ).astimezone(UTC)


def test_is_instance_due_prevents_retroactive_and_duplicate_firing():
    from semantica_dataagent.scheduler import is_instance_due

    # Created at 02:05+08:00 (18:05 UTC previous day) with 02:00+08:00 schedule
    created_utc = datetime(2026, 6, 14, 18, 5, tzinfo=UTC)
    inst = SimpleNamespace(
        enabled=True,
        schedule_cron="0 2 * * *",
        timezone="Asia/Shanghai",
        created_at=created_utc,
        last_triggered_at=None,
    )
    # 02:10+08:00 same morning: slot was 02:00 <= created_at (02:05) -> must NOT fire retroactively
    assert not is_instance_due(inst, datetime(2026, 6, 14, 18, 10, tzinfo=UTC))

    # Next day 02:00+08:00 (2026-06-15 18:00 UTC) -> due!
    assert is_instance_due(inst, datetime(2026, 6, 15, 18, 0, tzinfo=UTC))

    # Once triggered at 18:00:30 UTC, checking again at 18:05 UTC is NOT due
    inst.last_triggered_at = datetime(2026, 6, 15, 18, 0, 30, tzinfo=UTC)
    assert not is_instance_due(inst, datetime(2026, 6, 15, 18, 5, tzinfo=UTC))


def test_tick_once_restart_no_drift_cross_midnight_and_frozen_projects(
    db_env, monkeypatch
):
    from semantica_dataagent import project_deletion, pull_service
    from semantica_dataagent.db import ProjectPullInstance, sessions
    from semantica_dataagent.project_api import create_project_record
    from semantica_dataagent.scheduler import PullInstanceScheduler

    proj = create_project_record("WallClock", "warehouse")
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        pull_service,
        "run_instance_pull",
        lambda project_id, instance_id, *, trigger_type="manual", force=False: (
            calls.append((project_id, instance_id, trigger_type))
        ),
    )

    created_at = datetime(2026, 6, 15, 15, 50, tzinfo=UTC)  # 2026-06-15 23:50+08:00
    with sessions().begin() as db:
        db.add(
            ProjectPullInstance(
                project_id=proj.id,
                id="nightly",
                plugin_id="ossie",
                display_name="Nightly",
                enabled=True,
                schedule_cron="0 0 * * *",
                timezone="Asia/Shanghai",
                created_at=created_at,
                updated_at=created_at,
            )
        )
        db.add(
            ProjectPullInstance(
                project_id=proj.id,
                id="disabled-inst",
                plugin_id="ossie",
                display_name="Disabled",
                enabled=False,
                schedule_cron="0 0 * * *",
                timezone="Asia/Shanghai",
                created_at=created_at,
                updated_at=created_at,
            )
        )
        db.add(
            ProjectPullInstance(
                project_id=proj.id,
                id="manual-only",
                plugin_id="ossie",
                display_name="Manual",
                enabled=True,
                schedule_cron=None,
                timezone="Asia/Shanghai",
                created_at=created_at,
                updated_at=created_at,
            )
        )

    sched = PullInstanceScheduler()

    # Before midnight (23:55+08:00) -> nothing due
    assert sched.tick_once(now=datetime(2026, 6, 15, 15, 55, tzinfo=UTC)) == []
    assert calls == []

    # Cross-midnight (00:05+08:00, fired 5 minutes late) -> fires once
    assert sched.tick_once(now=datetime(2026, 6, 15, 16, 5, tzinfo=UTC)) == [
        (proj.id, "nightly")
    ]
    assert calls == [(proj.id, "nightly", "scheduled")]

    # Simulate process restart with a fresh PullInstanceScheduler inside same window (00:20+08:00)
    calls.clear()
    restarted_sched = PullInstanceScheduler()
    assert (
        restarted_sched.tick_once(now=datetime(2026, 6, 15, 16, 20, tzinfo=UTC)) == []
    )
    assert calls == []

    # Next night at exact 00:00+08:00 -> NO drift from yesterday's 00:05 late run!
    assert restarted_sched.tick_once(now=datetime(2026, 6, 16, 16, 0, tzinfo=UTC)) == [
        (proj.id, "nightly")
    ]
    assert calls == [(proj.id, "nightly", "scheduled")]

    # Archive project -> skipped cleanly without updating last_triggered_at
    calls.clear()
    project_deletion.archive_project(proj.id, "admin", "Admin")
    with sessions()() as db:
        before_ts = db.get(ProjectPullInstance, (proj.id, "nightly")).last_triggered_at
    assert restarted_sched.tick_once(now=datetime(2026, 6, 17, 16, 1, tzinfo=UTC)) == []
    assert calls == []
    with sessions()() as db:
        assert (
            db.get(ProjectPullInstance, (proj.id, "nightly")).last_triggered_at
            == before_ts
        )

    # Restore project -> resumes and fires missed slot on next tick
    project_deletion.restore_project(proj.id, "admin", "Admin")
    assert restarted_sched.tick_once(now=datetime(2026, 6, 17, 16, 2, tzinfo=UTC)) == [
        (proj.id, "nightly")
    ]
    assert calls == [(proj.id, "nightly", "scheduled")]

    # Deleting project -> skipped cleanly
    calls.clear()
    project_deletion.archive_project(proj.id, "admin", "Admin")
    assert (
        project_deletion.prepare_deletion(proj.id, proj.name, "admin", "Admin")
        == "active"
    )
    assert restarted_sched.tick_once(now=datetime(2026, 6, 18, 16, 2, tzinfo=UTC)) == []
    assert calls == []


def test_app_lifespan_and_crud_wake_instance_scheduler(db_env, monkeypatch):
    from semantica_dataagent import plugins, pull_instances
    from semantica_dataagent.app import app
    from semantica_dataagent.scheduler import instance_pull_scheduler

    class _StubPuller:
        id = "stub"
        version = "1.0"
        display_name = "Stub"
        parameters_schema: ClassVar[dict] = {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        }

        def pull(self, context):
            raise NotImplementedError

    base = dict(plugins.discover())
    base[("puller", "stub")] = _StubPuller()
    monkeypatch.setattr(plugins, "discover", lambda: dict(base))
    monkeypatch.setattr(pull_instances, "discover", lambda: dict(base))

    events: list[str] = []
    orig_start = instance_pull_scheduler.start
    orig_stop = instance_pull_scheduler.stop
    orig_wake = instance_pull_scheduler.wake
    monkeypatch.setattr(
        instance_pull_scheduler,
        "start",
        lambda: (events.append("start"), orig_start())[-1],
    )
    monkeypatch.setattr(
        instance_pull_scheduler,
        "stop",
        lambda: (events.append("stop"), orig_stop())[-1],
    )
    monkeypatch.setattr(
        instance_pull_scheduler,
        "wake",
        lambda: (events.append("wake"), orig_wake())[-1],
    )

    headers = {"Authorization": f"Bearer {db_env}"}
    with TestClient(app, headers=headers) as c:
        assert "start" in events
        created = c.post(
            "/api/v1/projects/default/pull-instances",
            json={
                "id": "wake-inst",
                "plugin_id": "stub",
                "display_name": "Wake Test",
                "parameters": {"url": "https://stub.example"},
                "schedule_cron": "03:00",
            },
        )
        assert created.status_code == 201, created.text
        updated = c.put(
            "/api/v1/projects/default/pull-instances/wake-inst",
            json={"schedule_cron": "04:00"},
        )
        assert updated.status_code == 200
        deleted = c.delete("/api/v1/projects/default/pull-instances/wake-inst")
        assert deleted.status_code == 204
    assert events.count("wake") >= 3
    assert "stop" in events
