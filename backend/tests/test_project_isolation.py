"""Project-level generation, build, settings and REST isolation (#175)."""

from __future__ import annotations

import base64
import os
import re
import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

DEFAULT = "default"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    token = initialize()
    with TestClient(app) as test_client:
        yield test_client, token, tmp_path


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create_project(client: TestClient, token: str, name: str, kind: str = "document"):
    return client.post(
        "/api/v1/projects",
        headers=headers(token),
        json={"name": name, "type": kind},
    )


def fetch(sql: str, params: tuple = ()) -> list[tuple]:
    with sqlite3.connect(os.environ["DATAAGENT_DB_PATH"]) as db:
        return db.execute(sql, params).fetchall()


def test_create_project_sets_prefix_directory_and_initial_pointer(client):
    c, token, tmp_path = client
    response = create_project(c, token, "  Platform Docs  ")
    assert response.status_code == 201, response.text
    project = response.json()
    project_id = project["id"]
    assert re.fullmatch(r"[a-z][a-z0-9]{0,23}", project_id)
    assert project["name"] == "Platform Docs"
    assert project["type"] == "document"
    assert project["status"] == "active"
    assert project["my_role"] == "admin"
    assert project["created_at"].endswith("Z")

    source = tmp_path / "projects" / project_id / "source"
    assert source.is_dir()
    settings = dict(
        fetch(
            "SELECT key, value FROM project_settings WHERE project_id = ?",
            (project_id,),
        )
    )
    assert settings["source_directory"] == str(source)
    assert settings["generation_prefix"] == project_id + "_"
    assert fetch(
        "SELECT project_id FROM knowledge_generations WHERE name = ?",
        (project_id + "_initial",),
    ) == [(project_id,)]
    assert fetch(
        "SELECT project_id, generation FROM generation_pointer WHERE project_id = ?",
        (project_id,),
    ) == [(project_id, project_id + "_initial")]

    listing = c.get("/api/v1/projects", headers=headers(token)).json()
    assert [item["id"] for item in listing] == [DEFAULT, project_id]

    duplicate = create_project(c, token, "platform docs")
    assert duplicate.status_code == 409, duplicate.text
    assert create_project(c, token, "Bad type", "other").status_code == 422
    assert (
        c.post(
            "/api/v1/projects",
            headers=headers(token),
            json={"name": "Extra", "type": "document", "id": "hacked"},
        ).status_code
        == 422
    )


def test_only_system_admins_can_create_or_see_other_projects(client):
    c, admin_token, _ = client
    project = create_project(c, admin_token, "Ops Docs").json()
    user = c.post(
        "/api/v1/tokens",
        headers=headers(admin_token),
        json={"name": "user", "role": "user"},
    ).json()
    user_headers = headers(user["token"])
    assert (
        c.post(
            "/api/v1/projects",
            headers=user_headers,
            json={"name": "Nope", "type": "document"},
        ).status_code
        == 403
    )
    listing = c.get("/api/v1/projects", headers=user_headers).json()
    assert [item["id"] for item in listing] == [DEFAULT]
    for path in (
        f"/api/v1/search?query=x&project={project['id']}",
        f"/api/v1/tables?project={project['id']}",
        f"/api/v1/categories?project={project['id']}",
        f"/api/v1/document?doc_id=document%3Ax%2Fy.md&project={project['id']}",
        f"/api/v1/builds?project={project['id']}",
        f"/api/v1/generations?project={project['id']}",
    ):
        assert c.get(path, headers=user_headers).status_code == 404, path
    assert (
        c.get(
            f"/api/v1/search?query=x&project={project['id']}",
            headers=headers(admin_token),
        ).status_code
        == 200
    )
    assert (
        c.get(
            "/api/v1/search?query=x&project=missing",
            headers=headers(admin_token),
        ).status_code
        == 404
    )
    project_id = project["id"]
    admin_only = (
        ("post", f"/api/v1/builds?project={project_id}", {"build_type": "full"}),
        (
            "post",
            f"/api/v1/source/upload?project={project_id}",
            {"target": "knowledge", "files": []},
        ),
        ("get", f"/api/v1/source/files?project={project_id}", None),
        (
            "post",
            f"/api/v1/generations/{project_id}_initial/switch?project={project_id}",
            None,
        ),
    )
    for method, path, body in admin_only:
        response = getattr(c, method)(
            path, headers=user_headers, **({"json": body} if body else {})
        )
        assert response.status_code == 404, (path, response.text)


def test_new_project_search_is_empty_before_any_build(client):
    c, token, _ = client
    project = create_project(c, token, "Empty Docs").json()
    response = c.get(
        f"/api/v1/search?query=anything&project={project['id']}",
        headers=headers(token),
    )
    assert response.status_code == 200, response.text
    assert response.json() == []


def test_project_settings_overlay_keeps_default_values(client):
    c, token, _ = client
    project = create_project(c, token, "Overrides").json()
    from semantica_dataagent import project_context
    from semantica_dataagent.db import ProjectSetting, Setting, sessions

    with sessions().begin() as db:
        db.add(Setting(key=project_context.SOURCE_DIRECTORY_KEY, value="/tmp/global"))
        db.add(Setting(key=project_context.GENERATION_RETENTION_KEY, value="3"))
        db.add(Setting(key=project_context.DIMENSION_VALUE_LIMIT_KEY, value="500"))
        db.add(
            ProjectSetting(
                project_id=project["id"],
                key=project_context.GENERATION_RETENTION_KEY,
                value="7",
            )
        )
        db.add(
            ProjectSetting(
                project_id=project["id"],
                key=project_context.DIMENSION_VALUE_LIMIT_KEY,
                value="25",
            )
        )

    default = project_context.current_project()
    scoped = project_context.current_project(project["id"])
    assert default.project_id == DEFAULT
    assert default.generation_retention == 3
    assert default.dimension_value_limit_int == 500
    assert default.source_directory_value == "/tmp/global"
    assert scoped.project_id == project["id"]
    assert scoped.generation_retention == 7
    assert scoped.dimension_value_limit_int == 25
    assert scoped.source_directory_value.endswith(f"/{project['id']}/source")


def test_project_without_source_override_does_not_use_global_directory(client):
    c, token, tmp_path = client
    global_source = tmp_path / "global-source"
    assert (
        c.put(
            "/api/v1/settings",
            headers=headers(token),
            json={"values": {"source_directory": str(global_source)}},
        ).status_code
        == 200
    )
    project = create_project(c, token, "Isolated Source").json()
    from semantica_dataagent import project_context
    from semantica_dataagent.db import ProjectSetting, sessions

    with sessions().begin() as db:
        db.query(ProjectSetting).filter(
            ProjectSetting.project_id == project["id"],
            ProjectSetting.key == project_context.SOURCE_DIRECTORY_KEY,
        ).delete()
    assert project_context.source_directory_value(project["id"]) is None


def test_creation_failure_before_db_writes_removes_empty_root(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    from semantica_dataagent import project_api
    from semantica_dataagent.cli import initialize

    initialize()
    original_mkdir = Path.mkdir

    def fail_staging_mkdir(self, *args, **kwargs):
        if ".creating-" in str(self):
            raise OSError("injected staging failure")
        return original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_staging_mkdir)
    with pytest.raises(OSError):
        project_api.create_project_record("Broken Staging", "document")
    root = tmp_path / "projects"
    assert not root.exists() or list(root.iterdir()) == []


def test_build_queue_records_project_and_lists_are_filtered(client, monkeypatch):
    c, token, _ = client
    project = create_project(c, token, "Build Target").json()["id"]
    from semantica_dataagent import knowledge

    monkeypatch.setattr(knowledge, "run_build", lambda build_id: None)
    response = c.post(
        f"/api/v1/builds?project={project}",
        headers=headers(token),
        json={"build_type": "full"},
    )
    assert response.status_code == 202, response.text
    build = response.json()
    assert build["project_id"] == project
    assert build["status"] in {"queued", "running"}
    assert [
        row["id"]
        for row in c.get(
            f"/api/v1/builds?project={project}", headers=headers(token)
        ).json()
    ] == [build["id"]]
    assert c.get("/api/v1/builds", headers=headers(token)).json() == []
    assert (
        c.get(f"/api/v1/builds/{build['id']}", headers=headers(token)).status_code
        == 404
    )
    assert fetch("SELECT project_id FROM builds WHERE id = ?", (build["id"],)) == [
        (project,)
    ]


def test_source_uploads_are_written_below_the_project_directory(client, monkeypatch):
    c, token, tmp_path = client
    global_source = tmp_path / "global-source"
    assert (
        c.put(
            "/api/v1/settings",
            headers=headers(token),
            json={"values": {"source_directory": str(global_source)}},
        ).status_code
        == 200
    )
    project = create_project(c, token, "Source Target").json()
    body = {
        "target": "knowledge/ops",
        "files": [
            {
                "path": "arch.md",
                "content_base64": base64.b64encode(b"# Arch\n").decode(),
            }
        ],
    }
    response = c.post(
        f"/api/v1/source/upload?project={project['id']}",
        headers=headers(token),
        json=body,
    )
    assert response.status_code == 200, response.text
    project_file = (
        tmp_path
        / "projects"
        / project["id"]
        / "source"
        / "knowledge"
        / "ops"
        / "arch.md"
    )
    assert project_file.read_text() == "# Arch\n"
    assert not (global_source / "knowledge" / "ops" / "arch.md").exists()


def test_creation_failure_removes_rows_and_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    from semantica_dataagent import project_api
    from semantica_dataagent.cli import initialize

    initialize()

    def fail(source, target):
        raise OSError("injected publish failure")

    monkeypatch.setattr(project_api.os, "rename", fail)
    with pytest.raises(OSError):
        project_api.create_project_record("Broken", "document")
    assert fetch("SELECT id FROM projects WHERE id <> 'default'") == []
    assert fetch("SELECT project_id FROM project_settings") == []
    assert (
        fetch("SELECT name FROM knowledge_generations WHERE project_id <> 'default'")
        == []
    )
    assert (
        fetch("SELECT project_id FROM generation_pointer WHERE project_id <> 'default'")
        == []
    )
    root = tmp_path / "projects"
    assert not root.exists() or list(root.iterdir()) == []


@pytest.mark.parametrize("crash_before_commit", [True, False])
def test_project_creation_recovers_after_process_exit(
    tmp_path, monkeypatch, crash_before_commit
):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    from semantica_dataagent.cli import initialize

    initialize()
    pid = os.fork()
    if pid == 0:
        from semantica_dataagent import project_api

        if crash_before_commit:
            from sqlalchemy import event
            from sqlalchemy.orm import Session

            @event.listens_for(Session, "before_commit")
            def crash(session, *_):
                os._exit(0)

        else:
            project_api.os.rename = lambda *_args: os._exit(0)
        project_api.create_project_record("Crash Project", "document")
        os._exit(1)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0

    from semantica_dataagent import project_context

    project_context.recover_projects()
    rows = fetch("SELECT id FROM projects WHERE name = 'Crash Project'")
    root = tmp_path / "projects"
    if crash_before_commit:
        assert rows == []
        assert not root.exists() or list(root.iterdir()) == []
    else:
        assert len(rows) == 1
        project_id = rows[0][0]
        assert (root / project_id / "source").is_dir()
        assert not list(root.glob(".creating-*"))
