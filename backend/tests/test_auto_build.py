"""Debounced automatic incremental builds after source uploads."""

from __future__ import annotations

import base64
import os
import signal
import sqlite3
import time
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


def payload(text):
    return base64.b64encode(text.encode()).decode()


def upload(client, auth, project, path, text, **kwargs):
    return client.post(
        "/api/v1/source/upload",
        headers=auth,
        params={"project": project},
        json={
            "target": f"knowledge/auto/path/{path}",
            "files": [{"path": path, "content_base64": payload(text)}],
            **kwargs,
        },
    )


def pause_build_queue(monkeypatch):
    from semantica_dataagent.build_queue import build_queue

    monkeypatch.setattr(build_queue, "start", lambda: None)
    monkeypatch.setattr(build_queue, "stop", lambda timeout=5: None)


def pending_rows(project_id=None):
    with sqlite3.connect(os.environ["DATAAGENT_DB_PATH"]) as db:
        query = "SELECT project_id, due_at, requested_by FROM project_pending_builds"
        if project_id is None:
            return db.execute(query).fetchall()
        return db.execute(query + " WHERE project_id = ?", (project_id,)).fetchall()


def build_rows(project_id="default"):
    with sqlite3.connect(os.environ["DATAAGENT_DB_PATH"]) as db:
        return db.execute(
            "SELECT id, status, build_type, triggered_by, source_commit "
            "FROM builds WHERE project_id = ? ORDER BY id",
            (project_id,),
        ).fetchall()


def insert_build(
    project_id, status, *, build_type="incremental", triggered_by="upload", error=None
):
    with sqlite3.connect(os.environ["DATAAGENT_DB_PATH"]) as db:
        cursor = db.execute(
            "INSERT INTO builds "
            "(project_id, status, started_at, counts, build_type, triggered_by, error) "
            "VALUES (?, ?, ?, '{}', ?, ?, ?)",
            (
                project_id,
                status,
                datetime.now(UTC).isoformat(),
                build_type,
                triggered_by,
                error,
            ),
        )
        return cursor.lastrowid


def wait_for(predicate, timeout=10, message="condition not reached"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(message)


def test_uploads_in_one_window_create_one_incremental_build(tmp_path, monkeypatch):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import app
    from semantica_dataagent.db import Build, sessions

    token = configure(tmp_path, monkeypatch)
    finished = []

    def run(build_id):
        with sessions().begin() as db:
            row = db.get(Build, build_id)
            row.status = "succeeded"
            row.error = None
            row.finished_at = datetime.now(UTC).isoformat()
        finished.append(build_id)

    monkeypatch.setattr(knowledge, "run_build", run)
    auth = headers(token)
    with TestClient(app, headers=auth) as client:
        root = tmp_path / "sources"
        assert (
            client.put(
                "/api/v1/settings",
                json={
                    "values": {
                        "source_directory": str(root),
                        "auto_build_debounce_seconds": "1",
                    }
                },
            ).status_code
            == 200
        )
        for name in ("a.md", "b.md", "c.md"):
            response = upload(client, auth, "default", name, f"{name}\n")
            assert response.status_code == 200, response.text

        wait_for(lambda: len(finished) == 1, message="debounced build did not run")
        rows = build_rows()
        assert len(rows) == 1
        assert rows[0][1:4] == ("succeeded", "incremental", "upload")
        time.sleep(1.3)
        assert len(build_rows()) == 1, "one upload window produced several builds"

        assert upload(client, auth, "default", "d.md", "d\n").status_code == 200
        wait_for(lambda: len(finished) == 2, message="next window did not build")
        assert len(build_rows()) == 2


def test_auto_build_switch_skips_and_discards_requests(tmp_path, monkeypatch):
    from semantica_dataagent.app import app

    token = configure(tmp_path, monkeypatch)
    pause_build_queue(monkeypatch)
    auth = headers(token)
    with TestClient(app, headers=auth) as client:
        assert (
            client.put(
                "/api/v1/settings",
                json={
                    "values": {
                        "source_directory": str(tmp_path / "sources"),
                        "auto_build_debounce_seconds": "60",
                    }
                },
            ).status_code
            == 200
        )
        assert client.get("/api/v1/projects/default/auto-build").json() == {
            "enabled": True
        }
        assert client.put(
            "/api/v1/projects/default/auto-build", json={"enabled": False}
        ).json() == {"enabled": False}
        assert upload(client, auth, "default", "off.md", "off\n").status_code == 200
        assert pending_rows() == []

        assert client.put(
            "/api/v1/projects/default/auto-build", json={"enabled": True}
        ).json() == {"enabled": True}
        assert upload(client, auth, "default", "on.md", "on\n").status_code == 200
        assert pending_rows("default")
        assert (
            client.put(
                "/api/v1/projects/default/auto-build", json={"enabled": False}
            ).status_code
            == 200
        )
        assert pending_rows() == [], "disabling auto build kept a stale request"


def test_pending_request_survives_sigkill_and_runs_once_after_restart(
    tmp_path, monkeypatch
):
    from semantica_dataagent.app import app

    token = configure(tmp_path, monkeypatch)
    pause_build_queue(monkeypatch)
    due = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    child = os.fork()
    if child == 0:
        with sqlite3.connect(os.environ["DATAAGENT_DB_PATH"]) as db:
            db.execute(
                "INSERT INTO project_pending_builds "
                "(project_id, due_at, requested_by, requested_at) "
                "VALUES ('default', ?, 'uploader', ?)",
                (due, datetime.now(UTC).isoformat()),
            )
            db.commit()
        os.kill(os.getpid(), signal.SIGKILL)
    _, status = os.waitpid(child, 0)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
    assert len(pending_rows("default")) == 1

    with TestClient(app, headers=headers(token)) as client:
        wait_for(
            lambda: len([row for row in build_rows() if row[1] == "queued"]) == 1,
            message="due request was not resumed after restart",
        )
        time.sleep(1.5)
        assert len([row for row in build_rows() if row[1] == "queued"]) == 1
        assert pending_rows() == []
        assert client.get("/api/v1/projects/default/source-status").status_code == 200


def test_sigkill_between_deleting_request_and_enqueue_does_not_lose_it(
    tmp_path, monkeypatch
):
    from semantica_dataagent.app import app

    token = configure(tmp_path, monkeypatch)
    pause_build_queue(monkeypatch)
    due = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(os.environ["DATAAGENT_DB_PATH"]) as db:
        db.execute(
            "INSERT INTO project_pending_builds "
            "(project_id, due_at, requested_by, requested_at) "
            "VALUES ('default', ?, 'uploader', ?)",
            (due, datetime.now(UTC).isoformat()),
        )
        db.commit()

    child = os.fork()
    if child == 0:
        from semantica_dataagent import auto_build, project_context

        project_context.add_queued_build = lambda *args, **kwargs: os.kill(
            os.getpid(), signal.SIGKILL
        )
        auto_build.process_due_pending_builds()
        os._exit(1)
    _, status = os.waitpid(child, 0)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
    assert len(pending_rows("default")) == 1, "uncommitted delete reached the database"
    assert build_rows() == []

    with TestClient(app, headers=headers(token)) as client:
        wait_for(
            lambda: len([row for row in build_rows() if row[1] == "queued"]) == 1,
            message="request was lost between delete and enqueue",
        )
        time.sleep(1.2)
        assert len(build_rows()) == 1
        assert pending_rows() == []
        assert client.get("/api/v1/projects/default/source-status").status_code == 200


def test_running_build_gets_one_followup_and_queued_uploads_merge(
    tmp_path, monkeypatch
):
    from semantica_dataagent.app import app

    token = configure(tmp_path, monkeypatch)
    pause_build_queue(monkeypatch)
    auth = headers(token)
    with TestClient(app, headers=auth) as client:
        root = tmp_path / "sources"
        assert (
            client.put(
                "/api/v1/settings",
                json={
                    "values": {
                        "source_directory": str(root),
                        "auto_build_debounce_seconds": "0",
                    }
                },
            ).status_code
            == 200
        )
        a = client.post(
            "/api/v1/projects", json={"name": "Auto A", "type": "document"}
        ).json()
        b = client.post(
            "/api/v1/projects", json={"name": "Auto B", "type": "document"}
        ).json()
        insert_build(a["id"], "running", build_type="incremental")

        assert upload(client, auth, a["id"], "one.md", "one\n").status_code == 200
        wait_for(
            lambda: (
                len(
                    [
                        row
                        for row in build_rows(a["id"])
                        if row[1] == "queued" and row[2] == "incremental"
                    ]
                )
                == 1
            ),
            message="running build did not get a follow-up",
        )
        assert upload(client, auth, a["id"], "two.md", "two\n").status_code == 200
        time.sleep(1.3)
        assert (
            len(
                [
                    row
                    for row in build_rows(a["id"])
                    if row[1] == "queued" and row[2] == "incremental"
                ]
            )
            == 1
        ), "a second follow-up was queued while one was already waiting"
        assert build_rows(b["id"]) == []
        assert pending_rows(b["id"]) == []
        assert build_rows() == [], "上传到其它项目不应在默认项目排队构建"
        assert pending_rows("default") == [], "上传到其它项目不应登记默认项目请求"


def test_auto_build_permissions_and_project_isolation(tmp_path, monkeypatch):
    from semantica_dataagent.app import app

    token = configure(tmp_path, monkeypatch)
    pause_build_queue(monkeypatch)
    auth = headers(token)
    with TestClient(app, headers=auth) as client:
        project = client.post(
            "/api/v1/projects", json={"name": "Auto Perm", "type": "document"}
        ).json()

        def user(name, role):
            response = client.post(
                "/api/v1/tokens",
                json={
                    "name": name,
                    "role": "user",
                    "projects": [{"project_id": project["id"], "role": role}],
                },
            )
            assert response.status_code == 201, response.text
            return headers(response.json()["token"])

        admin = user("project admin", "admin")
        member = user("project member", "member")
        outsider = headers(
            client.post(
                "/api/v1/tokens",
                json={"name": "outsider", "role": "user", "projects": []},
            ).json()["token"]
        )
        assert (
            client.get(
                f"/api/v1/projects/{project['id']}/auto-build", headers=admin
            ).status_code
            == 200
        )
        assert (
            client.put(
                f"/api/v1/projects/{project['id']}/auto-build",
                headers=admin,
                json={"enabled": False},
            ).status_code
            == 200
        )
        assert (
            client.put(
                f"/api/v1/projects/{project['id']}/auto-build",
                headers=member,
                json={"enabled": True},
            ).status_code
            == 403
        )
        assert (
            client.get(
                f"/api/v1/projects/{project['id']}/source-status", headers=member
            ).status_code
            == 200
        )
        assert (
            client.get(
                f"/api/v1/projects/{project['id']}/source-status", headers=outsider
            ).status_code
            == 404
        )
        assert (
            client.get(
                f"/api/v1/projects/{project['id']}/auto-build", headers=outsider
            ).status_code
            == 404
        )


def test_restart_keeps_queued_upload_builds(tmp_path, monkeypatch):
    from semantica_dataagent.build_queue import recover_interrupted_builds

    configure(tmp_path, monkeypatch)
    upload_id = insert_build("default", "queued", triggered_by="upload")
    scheduler_id = insert_build("default", "queued", triggered_by="scheduler")

    recover_interrupted_builds()

    with sqlite3.connect(os.environ["DATAAGENT_DB_PATH"]) as db:
        statuses = dict(
            db.execute(
                "SELECT id, status FROM builds WHERE id IN (?, ?)",
                (upload_id, scheduler_id),
            ).fetchall()
        )
    assert statuses[upload_id] == "queued"
    assert statuses[scheduler_id] == "cancelled"
