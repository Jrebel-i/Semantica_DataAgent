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
