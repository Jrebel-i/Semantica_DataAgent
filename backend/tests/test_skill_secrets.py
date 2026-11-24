"""Personal Skill secret behavior through the authenticated API."""

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
