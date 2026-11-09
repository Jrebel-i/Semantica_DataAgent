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


def test_puller_interval_restored_on_restart(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient

    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.scheduler import pull_scheduler

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "pull.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    token = initialize()
    starts = []
    original = pull_scheduler.start

    def observe(interval):
        starts.append(interval)
        original(interval)

    monkeypatch.setattr(pull_scheduler, "start", observe)
    with TestClient(app) as client:
        result = client.put(
            "/api/v1/plugins/puller/dtb",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "enabled": True,
                "parameters": {"url": "http://dtb.test", "interval_seconds": 42},
            },
        )
        assert result.status_code == 200
    with TestClient(app):
        pass
    assert starts == [0, 42]


def test_pull_scheduler_runs_the_due_project(monkeypatch):
    from semantica_dataagent import project_context, pull_service
    from semantica_dataagent.scheduler import _scheduled_pull

    calls = []
    monkeypatch.setattr(
        pull_service, "run_pull", lambda project_id: calls.append(project_id)
    )
    monkeypatch.setattr(project_context, "project_is_frozen", lambda project_id: False)

    _scheduled_pull("alpha")

    assert calls == ["alpha"]


def test_pull_scheduler_skips_archived_project_and_keeps_its_interval(
    tmp_path, monkeypatch, caplog
):
    import logging

    from semantica_dataagent import project_context, pull_service
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.scheduler import (
        Scheduler,
        _scheduled_pull,
        pull_scheduler,
    )

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "frozen-pull.db"))
    caplog.set_level(logging.DEBUG, logger="semantica_dataagent.scheduler")
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    token = initialize()
    headers = {"Authorization": f"Bearer {token}"}
    calls = []
    monkeypatch.setattr(
        pull_service, "run_pull", lambda project_id: calls.append(project_id)
    )

    with TestClient(app, headers=headers) as client:
        created = client.post(
            "/api/v1/projects", json={"name": "Frozen Pull", "type": "warehouse"}
        )
        assert created.status_code == 201, created.text
        project_id = created.json()["id"]
        saved = client.put(
            f"/api/v1/projects/{project_id}/adapters",
            json={
                "adapters": [
                    {
                        "id": "dtb",
                        "enabled": True,
                        "parameters": {},
                        "pull": {
                            "enabled": True,
                            "parameters": {
                                "url": "https://dtb.example",
                                "interval_seconds": 9,
                            },
                        },
                    }
                ]
            },
        )
        assert saved.status_code == 200, saved.text
        assert pull_scheduler._schedules[project_id].interval == 9

        now = [0.0]
        scheduler = Scheduler(_scheduled_pull, clock=lambda: now[0])
        scheduler.configure(9, project_id)

        archived = client.post(f"/api/v1/projects/{project_id}/archive")
        assert archived.status_code == 200, archived.text
        assert pull_service.intervals() == {project_id: 9}
        assert pull_scheduler._schedules[project_id].interval == 9

        def tick():
            for due in scheduler.due_projects():
                _scheduled_pull(due)

        now[0] = 9
        tick()
        assert calls == [], "归档项目的定时拉取没有被跳过"
        assert any(
            "Skipping scheduled pull" in record.getMessage()
            for record in caplog.records
            if record.name == "semantica_dataagent.scheduler"
        ), "跳过冻结项目的定时拉取时没有记录 debug"
        now[0] = 18
        tick()
        assert calls == [], "归档项目在下一个周期仍然执行了拉取"

        restored = client.post(f"/api/v1/projects/{project_id}/restore")
        assert restored.status_code == 200, restored.text
        assert pull_service.intervals() == {project_id: 9}
        assert pull_scheduler._schedules[project_id].interval == 9

        now[0] = 27
        tick()
        assert calls == [project_id], "恢复后没有按原间隔继续定时拉取"
        assert project_context.project_status(project_id) == "active"


def test_pull_intervals_are_collected_per_project(tmp_path, monkeypatch):
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.pull_service import intervals

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "pull-intervals.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    token = initialize()
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app, headers=headers) as client:
        created = client.post(
            "/api/v1/projects", json={"name": "Pull Intervals", "type": "warehouse"}
        )
        assert created.status_code == 201, created.text
        project_id = created.json()["id"]
        saved = client.put(
            f"/api/v1/projects/{project_id}/adapters",
            json={
                "adapters": [
                    {
                        "id": "dtb",
                        "enabled": True,
                        "parameters": {},
                        "pull": {
                            "enabled": True,
                            "parameters": {
                                "url": "https://dtb.example",
                                "interval_seconds": 7,
                            },
                        },
                    }
                ]
            },
        )
        assert saved.status_code == 200, saved.text

    assert intervals() == {project_id: 7}


def test_project_puller_interval_is_saved_and_restored(tmp_path, monkeypatch):
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.scheduler import pull_scheduler

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "project-pull.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    token = initialize()
    headers = {"Authorization": f"Bearer {token}"}
    configured = []
    original_configure = pull_scheduler.configure

    def observe_configure(interval, project_id="default"):
        configured.append((project_id, interval))
        original_configure(interval, project_id=project_id)

    monkeypatch.setattr(pull_scheduler, "configure", observe_configure)
    with TestClient(app, headers=headers) as client:
        created = client.post(
            "/api/v1/projects", json={"name": "Scheduled Pull", "type": "warehouse"}
        )
        assert created.status_code == 201, created.text
        project_id = created.json()["id"]
        saved = client.put(
            f"/api/v1/projects/{project_id}/adapters",
            json={
                "adapters": [
                    {
                        "id": "dtb",
                        "enabled": True,
                        "parameters": {},
                        "pull": {
                            "enabled": True,
                            "parameters": {
                                "url": "https://dtb.example",
                                "interval_seconds": 9,
                            },
                        },
                    }
                ]
            },
        )
        assert saved.status_code == 200, saved.text

    assert (project_id, 9) in configured

    starts = []
    original_start = pull_scheduler.start

    def observe_start(interval, project_id="default"):
        starts.append((project_id, interval))
        original_start(interval, project_id=project_id)

    monkeypatch.setattr(pull_scheduler, "start", observe_start)
    with TestClient(app):
        pass
    assert (project_id, 9) in starts


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
        assert saved.json() == {"interval_seconds": 15}
        assert (
            client.get("/api/v1/settings").json()["schedule_interval_seconds"] == "15"
        )
        assert client.get("/api/v1/projects/default/schedule").json() == {
            "interval_seconds": 15
        }
        assert configured == [("default", 15)]

        configured.clear()
        disabled = client.put(
            "/api/v1/projects/default/schedule", json={"interval_seconds": 0}
        )
        assert disabled.status_code == 200, disabled.text
        assert disabled.json() == {"interval_seconds": 0}
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
        assert loaded.json() == {"interval_seconds": 7}
        saved = client.put(
            f"/api/v1/projects/{project.id}/schedule",
            json={"interval_seconds": 11},
        )
        assert saved.status_code == 200, saved.text
        assert client.get(f"/api/v1/projects/{project.id}/schedule").json() == {
            "interval_seconds": 11
        }
        assert ("configure", project.id, 11) in seen
        from semantica_dataagent.project_context import schedule_interval

        assert schedule_interval() == 0
        assert schedule_interval(project.id) == 11
