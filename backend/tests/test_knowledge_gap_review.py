"""Knowledge gap decisions are project-scoped and final."""

from fastapi.testclient import TestClient
from test_project_corrections import (
    configure,
    project_source_a_and_b,
    proposal_target_evidence,
    upload,
)


def test_review_is_final_and_project_scoped(tmp_path, monkeypatch):
    from semantica_dataagent.app import app
    from semantica_dataagent.db import KnowledgeGap, sessions

    token = configure(tmp_path, monkeypatch)
    a, b = project_source_a_and_b(tmp_path)
    with sessions().begin() as db:
        db.add(
            KnowledgeGap(
                id="gap", project_id=a.id, owner_id="u", question="q", missing="m"
            )
        )
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
        row = client.get("/api/v1/knowledge-gaps", params={"project": a.id}).json()[0]
        assert row["status"] == "pending"
        assert row["can_review"] is True
        url = "/api/v1/knowledge-gaps/gap/review"
        assert (
            client.post(
                url, params={"project": b.id}, json={"status": "confirmed"}
            ).status_code
            == 404
        )
        result = client.post(
            url, params={"project": a.id}, json={"status": "confirmed"}
        )
        assert result.status_code == 200
        assert result.json()["reviewed_at"].endswith("Z")
        assert (
            client.post(
                url, params={"project": a.id}, json={"status": "rejected"}
            ).status_code
            == 409
        )


def test_gap_proposal_requires_confirmed_same_project_and_review(tmp_path, monkeypatch):
    from semantica_dataagent.app import app
    from semantica_dataagent.db import KnowledgeGap, ProjectSetting, sessions

    token = configure(tmp_path, monkeypatch)
    a, b = project_source_a_and_b(tmp_path)
    proposal_target_evidence(monkeypatch)
    with sessions().begin() as db:
        db.add(
            ProjectSetting(
                project_id=a.id, key="correction_require_review", value="false"
            )
        )
        db.add(
            KnowledgeGap(
                id="gap", project_id=a.id, owner_id="u", question="q", missing="m"
            )
        )
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
        upload(client, {}, a.id, f"{a.id}.md", f"before-{a.id}\n")
        body = {
            "target": f"document:metrics/{a.id}.md",
            "patch": "new\n",
            "reason": "fix",
            "knowledge_gap_id": "gap",
        }
        assert (
            client.post(
                "/api/v1/corrections", params={"project": a.id}, json=body
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/corrections", params={"project": b.id}, json=body
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/knowledge-gaps/gap/review",
                params={"project": a.id},
                json={"status": "confirmed"},
            ).status_code
            == 200
        )
        result = client.post("/api/v1/corrections", params={"project": a.id}, json=body)
        assert result.status_code == 201, result.text
        assert result.json()["status"] == "pending"


def test_review_permissions_and_concurrent_decisions(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from semantica_dataagent.app import app
    from semantica_dataagent.db import KnowledgeGap, ProjectMember, sessions

    token = configure(tmp_path, monkeypatch)
    a, b = project_source_a_and_b(tmp_path)
    with sessions().begin() as db:
        db.add(
            KnowledgeGap(
                id="race", project_id=a.id, owner_id="u", question="q", missing="m"
            )
        )
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
        member = client.post(
            "/api/v1/tokens", json={"name": "member", "role": "user"}
        ).json()
        reviewer = client.post(
            "/api/v1/tokens", json={"name": "reviewer", "role": "user"}
        ).json()
        with sessions().begin() as db:
            db.add(ProjectMember(project_id=a.id, token_id=member["id"], role="member"))
            db.add(
                ProjectMember(project_id=a.id, token_id=reviewer["id"], role="admin")
            )
        member_headers = {"Authorization": f"Bearer {member['token']}"}
        reviewer_headers = {"Authorization": f"Bearer {reviewer['token']}"}
        url = "/api/v1/knowledge-gaps/race/review"
        assert (
            client.get(
                "/api/v1/knowledge-gaps",
                params={"project": a.id},
                headers=member_headers,
            ).json()[0]["can_review"]
            is False
        )
        assert (
            client.post(
                url,
                params={"project": a.id},
                headers=member_headers,
                json={"status": "confirmed"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                url,
                params={"project": b.id},
                headers=reviewer_headers,
                json={"status": "confirmed"},
            ).status_code
            == 404
        )

        def decide(status):
            return client.post(
                url,
                params={"project": a.id},
                headers=reviewer_headers,
                json={"status": status},
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(decide, ("confirmed", "rejected")))
        assert sorted(r.status_code for r in results) == [200, 409]
        winner = next(r.json() for r in results if r.status_code == 200)
        assert (
            client.get("/api/v1/knowledge-gaps", params={"project": a.id}).json()[0][
                "status"
            ]
            == winner["status"]
        )
        assert client.post(f"/api/v1/projects/{a.id}/archive").status_code == 200
        assert (
            client.post(
                url, params={"project": a.id}, json={"status": "confirmed"}
            ).status_code
            == 409
        )


def test_upgrade_preserves_historical_gaps_as_pending(tmp_path):
    import sqlite3

    from alembic import command
    from alembic.script import ScriptDirectory
    from test_cli_migrate import _config

    path = tmp_path / "old.sqlite"
    config = _config(path)
    command.upgrade(config, "0041")
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO knowledge_gaps (id, project_id, owner_id, question, missing, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("old", "default", "owner", "question", "missing", "2026-01-01 00:00:00"),
        )
    command.upgrade(config, "head")
    assert ScriptDirectory.from_config(config).get_heads() == ["0042"]
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT id, project_id, owner_id, question, missing, status, reviewed_at, reviewer_id, reviewer_name FROM knowledge_gaps"
        ).fetchone() == (
            "old",
            "default",
            "owner",
            "question",
            "missing",
            "pending",
            None,
            None,
            None,
        )
