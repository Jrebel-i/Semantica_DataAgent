"""Debounced automatic incremental builds after source uploads."""

from __future__ import annotations

import base64
import os
import signal
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import pytest
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


def insert_setting(key, value):
    with sqlite3.connect(os.environ["DATAAGENT_DB_PATH"]) as db:
        db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )


def pending_due(project_id="default"):
    rows = pending_rows(project_id)
    assert len(rows) == 1, rows
    return datetime.fromisoformat(rows[0][1])


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


def test_uploads_in_one_window_create_one_build(tmp_path, monkeypatch):
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
        assert rows[0][1:4] == ("succeeded", "full", "upload")
        time.sleep(1.3)
        assert len(build_rows()) == 1, "one upload window produced several builds"

        assert upload(client, auth, "default", "d.md", "d\n").status_code == 200
        wait_for(lambda: len(finished) == 2, message="next window did not build")
        assert len(build_rows()) == 2
        assert build_rows()[1][1:4] == ("succeeded", "incremental", "upload")


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
        insert_build(a["id"], "succeeded", build_type="full")
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


def create_document_project(client, name):
    response = client.post("/api/v1/projects", json={"name": name, "type": "document"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def configure_upload_window(client, tmp_path):
    response = client.put(
        "/api/v1/settings",
        json={
            "values": {
                "source_directory": str(tmp_path / "sources"),
                "auto_build_debounce_seconds": "0",
            }
        },
    )
    assert response.status_code == 200, response.text


def queued_rows(project_id):
    return [row for row in build_rows(project_id) if row[1] == "queued"]


def test_first_upload_in_new_project_queues_full_build(tmp_path, monkeypatch):
    from semantica_dataagent.app import app

    token = configure(tmp_path, monkeypatch)
    pause_build_queue(monkeypatch)
    auth = headers(token)
    with TestClient(app, headers=auth) as client:
        configure_upload_window(client, tmp_path)
        project_id = create_document_project(client, "首次上传全量")

        assert (
            upload(client, auth, project_id, "first.md", "first\n").status_code == 200
        )
        wait_for(
            lambda: len(queued_rows(project_id)) == 1,
            message="新项目首次上传后没有入队构建",
        )
        assert build_rows(project_id)[0][1:4] == ("queued", "full", "upload")
        assert pending_rows(project_id) == []


def test_failed_full_build_still_queues_full_after_upload(tmp_path, monkeypatch):
    from semantica_dataagent.app import app

    token = configure(tmp_path, monkeypatch)
    pause_build_queue(monkeypatch)
    auth = headers(token)
    with TestClient(app, headers=auth) as client:
        configure_upload_window(client, tmp_path)
        project_id = create_document_project(client, "只有失败全量")
        insert_build(
            project_id,
            "failed",
            build_type="full",
            error="A successful full build is required before incremental builds",
        )

        assert (
            upload(client, auth, project_id, "again.md", "again\n").status_code == 200
        )
        wait_for(
            lambda: len(queued_rows(project_id)) == 1,
            message="失败的全量构建被当成了成功全量",
        )
        assert queued_rows(project_id)[0][2:4] == ("full", "upload")


def test_successful_full_build_keeps_upload_build_incremental(tmp_path, monkeypatch):
    from semantica_dataagent.app import app

    token = configure(tmp_path, monkeypatch)
    pause_build_queue(monkeypatch)
    auth = headers(token)
    with TestClient(app, headers=auth) as client:
        configure_upload_window(client, tmp_path)
        project_id = create_document_project(client, "已有成功全量")
        insert_build(project_id, "succeeded", build_type="full")

        assert upload(client, auth, project_id, "next.md", "next\n").status_code == 200
        wait_for(
            lambda: len(queued_rows(project_id)) == 1,
            message="已有成功全量后上传没有入队",
        )
        assert queued_rows(project_id)[0][2:4] == ("incremental", "upload")


def test_full_build_requirement_is_per_project(tmp_path, monkeypatch):
    from semantica_dataagent.app import app

    token = configure(tmp_path, monkeypatch)
    pause_build_queue(monkeypatch)
    auth = headers(token)
    with TestClient(app, headers=auth) as client:
        configure_upload_window(client, tmp_path)
        built = create_document_project(client, "有全量的项目")
        fresh = create_document_project(client, "没有全量的项目")
        insert_build(built, "succeeded", build_type="full")

        assert upload(client, auth, built, "b.md", "built\n").status_code == 200
        assert upload(client, auth, fresh, "f.md", "fresh\n").status_code == 200
        wait_for(
            lambda: len(queued_rows(built)) == 1 and len(queued_rows(fresh)) == 1,
            message="两个项目没有各自入队",
        )
        assert queued_rows(built)[0][2:4] == ("incremental", "upload")
        assert queued_rows(fresh)[0][2:4] == ("full", "upload")
        assert len(queued_rows(built)) == 1
        assert len(queued_rows(fresh)) == 1


def test_upload_while_first_full_runs_queues_full_followup(tmp_path, monkeypatch):
    from semantica_dataagent.app import app

    token = configure(tmp_path, monkeypatch)
    pause_build_queue(monkeypatch)
    auth = headers(token)
    with TestClient(app, headers=auth) as client:
        configure_upload_window(client, tmp_path)
        project_id = create_document_project(client, "首次全量运行中")
        insert_build(project_id, "running", build_type="full")

        assert upload(client, auth, project_id, "one.md", "one\n").status_code == 200
        wait_for(
            lambda: len(queued_rows(project_id)) == 1,
            message="运行中的首次全量没有获得后续构建",
        )
        assert queued_rows(project_id)[0][2:4] == ("full", "upload"), (
            "首次全量运行时上传排入了注定失败的增量构建"
        )

        assert upload(client, auth, project_id, "two.md", "two\n").status_code == 200
        time.sleep(1.3)
        assert len(queued_rows(project_id)) == 1, "同一项目重复排了后续构建"


def test_auto_build_switch_off_skips_new_project_upload(tmp_path, monkeypatch):
    from semantica_dataagent.app import app

    token = configure(tmp_path, monkeypatch)
    pause_build_queue(monkeypatch)
    auth = headers(token)
    with TestClient(app, headers=auth) as client:
        configure_upload_window(client, tmp_path)
        project_id = create_document_project(client, "关闭自动构建")
        response = client.put(
            f"/api/v1/projects/{project_id}/auto-build", json={"enabled": False}
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"enabled": False}

        assert upload(client, auth, project_id, "off.md", "off\n").status_code == 200
        time.sleep(1.5)
        assert build_rows(project_id) == []
        assert pending_rows(project_id) == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 30.0),
        ("", 30.0),
        ("   ", 30.0),
        ("\n\t", 30.0),
        ("0", 0.0),
        ("5", 5.0),
        ("-3", 0.0),
        ("not-a-number", 30.0),
        ("nan", 30.0),
        ("inf", 30.0),
    ],
)
def test_debounce_seconds_reads_setting_values(tmp_path, monkeypatch, value, expected):
    from semantica_dataagent import auto_build
    from semantica_dataagent.db import sessions

    configure(tmp_path, monkeypatch)
    if value is not None:
        insert_setting(auto_build.DEBOUNCE_KEY, value)
    with sessions()() as db:
        assert auto_build.debounce_seconds(db) == expected


def test_upload_without_debounce_setting_uses_default_window(tmp_path, monkeypatch):
    from semantica_dataagent import auto_build
    from semantica_dataagent.app import app

    token = configure(tmp_path, monkeypatch)
    pause_build_queue(monkeypatch)
    monkeypatch.setattr(auto_build.pending_build_scheduler, "start", lambda: None)
    auth = headers(token)
    with TestClient(app, headers=auth) as client:
        assert (
            client.put(
                "/api/v1/settings",
                json={"values": {"source_directory": str(tmp_path / "sources")}},
            ).status_code
            == 200
        )
        before = datetime.now(UTC)
        assert (
            upload(client, auth, "default", "default.md", "default\n").status_code
            == 200
        )

        due = pending_due("default")
        window = (due - before).total_seconds()
        assert 28 <= window <= 33, f"缺失设置没有使用 30 秒默认窗口: {window}"
        status = client.get("/api/v1/projects/default/source-status").json()
        assert status["state"] == "pending", status
        assert 25 <= status["due_in_seconds"] <= 31, status
        assert build_rows() == []


def test_explicit_zero_debounce_queues_immediately(tmp_path, monkeypatch):
    from semantica_dataagent import auto_build
    from semantica_dataagent.app import app

    token = configure(tmp_path, monkeypatch)
    pause_build_queue(monkeypatch)
    monkeypatch.setattr(auto_build.pending_build_scheduler, "start", lambda: None)
    auth = headers(token)
    with TestClient(app, headers=auth) as client:
        assert (
            client.put(
                "/api/v1/settings",
                json={
                    "values": {
                        "source_directory": str(tmp_path / "sources"),
                        "auto_build_debounce_seconds": "0",
                    }
                },
            ).status_code
            == 200
        )
        before = datetime.now(UTC)
        assert upload(client, auth, "default", "zero.md", "zero\n").status_code == 200

        due = pending_due("default")
        assert (due - before).total_seconds() < 2, due
        assert auto_build.process_due_pending_builds() == ["default"]
        assert len([row for row in build_rows() if row[1] == "queued"]) == 1
        assert pending_rows() == []


def test_explicit_five_second_debounce_is_honoured(tmp_path, monkeypatch):
    from semantica_dataagent import auto_build
    from semantica_dataagent.app import app

    token = configure(tmp_path, monkeypatch)
    pause_build_queue(monkeypatch)
    monkeypatch.setattr(auto_build.pending_build_scheduler, "start", lambda: None)
    auth = headers(token)
    with TestClient(app, headers=auth) as client:
        assert (
            client.put(
                "/api/v1/settings",
                json={
                    "values": {
                        "source_directory": str(tmp_path / "sources"),
                        "auto_build_debounce_seconds": "5",
                    }
                },
            ).status_code
            == 200
        )
        before = datetime.now(UTC)
        assert upload(client, auth, "default", "five.md", "five\n").status_code == 200

        due = pending_due("default")
        assert 4 <= (due - before).total_seconds() <= 6, due
        status = client.get("/api/v1/projects/default/source-status").json()
        assert status["state"] == "pending", status
        assert 4 <= status["due_in_seconds"] <= 5, status


@pytest.mark.parametrize("value", ["", "   ", "\n\t"])
def test_blank_debounce_setting_uses_default_window(tmp_path, monkeypatch, value):
    from semantica_dataagent import auto_build
    from semantica_dataagent.app import app

    token = configure(tmp_path, monkeypatch)
    pause_build_queue(monkeypatch)
    monkeypatch.setattr(auto_build.pending_build_scheduler, "start", lambda: None)
    insert_setting(auto_build.DEBOUNCE_KEY, value)
    auth = headers(token)
    with TestClient(app, headers=auth) as client:
        assert (
            client.put(
                "/api/v1/settings",
                json={"values": {"source_directory": str(tmp_path / "sources")}},
            ).status_code
            == 200
        )
        before = datetime.now(UTC)
        assert upload(client, auth, "default", "blank.md", "blank\n").status_code == 200

        due = pending_due("default")
        assert 28 <= (due - before).total_seconds() <= 33, due
        status = client.get("/api/v1/projects/default/source-status").json()
        assert status["state"] == "pending", status
        assert 25 <= status["due_in_seconds"] <= 31, status


def test_two_uploads_without_setting_share_one_debounce_window(tmp_path, monkeypatch):
    from semantica_dataagent import auto_build
    from semantica_dataagent.app import app

    token = configure(tmp_path, monkeypatch)
    pause_build_queue(monkeypatch)
    monkeypatch.setattr(auto_build.pending_build_scheduler, "start", lambda: None)
    auth = headers(token)
    with TestClient(app, headers=auth) as client:
        assert (
            client.put(
                "/api/v1/settings",
                json={"values": {"source_directory": str(tmp_path / "sources")}},
            ).status_code
            == 200
        )
        assert upload(client, auth, "default", "one.md", "one\n").status_code == 200
        before = datetime.now(UTC)
        assert upload(client, auth, "default", "two.md", "two\n").status_code == 200

        rows = pending_rows("default")
        assert len(rows) == 1, rows
        due = pending_due("default")
        window = (due - before).total_seconds()
        assert 28 <= window <= 33, f"两次上传没有共用一个 30 秒窗口: {window}"

        assert auto_build.process_due_pending_builds(
            due + timedelta(milliseconds=1)
        ) == ["default"]
        assert pending_rows() == []
        queued = [row for row in build_rows() if row[1] == "queued"]
        assert len(queued) == 1, queued
