"""Tests for TraceSource plugins, project evolution config API, runs API, and Cron scheduler."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar
from zoneinfo import ZoneInfo

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


def test_trace_source_plugins_endpoint_exposes_schema_and_secret_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    c, admin_token = _setup_client(tmp_path, monkeypatch)
    with c:
        resp = c.get("/api/v1/trace-source-plugins", headers=_headers(admin_token))
        assert resp.status_code == 200, resp.text
        items = resp.json()
        lf = next(item for item in items if item["id"] == "langfuse")
        assert lf["display_name"] == "Langfuse"
        assert lf["secret_parameter_keys"] == ["secret_key"]
        assert "base_url" in lf["parameters_schema"]["properties"]


def test_evolution_config_crud_secret_masking_cron_validation_and_archived_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    c, admin_token = _setup_client(tmp_path, monkeypatch)
    with c:
        headers = _headers(admin_token)
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

        # Unset DB config -> defaults
        cfg_resp = c.get(
            f"/api/v1/projects/{pid}/evolution/config",
            headers=_headers(padmin_tok),
        )
        assert cfg_resp.status_code == 200, cfg_resp.text
        cfg = cfg_resp.json()
        assert cfg["enabled"] is False
        assert cfg["trace_source_plugin"] is None
        assert cfg["trace_source_parameters"] == {}
        assert cfg["trace_source_secrets"] == {}
        assert cfg["configured_secrets"] == []
        assert cfg["secret_configured"] is False
        assert cfg["schedule_cron"] is None
        assert cfg["timezone"] == "Asia/Shanghai"
        assert cfg["max_traces_per_run"] == 200
        assert cfg["last_trace_timestamp"] is None
        assert cfg["next_run_at"] is None

        # Regular member cannot read/write evolution config
        assert (
            c.get(
                f"/api/v1/projects/{pid}/evolution/config",
                headers=_headers(member_tok),
            ).status_code
            == 403
        )
        assert (
            c.put(
                f"/api/v1/projects/{pid}/evolution/config",
                headers=_headers(member_tok),
                json={"enabled": True},
            ).status_code
            == 403
        )

        # Register provider
        prov = c.post(
            "/api/v1/providers",
            headers=headers,
            json={
                "name": "EvoProv",
                "type": "openai-completions",
                "base_url": "http://127.0.0.1:18080/v1",
                "models": ["evo-m1"],
            },
        )
        assert prov.status_code == 201
        prov_id = prov.json()["id"]

        # Save Langfuse plugin config + secrets + HH:MM schedule (normalized to 5-field cron)
        upd = c.put(
            f"/api/v1/projects/{pid}/evolution/config",
            headers=_headers(padmin_tok),
            json={
                "enabled": True,
                "trace_source_plugin": "langfuse",
                "trace_source_parameters": {
                    "base_url": "https://langfuse.example.com",
                    "public_key": "pk-lf-123",
                },
                "trace_source_secrets": {"secret_key": "sk-lf-super-secret"},
                "schedule_cron": "02:30",
                "timezone": "Asia/Shanghai",
                "evolution_provider_id": prov_id,
                "evolution_model": "evo-m1",
                "max_traces_per_run": 150,
            },
        )
        assert upd.status_code == 200, upd.text
        data = upd.json()
        assert data["enabled"] is True
        assert data["trace_source_plugin"] == "langfuse"
        assert data["trace_source_parameters"] == {
            "base_url": "https://langfuse.example.com",
            "public_key": "pk-lf-123",
        }
        # Secrets must be masked, never plaintext
        assert data["trace_source_secrets"] == {"secret_key": "***"}
        assert data["configured_secrets"] == ["secret_key"]
        assert data["secret_configured"] is True
        assert "sk-lf-super-secret" not in upd.text
        assert data["schedule_cron"] == "30 2 * * *"
        assert data["timezone"] == "Asia/Shanghai"
        assert data["evolution_provider_id"] == prov_id
        assert data["evolution_model"] == "evo-m1"
        assert data["max_traces_per_run"] == 150
        assert data["next_run_at"] is not None and data["next_run_at"].endswith("Z")

        # Update only cron without passing trace_source_secrets -> preserves existing secret!
        upd2 = c.put(
            f"/api/v1/projects/{pid}/evolution/config",
            headers=_headers(padmin_tok),
            json={"schedule_cron": "15 3 * * 1-5", "timezone": "UTC"},
        )
        assert upd2.status_code == 200, upd2.text
        assert upd2.json()["schedule_cron"] == "15 3 * * 1-5"
        assert upd2.json()["timezone"] == "UTC"
        assert upd2.json()["configured_secrets"] == ["secret_key"]
        assert upd2.json()["trace_source_secrets"] == {"secret_key": "***"}

        from semantica_dataagent import evolution_runner

        _, _, resolved_params, resolved_secrets = evolution_runner.resolve_trace_source(
            pid
        )
        assert resolved_params["public_key"] == "pk-lf-123"
        assert resolved_secrets == {"secret_key": "sk-lf-super-secret"}

        # Validation failures -> 422
        assert (
            c.put(
                f"/api/v1/projects/{pid}/evolution/config",
                headers=_headers(padmin_tok),
                json={"schedule_cron": "bad-cron"},
            ).status_code
            == 422
        )
        assert (
            c.put(
                f"/api/v1/projects/{pid}/evolution/config",
                headers=_headers(padmin_tok),
                json={"timezone": "Mars/Olympus"},
            ).status_code
            == 422
        )
        assert (
            c.put(
                f"/api/v1/projects/{pid}/evolution/config",
                headers=_headers(padmin_tok),
                json={"evolution_provider_id": prov_id, "evolution_model": "nope"},
            ).status_code
            == 422
        )
        assert (
            c.put(
                f"/api/v1/projects/{pid}/evolution/config",
                headers=_headers(padmin_tok),
                json={"trace_source_plugin": "unknown-plugin"},
            ).status_code
            == 422
        )

        # Archive project -> GET 200, PUT 409, POST run 409
        assert (
            c.post(f"/api/v1/projects/{pid}/archive", headers=headers).status_code
            == 200
        )
        assert (
            c.get(
                f"/api/v1/projects/{pid}/evolution/config",
                headers=_headers(padmin_tok),
            ).status_code
            == 200
        )
        assert (
            c.put(
                f"/api/v1/projects/{pid}/evolution/config",
                headers=_headers(padmin_tok),
                json={"enabled": False},
            ).status_code
            == 409
        )
        assert (
            c.post(
                f"/api/v1/projects/{pid}/evolution/runs",
                headers=_headers(padmin_tok),
            ).status_code
            == 409
        )


def test_manual_trigger_concurrent_409_and_time_window_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    c, admin_token = _setup_client(tmp_path, monkeypatch)
    with c:
        from semantica_dataagent import evolution_runner
        from semantica_dataagent.db import EvolutionRun, sessions

        headers = _headers(admin_token)
        proj = c.post(
            "/api/v1/projects",
            headers=headers,
            json={"name": "Manual Trigger Proj", "type": "warehouse"},
        )
        pid = proj.json()["id"]

        # Bad window since >= until -> 422
        bad = c.post(
            f"/api/v1/projects/{pid}/evolution/runs",
            headers=headers,
            json={
                "since": "2026-11-24T10:00:00Z",
                "until": "2026-11-24T09:00:00Z",
            },
        )
        assert bad.status_code == 422

        entered = threading.Event()
        release = threading.Event()

        def fake_execute(run_id: str, **kwargs):
            entered.set()
            release.wait(5)
            with sessions().begin() as db:
                row = db.get(EvolutionRun, run_id)
                assert row is not None
                row.status = "succeeded"
                row.stop_reason = "no_traces"
                row.signal_summary = '{"traces_fetched": 0, "proposals_created": 0, "duplicates_skipped": 0, "summary": "done"}'
                row.finished_at = datetime.now(UTC)

        monkeypatch.setattr(evolution_runner, "execute_evolution_run", fake_execute)

        first = c.post(
            f"/api/v1/projects/{pid}/evolution/runs",
            headers=headers,
            json={
                "since": "2026-11-24T01:00:00Z",
                "until": "2026-11-24T05:00:00Z",
                "max_traces": 50,
            },
        )
        assert first.status_code == 202, first.text
        run_id = first.json()["id"]
        assert first.json()["status"] == "running"
        assert first.json()["window_since"] == "2026-11-24T01:00:00Z"
        assert first.json()["window_until"] == "2026-11-24T05:00:00Z"
        assert entered.wait(5)

        # Second concurrent run on same project -> 409
        second = c.post(
            f"/api/v1/projects/{pid}/evolution/runs",
            headers=headers,
        )
        assert second.status_code == 409

        release.set()
        for _ in range(50):
            detail = c.get(
                f"/api/v1/projects/{pid}/evolution/runs/{run_id}",
                headers=headers,
            )
            assert detail.status_code == 200
            if detail.json()["status"] != "running":
                break
            time.sleep(0.05)
        assert detail.json()["status"] == "succeeded"

        listed = c.get(
            f"/api/v1/projects/{pid}/evolution/runs",
            headers=headers,
        )
        assert listed.status_code == 200
        assert [r["id"] for r in listed.json()] == [run_id]


def test_wall_clock_cron_scheduler_timezone_dst_restart_no_drift_and_frozen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    c, admin_token = _setup_client(tmp_path, monkeypatch)
    with c:
        from semantica_dataagent import (
            evolution_runner,
            evolution_scheduler,
            plugins,
            project_deletion,
        )
        from semantica_dataagent.db import ProjectEvolutionConfig, sessions
        from semantica_dataagent.plugins import TraceConversation, TraceMessage

        class _FakeTracePlugin:
            id = "fake-trace"
            version = "1.0"
            display_name = "Fake Trace"
            parameters_schema: ClassVar[dict] = {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            }

            def fetch_traces(self, query):
                return [
                    TraceConversation(
                        trace_id="tr-cron-1",
                        project_id=query.project_id,
                        messages=[TraceMessage(role="user", content="test")],
                    )
                ]

        base = dict(plugins.discover())
        base[("trace_source", "fake-trace")] = _FakeTracePlugin()
        monkeypatch.setattr(plugins, "discover", lambda: dict(base))

        headers = _headers(admin_token)
        pid = c.post(
            "/api/v1/projects",
            headers=headers,
            json={"name": "Cron Evo Proj", "type": "warehouse"},
        ).json()["id"]

        created_at = datetime(2026, 6, 15, 15, 50, tzinfo=UTC)  # 23:50+08:00
        with sessions().begin() as db:
            db.add(
                ProjectEvolutionConfig(
                    project_id=pid,
                    enabled=True,
                    trace_source_plugin="fake-trace",
                    schedule_cron="0 0 * * *",
                    timezone="Asia/Shanghai",
                    created_at=created_at,
                    updated_at=created_at,
                )
            )

        dispatched: list[str] = []
        monkeypatch.setattr(
            evolution_runner,
            "execute_evolution_run",
            lambda run_id, **kwargs: None,
        )

        sched = evolution_scheduler.EvolutionScheduler()
        monkeypatch.setattr(
            sched, "dispatch_run", lambda run_id: dispatched.append(run_id)
        )

        # 23:55+08:00 -> not due
        assert sched.tick_once(now=datetime(2026, 6, 15, 15, 55, tzinfo=UTC)) == []
        assert dispatched == []

        # 00:05+08:00 (5 min late cross-midnight) -> fires once
        fired_1 = sched.tick_once(now=datetime(2026, 6, 15, 16, 5, tzinfo=UTC))
        assert len(fired_1) == 1
        assert dispatched == fired_1

        # Simulate restart + tick in same window (00:20+08:00) -> does NOT duplicate
        from semantica_dataagent.db import EvolutionRun

        with sessions().begin() as db:
            db.get(EvolutionRun, fired_1[0]).status = "succeeded"

        restarted = evolution_scheduler.EvolutionScheduler()
        monkeypatch.setattr(
            restarted, "dispatch_run", lambda run_id: dispatched.append(run_id)
        )
        assert restarted.tick_once(now=datetime(2026, 6, 15, 16, 20, tzinfo=UTC)) == []

        # Next night at exact 00:00+08:00 -> NO drift from previous 00:05 late run!
        fired_2 = restarted.tick_once(now=datetime(2026, 6, 16, 16, 0, tzinfo=UTC))
        assert len(fired_2) == 1
        with sessions().begin() as db:
            db.get(EvolutionRun, fired_2[0]).status = "succeeded"

        # Archived project -> skipped without updating last_triggered_at
        project_deletion.archive_project(pid, "admin", "Admin")
        with sessions()() as db:
            before_ts = db.get(ProjectEvolutionConfig, pid).last_triggered_at
        assert restarted.tick_once(now=datetime(2026, 6, 17, 16, 1, tzinfo=UTC)) == []
        with sessions()() as db:
            assert db.get(ProjectEvolutionConfig, pid).last_triggered_at == before_ts

        # Restored project -> resumes missed slot
        project_deletion.restore_project(pid, "admin", "Admin")
        fired_3 = restarted.tick_once(now=datetime(2026, 6, 17, 16, 2, tzinfo=UTC))
        assert len(fired_3) == 1

        # DST spring-forward check for next_run_at_for_config
        ny_cfg = ProjectEvolutionConfig(
            project_id="ny",
            enabled=True,
            schedule_cron="30 2 * * *",
            timezone="America/New_York",
            created_at=datetime(2026, 3, 8, 6, 0, tzinfo=UTC),
            updated_at=datetime(2026, 3, 8, 6, 0, tzinfo=UTC),
        )
        nxt = evolution_scheduler.next_run_at_for_config(
            ny_cfg, now=datetime(2026, 3, 8, 6, 0, tzinfo=UTC)
        )
        # 2026-03-08 02:30 doesn't exist in America/New_York -> next valid slot is 2026-03-09 02:30 EDT
        assert nxt == datetime(
            2026, 3, 9, 2, 30, tzinfo=ZoneInfo("America/New_York")
        ).astimezone(UTC)

        # DST fall-back check: 2026-11-01 01:00..01:59 occurs twice (EDT -0400, EST -0500).
        # At 05:10 UTC (01:10 EDT), cron "0,30 1 * * *" MUST pick 05:30 UTC (01:30 EDT), not 06:00 UTC (01:00 EST)!
        from semantica_dataagent import scheduler as sched_mod

        assert sched_mod.next_scheduled_slot(
            "0,30 1 * * *", "America/New_York", datetime(2026, 11, 1, 5, 10, tzinfo=UTC)
        ) == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
        assert sched_mod.next_scheduled_slot(
            "0,30 1 * * *", "America/New_York", datetime(2026, 11, 1, 5, 40, tzinfo=UTC)
        ) == datetime(2026, 11, 1, 6, 0, tzinfo=UTC)


def test_switching_trace_source_plugin_clears_previous_plugin_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    c, admin_token = _setup_client(tmp_path, monkeypatch)
    with c:
        from semantica_dataagent import evolution_runner, plugins

        class _AltTracePlugin:
            id = "alt-trace"
            version = "1.0"
            display_name = "Alt Trace"
            parameters_schema: ClassVar[dict] = {
                "type": "object",
                "properties": {
                    "endpoint": {"type": "string"},
                    "api_token": {"type": "string", "format": "password"},
                },
                "required": ["endpoint", "api_token"],
                "additionalProperties": False,
            }

            def fetch_traces(self, query):
                return []

        class _SharedKeyTracePlugin:
            id = "shared-key-trace"
            version = "1.0"
            display_name = "Shared Key Trace"
            parameters_schema: ClassVar[dict] = {
                "type": "object",
                "properties": {
                    "secret_key": {"type": "string", "format": "password"},
                },
                "required": ["secret_key"],
                "additionalProperties": False,
            }

            def fetch_traces(self, query):
                return []

        base = dict(plugins.discover())
        base[("trace_source", "alt-trace")] = _AltTracePlugin()
        base[("trace_source", "shared-key-trace")] = _SharedKeyTracePlugin()
        monkeypatch.setattr(plugins, "discover", lambda: dict(base))

        headers = _headers(admin_token)
        pid = c.post(
            "/api/v1/projects",
            headers=headers,
            json={"name": "Switch Plugin Proj", "type": "warehouse"},
        ).json()["id"]

        # 1. Configure langfuse with secret_key
        first = c.put(
            f"/api/v1/projects/{pid}/evolution/config",
            headers=headers,
            json={
                "enabled": True,
                "trace_source_plugin": "langfuse",
                "trace_source_parameters": {
                    "base_url": "https://lf.example.com",
                    "public_key": "pk-1",
                },
                "trace_source_secrets": {"secret_key": "sk-langfuse-old"},
            },
        )
        assert first.status_code == 200, first.text
        assert first.json()["configured_secrets"] == ["secret_key"]

        # 2. Switch to shared-key-trace passing "***" -> MUST NOT reuse langfuse's secret_key!
        reused = c.put(
            f"/api/v1/projects/{pid}/evolution/config",
            headers=headers,
            json={
                "enabled": True,
                "trace_source_plugin": "shared-key-trace",
                "trace_source_parameters": {},
                "trace_source_secrets": {"secret_key": "***"},
            },
        )
        assert reused.status_code == 422

        # 3. Switch to alt-trace with its own api_token -> old secret_key must be completely gone!
        switched = c.put(
            f"/api/v1/projects/{pid}/evolution/config",
            headers=headers,
            json={
                "enabled": True,
                "trace_source_plugin": "alt-trace",
                "trace_source_parameters": {"endpoint": "https://alt.example.com"},
                "trace_source_secrets": {"api_token": "tok-alt-new"},
            },
        )
        assert switched.status_code == 200, switched.text
        assert switched.json()["trace_source_plugin"] == "alt-trace"
        assert switched.json()["configured_secrets"] == ["api_token"]
        assert switched.json()["trace_source_secrets"] == {"api_token": "***"}

        _, plugin_id, params, secrets = evolution_runner.resolve_trace_source(pid)
        assert plugin_id == "alt-trace"
        assert params == {"endpoint": "https://alt.example.com"}
        assert secrets == {"api_token": "tok-alt-new"}
        assert "secret_key" not in secrets
