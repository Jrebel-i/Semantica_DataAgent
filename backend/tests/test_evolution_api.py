"""Tests for evolution_scheduler.py and evolution REST endpoints (#296)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


def _setup_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("DATAAGENT_DTB_CONFIG_DIR", str(tmp_path / "dtb"))

    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    admin_token = initialize()
    return TestClient(app), admin_token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_evolution_config_defaults_permissions_and_archived_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    c, admin_token = _setup_client(tmp_path, monkeypatch)
    with c:
        headers = _headers(admin_token)
        # Create project + regular member + project admin
        proj = c.post(
            "/api/v1/projects",
            headers=headers,
            json={"name": "Evo Config Proj", "type": "warehouse"},
        )
        assert proj.status_code == 201
        pid = proj.json()["id"]

        member_tok = c.post(
            "/api/v1/tokens",
            headers=headers,
            json={
                "name": "member-u",
                "role": "user",
                "projects": [{"project_id": pid, "role": "member"}],
            },
        ).json()["token"]
        padmin_tok = c.post(
            "/api/v1/tokens",
            headers=headers,
            json={
                "name": "padmin-u",
                "role": "user",
                "projects": [{"project_id": pid, "role": "admin"}],
            },
        ).json()["token"]

        # Unset DB config -> auto_evolve_enabled MUST default to False!
        cfg_resp = c.get(
            f"/api/v1/projects/{pid}/evolution-config",
            headers=_headers(padmin_tok),
        )
        assert cfg_resp.status_code == 200
        cfg = cfg_resp.json()
        assert cfg["auto_evolve_enabled"] is False
        assert cfg["min_trajectories"] == 30
        assert cfg["min_interval_hours"] == 24
        assert cfg["max_idle_days"] == 7
        assert cfg["max_rounds"] == 3
        assert cfg["max_eval_cases"] == 20
        assert cfg["evolution_provider_id"] is None
        assert cfg["evolution_model"] is None
        assert cfg["judge_provider_id"] is None
        assert cfg["judge_model"] is None
        assert cfg["checkpoint_trajectory_id"] is None
        assert cfg["checkpoint_at"] is None
        assert cfg["pending_trajectories"] == 0

        # Regular member cannot read/write evolution config
        assert (
            c.get(
                f"/api/v1/projects/{pid}/evolution-config",
                headers=_headers(member_tok),
            ).status_code
            == 403
        )
        assert (
            c.put(
                f"/api/v1/projects/{pid}/evolution-config",
                headers=_headers(member_tok),
                json={"auto_evolve_enabled": True},
            ).status_code
            == 403
        )

        # Register provider and update config
        prov = c.post(
            "/api/v1/providers",
            headers=headers,
            json={
                "name": "EvoProv",
                "type": "openai-completions",
                "base_url": "http://127.0.0.1:18080/v1",
                "models": ["evo-m1", "judge-m1"],
            },
        )
        assert prov.status_code == 201
        prov_id = prov.json()["id"]

        upd = c.put(
            f"/api/v1/projects/{pid}/evolution-config",
            headers=_headers(padmin_tok),
            json={
                "auto_evolve_enabled": True,
                "min_trajectories": 12,
                "min_interval_hours": 6,
                "max_idle_days": 3,
                "max_rounds": 4,
                "max_eval_cases": 15,
                "evolution_provider_id": prov_id,
                "evolution_model": "evo-m1",
                "judge_provider_id": prov_id,
                "judge_model": "judge-m1",
            },
        )
        assert upd.status_code == 200
        assert upd.json()["auto_evolve_enabled"] is True
        assert upd.json()["min_trajectories"] == 12
        assert upd.json()["evolution_model"] == "evo-m1"
        assert upd.json()["judge_model"] == "judge-m1"

        # Invalid model for provider -> 422
        assert (
            c.put(
                f"/api/v1/projects/{pid}/evolution-config",
                headers=_headers(padmin_tok),
                json={"evolution_provider_id": prov_id, "evolution_model": "nope"},
            ).status_code
            == 422
        )

        # Archive project -> GET still 200, PUT returns 409, POST run returns 409
        assert (
            c.post(f"/api/v1/projects/{pid}/archive", headers=headers).status_code
            == 200
        )
        assert (
            c.get(
                f"/api/v1/projects/{pid}/evolution-config",
                headers=_headers(padmin_tok),
            ).status_code
            == 200
        )
        assert (
            c.put(
                f"/api/v1/projects/{pid}/evolution-config",
                headers=_headers(padmin_tok),
                json={"auto_evolve_enabled": False},
            ).status_code
            == 409
        )
        assert (
            c.post(
                "/api/v1/evolution/runs",
                params={"project": pid},
                headers=_headers(padmin_tok),
            ).status_code
            == 409
        )


def test_manual_trigger_works_when_auto_disabled_and_rejects_concurrent_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import threading

    c, admin_token = _setup_client(tmp_path, monkeypatch)
    with c:
        from semantica_dataagent import evolution_runner

        headers = _headers(admin_token)
        proj = c.post(
            "/api/v1/projects",
            headers=headers,
            json={"name": "Manual Trigger Proj", "type": "warehouse"},
        )
        pid = proj.json()["id"]

        # Verify auto_evolve_enabled is False by default
        assert (
            c.get(f"/api/v1/projects/{pid}/evolution-config", headers=headers).json()[
                "auto_evolve_enabled"
            ]
            is False
        )

        entered = threading.Event()
        release = threading.Event()

        def fake_execute(run_id: str, **kwargs):
            from semantica_dataagent.db import EvolutionRun, sessions

            entered.set()
            release.wait(5)
            with sessions().begin() as db:
                row = db.get(EvolutionRun, run_id)
                assert row is not None
                row.status = "incomplete"
                row.stop_reason = "no_signals"
                row.finished_at = datetime.now(UTC)

        monkeypatch.setattr(evolution_runner, "execute_evolution_run", fake_execute)

        first = c.post(
            "/api/v1/evolution/runs", params={"project": pid}, headers=headers
        )
        assert first.status_code == 202, first.text
        run_id = first.json()["id"]
        assert first.json()["status"] == "running"
        assert entered.wait(5)

        # Second concurrent run on same project -> 409!
        second = c.post(
            "/api/v1/evolution/runs", params={"project": pid}, headers=headers
        )
        assert second.status_code == 409

        release.set()
        for _ in range(50):
            detail = c.get(
                f"/api/v1/evolution/runs/{run_id}",
                params={"project": pid},
                headers=headers,
            )
            assert detail.status_code == 200
            if detail.json()["status"] != "running":
                break
            time.sleep(0.05)
        assert detail.json()["status"] == "incomplete"

        listed = c.get(
            "/api/v1/evolution/runs", params={"project": pid}, headers=headers
        )
        assert listed.status_code == 200
        assert [r["id"] for r in listed.json()] == [run_id]


def test_scheduler_unset_defaults_zero_history_and_trigger_thresholds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    c, admin_token = _setup_client(tmp_path, monkeypatch)
    with c:
        from semantica_dataagent import evolution_scheduler, trajectories
        from semantica_dataagent.db import EvolutionCheckpoint, sessions

        headers = _headers(admin_token)
        # 1. Unset DB config -> never auto-triggers even with 35 trajectories
        p1 = c.post(
            "/api/v1/projects",
            headers=headers,
            json={"name": "Unset Auto Proj", "type": "warehouse"},
        ).json()["id"]
        now = datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC)
        for i in range(35):
            trajectories.start_online_trajectory(
                project_id=p1,
                conversation_id=f"u-{i}",
                turn_id=i + 1,
                question=f"问题_{i}",
                now=now - timedelta(days=10),
            )
            trajectories.finish_online_trajectory(
                conversation_id=f"u-{i}",
                turn_id=i + 1,
                status="completed",
                answer="ok",
                now=now - timedelta(days=10),
            )
        assert evolution_scheduler.should_trigger_project(p1, now=now) is False

        # 2. Brand-new zero-history project with auto_evolve_enabled=True
        p2 = c.post(
            "/api/v1/projects",
            headers=headers,
            json={"name": "Zero History Proj", "type": "warehouse"},
        ).json()["id"]
        assert (
            c.put(
                f"/api/v1/projects/{p2}/evolution-config",
                headers=headers,
                json={"auto_evolve_enabled": True},
            ).status_code
            == 200
        )
        # 0 trajectories -> False (and no crash!)
        assert evolution_scheduler.should_trigger_project(p2, now=now) is False

        # 5 trajectories on day 0 (< 30 and < 7 days) -> False
        for i in range(5):
            trajectories.start_online_trajectory(
                project_id=p2,
                conversation_id=f"z-{i}",
                turn_id=i + 1,
                question=f"新问题_{i}",
                now=now - timedelta(hours=1),
            )
            trajectories.finish_online_trajectory(
                conversation_id=f"z-{i}",
                turn_id=i + 1,
                status="completed",
                answer="ok",
                now=now - timedelta(hours=1),
            )
        assert evolution_scheduler.should_trigger_project(p2, now=now) is False

        # Reach 30 trajectories on zero-history project -> first trigger fires!
        for i in range(5, 30):
            trajectories.start_online_trajectory(
                project_id=p2,
                conversation_id=f"z-{i}",
                turn_id=i + 1,
                question=f"新问题_{i}",
                now=now - timedelta(minutes=10),
            )
            trajectories.finish_online_trajectory(
                conversation_id=f"z-{i}",
                turn_id=i + 1,
                status="completed",
                answer="ok",
                now=now - timedelta(minutes=10),
            )
        assert evolution_scheduler.should_trigger_project(p2, now=now) is True

        # 3. Existing checkpoint: 30 trajectories within 2h (< 24h) -> False; after 25h -> True; 1 trajectory after 7d -> True
        p3 = c.post(
            "/api/v1/projects",
            headers=headers,
            json={"name": "Checkpoint Proj", "type": "warehouse"},
        ).json()["id"]
        assert (
            c.put(
                f"/api/v1/projects/{p3}/evolution-config",
                headers=headers,
                json={"auto_evolve_enabled": True},
            ).status_code
            == 200
        )
        with sessions().begin() as db:
            db.add(
                EvolutionCheckpoint(
                    project_id=p3,
                    checkpoint_trajectory_id=0,
                    checkpoint_at=now - timedelta(hours=2),
                    updated_at=now - timedelta(hours=2),
                )
            )
        for i in range(30):
            trajectories.start_online_trajectory(
                project_id=p3,
                conversation_id=f"c-{i}",
                turn_id=i + 1,
                question=f"检查点问题_{i}",
                now=now - timedelta(minutes=30),
            )
            trajectories.finish_online_trajectory(
                conversation_id=f"c-{i}",
                turn_id=i + 1,
                status="completed",
                answer="ok",
                now=now - timedelta(minutes=30),
            )
        assert evolution_scheduler.should_trigger_project(p3, now=now) is False
        assert (
            evolution_scheduler.should_trigger_project(
                p3, now=now + timedelta(hours=23)
            )
            is True
        )

        # Idle >= 7 days with >= 1 new trajectory
        p4 = c.post(
            "/api/v1/projects",
            headers=headers,
            json={"name": "Idle Proj", "type": "warehouse"},
        ).json()["id"]
        assert (
            c.put(
                f"/api/v1/projects/{p4}/evolution-config",
                headers=headers,
                json={"auto_evolve_enabled": True},
            ).status_code
            == 200
        )
        with sessions().begin() as db:
            db.add(
                EvolutionCheckpoint(
                    project_id=p4,
                    checkpoint_trajectory_id=0,
                    checkpoint_at=now - timedelta(days=7, minutes=1),
                    updated_at=now - timedelta(days=7, minutes=1),
                )
            )
        assert evolution_scheduler.should_trigger_project(p4, now=now) is False
        trajectories.start_online_trajectory(
            project_id=p4,
            conversation_id="idle-1",
            turn_id=1,
            question="低频问题",
            now=now - timedelta(minutes=5),
        )
        trajectories.finish_online_trajectory(
            conversation_id="idle-1",
            turn_id=1,
            status="completed",
            answer="ok",
            now=now - timedelta(minutes=5),
        )
        assert evolution_scheduler.should_trigger_project(p4, now=now) is True
