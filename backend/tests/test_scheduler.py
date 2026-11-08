"""Scheduler timing and persisted queue behavior without external services."""

import threading
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
