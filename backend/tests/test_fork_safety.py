"""Forked children must not inherit process-global locks held by other threads (#231)."""

import os
import signal
import threading
import time

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
