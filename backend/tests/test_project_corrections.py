"""Corrections and knowledge gaps stay inside their project."""

from __future__ import annotations

import subprocess

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


def configure(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    from semantica_dataagent.cli import initialize

    return initialize()


def upload(client, headers, project, name, content):
    import base64

    response = client.post(
        "/api/v1/source/upload",
        params={"project": project},
        headers=headers,
        json={
            "target": "knowledge/metrics",
            "files": [
                {
                    "path": name,
                    "content_base64": base64.b64encode(content.encode()).decode(),
                }
            ],
        },
    )
    assert response.status_code == 200, response.text


def project_source_a_and_b(tmp_path):
    from semantica_dataagent.project_api import create_project_record

    a = create_project_record("Correction A", "document")
    b = create_project_record("Correction B", "document")
    return a, b


def proposal_target_evidence(monkeypatch):
    from semantica_dataagent import knowledge

    def execute(_store, _query, parameters):
        project = parameters["generation"].split("_", 1)[0]
        return [
            {
                "node": {
                    "content": f"before-{project}\n",
                    "source_file": f"knowledge/metrics/{project}.md",
                }
            }
        ]

    monkeypatch.setattr(knowledge, "existing_refs", lambda refs, project_id=None: set(refs))
    monkeypatch.setattr(knowledge, "execute", execute)


def test_approval_writes_only_the_projects_source_and_build(tmp_path, monkeypatch):
    from semantica_dataagent.app import app

    token = configure(tmp_path, monkeypatch)
    a, b = project_source_a_and_b(tmp_path)
    headers = {"Authorization": f"Bearer {token}"}
    proposal_target_evidence(monkeypatch)
    with TestClient(app, headers=headers) as client:
        upload(client, headers, a.id, f"{a.id}.md", f"before-{a.id}\n")
        upload(client, headers, b.id, f"{b.id}.md", f"before-{b.id}\n")
        before_b = subprocess.check_output(
            ["git", "-C", str(tmp_path / "projects" / b.id / "source"), "rev-parse", "HEAD"],
            text=True,
        ).strip()
        created = client.post(
            "/api/v1/corrections",
            params={"project": a.id},
            json={
                "target": f"document:metrics/{a.id}.md",
                "patch": f"after-{a.id}\n",
                "reason": "fix",
            },
        )
        assert created.status_code == 201, created.text
        proposal = created.json()["proposal_id"]
        assert client.get("/api/v1/corrections").json() == []
        listed = client.get("/api/v1/corrections", params={"project": a.id}).json()
        assert [row["id"] for row in listed] == [proposal]
        assert client.get(
            f"/api/v1/corrections/{proposal}", params={"project": b.id}
        ).status_code == 404

        approved = client.post(
            f"/api/v1/corrections/{proposal}/approve", params={"project": a.id}
        )
        assert approved.status_code == 200, approved.text
        root_a = tmp_path / "projects" / a.id / "source"
        root_b = tmp_path / "projects" / b.id / "source"
        assert (root_a / f"knowledge/metrics/{a.id}.md").read_text() == f"after-{a.id}\n"
        assert (root_b / f"knowledge/metrics/{b.id}.md").read_text() == f"before-{b.id}\n"
        assert subprocess.check_output(
            ["git", "-C", str(root_b), "rev-parse", "HEAD"], text=True
        ).strip() == before_b
        builds_a = client.get("/api/v1/builds", params={"project": a.id}).json()
        builds_b = client.get("/api/v1/builds", params={"project": b.id}).json()
        assert any(
            row["build_type"] == "incremental" for row in builds_a
        ), builds_a
        assert builds_b == [], builds_b


def test_only_project_or_system_admins_can_review(tmp_path, monkeypatch):
    from semantica_dataagent.app import app
    from semantica_dataagent.db import ProjectMember, sessions

    token = configure(tmp_path, monkeypatch)
    a, _ = project_source_a_and_b(tmp_path)
    proposal_target_evidence(monkeypatch)
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app, headers=headers) as client:
        upload(client, headers, a.id, f"{a.id}.md", f"before-{a.id}\n")
        created = client.post(
            "/api/v1/corrections",
            params={"project": a.id},
            json={
                "target": f"document:metrics/{a.id}.md",
                "patch": f"after-{a.id}\n",
                "reason": "fix",
            },
        ).json()
        proposal = created["proposal_id"]
        issued = client.post(
            "/api/v1/tokens", json={"name": "project reviewer", "role": "user"}
        ).json()
        with sessions().begin() as db:
            db.add(ProjectMember(project_id=a.id, token_id=issued["id"], role="admin"))
        user_headers = {"Authorization": f"Bearer {issued['token']}"}
        denied = client.post(
            f"/api/v1/corrections/{proposal}/approve",
            params={"project": a.id},
            headers=user_headers,
        )
        assert denied.status_code == 404, denied.text
        default_denied = client.post(
            "/api/v1/corrections/missing/approve", headers=user_headers
        )
        assert default_denied.status_code == 403, default_denied.text


def test_knowledge_gaps_are_listed_per_project(tmp_path, monkeypatch):
    from semantica_dataagent.app import app
    from semantica_dataagent.db import KnowledgeGap, sessions

    token = configure(tmp_path, monkeypatch)
    a, _ = project_source_a_and_b(tmp_path)
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app, headers=headers) as client, sessions().begin() as db:
        db.add(
            KnowledgeGap(
                id="gap-a",
                project_id=a.id,
                owner_id="someone",
                question="project question",
                missing="project answer",
            )
        )
    with TestClient(app, headers=headers) as client:
        assert client.get("/api/v1/knowledge-gaps").json() == []
        rows = client.get(
            "/api/v1/knowledge-gaps", params={"project": a.id}
        ).json()
        assert [row["id"] for row in rows] == ["gap-a"]
