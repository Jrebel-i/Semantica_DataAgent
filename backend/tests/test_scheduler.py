"""Scheduler timing and persisted queue behavior without external services."""

import threading
import time
from datetime import UTC, datetime

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


def test_interval_changes_use_controlled_clock_and_resume(tmp_path, monkeypatch):
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.scheduler import Schedule

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    token = initialize()
    from semantica_dataagent.scheduler import scheduler

    starts = []
    original_start = scheduler.start

    def observe_start(interval):
        starts.append(interval)
        original_start(interval)

    monkeypatch.setattr(scheduler, "start", observe_start)
    now = [100.0]
    schedule = Schedule(lambda: now[0])
    schedule.configure(10)
    now[0] = 109
    assert not schedule.due()
    schedule.configure(2)
    now[0] = 110
    assert not schedule.due()
    now[0] = 111
    assert schedule.due()
    assert not schedule.due()
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
        assert (
            client.put(
                "/api/v1/settings", json={"values": {"schedule_interval_seconds": "42"}}
            ).status_code
            == 200
        )
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
        assert (
            client.get("/api/v1/settings").json()["schedule_interval_seconds"] == "42"
        )
    assert starts == [0, 42], "saved schedule was not restored at restart"
    schedule.configure(0)
    now[0] += 1000
    assert not schedule.due()


def test_scheduler_queues_behind_manual_build(tmp_path, monkeypatch):
    from semantica_dataagent import knowledge
    from semantica_dataagent.build_queue import BuildQueue, enqueue_build
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.db import Build, sessions
    from semantica_dataagent.scheduler import Schedule

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    initialize()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    executed = []

    def run(build_id):
        executed.append(build_id)
        if len(executed) == 1:
            entered.set()
            assert release.wait(10)
        with sessions().begin() as db:
            row = db.get(Build, build_id)
            row.status = "succeeded"
            row.finished_at = datetime.now(UTC).isoformat()
        if len(executed) == 2:
            finished.set()

    monkeypatch.setattr(knowledge, "run_build", run)
    queue = BuildQueue()
    queue.start()
    try:
        first = enqueue_build("full", "admin", queue)
        assert entered.wait(10)
        clock = [0.0]
        schedule = Schedule(lambda: clock[0])
        schedule.configure(2)
        clock[0] = 2
        assert schedule.due()
        second = enqueue_build("incremental", "scheduler", queue)
        with sessions()() as db:
            assert db.get(Build, second).status == "queued"
            assert db.get(Build, second).triggered_by == "scheduler"
        release.set()
        assert finished.wait(10)
        assert executed == [first, second]
    finally:
        release.set()
        queue.stop()


def test_each_project_has_its_own_schedule_clock():
    from semantica_dataagent.scheduler import Scheduler

    now = [0.0]
    scheduler = Scheduler(clock=lambda: now[0])
    scheduler.configure(10, project_id="a")
    scheduler.configure(20, project_id="b")
    now[0] = 9
    assert scheduler.due_projects() == []
    now[0] = 10
    assert scheduler.due_projects() == ["a"]
    assert scheduler.due_projects() == []
    now[0] = 20
    assert scheduler.due_projects() == ["a", "b"]
    scheduler.remove("a")
    now[0] = 40
    assert scheduler.due_projects() == ["b"]


def test_project_schedule_api_reads_writes_and_restarts_timer(tmp_path, monkeypatch):
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.scheduler import scheduler

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "schedule.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    token = initialize()
    configured = []
    original = scheduler.configure

    def observe(interval, project_id="default"):
        configured.append((project_id, interval))
        original(interval, project_id=project_id)

    monkeypatch.setattr(scheduler, "configure", observe)
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app, headers=headers) as client:
        configured.clear()
        saved = client.put(
            "/api/v1/projects/default/schedule", json={"interval_seconds": 15}
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["interval_seconds"] == 15
        assert saved.json()["legacy_interval"] is True
        assert (
            client.get("/api/v1/settings").json()["schedule_interval_seconds"] == "15"
        )
        assert client.get("/api/v1/projects/default/schedule").json()[
            "interval_seconds"
        ] == 15
        assert configured == [("default", 15)]

        configured.clear()
        disabled = client.put(
            "/api/v1/projects/default/schedule", json={"interval_seconds": 0}
        )
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["interval_seconds"] == 0
        assert disabled.json()["legacy_interval"] is False
        assert configured == [("default", 0)]

        for value in (-1, 604801):
            invalid = client.put(
                "/api/v1/projects/default/schedule",
                json={"interval_seconds": value},
            )
            assert invalid.status_code == 422, invalid.text


def test_scheduled_enqueue_merges_duplicates_per_project(tmp_path, monkeypatch):
    from semantica_dataagent.build_queue import enqueue_build
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.project_api import create_project_record

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "dedupe.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    initialize()
    a = create_project_record("Schedule A", "document")
    first = enqueue_build(
        "incremental", "scheduler", project_id=a.id, notify=False, dedupe=True
    )
    second = enqueue_build(
        "incremental", "scheduler", project_id=a.id, notify=False, dedupe=True
    )
    assert first == second
    other = enqueue_build(
        "incremental", "scheduler", project_id="default", notify=False, dedupe=True
    )
    assert other != first


def test_scheduler_thread_fires_each_project_and_remove_takes_effect():
    from semantica_dataagent.scheduler import Scheduler

    fired = []
    condition = threading.Condition()

    def action(project_id):
        with condition:
            fired.append(project_id)
            condition.notify_all()

    scheduler = Scheduler(action=action)
    scheduler.start(0)
    try:
        scheduler.configure(0.2, project_id="a")
        scheduler.configure(0.4, project_id="b")
        deadline = time.monotonic() + 5
        with condition:
            while not {"a", "b"} <= set(fired) and time.monotonic() < deadline:
                condition.wait(0.1)
        assert {"a", "b"} <= set(fired), fired

        scheduler.remove("a")
        time.sleep(0.3)
        with condition:
            fired.clear()
        time.sleep(0.5)
        with condition:
            assert "a" not in fired, fired
    finally:
        scheduler.stop()


def test_each_project_schedule_api_and_startup_wiring(tmp_path, monkeypatch):
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.db import ProjectSetting, sessions
    from semantica_dataagent.project_api import create_project_record
    from semantica_dataagent.project_context import (
        SCHEDULE_INTERVAL_KEY,
        project_schedule_intervals,
        start_schedule,
    )
    from semantica_dataagent.scheduler import scheduler

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "project-schedule.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    token = initialize()
    project = create_project_record("Scheduled docs", "document")
    with sessions().begin() as db:
        db.add(
            ProjectSetting(
                project_id=project.id,
                key=SCHEDULE_INTERVAL_KEY,
                value="7",
            )
        )
    assert project_schedule_intervals()[project.id] == 7

    seen = []
    monkeypatch.setattr(
        scheduler,
        "start",
        lambda interval, project_id="default": seen.append(
            ("start", project_id, interval)
        ),
    )
    monkeypatch.setattr(
        scheduler,
        "configure",
        lambda interval, project_id="default": seen.append(
            ("configure", project_id, interval)
        ),
    )
    start_schedule()
    assert ("configure", project.id, 7) in seen

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app, headers=headers) as client:
        loaded = client.get(f"/api/v1/projects/{project.id}/schedule")
        assert loaded.status_code == 200, loaded.text
        assert loaded.json()["interval_seconds"] == 7
        assert loaded.json()["legacy_interval"] is True
        saved = client.put(
            f"/api/v1/projects/{project.id}/schedule",
            json={"interval_seconds": 11},
        )
        assert saved.status_code == 200, saved.text
        assert (
            client.get(f"/api/v1/projects/{project.id}/schedule").json()[
                "interval_seconds"
            ]
            == 11
        )
        assert ("configure", project.id, 11) in seen
        from semantica_dataagent.project_context import schedule_interval

        assert schedule_interval() == 0
        assert schedule_interval(project.id) == 11


def test_project_cron_schedule_validation_timezone_and_ticks(tmp_path, monkeypatch):
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.db import Build, sessions
    from semantica_dataagent.project_api import create_project_record
    from semantica_dataagent.scheduler import BuildCronScheduler

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "cron-schedule.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    token = initialize()
    other = create_project_record("Docs", "document")
    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(app, headers=headers) as client:
        # Legacy interval preserved until Cron is explicitly saved
        legacy = client.put(
            f"/api/v1/projects/{other.id}/schedule",
            json={"interval_seconds": 30},
        )
        assert legacy.status_code == 200
        assert legacy.json()["legacy_interval"] is True

        # Validation rejects invalid cron, invalid timezone, and enabled without cron
        for bad_payload in (
            {"enabled": True, "schedule_cron": "", "timezone": "Asia/Shanghai"},
            {"enabled": True, "schedule_cron": "99 * * * *", "timezone": "Asia/Shanghai"},
            {"enabled": True, "schedule_cron": "0 2 * * *", "timezone": "Mars/Olympus"},
        ):
            bad = client.put(
                f"/api/v1/projects/{other.id}/schedule", json=bad_payload
            )
            assert bad.status_code == 422, bad.text

        # Saving Cron switches off legacy interval and computes next_run_at
        saved = client.put(
            f"/api/v1/projects/{other.id}/schedule",
            json={
                "enabled": True,
                "schedule_cron": "0 2 * * *",
                "timezone": "Asia/Shanghai",
            },
        )
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert body["enabled"] is True
        assert body["schedule_cron"] == "0 2 * * *"
        assert body["timezone"] == "Asia/Shanghai"
        assert body["legacy_interval"] is False
        assert body["interval_seconds"] == 0
        assert body["next_run_at"] is not None

        # Default project remains untouched (project isolation)
        default_view = client.get("/api/v1/projects/default/schedule").json()
        assert default_view["enabled"] is False
        assert default_view["schedule_cron"] is None

        # Disabling preserves Cron and timezone without scheduling
        paused = client.put(
            f"/api/v1/projects/{other.id}/schedule",
            json={
                "enabled": False,
                "schedule_cron": "0 2 * * *",
                "timezone": "Asia/Shanghai",
            },
        )
        assert paused.status_code == 200
        assert paused.json()["enabled"] is False
        assert paused.json()["schedule_cron"] == "0 2 * * *"
        assert paused.json()["next_run_at"] is None

        # Re-enable daily 02:00 UTC for deterministic tick testing
        re_enabled = client.put(
            f"/api/v1/projects/{other.id}/schedule",
            json={
                "enabled": True,
                "schedule_cron": "0 2 * * *",
                "timezone": "UTC",
            },
        )
        assert re_enabled.status_code == 200

    # Force anchor earlier and simulate downtime across multiple days -> triggers at most once
    from semantica_dataagent import project_context

    with sessions().begin() as db:
        project_context.write_project_setting_value(
            db,
            other.id,
            project_context.SCHEDULE_LAST_TRIGGERED_AT_KEY,
            "2026-01-01T00:00:00+00:00",
        )

    cron_sched = BuildCronScheduler()
    fired = cron_sched.tick_once(now=datetime(2026, 1, 5, 3, 0, tzinfo=UTC))
    assert fired == [other.id]
    # Calling again at same/later time before next day 02:00 does NOT backfill extra runs
    assert cron_sched.tick_once(now=datetime(2026, 1, 5, 3, 5, tzinfo=UTC)) == []
    with sessions()() as db:
        rows = (
            db.query(Build)
            .filter(Build.project_id == other.id, Build.triggered_by == "scheduler")
            .all()
        )
        assert len(rows) == 1
        assert rows[0].build_type == "incremental"

    # Next day 02:01 triggers again
    assert cron_sched.tick_once(now=datetime(2026, 1, 6, 2, 1, tzinfo=UTC)) == [
        other.id
    ]


def test_builds_list_omits_parse_failures_by_default_and_limits(tmp_path, monkeypatch):
    import json

    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.db import Build, sessions

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "builds-list.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    token = initialize()
    failures = [{"task_id": "t1", "reason": "bad sql"}]
    logs = [
        {
            "timestamp": "2026-01-01T00:00:10+00:00",
            "stage": "table",
            "level": "info",
            "message": "parsed 3 tables",
        }
    ]
    with sessions().begin() as db:
        for idx in range(3):
            db.add(
                Build(
                    project_id="default",
                    status="succeeded",
                    started_at=f"2026-01-0{idx + 1}T00:00:00+00:00",
                    finished_at=f"2026-01-0{idx + 1}T00:01:00+00:00",
                    counts=json.dumps({"table": idx + 1}),
                    parse_failures=json.dumps(failures),
                    logs=json.dumps(logs),
                    build_type="full",
                    triggered_by="admin",
                )
            )

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app, headers=headers) as client:
        default_list = client.get("/api/v1/builds").json()
        assert len(default_list) == 3
        assert all(item["parse_failures"] == [] for item in default_list)
        assert all(item["logs"] == [] for item in default_list)

        limited = client.get("/api/v1/builds", params={"limit": 2}).json()
        assert len(limited) == 2

        with_details = client.get(
            "/api/v1/builds",
            params={"include_parse_failures": True, "include_logs": True},
        ).json()
        assert all(item["parse_failures"] == failures for item in with_details)
        assert all(
            len(item["logs"]) == 1
            and item["logs"][0]["stage"] == "table"
            and item["logs"][0]["message"] == "parsed 3 tables"
            for item in with_details
        )

        detail = client.get(f"/api/v1/builds/{default_list[0]['id']}").json()
        assert detail["parse_failures"] == failures
        assert len(detail["logs"]) == 1
        assert detail["logs"][0]["message"] == "parsed 3 tables"
