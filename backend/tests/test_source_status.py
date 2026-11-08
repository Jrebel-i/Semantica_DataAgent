"""Source status states compare the last successful build with the repository HEAD."""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


def configure(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    from semantica_dataagent.cli import initialize

    return initialize()


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def pause_build_queue(monkeypatch):
    from semantica_dataagent.build_queue import build_queue

    monkeypatch.setattr(build_queue, "start", lambda: None)
    monkeypatch.setattr(build_queue, "stop", lambda timeout=5: None)


def insert_build(project_id, status, *, error=None, source_commit=None, build_type="incremental"):
    with sqlite3.connect(os.environ["DATAAGENT_DB_PATH"]) as db:
        cursor = db.execute(
            "INSERT INTO builds "
            "(project_id, status, started_at, counts, build_type, triggered_by, "
            "error, source_commit) VALUES (?, ?, ?, '{}', ?, 'upload', ?, ?)",
            (
                project_id,
                status,
                datetime.now(UTC).isoformat(),
                build_type,
                error,
                source_commit,
            ),
        )
        return cursor.lastrowid


def insert_pending(project_id="default", seconds=60):
    due = (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()
    with sqlite3.connect(os.environ["DATAAGENT_DB_PATH"]) as db:
        db.execute(
            "INSERT OR REPLACE INTO project_pending_builds "
            "(project_id, due_at, requested_by, requested_at) VALUES (?, ?, 'u', ?)",
            (project_id, due, datetime.now(UTC).isoformat()),
        )


def status(client, auth, project="default"):
    response = client.get(
        f"/api/v1/projects/{project}/source-status", headers=auth
    )
    assert response.status_code == 200, response.text
    return response.json()


def source_commit(tmp_path, monkeypatch):
    from semantica_dataagent import source_directory

    root = tmp_path / "sources"
    with source_directory.repository(root) as repository:
        return source_directory.upload(
            repository, "knowledge/status", {"a.md": b"a\n"}, False, "tester"
        )


def test_unknown_when_project_has_no_builds(tmp_path, monkeypatch):
    from semantica_dataagent.app import app

    token = configure(tmp_path, monkeypatch)
    pause_build_queue(monkeypatch)
    auth = headers(token)
    with TestClient(app, headers=auth) as client:
        row = status(client, auth)
        assert row["state"] == "unknown"
        assert row["last_build_id"] is None
        assert row["source_commit"] is None
        assert row["auto_build"] is True
        assert row["due_in_seconds"] is None
        assert row["error"] is None


def test_pending_request_reports_countdown(tmp_path, monkeypatch):
    from semantica_dataagent.app import app

    token = configure(tmp_path, monkeypatch)
    pause_build_queue(monkeypatch)
    auth = headers(token)
    with TestClient(app, headers=auth) as client:
        insert_pending(seconds=60)
        row = status(client, auth)
        assert row["state"] == "pending"
        assert 55 <= row["due_in_seconds"] <= 61
        assert row["last_build_id"] is None


def test_queued_incremental_build_is_pending(tmp_path, monkeypatch):
    from semantica_dataagent.app import app

    token = configure(tmp_path, monkeypatch)
    pause_build_queue(monkeypatch)
    auth = headers(token)
    with TestClient(app, headers=auth) as client:
        build_id = insert_build("default", "queued")
        row = status(client, auth)
        assert row["state"] == "pending"
        assert row["last_build_id"] == build_id
        assert row["due_in_seconds"] is None


def test_running_build_is_building(tmp_path, monkeypatch):
    from semantica_dataagent.app import app

    token = configure(tmp_path, monkeypatch)
    pause_build_queue(monkeypatch)
    auth = headers(token)
    with TestClient(app, headers=auth) as client:
        build_id = insert_build("default", "running")
        row = status(client, auth)
        assert row["state"] == "building"
        assert row["last_build_id"] == build_id


def test_failed_build_exposes_a_bounded_error(tmp_path, monkeypatch):
    from semantica_dataagent.app import app

    token = configure(tmp_path, monkeypatch)
    pause_build_queue(monkeypatch)
    auth = headers(token)
    with TestClient(app, headers=auth) as client:
        build_id = insert_build("default", "failed", error="E" * 500)
        row = status(client, auth)
        assert row["state"] == "failed"
        assert row["last_build_id"] == build_id
        assert row["error"].startswith("E")
        assert len(row["error"]) <= 300


def test_effective_compares_last_successful_commit_with_head(tmp_path, monkeypatch):
    from semantica_dataagent.app import app

    token = configure(tmp_path, monkeypatch)
    pause_build_queue(monkeypatch)
    auth = headers(token)
    commit = source_commit(tmp_path, monkeypatch)
    with TestClient(app, headers=auth) as client:
        assert (
            client.put(
                "/api/v1/settings",
                json={"values": {"source_directory": str(tmp_path / "sources")}},
            ).status_code
            == 200
        )
        build_id = insert_build("default", "succeeded", source_commit=commit)
        row = status(client, auth)
        assert row["state"] == "effective"
        assert row["last_build_id"] == build_id
        assert row["source_commit"] == commit

        insert_build("default", "succeeded", source_commit="a" * 40)
        stale = status(client, auth)
        assert stale["state"] == "pending"
        assert stale["source_commit"] == "a" * 40
