"""HTTP acceptance checks for the Skill catalog (#321)."""

import asyncio
import base64
import io
import os
import subprocess
import sys
import threading
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


def test_catalog_routes_require_valid_bearer_credentials(client):
    c, _ = client
    requests = [
        ("GET", "/api/v1/skills", {}),
        ("GET", "/api/v1/skills/available", {}),
        ("GET", "/api/v1/skills/audit", {}),
        ("POST", "/api/v1/skills/preview", {"content": b"invalid"}),
        ("POST", "/api/v1/skills", {"content": b"invalid"}),
        ("PUT", "/api/v1/skills/sample/enabled", {"json": {"enabled": True}}),
        ("DELETE", "/api/v1/skills/sample", {}),
    ]
    for credential in (None, "Bearer wrong", "Basic wrong", "Bearer"):
        headers = {} if credential is None else {"Authorization": credential}
        for method, path, kwargs in requests:
            assert c.request(method, path, headers=headers, **kwargs).status_code == 401


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


def test_member_catalog_does_not_disclose_other_project_ids(client):
    c, token = client
    admin = {"Authorization": f"Bearer {token}"}
    first = c.post(
        "/api/v1/projects",
        headers=admin,
        json={"name": "First Private", "type": "document"},
    ).json()["id"]
    second = c.post(
        "/api/v1/projects",
        headers=admin,
        json={"name": "Second Private", "type": "document"},
    ).json()["id"]
    member = c.post(
        "/api/v1/tokens",
        headers=admin,
        json={
            "name": "member",
            "role": "user",
            "projects": [{"project_id": first, "role": "member"}],
        },
    ).json()
    package = archive(
        {
            "sample/SKILL.md": f"---\nname: sample\ndescription: Sample\nprojects: [{first}, {second}]\n---\nBody\n"
        }
    )
    assert c.post("/api/v1/skills", headers=admin, content=package).status_code == 201
    assert (
        c.put(
            "/api/v1/skills/sample/enabled", headers=admin, json={"enabled": True}
        ).status_code
        == 200
    )
    response = c.get(
        "/api/v1/skills/available",
        headers={"Authorization": f"Bearer {member['token']}"},
        params={"project": first},
    )
    assert response.status_code == 200
    assert second not in response.text
    assert next(item for item in response.json() if item["name"] == "sample")[
        "projects"
    ] == [first]


def test_deeply_nested_yaml_is_rejected(client):
    c, token = client
    package = archive(
        {
            "sample/SKILL.md": "---\nname: sample\ndescription: Sample\nextra: "
            + "[" * 1500
            + "]" * 1500
            + "\n---\nBody\n"
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


def test_deep_archive_path_is_rejected(client):
    c, token = client
    package = archive(
        {
            "sample/SKILL.md": "---\nname: sample\ndescription: Sample\n---\nBody\n",
            "sample/" + "a/" * 100 + "file": "x",
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


@pytest.mark.parametrize("operation", ["validate_package", "_persist_skill"])
def test_slow_upload_does_not_block_other_requests(client, monkeypatch, operation):
    import httpx
    from semantica_dataagent import skill_api

    c, token = client
    entered = threading.Event()
    release = threading.Event()
    original = getattr(skill_api, operation)

    def delayed(*args):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("Upload prevented concurrent requests")
        return original(*args)

    monkeypatch.setattr(skill_api, operation, delayed)
    package = archive(
        {"sample/SKILL.md": "---\nname: sample\ndescription: Sample\n---\nBody\n"}
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=c.app), base_url="http://test"
        ) as web:
            headers = {"Authorization": f"Bearer {token}"}
            task = asyncio.create_task(
                web.post("/api/v1/skills", headers=headers, content=package)
            )
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                response = await asyncio.wait_for(
                    web.get("/api/v1/skill", headers=headers), timeout=3
                )
                assert response.status_code == 200
            finally:
                release.set()
            assert (await task).status_code == 201

    asyncio.run(run())


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


def test_rejects_forged_entry_count_before_zip_parser(client, monkeypatch):
    import struct

    c, token = client
    data = bytearray(archive({f"sample/{index}": "" for index in range(1001)}))
    end = data.rfind(b"PK\x05\x06")
    struct.pack_into("<HH", data, end + 8, 1, 1)

    def unexpected_parser(*args, **kwargs):
        raise AssertionError("Oversized directory reached ZIP parser")

    monkeypatch.setattr(zipfile, "ZipFile", unexpected_parser)
    response = c.post(
        "/api/v1/skills",
        headers={"Authorization": f"Bearer {token}"},
        content=bytes(data),
    )
    assert response.status_code == 422


@pytest.mark.parametrize("trick", ["comment_signature", "zip64_locator"])
def test_rejects_ambiguous_zip_directory_before_parser(client, monkeypatch, trick):
    import struct

    c, token = client
    data = bytearray(
        archive(
            {"sample/SKILL.md": "---\nname: sample\ndescription: Sample\n---\nBody\n"}
        )
    )
    end = data.rfind(b"PK\x05\x06")
    if trick == "comment_signature":
        struct.pack_into("<H", data, end + 20, 4)
        data.extend(b"PK\x05\x06")
    else:
        output = io.BytesIO()
        entry = zipfile.ZipInfo("sample/SKILL.md")
        entry.comment = b"PK\x06\x07" + bytes(16)
        with zipfile.ZipFile(output, "w") as package:
            package.writestr(
                entry, "---\nname: sample\ndescription: Sample\n---\nBody\n"
            )
        data = bytearray(output.getvalue())

    def unexpected_parser(*args, **kwargs):
        raise AssertionError("Ambiguous directory reached ZIP parser")

    monkeypatch.setattr(zipfile, "ZipFile", unexpected_parser)
    response = c.post(
        "/api/v1/skills",
        headers={"Authorization": f"Bearer {token}"},
        content=bytes(data),
    )
    assert response.status_code == 422


def test_rejects_unsupported_zip_version(client):
    c, token = client
    data = bytearray(
        archive(
            {"sample/SKILL.md": "---\nname: sample\ndescription: Sample\n---\nBody\n"}
        )
    )
    central = data.find(b"PK\x01\x02")
    data[central + 6] = 255
    assert (
        c.post(
            "/api/v1/skills",
            headers={"Authorization": f"Bearer {token}"},
            content=bytes(data),
        ).status_code
        == 422
    )


def test_rejects_invalid_utf8_zip_filename(client):
    import struct

    c, token = client
    data = bytearray(
        archive(
            {"sample/SKILL.md": "---\nname: sample\ndescription: Sample\n---\nBody\n"}
        )
    )
    central = data.find(b"PK\x01\x02")
    struct.pack_into("<H", data, central + 8, 0x800)
    data[central + 46] = 0xFF
    assert (
        c.post(
            "/api/v1/skills",
            headers={"Authorization": f"Bearer {token}"},
            content=bytes(data),
        ).status_code
        == 422
    )


def test_rejects_surrogates_without_persisting_partial_skill(client):
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    package = archive(
        {"sample/SKILL.md": '---\nname: sample\ndescription: "\\uD800"\n---\nBody\n'}
    )
    assert c.post("/api/v1/skills", headers=headers, content=package).status_code == 422
    response = c.get("/api/v1/skills", headers=headers)
    assert response.status_code == 200
    assert "sample" not in {item["name"] for item in response.json()}


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


def test_runtime_does_not_reveal_corrupt_skill_to_other_project(client):
    from fastapi import HTTPException
    from semantica_dataagent.auth import Requester
    from semantica_dataagent.db import CatalogSkill, Token, digest, sessions
    from semantica_dataagent.skill_catalog import resolve_skill

    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    project = c.post(
        "/api/v1/projects", headers=headers, json={"name": "Other", "type": "document"}
    ).json()["id"]
    package = archive(
        {
            "sample/SKILL.md": "---\nname: sample\ndescription: Sample\nprojects: [default]\n---\nBody\n"
        }
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
    with sessions()() as db, pytest.raises(HTTPException) as error:
        resolve_skill(db, requester, project, "sample")
    assert error.value.status_code == 404


def test_runtime_rejects_same_name_replacement_that_is_disabled(client):
    from fastapi import HTTPException
    from semantica_dataagent.auth import Requester
    from semantica_dataagent.db import Token, digest, sessions
    from semantica_dataagent.skill_catalog import resolve_skill
    from sqlalchemy import event

    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    make_package = lambda body: archive(
        {"sample/SKILL.md": "---\nname: sample\ndescription: Sample\n---\n" + body}
    )
    assert (
        c.post(
            "/api/v1/skills", headers=headers, content=make_package("old")
        ).status_code
        == 201
    )
    assert (
        c.put(
            "/api/v1/skills/sample/enabled", headers=headers, json={"enabled": True}
        ).status_code
        == 200
    )
    with sessions()() as db:
        owner = db.get(Token, digest(token))
        requester = Requester(id=owner.id, role=owner.role, token=token)
        engine = db.get_bind()
    before_read = threading.Event()
    resume = threading.Event()

    def pause(conn, cursor, statement, parameters, context, executemany):
        if (
            statement.lstrip().upper().startswith("SELECT")
            and "catalog_skills.archive" in statement
        ):
            before_read.set()
            if not resume.wait(10):
                raise RuntimeError("timed out waiting for replacement")

    def lookup():
        with sessions()() as db:
            try:
                return resolve_skill(db, requester, "default", "sample")
            except HTTPException as exc:
                return exc.status_code

    event.listen(engine, "before_cursor_execute", pause)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(lookup)
            try:
                assert before_read.wait(5)
                assert (
                    c.delete("/api/v1/skills/sample", headers=headers).status_code
                    == 204
                )
                assert (
                    c.post(
                        "/api/v1/skills", headers=headers, content=make_package("new")
                    ).status_code
                    == 201
                )
            finally:
                resume.set()
            assert result.result(timeout=10) == 404
    finally:
        event.remove(engine, "before_cursor_execute", pause)


@pytest.mark.parametrize("change, expected", [("revoke", 404), ("archive", 409)])
def test_runtime_rechecks_project_access_at_snapshot(client, change, expected):
    from fastapi import HTTPException
    from semantica_dataagent.auth import Requester
    from semantica_dataagent.db import sessions
    from semantica_dataagent.skill_catalog import resolve_skill
    from sqlalchemy import event

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
    with sessions()() as db:
        engine = db.get_bind()
    before_read, resume = threading.Event(), threading.Event()

    def pause(conn, cursor, statement, parameters, context, executemany):
        if (
            statement.lstrip().upper().startswith("SELECT")
            and "catalog_skills.archive" in statement
        ):
            before_read.set()
            if not resume.wait(10):
                raise RuntimeError("timed out waiting for permission change")

    def lookup():
        with sessions()() as db:
            try:
                resolve_skill(db, requester, "default", "sample")
            except HTTPException as exc:
                return exc.status_code
            return 200

    event.listen(engine, "before_cursor_execute", pause)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(lookup)
            try:
                assert before_read.wait(5)
                if change == "revoke":
                    assert (
                        c.delete(
                            f"/api/v1/tokens/{member['id']}", headers=headers
                        ).status_code
                        == 204
                    )
                else:
                    assert (
                        c.post(
                            "/api/v1/projects/default/archive", headers=headers
                        ).status_code
                        == 200
                    )
            finally:
                resume.set()
            assert result.result(timeout=10) == expected
    finally:
        event.remove(engine, "before_cursor_execute", pause)


def test_available_list_rechecks_revoked_membership_at_query(client):
    from semantica_dataagent.db import sessions
    from sqlalchemy import event

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
    package = archive(
        {"sample/SKILL.md": "---\nname: sample\ndescription: Sample\n---\nBody\n"}
    )
    assert c.post("/api/v1/skills", headers=headers, content=package).status_code == 201
    with sessions()() as db:
        engine = db.get_bind()
    before_read, resume = threading.Event(), threading.Event()

    def pause(conn, cursor, statement, parameters, context, executemany):
        if (
            not before_read.is_set()
            and statement.lstrip().upper().startswith("SELECT")
            and "catalog_skills.metadata_json" in statement
        ):
            before_read.set()
            if not resume.wait(10):
                raise RuntimeError("timed out waiting for permission change")

    event.listen(engine, "before_cursor_execute", pause)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(
                c.get,
                "/api/v1/skills/available",
                headers={"Authorization": f"Bearer {member['token']}"},
            )
            try:
                assert before_read.wait(5)
                assert (
                    c.delete(
                        f"/api/v1/tokens/{member['id']}", headers=headers
                    ).status_code
                    == 204
                )
                assert (
                    c.put(
                        "/api/v1/skills/sample/enabled",
                        headers=headers,
                        json={"enabled": True},
                    ).status_code
                    == 200
                )
            finally:
                resume.set()
            assert result.result(timeout=10).status_code == 404
    finally:
        event.remove(engine, "before_cursor_execute", pause)


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
