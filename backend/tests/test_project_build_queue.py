"""Per-project build queue behavior across independent projects."""

from __future__ import annotations

import os
import signal
import threading
import time

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


def configure(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    from semantica_dataagent.cli import initialize

    return initialize()


def create_projects():
    from semantica_dataagent.project_api import create_project_record

    return [
        create_project_record("Project A", "document"),
        create_project_record("Project B", "document"),
    ]


def wait_status(client, project, build_id, statuses, timeout=10):
    deadline = time.monotonic() + timeout
    row = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/builds/{build_id}", params={"project": project})
        assert response.status_code == 200, response.text
        row = response.json()
        if row["status"] in statuses:
            return row
        time.sleep(0.02)
    raise AssertionError(f"build {build_id} did not reach {statuses}: {row}")


def project_runner(entered, release, failed=()):
    from semantica_dataagent.db import Build, sessions

    def run(build_id):
        with sessions()() as db:
            row = db.get(Build, build_id)
            project = row.project_id
        entered[build_id].set()
        if project in failed:
            raise RuntimeError(f"build failed in {project}")
        assert release.wait(10), "test did not release build"
        with sessions().begin() as db:
            row = db.get(Build, build_id)
            row.status = "succeeded"
            row.finished_at = "2026-01-01T00:00:01+00:00"

    return run


def test_different_projects_build_in_parallel_and_same_project_queues(
    tmp_path, monkeypatch
):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import app

    token = configure(tmp_path, monkeypatch)
    a, b = create_projects()
    entered = {index: threading.Event() for index in range(1, 4)}
    release = threading.Event()
    monkeypatch.setattr(knowledge, "run_build", project_runner(entered, release))
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app, headers=headers) as client:
        first_a = client.post(
            "/api/v1/builds", params={"project": a.id}, json={"build_type": "full"}
        ).json()
        first_b = client.post(
            "/api/v1/builds", params={"project": b.id}, json={"build_type": "full"}
        ).json()
        second_a = client.post(
            "/api/v1/builds",
            params={"project": a.id},
            json={"build_type": "incremental"},
        ).json()

        assert entered[first_a["id"]].wait(10)
        assert entered[first_b["id"]].wait(10), "project B waited for project A"
        assert (
            client.get(
                f"/api/v1/builds/{second_a['id']}", params={"project": a.id}
            ).json()["status"]
            == "queued"
        )
        assert {
            row["id"]
            for row in client.get("/api/v1/builds", params={"project": a.id}).json()
        } == {first_a["id"], second_a["id"]}
        release.set()
        wait_status(client, a.id, first_a["id"], {"succeeded"})
        wait_status(client, a.id, second_a["id"], {"succeeded"})
        wait_status(client, b.id, first_b["id"], {"succeeded"})


def test_failed_project_does_not_stop_another_projects_build(tmp_path, monkeypatch):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import app

    token = configure(tmp_path, monkeypatch)
    a, b = create_projects()
    entered = {index: threading.Event() for index in range(1, 3)}
    release = threading.Event()
    monkeypatch.setattr(
        knowledge, "run_build", project_runner(entered, release, failed={a.id})
    )
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app, headers=headers) as client:
        failed = client.post(
            "/api/v1/builds", params={"project": a.id}, json={"build_type": "full"}
        ).json()
        healthy = client.post(
            "/api/v1/builds", params={"project": b.id}, json={"build_type": "full"}
        ).json()
        wait_status(client, a.id, failed["id"], {"failed"})
        assert (
            client.get(
                f"/api/v1/builds/{healthy['id']}", params={"project": b.id}
            ).json()["status"]
            == "running"
        )
        release.set()
        wait_status(client, b.id, healthy["id"], {"succeeded"})


def test_concurrency_limit_is_read_from_settings(tmp_path, monkeypatch):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import app

    token = configure(tmp_path, monkeypatch)
    a, b = create_projects()
    entered = {index: threading.Event() for index in range(1, 3)}
    release = threading.Event()
    monkeypatch.setattr(knowledge, "run_build", project_runner(entered, release))
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app, headers=headers) as client:
        assert (
            client.put(
                "/api/v1/settings",
                json={"values": {"build_concurrency": "1"}},
            ).status_code
            == 200
        )
        first = client.post(
            "/api/v1/builds", params={"project": a.id}, json={"build_type": "full"}
        ).json()
        second = client.post(
            "/api/v1/builds", params={"project": b.id}, json={"build_type": "full"}
        ).json()
        assert entered[first["id"]].wait(10)
        time.sleep(0.2)
        assert (
            client.get(
                f"/api/v1/builds/{second['id']}", params={"project": b.id}
            ).json()["status"]
            == "queued"
        )
        release.set()
        wait_status(client, a.id, first["id"], {"succeeded"})
        wait_status(client, b.id, second["id"], {"succeeded"})


def test_startup_recovery_is_per_project_and_tolerates_one_failure(
    tmp_path, monkeypatch
):
    from sqlalchemy import text

    from semantica_dataagent.build_queue import recover_interrupted_builds
    from semantica_dataagent.db import Build, sessions

    configure(tmp_path, monkeypatch)
    a, b = create_projects()
    with sessions().begin() as db:
        running_a = Build(
            project_id=a.id,
            status="running",
            started_at="2026-01-01T00:00:00+00:00",
            counts="{}",
            build_type="full",
        )
        running_b = Build(
            project_id=b.id,
            status="running",
            started_at="2026-01-01T00:00:00+00:00",
            counts="{}",
            build_type="full",
        )
        queued_b = Build(
            project_id=b.id,
            status="queued",
            started_at="2026-01-01T00:00:00+00:00",
            counts="{}",
            build_type="incremental",
        )
        correction_b = Build(
            project_id=b.id,
            status="queued",
            started_at="2026-01-01T00:00:00+00:00",
            counts="{}",
            build_type="incremental",
            correction_proposal_id="proposal-b",
        )
        db.add_all([running_a, running_b, queued_b, correction_b])
        db.flush()
        ids = {
            "running_a": running_a.id,
            "running_b": running_b.id,
            "queued_b": queued_b.id,
            "correction_b": correction_b.id,
        }
    with sessions().begin() as db:
        db.execute(
            text(
                "CREATE TRIGGER fail_project_a BEFORE UPDATE ON builds "
                f"WHEN OLD.project_id = '{a.id}' "
                "BEGIN SELECT RAISE(ABORT, 'simulated recovery failure'); END"
            )
        )

    recover_interrupted_builds()

    with sessions()() as db:
        assert db.get(Build, ids["running_a"]).status == "running"
        assert db.get(Build, ids["running_b"]).status == "failed"
        assert db.get(Build, ids["queued_b"]).status == "cancelled"
        assert db.get(Build, ids["correction_b"]).status == "queued"


def test_build_killed_in_progress_is_recovered_by_project(tmp_path, monkeypatch):
    from semantica_dataagent.build_queue import recover_interrupted_builds
    from semantica_dataagent.db import Build, sessions

    configure(tmp_path, monkeypatch)
    a, b = create_projects()
    with sessions().begin() as db:
        build = Build(
            project_id=a.id,
            status="running",
            started_at="2026-01-01T00:00:00+00:00",
            counts="{}",
            build_type="full",
        )
        db.add(build)
        db.flush()
        build_id = build.id
    pid = os.fork()
    if pid == 0:
        with sessions().begin() as db:
            row = db.get(Build, build_id)
            row.logs = '[{"stage": "source", "message": "still running"}]'
        os.kill(os.getpid(), signal.SIGKILL)
    _, status = os.waitpid(pid, 0)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL

    recover_interrupted_builds()

    with sessions()() as db:
        row = db.get(Build, build_id)
        assert row.status == "failed"
        assert "interrupted" in row.error.lower()
        assert db.query(Build).filter_by(project_id=b.id).count() == 0


class RecordingRunner:
    """Fake build runner that blocks and records simultaneous build counts."""

    def __init__(self):
        self.lock = threading.Lock()
        self.release = threading.Event()
        self.entered = {}
        self.active = {}
        self.peak = {}
        self.total = 0
        self.peak_total = 0
        self.finished = []

    def _event(self, build_id):
        with self.lock:
            return self.entered.setdefault(build_id, threading.Event())

    def wait_started(self, build_id, timeout=10):
        return self._event(build_id).wait(timeout)

    def run(self, build_id):
        from semantica_dataagent.db import Build, sessions

        with sessions()() as db:
            row = db.get(Build, build_id)
            project = (row.project_id if row is not None else None) or "default"
        with self.lock:
            self.entered.setdefault(build_id, threading.Event()).set()
            self.active[project] = self.active.get(project, 0) + 1
            self.total += 1
            self.peak[project] = max(self.peak.get(project, 0), self.active[project])
            self.peak_total = max(self.peak_total, self.total)
        assert self.release.wait(10), "test did not release build"
        with sessions().begin() as db:
            row = db.get(Build, build_id)
            row.status = "succeeded"
            row.finished_at = "2026-01-01T00:00:01+00:00"
        with self.lock:
            self.active[project] -= 1
            self.total -= 1
            self.finished.append(build_id)

    def peak_for(self, project_id):
        with self.lock:
            return self.peak.get(project_id, 0)

    def peak_overall(self):
        with self.lock:
            return self.peak_total


def post_build(client, project, build_type):
    response = client.post(
        "/api/v1/builds", params={"project": project}, json={"build_type": build_type}
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_same_project_builds_never_overlap(tmp_path, monkeypatch):
    from sqlalchemy import text

    from semantica_dataagent import knowledge
    from semantica_dataagent.app import app
    from semantica_dataagent.db import sessions

    token = configure(tmp_path, monkeypatch)
    # Isolate the queue's own scheduling guard: production also has a partial
    # unique index, so without dropping it the database would hide a queue that
    # claims two builds for one project.
    with sessions().begin() as db:
        db.execute(text("DROP INDEX one_running_build_per_project"))
    a, _ = create_projects()
    runner = RecordingRunner()
    monkeypatch.setattr(knowledge, "run_build", runner.run)
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app, headers=headers) as client:
        first = post_build(client, a.id, "full")
        assert runner.wait_started(first["id"]), "first build did not start"
        second = post_build(client, a.id, "incremental")
        assert not runner.wait_started(second["id"], timeout=1), (
            "a second build for the same project started before the first finished"
        )
        assert (
            client.get(
                f"/api/v1/builds/{second['id']}", params={"project": a.id}
            ).json()["status"]
            == "queued"
        )
        runner.release.set()
        wait_status(client, a.id, first["id"], {"succeeded"})
        assert runner.wait_started(second["id"]), "second build never started"
        wait_status(client, a.id, second["id"], {"succeeded"})
        assert runner.peak_for(a.id) == 1


def test_different_projects_run_at_the_same_time(tmp_path, monkeypatch):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import app

    token = configure(tmp_path, monkeypatch)
    a, b = create_projects()
    runner = RecordingRunner()
    monkeypatch.setattr(knowledge, "run_build", runner.run)
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app, headers=headers) as client:
        first = post_build(client, a.id, "full")
        second = post_build(client, b.id, "full")
        assert runner.wait_started(first["id"]), "project A build did not start"
        assert runner.wait_started(second["id"]), (
            "project B build did not start while project A was running"
        )
        assert runner.peak_for(a.id) == 1
        assert runner.peak_for(b.id) == 1
        assert runner.peak_overall() == 2
        runner.release.set()
        wait_status(client, a.id, first["id"], {"succeeded"})
        wait_status(client, b.id, second["id"], {"succeeded"})


def test_concurrency_limit_two_holds_back_the_third_project(tmp_path, monkeypatch):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import app
    from semantica_dataagent.project_api import create_project_record

    token = configure(tmp_path, monkeypatch)
    a, b = create_projects()
    c = create_project_record("Project C", "document")
    runner = RecordingRunner()
    monkeypatch.setattr(knowledge, "run_build", runner.run)
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app, headers=headers) as client:
        assert (
            client.put(
                "/api/v1/settings", json={"values": {"build_concurrency": "2"}}
            ).status_code
            == 200
        )
        first = post_build(client, a.id, "full")
        second = post_build(client, b.id, "full")
        third = post_build(client, c.id, "full")
        assert runner.wait_started(first["id"]), "first build did not start"
        assert runner.wait_started(second["id"]), "second build did not start"
        assert not runner.wait_started(third["id"], timeout=1), (
            "third build started while the concurrency limit of two was reached"
        )
        assert runner.peak_overall() == 2
        runner.release.set()
        assert runner.wait_started(third["id"]), "third build never started"
        wait_status(client, a.id, first["id"], {"succeeded"})
        wait_status(client, b.id, second["id"], {"succeeded"})
        wait_status(client, c.id, third["id"], {"succeeded"})
        assert runner.peak_overall() == 2


def test_duplicate_incremental_triggers_merge_only_within_the_same_project(
    tmp_path, monkeypatch
):
    from semantica_dataagent.build_queue import enqueue_build
    from semantica_dataagent.db import Build, sessions

    configure(tmp_path, monkeypatch)
    a, b = create_projects()
    first = enqueue_build(
        "incremental", "scheduler", project_id=a.id, notify=False, dedupe=True
    )
    merged = enqueue_build(
        "incremental", "scheduler", project_id=a.id, notify=False, dedupe=True
    )
    other = enqueue_build(
        "incremental", "scheduler", project_id=b.id, notify=False, dedupe=True
    )
    assert merged == first, "duplicate trigger for the same project was not merged"
    assert other != first, "a trigger for another project was merged into project A"
    with sessions()() as db:
        assert db.query(Build).filter_by(project_id=a.id).count() == 1
        assert db.query(Build).filter_by(project_id=b.id).count() == 1
