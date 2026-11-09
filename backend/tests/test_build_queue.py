import threading
import time
from contextlib import nullcontext
from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

TERMINAL = {"succeeded", "failed", "cancelled"}


def is_utc(value):
    return value.endswith(("+00:00", "Z"))


def configure(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from semantica_dataagent.cli import initialize

    return initialize()


def wait_for_build(client, build_id, timeout=10):
    deadline = time.monotonic() + timeout
    build = None
    while time.monotonic() < deadline:
        build = client.get(f"/api/v1/builds/{build_id}").json()
        if build["status"] in TERMINAL:
            return build
        time.sleep(0.02)
    raise AssertionError(f"Build {build_id} did not finish: {build}")


def test_second_build_queues_and_runs_after_first(tmp_path, monkeypatch):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import app
    from semantica_dataagent.db import Build, sessions

    token = configure(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    executed = []

    def fake_run(build_id):
        executed.append(build_id)
        if len(executed) == 1:
            entered.set()
            assert release.wait(10)
        with sessions().begin() as db:
            row = db.get(Build, build_id)
            row.status = "succeeded"
            row.finished_at = datetime.now(UTC).isoformat()

    monkeypatch.setattr(knowledge, "run_build", fake_run)
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app, headers=headers) as client:
        first = client.post("/api/v1/builds", json={"build_type": "full"})
        assert first.status_code == 202
        assert first.json()["status"] == "queued"
        assert entered.wait(10)
        running = client.get(f"/api/v1/builds/{first.json()['id']}").json()
        assert running["status"] == "running"

        second = client.post("/api/v1/builds", json={"build_type": "incremental"})
        assert second.status_code == 202
        assert second.json()["status"] == "queued"
        assert (
            client.get(f"/api/v1/builds/{second.json()['id']}").json()["status"]
            == "queued"
        )
        release.set()
        one = wait_for_build(client, first.json()["id"])
        two = wait_for_build(client, second.json()["id"])
        assert one["status"] == two["status"] == "succeeded"
        assert executed == [one["id"], two["id"]]
        assert is_utc(one["finished_at"])
        assert is_utc(two["started_at"])


def test_restart_cancels_queued_and_fails_running(tmp_path, monkeypatch):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import app
    from semantica_dataagent.db import Build, sessions

    token = configure(tmp_path, monkeypatch)
    called = []
    monkeypatch.setattr(
        knowledge, "run_build", lambda build_id: called.append(build_id)
    )
    with sessions().begin() as db:
        db.add_all(
            [
                Build(
                    status="running",
                    started_at="2026-01-01T00:00:00+00:00",
                    counts="{}",
                ),
                Build(
                    status="queued",
                    started_at="2026-01-01T00:00:01+00:00",
                    counts="{}",
                ),
            ]
        )
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app, headers=headers) as client:
        states = {item["status"]: item for item in client.get("/api/v1/builds").json()}
        assert states["failed"]["error"] == "Build interrupted by application shutdown"
        assert "cancelled" in states["cancelled"]["error"].lower()
        assert "restarted" in states["cancelled"]["error"].lower()
        assert is_utc(states["failed"]["finished_at"])
        assert is_utc(states["cancelled"]["finished_at"])
        assert called == []


def test_build_records_stage_logs_stats_and_utc_times(tmp_path, monkeypatch):
    from semantica_dataagent import knowledge, relationships, stages
    from semantica_dataagent.app import app
    from semantica_dataagent.stage_contract import StageResult, StorageHandles

    token = configure(tmp_path, monkeypatch)
    monkeypatch.setattr(
        knowledge,
        "settings",
        lambda: {
            "source_directory": str(tmp_path),
            "knowledge_generation": "test",
            "embedding_model": "test",
            "embedding_url": "http://localhost",
            "neo4j_uri": "bolt://localhost",
            "neo4j_user": "neo4j",
            "milvus_uri": "http://localhost",
        },
    )
    monkeypatch.setattr(knowledge, "configured", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        knowledge,
        "get_filters",
        lambda: {
            "database_include": [],
            "database_exclude": [],
            "task_include": [],
            "task_exclude": [],
        },
    )
    monkeypatch.setattr(
        knowledge,
        "build_storage",
        lambda values, stats=None: nullcontext(
            StorageHandles("test", None, None, values)
        ),
    )
    monkeypatch.setattr(knowledge, "read_graph", lambda values: None)
    monkeypatch.setattr(knowledge, "read_vectors", lambda values: None)
    monkeypatch.setattr(relationships, "reconcile", lambda *args: None)

    def fake_stage(records, generation, storage):
        return StageResult(counts={"table": 1})

    monkeypatch.setattr(stages, "STAGES", [fake_stage])
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app, headers=headers) as client:
        created = client.post("/api/v1/builds", json={"build_type": "full"})
        assert created.status_code == 202
        build = wait_for_build(client, created.json()["id"])
        assert build["status"] == "succeeded", build
        assert build["counts"]["table"] == 1
        stages_logged = {entry["stage"] for entry in build["logs"]}
        assert {"queue", "build", "source", "plan", "test_build_queue"} <= stages_logged
        assert any("Completed in" in entry["message"] for entry in build["logs"])
        assert any(entry["message"] == "Build succeeded" for entry in build["logs"])
        assert all(is_utc(entry["timestamp"]) for entry in build["logs"])
        assert is_utc(build["started_at"])
        assert is_utc(build["finished_at"])


def test_queue_retries_after_a_transient_claim_failure(tmp_path, monkeypatch):
    from semantica_dataagent import knowledge
    from semantica_dataagent.build_queue import BuildQueue
    from semantica_dataagent.db import Build, sessions

    configure(tmp_path, monkeypatch)
    executed = []
    monkeypatch.setattr(knowledge, "run_build", executed.append)
    queue = BuildQueue()
    claim = queue._claim
    attempts = []

    def flaky_claim():
        if not attempts:
            attempts.append("failed")
            raise RuntimeError("transient database lock")
        attempts.append("retried")
        return claim()

    queue._claim = flaky_claim
    with sessions().begin() as db:
        row = Build(
            status="queued", started_at="2026-01-01T00:00:00+00:00", counts="{}"
        )
        db.add(row)
        db.flush()
        build_id = row.id
    queue.start()
    try:
        queue.enqueue()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with sessions()() as db:
                if db.get(Build, build_id).status == "running":
                    break
            time.sleep(0.02)
        else:
            raise AssertionError("Queue worker stopped after a claim failure")
    finally:
        queue.stop()
    assert executed == [build_id]
    assert attempts[:2] == ["failed", "retried"]


@pytest.mark.parametrize("crash_before_commit", [True, False])
def test_full_build_publication_survives_process_exit(
    tmp_path, monkeypatch, crash_before_commit
):
    import json
    import os

    from semantica_dataagent import knowledge, relationships, stages
    from semantica_dataagent.db import Build, GenerationPointer, sessions
    from semantica_dataagent.stage_contract import StorageHandles

    configure(tmp_path, monkeypatch)
    monkeypatch.setattr(
        knowledge,
        "settings",
        lambda: {
            "source_directory": str(tmp_path),
            "knowledge_generation": "old",
            "embedding_model": "test",
            "embedding_url": "http://localhost",
            "neo4j_uri": "bolt://localhost",
            "neo4j_user": "neo4j",
            "milvus_uri": "http://localhost",
        },
    )
    monkeypatch.setattr(knowledge, "configured", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        knowledge,
        "get_filters",
        lambda: {
            "database_include": [],
            "database_exclude": [],
            "task_include": [],
            "task_exclude": [],
        },
    )
    monkeypatch.setattr(knowledge, "read_sources", lambda *args: ([], {}, None, {}))
    monkeypatch.setattr(
        knowledge,
        "build_storage",
        lambda values, stats=None: nullcontext(
            StorageHandles("new", None, None, values)
        ),
    )
    monkeypatch.setattr(knowledge, "read_graph", lambda values: None)
    monkeypatch.setattr(knowledge, "read_vectors", lambda values: None)
    monkeypatch.setattr(relationships, "reconcile", lambda *args: None)
    monkeypatch.setattr(stages, "STAGES", [])
    monkeypatch.setattr(knowledge, "prune_generations", lambda values: os._exit(0))
    with sessions().begin() as db:
        db.add(GenerationPointer(id=1, generation="old"))
        build = Build(
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
        if crash_before_commit:
            from sqlalchemy import event
            from sqlalchemy.orm import Session

            @event.listens_for(Session, "before_flush")
            def crash_before_publication(session, *_):
                if any(
                    isinstance(row, GenerationPointer) and row.generation != "old"
                    for row in session.dirty
                ):
                    os._exit(0)

        knowledge.run_build(build_id)
        os._exit(1)
    _, result = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(result) == 0
    with sessions()() as db:
        pointer = db.get(GenerationPointer, 1)
        build = db.get(Build, build_id)
        if crash_before_commit:
            assert pointer.generation == "old"
            assert build.status == "running"
        else:
            assert pointer.generation == build.generation
            assert build.status == "succeeded", (
                "current generation was published without recording success"
            )
            assert build.finished_at
            assert json.loads(build.counts)["added"] == 0
