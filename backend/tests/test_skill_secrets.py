"""Personal Skill secret behavior through the authenticated API."""

import pytest
from test_skill_catalog import archive

pytest_plugins = ("test_skill_catalog",)


def test_personal_secret_is_write_only_and_can_be_replaced_and_deleted(client):
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    package = archive(
        {
            "sample/SKILL.md": "---\nname: sample\ndescription: Sample\nsecrets: [API_KEY]\n---\nBody\n"
        }
    )
    assert c.post("/api/v1/skills", headers=headers, content=package).status_code == 201
    assert (
        c.put(
            "/api/v1/skills/sample/enabled", headers=headers, json={"enabled": True}
        ).status_code
        == 200
    )
    path = "/api/v1/skills/sample/secrets/API_KEY?project=default"
    assert c.get(
        "/api/v1/skills/sample/secrets?project=default", headers=headers
    ).json() == {"API_KEY": False}
    for value in ("synthetic-first-secret", "synthetic-replacement-secret"):
        response = c.put(path, headers=headers, json={"value": value})
        assert response.status_code == 204
        assert value not in response.text
        response = c.get(
            "/api/v1/skills/sample/secrets?project=default", headers=headers
        )
        assert response.json() == {"API_KEY": True}
        assert value not in response.text
    assert c.delete(path, headers=headers).status_code == 204
    assert c.get(
        "/api/v1/skills/sample/secrets?project=default", headers=headers
    ).json() == {"API_KEY": False}


def test_runtime_requires_personal_secrets_and_clears_scope(client):
    import pytest
    from fastapi import HTTPException

    from semantica_dataagent.auth import Requester
    from semantica_dataagent.db import Token, digest, sessions
    from semantica_dataagent.skill_secrets import skill_run_secrets

    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    package = archive(
        {
            "sample/SKILL.md": "---\nname: sample\ndescription: Sample\nsecrets: [API_KEY]\n---\nBody\n"
        }
    )
    assert c.post("/api/v1/skills", headers=headers, content=package).status_code == 201
    assert (
        c.put(
            "/api/v1/skills/sample/enabled", headers=headers, json={"enabled": True}
        ).status_code
        == 200
    )
    with sessions()() as db:
        actor = db.get(Token, digest(token))
        requester = Requester(id=actor.id, role=actor.role, token=token)
    with (
        pytest.raises(HTTPException) as missing,
        skill_run_secrets(requester, "default", "sample"),
    ):
        pass
    assert missing.value.status_code == 409
    assert (
        c.put(
            "/api/v1/skills/sample/secrets/API_KEY?project=default",
            headers=headers,
            json={"value": "synthetic-run-secret"},
        ).status_code
        == 204
    )
    with skill_run_secrets(requester, "default", "sample") as (snapshot, values):
        assert snapshot.name == "sample"
        assert values == {"API_KEY": "synthetic-run-secret"}
    assert values == {}
    assert (
        c.put(
            "/api/v1/skills/sample/enabled", headers=headers, json={"enabled": False}
        ).status_code
        == 200
    )
    with (
        pytest.raises(HTTPException) as disabled,
        skill_run_secrets(requester, "default", "sample"),
    ):
        pass
    assert disabled.value.status_code == 404


def test_secrets_are_isolated_by_user_and_project(client):
    import pytest
    from fastapi import HTTPException

    from semantica_dataagent.auth import Requester
    from semantica_dataagent.skill_secrets import skill_run_secrets

    c, token = client
    admin = {"Authorization": f"Bearer {token}"}
    project = c.post(
        "/api/v1/projects", headers=admin, json={"name": "Second", "type": "warehouse"}
    ).json()["id"]
    member = c.post(
        "/api/v1/tokens",
        headers=admin,
        json={
            "name": "Member",
            "role": "user",
            "projects": [
                {"project_id": "default", "role": "member"},
                {"project_id": project, "role": "member"},
            ],
        },
    ).json()
    other = c.post(
        "/api/v1/tokens", headers=admin, json={"name": "Other", "role": "user"}
    ).json()
    package = archive(
        {
            "sample/SKILL.md": "---\nname: sample\ndescription: Sample\nsecrets: [API_KEY]\n---\nBody\n"
        }
    )
    assert c.post("/api/v1/skills", headers=admin, content=package).status_code == 201
    assert (
        c.put(
            "/api/v1/skills/sample/enabled", headers=admin, json={"enabled": True}
        ).status_code
        == 200
    )
    own = {"Authorization": f"Bearer {member['token']}"}
    peer = {"Authorization": f"Bearer {other['token']}"}
    assert (
        c.put(
            "/api/v1/skills/sample/secrets/API_KEY?project=default",
            headers=own,
            json={"value": "synthetic-private"},
        ).status_code
        == 204
    )
    assert c.get(
        "/api/v1/skills/sample/secrets?project=default", headers=peer
    ).json() == {"API_KEY": False}
    assert c.get(
        "/api/v1/skills/sample/secrets", headers=own, params={"project": project}
    ).json() == {"API_KEY": False}
    requester = Requester(id=member["id"], role="user", token=member["token"])
    with skill_run_secrets(requester, "default", "sample") as (_, values):
        assert values == {"API_KEY": "synthetic-private"}
    with (
        pytest.raises(HTTPException) as missing,
        skill_run_secrets(requester, project, "sample"),
    ):
        pass
    assert missing.value.status_code == 409
    other_requester = Requester(id=other["id"], role="user", token=other["token"])
    with (
        pytest.raises(HTTPException) as other_missing,
        skill_run_secrets(other_requester, "default", "sample"),
    ):
        pass
    assert other_missing.value.status_code == 409
    forged = Requester(id=member["id"], role="user", token=other["token"])
    with (
        pytest.raises(HTTPException) as denied,
        skill_run_secrets(forged, "default", "sample"),
    ):
        pass
    assert denied.value.status_code == 404


def test_deleting_skill_removes_secrets_even_after_reupload(client):
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    package = archive(
        {
            "sample/SKILL.md": "---\nname: sample\ndescription: Sample\nsecrets: [API_KEY]\n---\nBody\n"
        }
    )

    def upload():
        assert (
            c.post("/api/v1/skills", headers=headers, content=package).status_code
            == 201
        )
        assert (
            c.put(
                "/api/v1/skills/sample/enabled", headers=headers, json={"enabled": True}
            ).status_code
            == 200
        )

    upload()
    assert (
        c.put(
            "/api/v1/skills/sample/secrets/API_KEY?project=default",
            headers=headers,
            json={"value": "synthetic-deletion-secret"},
        ).status_code
        == 204
    )
    assert c.delete("/api/v1/skills/sample", headers=headers).status_code == 204
    upload()
    assert c.get(
        "/api/v1/skills/sample/secrets?project=default", headers=headers
    ).json() == {"API_KEY": False}


def test_secret_payloads_are_not_echoed_or_stored_in_plaintext(client, tmp_path):
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    package = archive(
        {
            "sample/SKILL.md": "---\nname: sample\ndescription: Sample\nsecrets: [API_KEY]\n---\nBody\n"
        }
    )
    assert c.post("/api/v1/skills", headers=headers, content=package).status_code == 201
    assert (
        c.put(
            "/api/v1/skills/sample/enabled", headers=headers, json={"enabled": True}
        ).status_code
        == 200
    )
    value = "synthetic-sensitive-value"
    path = "/api/v1/skills/sample/secrets/API_KEY?project=default"
    response = c.put(path, headers=headers, json={"value": value, "unexpected": value})
    assert response.status_code == 422 and value not in response.text
    assert c.put(path, headers=headers, json={"value": value}).status_code == 204
    assert value.encode() not in (tmp_path / "app.db").read_bytes()
    assert value not in c.get("/api/v1/skills/audit", headers=headers).text


def test_crash_during_skill_deletion_rolls_back_secret_and_catalog(client):
    import os
    import subprocess
    import sys

    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    package = archive(
        {
            "sample/SKILL.md": "---\nname: sample\ndescription: Sample\nsecrets: [API_KEY]\n---\nBody\n"
        }
    )
    assert c.post("/api/v1/skills", headers=headers, content=package).status_code == 201
    assert (
        c.put(
            "/api/v1/skills/sample/enabled", headers=headers, json={"enabled": True}
        ).status_code
        == 200
    )
    assert (
        c.put(
            "/api/v1/skills/sample/secrets/API_KEY?project=default",
            headers=headers,
            json={"value": "synthetic-crash-secret"},
        ).status_code
        == 204
    )
    script = """
import os, sys
from sqlalchemy import event
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from semantica_dataagent.app import app
from semantica_dataagent.db import SkillAudit
@event.listens_for(Session, "after_flush")
def crash(session, context):
    if any(isinstance(item, SkillAudit) and item.action == "delete" for item in session.new):
        os._exit(73)
TestClient(app).delete("/api/v1/skills/sample", headers={"Authorization": "Bearer " + sys.argv[1]})
"""
    proc = subprocess.run(
        [sys.executable, "-c", script, token],
        env=os.environ.copy(),
        timeout=30,
        check=False,
    )
    assert proc.returncode == 73
    assert c.get(
        "/api/v1/skills/sample/secrets?project=default", headers=headers
    ).json() == {"API_KEY": True}
    assert c.delete("/api/v1/skills/sample", headers=headers).status_code == 204
    assert c.post("/api/v1/skills", headers=headers, content=package).status_code == 201
    assert (
        c.put(
            "/api/v1/skills/sample/enabled", headers=headers, json={"enabled": True}
        ).status_code
        == 200
    )
    assert c.get(
        "/api/v1/skills/sample/secrets?project=default", headers=headers
    ).json() == {"API_KEY": False}


def test_revocation_blocks_new_runtime_and_context_clears_on_error(client):
    import pytest
    from fastapi import HTTPException

    from semantica_dataagent.auth import Requester
    from semantica_dataagent.skill_secrets import skill_run_secrets

    c, token = client
    admin = {"Authorization": f"Bearer {token}"}
    member = c.post(
        "/api/v1/tokens", headers=admin, json={"name": "Member", "role": "user"}
    ).json()
    headers = {"Authorization": f"Bearer {member['token']}"}
    package = archive(
        {
            "sample/SKILL.md": "---\nname: sample\ndescription: Sample\nsecrets: [API_KEY]\n---\nBody\n"
        }
    )
    assert c.post("/api/v1/skills", headers=admin, content=package).status_code == 201
    assert (
        c.put(
            "/api/v1/skills/sample/enabled", headers=admin, json={"enabled": True}
        ).status_code
        == 200
    )
    assert (
        c.put(
            "/api/v1/skills/sample/secrets/API_KEY?project=default",
            headers=headers,
            json={"value": "synthetic-revocation-secret"},
        ).status_code
        == 204
    )
    requester = Requester(id=member["id"], role="user", token=member["token"])
    with (
        pytest.raises(RuntimeError),
        skill_run_secrets(requester, "default", "sample") as (_, values),
    ):
        assert (
            c.delete(f"/api/v1/tokens/{member['id']}", headers=admin).status_code == 204
        )
        raise RuntimeError("synthetic failure")
    assert values == {}
    with (
        pytest.raises(HTTPException) as denied,
        skill_run_secrets(requester, "default", "sample"),
    ):
        pass
    assert denied.value.status_code == 404


def test_concurrent_secret_writes_and_skill_deletion_do_not_reappear(client):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    package = archive(
        {
            "sample/SKILL.md": "---\nname: sample\ndescription: Sample\nsecrets: [API_KEY]\n---\nBody\n"
        }
    )
    assert c.post("/api/v1/skills", headers=headers, content=package).status_code == 201
    assert (
        c.put(
            "/api/v1/skills/sample/enabled", headers=headers, json={"enabled": True}
        ).status_code
        == 200
    )
    barrier = threading.Barrier(2)

    def write():
        barrier.wait()
        return c.put(
            "/api/v1/skills/sample/secrets/API_KEY?project=default",
            headers=headers,
            json={"value": "synthetic-race-secret"},
        ).status_code

    def remove():
        barrier.wait()
        return c.delete("/api/v1/skills/sample", headers=headers).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(write), pool.submit(remove)
        assert a.result() in (204, 404)
        assert b.result() == 204
    assert c.post("/api/v1/skills", headers=headers, content=package).status_code == 201
    assert (
        c.put(
            "/api/v1/skills/sample/enabled", headers=headers, json={"enabled": True}
        ).status_code
        == 200
    )
    assert c.get(
        "/api/v1/skills/sample/secrets?project=default", headers=headers
    ).json() == {"API_KEY": False}


def test_secret_routes_reject_untrusted_identity_and_undeclared_names(client):
    c, token = client
    admin = {"Authorization": f"Bearer {token}"}
    member = c.post(
        "/api/v1/tokens",
        headers=admin,
        json={"name": "Member", "role": "user", "projects": []},
    ).json()
    package = archive(
        {
            "sample/SKILL.md": "---\nname: sample\ndescription: Sample\nsecrets: [API_KEY]\n---\nBody\n"
        }
    )
    assert c.post("/api/v1/skills", headers=admin, content=package).status_code == 201
    assert (
        c.put(
            "/api/v1/skills/sample/enabled", headers=admin, json={"enabled": True}
        ).status_code
        == 200
    )
    path = "/api/v1/skills/sample/secrets/API_KEY?project=default"
    for authorization in (None, "Bearer wrong", "Basic wrong"):
        headers = {} if authorization is None else {"Authorization": authorization}
        assert (
            c.put(path, headers=headers, json={"value": "synthetic"}).status_code == 401
        )
    member_headers = {"Authorization": f"Bearer {member['token']}"}
    assert (
        c.put(path, headers=member_headers, json={"value": "synthetic"}).status_code
        == 404
    )
    assert (
        c.put(
            "/api/v1/skills/sample/secrets/OTHER?project=default",
            headers=admin,
            json={"value": "synthetic"},
        ).status_code
        == 404
    )
    assert (
        c.delete(
            "/api/v1/skills/sample/secrets/OTHER?project=default", headers=admin
        ).status_code
        == 404
    )


def test_crashed_replacement_retains_prior_secret(client):
    import os
    import subprocess
    import sys

    from semantica_dataagent.auth import Requester
    from semantica_dataagent.db import Token, digest, sessions
    from semantica_dataagent.skill_secrets import skill_run_secrets

    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    package = archive(
        {
            "sample/SKILL.md": "---\nname: sample\ndescription: Sample\nsecrets: [API_KEY]\n---\nBody\n"
        }
    )
    assert c.post("/api/v1/skills", headers=headers, content=package).status_code == 201
    assert (
        c.put(
            "/api/v1/skills/sample/enabled", headers=headers, json={"enabled": True}
        ).status_code
        == 200
    )
    path = "/api/v1/skills/sample/secrets/API_KEY?project=default"
    assert (
        c.put(path, headers=headers, json={"value": "synthetic-original"}).status_code
        == 204
    )
    script = """
import os, sys
from sqlalchemy import event
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from semantica_dataagent.app import app
from semantica_dataagent.db import SkillSecret
@event.listens_for(Session, "after_flush")
def crash(session, context):
    if any(isinstance(item, SkillSecret) for item in session.dirty):
        os._exit(73)
TestClient(app).put("/api/v1/skills/sample/secrets/API_KEY?project=default", headers={"Authorization": "Bearer " + sys.argv[1]}, json={"value": "synthetic-new"})
"""
    proc = subprocess.run(
        [sys.executable, "-c", script, token],
        env=os.environ.copy(),
        timeout=30,
        check=False,
    )
    assert proc.returncode == 73
    with sessions()() as db:
        actor = db.get(Token, digest(token))
        requester = Requester(id=actor.id, role=actor.role, token=token)
    with skill_run_secrets(requester, "default", "sample") as (_, values):
        assert values == {"API_KEY": "synthetic-original"}


@pytest.mark.parametrize("operation", ["status", "put", "delete", "runtime"])
def test_slow_package_validation_does_not_block_writes(client, monkeypatch, operation):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import semantica_dataagent.skill_catalog as catalog
    from semantica_dataagent.auth import Requester
    from semantica_dataagent.db import Token, digest, sessions
    from semantica_dataagent.skill_secrets import skill_run_secrets

    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    package = archive(
        {
            "sample/SKILL.md": "---\nname: sample\ndescription: Sample\nsecrets: [API_KEY]\n---\nBody\n"
        }
    )
    assert c.post("/api/v1/skills", headers=headers, content=package).status_code == 201
    assert (
        c.put(
            "/api/v1/skills/sample/enabled", headers=headers, json={"enabled": True}
        ).status_code
        == 200
    )
    assert (
        c.put(
            "/api/v1/skills/sample/secrets/API_KEY?project=default",
            headers=headers,
            json={"value": "synthetic-value"},
        ).status_code
        == 204
    )
    with sessions()() as db:
        actor = db.get(Token, digest(token))
        requester = Requester(id=actor.id, role=actor.role, token=token)
    started, release = threading.Event(), threading.Event()
    original = catalog.validate_package

    def slow(raw):
        started.set()
        assert release.wait(30)
        return original(raw)

    monkeypatch.setattr(catalog, "validate_package", slow)

    def run():
        path = "/api/v1/skills/sample/secrets/API_KEY?project=default"
        if operation == "status":
            return c.get(
                "/api/v1/skills/sample/secrets?project=default", headers=headers
            ).status_code
        if operation == "put":
            return c.put(
                path, headers=headers, json={"value": "synthetic-next"}
            ).status_code
        if operation == "delete":
            return c.delete(path, headers=headers).status_code
        with skill_run_secrets(requester, "default", "sample"):
            return 200

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(run)
        written = threading.Event()

        def write():
            try:
                return c.put(
                    "/api/v1/skills/sample/enabled",
                    headers=headers,
                    json={"enabled": True},
                ).status_code
            finally:
                written.set()

        try:
            assert started.wait(10)
            other = pool.submit(write)
            assert written.wait(8), "unrelated write blocked during package validation"
            assert other.result() == 200
        finally:
            release.set()
        assert pending.result(timeout=10) in (200, 204)


@pytest.mark.parametrize("operation", ["status", "put", "delete", "runtime"])
@pytest.mark.parametrize("change", ["replace", "disable", "revoke"])
def test_prepared_skill_rechecks_changes_before_secret_access(
    client, monkeypatch, operation, change
):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from fastapi import HTTPException

    import semantica_dataagent.skill_catalog as catalog
    from semantica_dataagent.auth import Requester
    from semantica_dataagent.skill_secrets import skill_run_secrets

    c, token = client
    admin = {"Authorization": f"Bearer {token}"}
    member = c.post(
        "/api/v1/tokens", headers=admin, json={"name": "Member", "role": "user"}
    ).json()
    headers = {"Authorization": f"Bearer {member['token']}"}
    package = archive(
        {
            "sample/SKILL.md": "---\nname: sample\ndescription: Sample\nsecrets: [API_KEY]\n---\nBody\n"
        }
    )
    assert c.post("/api/v1/skills", headers=admin, content=package).status_code == 201
    assert (
        c.put(
            "/api/v1/skills/sample/enabled", headers=admin, json={"enabled": True}
        ).status_code
        == 200
    )
    path = "/api/v1/skills/sample/secrets/API_KEY?project=default"
    assert (
        c.put(path, headers=headers, json={"value": "synthetic-old"}).status_code == 204
    )
    requester = Requester(id=member["id"], role="user", token=member["token"])
    started, release = threading.Event(), threading.Event()
    original = catalog.validate_package
    first = True
    guard = threading.Lock()

    def slow(raw):
        nonlocal first
        with guard:
            pause, first = first, False
        if pause:
            started.set()
            assert release.wait(10)
        return original(raw)

    monkeypatch.setattr(catalog, "validate_package", slow)

    def run():
        if operation == "status":
            response = c.get(
                "/api/v1/skills/sample/secrets?project=default", headers=headers
            )
        elif operation == "put":
            response = c.put(path, headers=headers, json={"value": "synthetic-new"})
        elif operation == "delete":
            response = c.delete(path, headers=headers)
        else:
            try:
                with skill_run_secrets(requester, "default", "sample"):
                    return 200, ""
            except HTTPException as exc:
                return exc.status_code, str(exc.detail)
        return response.status_code, response.text

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(run)
        try:
            assert started.wait(10)
            if change == "replace":
                assert (
                    c.delete("/api/v1/skills/sample", headers=admin).status_code == 204
                )
                assert (
                    c.post("/api/v1/skills", headers=admin, content=package).status_code
                    == 201
                )
                assert (
                    c.put(
                        "/api/v1/skills/sample/enabled",
                        headers=admin,
                        json={"enabled": True},
                    ).status_code
                    == 200
                )
            elif change == "disable":
                assert (
                    c.put(
                        "/api/v1/skills/sample/enabled",
                        headers=admin,
                        json={"enabled": False},
                    ).status_code
                    == 200
                )
            else:
                assert (
                    c.delete(
                        f"/api/v1/tokens/{member['id']}", headers=admin
                    ).status_code
                    == 204
                )
        finally:
            release.set()
        status, body = pending.result(timeout=10)
    assert status == (409 if change == "replace" else 404)
    assert "synthetic-old" not in body and "synthetic-new" not in body
    if change == "replace":
        assert c.get(
            "/api/v1/skills/sample/secrets?project=default", headers=headers
        ).json() == {"API_KEY": False}
    else:
        from semantica_dataagent.crypto import decrypt
        from semantica_dataagent.db import SkillSecret, sessions

        with sessions()() as db:
            row = db.get(SkillSecret, (member["id"], "default", "sample", "API_KEY"))
            assert decrypt(row.ciphertext) == "synthetic-old"


def test_historical_sqlite_backup_retains_decryptable_secret_after_skill_deletion(
    client, tmp_path
):
    import sqlite3

    from semantica_dataagent.crypto import decrypt

    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    package = archive(
        {
            "sample/SKILL.md": "---\nname: sample\ndescription: Sample\nsecrets: [API_KEY]\n---\nBody\n"
        }
    )
    assert c.post("/api/v1/skills", headers=headers, content=package).status_code == 201
    assert (
        c.put(
            "/api/v1/skills/sample/enabled", headers=headers, json={"enabled": True}
        ).status_code
        == 200
    )
    assert (
        c.put(
            "/api/v1/skills/sample/secrets/API_KEY?project=default",
            headers=headers,
            json={"value": "synthetic-backup-proof"},
        ).status_code
        == 204
    )
    backup = tmp_path / "historical.db"
    with (
        sqlite3.connect(tmp_path / "app.db") as source,
        sqlite3.connect(backup) as target,
    ):
        source.backup(target)
    assert c.delete("/api/v1/skills/sample", headers=headers).status_code == 204
    with sqlite3.connect(backup) as db:
        ciphertext = db.execute("SELECT ciphertext FROM skill_secrets").fetchone()[0]
    assert decrypt(ciphertext) == "synthetic-backup-proof"
