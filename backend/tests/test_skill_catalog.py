"""HTTP acceptance checks for the Skill catalog (#321)."""

import base64
import io
import os
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    token = initialize()
    # Without entering the context, no background services are started.
    yield TestClient(app), token


def test_admin_can_list_protected_builtin_skills(client):
    c, token = client
    response = c.get("/api/v1/skills", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    skills = response.json()
    assert {skill["name"] for skill in skills} >= {
        "dataagent-retrieval",
        "dataagent-document-retrieval",
    }
    assert all(skill["protected"] for skill in skills if skill["source"] == "builtin")


def archive(files):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as package:
        for name, content in files.items():
            package.writestr(name, content)
    return data.getvalue()


def test_admin_uploads_complete_skill_without_overwriting(client):
    c, token = client
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/zip"}
    package = archive(
        {
            "example/SKILL.md": "---\nname: example\ndescription: Example skill\n---\nDo work.\n",
            "example/ref.txt": "context",
        }
    )
    result = c.post("/api/v1/skills", headers=headers, content=package)
    assert result.status_code == 201, result.text
    assert result.json()["name"] == "example"
    assert c.post("/api/v1/skills", headers=headers, content=package).status_code == 409
    listing = c.get("/api/v1/skills", headers=headers).json()
    assert any(
        skill["name"] == "example" and skill["source"] == "upload" for skill in listing
    )


def test_admin_controls_availability_and_builtin_is_protected(client):
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    package = archive(
        {"sample/SKILL.md": "---\nname: sample\ndescription: Sample\n---\nBody\n"}
    )
    assert c.post("/api/v1/skills", headers=headers, content=package).status_code == 201
    assert c.get("/api/v1/skills/available", headers=headers).status_code == 200
    assert "sample" not in {
        item["name"]
        for item in c.get("/api/v1/skills/available", headers=headers).json()
    }
    assert (
        c.put(
            "/api/v1/skills/sample/enabled", headers=headers, json={"enabled": True}
        ).status_code
        == 200
    )
    assert "sample" in {
        item["name"]
        for item in c.get("/api/v1/skills/available", headers=headers).json()
    }
    assert (
        c.delete("/api/v1/skills/dataagent-retrieval", headers=headers).status_code
        == 409
    )
    assert (
        c.put(
            "/api/v1/skills/dataagent-retrieval/enabled",
            headers=headers,
            json={"enabled": False},
        ).status_code
        == 409
    )
    assert c.delete("/api/v1/skills/sample", headers=headers).status_code == 204
    assert "sample" not in {
        item["name"]
        for item in c.get("/api/v1/skills/available", headers=headers).json()
    }


def test_upload_rejects_unsafe_archive_and_non_admin(client):
    c, token = client
    admin = {"Authorization": f"Bearer {token}"}
    member = c.post(
        "/api/v1/tokens", headers=admin, json={"name": "member", "role": "user"}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {member}"}
    good = archive(
        {"sample/SKILL.md": "---\nname: sample\ndescription: Sample\n---\nBody\n"}
    )
    assert c.post("/api/v1/skills", headers=headers, content=good).status_code == 403
    assert c.get("/api/v1/skills", headers=headers).status_code == 403
    bad = archive(
        {
            "sample/SKILL.md": "---\nname: sample\ndescription: Sample\n---\nBody\n",
            "sample/../../escape": "bad",
        }
    )
    assert c.post("/api/v1/skills", headers=admin, content=bad).status_code == 422
    link = zipfile.ZipInfo("sample/link")
    link.create_system = 3
    link.external_attr = 0o120777 << 16
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as package:
        package.writestr(
            "sample/SKILL.md", "---\nname: sample\ndescription: Sample\n---\nBody\n"
        )
        package.writestr(link, "target")
    assert (
        c.post("/api/v1/skills", headers=admin, content=data.getvalue()).status_code
        == 422
    )
    assert (
        c.put(
            "/api/v1/skills/sample/enabled", headers=headers, json={"enabled": True}
        ).status_code
        == 403
    )
    assert c.delete("/api/v1/skills/sample", headers=headers).status_code == 403


def test_skill_available_only_in_declared_projects_and_types(client):
    c, token = client
    admin = {"Authorization": f"Bearer {token}"}
    project = c.post(
        "/api/v1/projects", headers=admin, json={"name": "docs", "type": "document"}
    ).json()["id"]
    other = c.post(
        "/api/v1/projects", headers=admin, json={"name": "other", "type": "document"}
    ).json()["id"]
    package = archive(
        {
            "sample/SKILL.md": f"---\nname: sample\ndescription: Sample\nproject_types: [document]\nprojects: [{project}]\n---\nBody\n"
        }
    )
    assert c.post("/api/v1/skills", headers=admin, content=package).status_code == 201
    assert (
        c.put(
            "/api/v1/skills/sample/enabled", headers=admin, json={"enabled": True}
        ).status_code
        == 200
    )

    def names(project_id):
        return {
            item["name"]
            for item in c.get(
                "/api/v1/skills/available",
                headers=admin,
                params={"project": project_id},
            ).json()
        }

    assert "sample" in names(project)
    assert "sample" not in names(other)
    assert "sample" not in names("default")
    member = c.post(
        "/api/v1/tokens",
        headers=admin,
        json={
            "name": "member",
            "role": "user",
            "projects": [{"project_id": other, "role": "member"}],
        },
    ).json()["token"]
    assert (
        c.get(
            "/api/v1/skills/available",
            headers={"Authorization": f"Bearer {member}"},
            params={"project": project},
        ).status_code
        == 404
    )
    assert (
        c.post(f"/api/v1/projects/{project}/archive", headers=admin).status_code == 200
    )
    assert (
        c.get(
            "/api/v1/skills/available", headers=admin, params={"project": project}
        ).status_code
        == 409
    )


def test_upload_rejects_unknown_project(client):
    c, token = client
    package = archive(
        {
            "sample/SKILL.md": "---\nname: sample\ndescription: Sample\nprojects: [does-not-exist]\n---\nBody\n"
        }
    )
    response = c.post(
        "/api/v1/skills", headers={"Authorization": f"Bearer {token}"}, content=package
    )
    assert response.status_code == 422


def test_rejects_ambiguous_archive_paths_and_metadata(client):
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    ambiguous = archive(
        {
            "sample/SKILL.md": "---\nname: sample\nname: sample\ndescription: Sample\n---\nBody\n"
        }
    )
    assert (
        c.post("/api/v1/skills", headers=headers, content=ambiguous).status_code == 422
    )
    bad = archive(
        {
            "sample/SKILL.md": "---\nname: sample\ndescription: Sample\n---\nBody\n",
            "sample/file": "x",
            "sample/file/nested": "y",
        }
    )
    assert c.post("/api/v1/skills", headers=headers, content=bad).status_code == 422


@pytest.mark.parametrize(
    "path",
    [
        "/outside",
        "sample/../outside",
        "sample/../../outside",
        "sample/\\outside",
        "C:/outside",
        "sample/.hidden/file",
    ],
)
def test_rejects_unsafe_paths(client, path):
    c, token = client
    package = archive(
        {
            "sample/SKILL.md": "---\nname: sample\ndescription: Sample\n---\nBody\n",
            path: "bad",
        }
    )
    assert (
        c.post(
            "/api/v1/skills",
            headers={"Authorization": f"Bearer {token}"},
            content=package,
        ).status_code
        == 422
    )


def test_rejects_large_or_excessive_archive_entries(client):
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr(
            "sample/SKILL.md", "---\nname: sample\ndescription: Sample\n---\nBody\n"
        )
        package.writestr("sample/large", b"x" * (20 * 1024 * 1024 + 1))
    assert (
        c.post("/api/v1/skills", headers=headers, content=data.getvalue()).status_code
        == 422
    )
    many = {f"sample/{index}": "x" for index in range(1000)}
    many["sample/SKILL.md"] = "---\nname: sample\ndescription: Sample\n---\nBody\n"
    assert (
        c.post("/api/v1/skills", headers=headers, content=archive(many)).status_code
        == 422
    )
    assert (
        c.post(
            "/api/v1/skills", headers=headers, content=b"x" * (20 * 1024 * 1024 + 1)
        ).status_code
        == 413
    )


def test_preview_and_audit_do_not_publish_the_package(client):
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    package = archive(
        {
            "sample/SKILL.md": "---\nname: sample\ndescription: Sample\nsecrets: [API_KEY]\ndependencies: [python]\n---\nBody\n"
        }
    )
    preview = c.post("/api/v1/skills/preview", headers=headers, content=package)
    assert preview.status_code == 200
    assert preview.json()["secrets"] == ["API_KEY"]
    assert preview.json()["dependencies"] == ["python"]
    assert "sample" not in {
        item["name"] for item in c.get("/api/v1/skills", headers=headers).json()
    }
    assert c.post("/api/v1/skills", headers=headers, content=package).status_code == 201
    assert (
        c.put(
            "/api/v1/skills/sample/enabled", headers=headers, json={"enabled": True}
        ).status_code
        == 200
    )
    assert c.delete("/api/v1/skills/sample", headers=headers).status_code == 204
    audit = c.get("/api/v1/skills/audit", headers=headers)
    assert audit.status_code == 200
    assert [entry["action"] for entry in audit.json()] == ["delete", "enable", "upload"]
    assert all(entry["skill_name"] == "sample" for entry in audit.json())


def test_runtime_lookup_rechecks_skill_and_project_authority(client):
    from fastapi import HTTPException
    from semantica_dataagent.auth import Requester
    from semantica_dataagent.db import Token, digest, sessions
    from semantica_dataagent.skill_catalog import resolve_skill

    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    package = archive(
        {
            "sample/SKILL.md": "---\nname: sample\ndescription: Sample\nsecrets: [API_KEY]\n---\nBody\n"
        }
    )
    assert c.post("/api/v1/skills", headers=headers, content=package).status_code == 201
    with sessions()() as db:
        admin = db.get(Token, digest(token))
        requester = Requester(id=admin.id, role=admin.role, token=token)
        with pytest.raises(HTTPException):
            resolve_skill(db, requester, "default", "sample")
    assert (
        c.put(
            "/api/v1/skills/sample/enabled", headers=headers, json={"enabled": True}
        ).status_code
        == 200
    )
    with sessions()() as db:
        snapshot = resolve_skill(db, requester, "default", "sample")
        assert dict(snapshot.files)["sample/SKILL.md"].endswith(b"Body\n")
        assert snapshot.secrets == ("API_KEY",)
    assert (
        c.put(
            "/api/v1/skills/sample/enabled", headers=headers, json={"enabled": False}
        ).status_code
        == 200
    )
    with sessions()() as db, pytest.raises(HTTPException):
        resolve_skill(db, requester, "default", "sample")


def test_runtime_lookup_fails_closed_for_corrupt_stored_archive(client):
    from fastapi import HTTPException
    from semantica_dataagent.auth import Requester
    from semantica_dataagent.db import CatalogSkill, Token, digest, sessions
    from semantica_dataagent.skill_catalog import resolve_skill

    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    package = archive(
        {"sample/SKILL.md": "---\nname: sample\ndescription: Sample\n---\nBody\n"}
    )
    assert c.post("/api/v1/skills", headers=headers, content=package).status_code == 201
    assert (
        c.put(
            "/api/v1/skills/sample/enabled", headers=headers, json={"enabled": True}
        ).status_code
        == 200
    )
    with sessions().begin() as db:
        owner = db.get(Token, digest(token))
        requester = Requester(id=owner.id, role=owner.role, token=token)
        db.get(CatalogSkill, "sample").archive = b"damaged"
    with sessions()() as db, pytest.raises(HTTPException):
        resolve_skill(db, requester, "default", "sample")


def test_runtime_lookup_reflects_revocation_and_project_membership(client):
    from fastapi import HTTPException
    from semantica_dataagent.auth import Requester
    from semantica_dataagent.db import sessions
    from semantica_dataagent.skill_catalog import resolve_skill

    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    member = c.post(
        "/api/v1/tokens",
        headers=headers,
        json={
            "name": "member",
            "role": "user",
            "projects": [{"project_id": "default", "role": "member"}],
        },
    ).json()
    requester = Requester(id=member["id"], role="user", token=member["token"])
    with sessions()() as db:
        assert (
            resolve_skill(db, requester, "default", "dataagent-retrieval").source
            == "builtin"
        )
        with pytest.raises(HTTPException):
            resolve_skill(db, requester, "nonexistent", "dataagent-retrieval")
    assert (
        c.delete(f"/api/v1/tokens/{member['id']}", headers=headers).status_code == 204
    )
    with sessions()() as db, pytest.raises(HTTPException):
        resolve_skill(db, requester, "default", "dataagent-retrieval")


def test_concurrent_upload_has_one_winner_and_one_audit_entry(client):
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    package = archive(
        {"sample/SKILL.md": "---\nname: sample\ndescription: Sample\n---\nBody\n"}
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: (
                    c.post(
                        "/api/v1/skills", headers=headers, content=package
                    ).status_code
                ),
                range(2),
            )
        )
    assert sorted(results) == [201, 409]
    assert [
        event["action"]
        for event in c.get("/api/v1/skills/audit", headers=headers).json()
    ] == ["upload"]


def test_crashed_upload_rolls_back_and_can_be_retried(client):
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    package = archive(
        {"sample/SKILL.md": "---\nname: sample\ndescription: Sample\n---\nBody\n"}
    )
    script = """
import base64, os, sys
from sqlalchemy import event
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from semantica_dataagent.app import app
from semantica_dataagent.db import CatalogSkill
@event.listens_for(Session, "after_flush")
def crash(session, context):
    if any(isinstance(item, CatalogSkill) for item in session.new):
        os._exit(73)
TestClient(app).post("/api/v1/skills", headers={"Authorization": "Bearer " + sys.argv[1]}, content=base64.b64decode(sys.argv[2]))
"""
    process = subprocess.run(
        [sys.executable, "-c", script, token, base64.b64encode(package).decode()],
        env=os.environ.copy(),
        timeout=30,
        check=False,
    )
    assert process.returncode == 73
    assert "sample" not in {
        item["name"] for item in c.get("/api/v1/skills", headers=headers).json()
    }
    assert c.get("/api/v1/skills/audit", headers=headers).json() == []
    assert c.post("/api/v1/skills", headers=headers, content=package).status_code == 201


@pytest.mark.parametrize("action", ["enable", "delete"])
def test_crashed_catalog_change_is_atomic(client, action):
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    package = archive(
        {"sample/SKILL.md": "---\nname: sample\ndescription: Sample\n---\nBody\n"}
    )
    assert c.post("/api/v1/skills", headers=headers, content=package).status_code == 201
    script = """
import os, sys
from sqlalchemy import event
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from semantica_dataagent.app import app
from semantica_dataagent.db import SkillAudit
@event.listens_for(Session, "after_flush")
def crash(session, context):
    if any(isinstance(item, SkillAudit) for item in session.new):
        os._exit(73)
client = TestClient(app)
headers = {"Authorization": "Bearer " + sys.argv[1]}
if sys.argv[2] == "enable":
    client.put("/api/v1/skills/sample/enabled", headers=headers, json={"enabled": True})
else:
    client.delete("/api/v1/skills/sample", headers=headers)
"""
    process = subprocess.run(
        [sys.executable, "-c", script, token, action],
        env=os.environ.copy(),
        timeout=30,
        check=False,
    )
    assert process.returncode == 73
    skill = next(
        item
        for item in c.get("/api/v1/skills", headers=headers).json()
        if item["name"] == "sample"
    )
    assert skill["enabled"] is False
    assert [
        event["action"]
        for event in c.get("/api/v1/skills/audit", headers=headers).json()
    ] == ["upload"]
