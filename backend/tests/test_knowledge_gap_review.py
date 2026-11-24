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
            "caliber": "合" * 5000,
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


def test_upgrade_preserves_historical_gaps_as_pending(tmp_path, monkeypatch):
    import sqlite3

    from alembic import command
    from alembic.script import ScriptDirectory
    from cryptography.fernet import Fernet
    from test_cli_migrate import _config

    from semantica_dataagent import cli

    path = tmp_path / "old.sqlite"
    config = _config(path)
    command.upgrade(config, "0041")
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(path))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO knowledge_gaps (id, project_id, owner_id, question, missing, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("old", "default", "owner", "question", "missing", "2026-01-01 00:00:00"),
        )
        db.execute(
            "INSERT INTO tokens (digest, name, role, id, created_at) VALUES ('digest', 'name', 'user', 'owner', '2026-01-01')"
        )
        db.execute(
            "INSERT INTO project_members (project_id, token_id, role) VALUES ('default', 'owner', 'member')"
        )
        db.execute(
            "INSERT INTO conversations (id, owner_id, title, agent, provider_id, model, created_at, updated_at) VALUES ('c', 'owner', 'title', 'agent', 'provider', 'model', '2026-01-01', '2026-01-01')"
        )
        db.execute(
            "INSERT INTO correction_proposals (id, owner_id, target, patch, original, diff, reason, status, created_at) VALUES ('p', 'owner', 'target', 'patch', 'original', 'diff', 'reason', 'pending', '2026-01-01')"
        )
        db.execute(
            "INSERT INTO builds (id, status, started_at, counts) VALUES (123, 'succeeded', '2026-01-01', '{}')"
        )
        db.execute("INSERT INTO settings (key, value) VALUES ('synthetic', 'value')")
        db.execute("INSERT INTO knowledge_generations (name) VALUES ('synthetic')")
        db.execute(
            "INSERT INTO generation_pointer (id, generation) VALUES (1, 'synthetic')"
        )
        tables = (
            "conversations",
            "tokens",
            "correction_proposals",
            "builds",
            "settings",
            "knowledge_generations",
            "generation_pointer",
            "projects",
            "project_members",
        )
        columns = {
            table: [
                row[1]
                for row in db.execute(f"PRAGMA table_info({table})")
                if not (table == "knowledge_generations" and row[1] == "is_candidate")
            ]
            for table in tables
        }
        before = {
            table: db.execute(
                f"SELECT {', '.join(columns[table])} FROM {table} ORDER BY rowid"
            ).fetchall()
            for table in tables
        }
    assert cli.main(["migrate"]) == 0
    assert ScriptDirectory.from_config(config).get_heads() == ["0048"]
    backups = list(tmp_path.glob("old.sqlite.bak-*"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0041",
        )
    with sqlite3.connect(path) as db:
        assert {
            table: db.execute(
                f"SELECT {', '.join(columns[table])} FROM {table} ORDER BY rowid"
            ).fetchall()
            for table in tables
        } == before
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


def test_reported_gap_starts_pending(tmp_path, monkeypatch):
    from semantica_dataagent.auth import Requester
    from semantica_dataagent.db import KnowledgeGap, sessions
    from semantica_dataagent.gaps import report_knowledge_gap
    from semantica_dataagent.mcp_context import bind_mcp_context

    configure(tmp_path, monkeypatch)
    with bind_mcp_context(Requester(id="reporter", role="user", token="synthetic")):
        gap_id = report_knowledge_gap("question", "missing")["gap_id"]
    with sessions()() as db:
        gap = db.get(KnowledgeGap, gap_id)
        assert gap.status == "pending"
        assert gap.reviewed_at is None
