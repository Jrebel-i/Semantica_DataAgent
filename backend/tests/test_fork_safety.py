"""Forked children must not inherit process-global locks held by other threads (#231)."""

import os
import signal
import threading
import time

import pytest
from cryptography.fernet import Fernet

CHILD_TIMEOUT_SECONDS = 5


def _configure(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from semantica_dataagent.cli import initialize

    initialize()


def _hold_in_thread(lock):
    held = threading.Event()
    release = threading.Event()

    def hold():
        with lock:
            held.set()
            release.wait(10)

    thread = threading.Thread(target=hold, name="lock-holder", daemon=True)
    thread.start()
    assert held.wait(5)
    return thread, release


def _wait_for_child(pid):
    """Reap the child, aborting it after a timeout so faulthandler prints its stack."""
    deadline = time.monotonic() + CHILD_TIMEOUT_SECONDS
    while True:
        reaped, status = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return status
        if time.monotonic() >= deadline:
            os.kill(pid, signal.SIGABRT)
            _, status = os.waitpid(pid, 0)
            return status
        time.sleep(0.02)


def _fork_and_wait(lock, action):
    thread, release = _hold_in_thread(lock)
    try:
        pid = os.fork()
        if pid == 0:
            try:
                action()
            except BaseException:  # noqa: BLE001 - the parent sees the exit code
                os._exit(2)
            os._exit(0)
        status = _wait_for_child(pid)
    finally:
        release.set()
        thread.join(5)
    assert os.waitstatus_to_exitcode(status) == 0, (
        "forked child deadlocked on a lock inherited from the parent process"
    )


def _query():
    from sqlalchemy import text

    from semantica_dataagent.db import sessions

    with sessions().begin() as db:
        db.execute(text("SELECT 1"))


def test_forked_child_does_not_inherit_database_factory_lock(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    from semantica_dataagent import db

    _fork_and_wait(db._factory_lock, _query)


def test_forked_child_does_not_inherit_connection_pool_lock(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    from semantica_dataagent import db

    engine = db.sessions().kw["bind"]
    _fork_and_wait(engine.pool._pool.mutex, _query)


def test_forked_child_does_not_inherit_knowledge_locks(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    from semantica_dataagent import knowledge

    _fork_and_wait(knowledge._round_trip_lock, knowledge.round_trip_counts)


def _embedding_slot():
    from semantica_dataagent.embedding_client import _LIMITER

    with _LIMITER.slot(1):
        pass


def _project_deletion_lock():
    from semantica_dataagent import project_deletion

    with project_deletion._lock("fork-test"):
        pass


def _pull_lock():
    from semantica_dataagent import pull_service

    with pull_service._lock_for("fork-test"):
        pass


def _telemetry_lock():
    from semantica_dataagent import telemetry

    with telemetry._lock:
        pass


def _provider_probe_lock():
    from semantica_dataagent import provider_probe

    provider_probe.begin_probe("fork-test")
    provider_probe.end_probe("fork-test")


def _sandbox_lock():
    from semantica_dataagent import agent_sandbox

    with agent_sandbox.ACTIVE_LOCK:
        pass


@pytest.mark.parametrize(
    "name",
    [
        "build_queue",
        "pending_builds",
        "scheduler",
        "pull_scheduler",
        "embedding_limiter",
        "project_deletion",
        "pull_service",
        "telemetry",
        "provider_probe",
        "agent_sandbox",
    ],
)
def test_forked_child_resets_other_process_global_locks(name):
    if name == "build_queue":
        from semantica_dataagent import build_queue

        lock, action = (
            build_queue.build_queue._condition,
            build_queue.build_queue.enqueue,
        )
    elif name == "pending_builds":
        from semantica_dataagent import auto_build

        lock = auto_build.pending_build_scheduler._condition
        action = auto_build.pending_build_scheduler.wake
    elif name == "scheduler":
        from semantica_dataagent import scheduler

        lock, action = scheduler.scheduler._condition, scheduler.scheduler.due_projects
    elif name == "pull_scheduler":
        from semantica_dataagent import scheduler

        lock = scheduler.pull_scheduler._condition
        action = scheduler.pull_scheduler.due_projects
    elif name == "embedding_limiter":
        from semantica_dataagent.embedding_client import _LIMITER

        lock, action = _LIMITER._condition, _embedding_slot
    elif name == "project_deletion":
        from semantica_dataagent import project_deletion

        lock, action = project_deletion._locks_guard, _project_deletion_lock
    elif name == "pull_service":
        from semantica_dataagent import pull_service

        lock, action = pull_service._locks_guard, _pull_lock
    elif name == "telemetry":
        from semantica_dataagent import telemetry

        lock, action = telemetry._lock, _telemetry_lock
    elif name == "provider_probe":
        from semantica_dataagent import provider_probe

        lock, action = provider_probe._active_lock, _provider_probe_lock
    else:
        from semantica_dataagent import agent_sandbox

        lock, action = agent_sandbox.ACTIVE_LOCK, _sandbox_lock
    _fork_and_wait(lock, action)
