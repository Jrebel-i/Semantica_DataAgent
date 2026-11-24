"""REST API time fields carry an explicit UTC offset.

The API contract is ISO 8601 with a timezone.  Values already stored without
one in SQLite are UTC, so serialization must not expose them as local/naive
timestamps.
"""

import json
import re
import subprocess
from datetime import datetime

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

TIME_NAME = re.compile(r"(^|_)(at|date|time|timestamp)$")
UTC_SUFFIX = re.compile(r"Z$")


def time_fields(payload, path=""):
    if isinstance(payload, dict):
        for key, value in payload.items():
            child = f"{path}.{key}" if path else key
            if TIME_NAME.search(key):
                yield child, value
            yield from time_fields(value, child)
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            yield from time_fields(value, f"{path}[{index}]")


def utc_failures(payload, path=""):
    failures = []
    for field, value in time_fields(payload, path):
        if value is None:
            continue
        if not isinstance(value, str) or not UTC_SUFFIX.search(value):
            failures.append(f"{field}={value!r}")
    return failures


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    token = initialize()
    with TestClient(app) as test_client:
        yield test_client, token


def test_openapi_declares_every_time_field_as_datetime():
    from semantica_dataagent.app import app

    failures = []
    for model, schema in app.openapi()["components"]["schemas"].items():
        for name, prop in schema.get("properties", {}).items():
            if not TIME_NAME.search(name):
                continue
            variants = prop.get("anyOf", [prop])
            concrete = [item for item in variants if item.get("type") != "null"]
            if not concrete or any(
                item.get("format") != "date-time" for item in concrete
            ):
                failures.append(f"{model}.{name}")
    assert not failures, f"time fields without date-time schema: {failures}"


def test_rest_time_fields_have_utc_offset(client, monkeypatch):
    from semantica_dataagent.db import (
        Build,
        Conversation,
        CorrectionProposal,
        DiscoveryCandidate,
        DiscoveryRun,
        EvolutionCheckpoint,
        EvolutionRound,
        EvolutionRun,
        GenerationPointer,
        KnowledgeGap,
        KnowledgeGeneration,
        PersonalDtbToken,
        ProjectAudit,
        ProjectEvolutionConfig,
        ProjectPullInstance,
        Provider,
        PullRecord,
        Token,
        Turn,
        digest,
        sessions,
    )

    c, admin_token = client
    headers = {"Authorization": f"Bearer {admin_token}"}
    with sessions().begin() as db:
        owner = db.get(Token, digest(admin_token))
        build = Build(
            status="succeeded",
            started_at="2026-01-01T00:00:00",
            finished_at="2026-01-01T00:01:00",
            counts="{}",
            logs=json.dumps(
                [
                    {
                        "timestamp": "2026-01-01T00:00:00",
                        "stage": "build",
                        "level": "info",
                        "message": "started",
                    }
                ]
            ),
        )
        db.add_all(
            [
                build,
                CorrectionProposal(
                    id="time-proposal",
                    owner_id=owner.id,
                    target="field:shop.orders.id",
                    patch="new",
                    original_patch="new",
                    original="old",
                    diff="diff",
                    reason="reason",
                    status="reverted",
                    reviewer_id=owner.id,
                    reviewed_at=datetime(2026, 1, 1),
                    reverted_at=datetime(2026, 1, 2),
                    revert_commit_sha="deadbeef",
                    created_at=datetime(2026, 1, 1),
                ),
                KnowledgeGap(
                    id="time-gap",
                    owner_id=owner.id,
                    question="question",
                    missing="missing",
                    created_at=datetime(2026, 1, 1),
                ),
                ProjectAudit(
                    project_id="default",
                    actor_id=owner.id,
                    actor_name="time",
                    action="project.create",
                    detail="{}",
                    created_at=datetime(2026, 1, 1),
                ),
                KnowledgeGeneration(name="naive", created_at="2026-01-01T00:00:00"),
                GenerationPointer(id=1, generation="naive"),
                Provider(
                    id="provider",
                    name="Local",
                    type="openai-completions",
                    base_url="http://127.0.0.1:1",
                    models='["model"]',
                    created_at=datetime(2026, 1, 1),
                    updated_at=datetime(2026, 1, 1),
                ),
                Conversation(
                    id="conversation",
                    owner_id=owner.id,
                    title="question",
                    agent="pi",
                    provider_id="provider",
                    model="model",
                    created_at=datetime(2026, 1, 1),
                    updated_at=datetime(2026, 1, 1),
                ),
                Turn(
                    conversation_id="conversation",
                    sequence=1,
                    question="question",
                    status="completed",
                    answer="answer",
                    sql=json.dumps(
                        [
                            {
                                "sql": "SELECT id FROM shop.orders",
                                "dialect": "hive",
                                "validated": True,
                            }
                        ]
                    ),
                    citations="[]",
                    started_at=datetime(2026, 1, 1),
                    finished_at=datetime(2026, 1, 1, 0, 1),
                ),
                PullRecord(
                    project_id="default",
                    status="succeeded",
                    started_at=datetime(2026, 1, 1),
                    finished_at=datetime(2026, 1, 1, 0, 1),
                    tables=1,
                    tasks=0,
                ),
                ProjectPullInstance(
                    project_id="default",
                    id="time-inst",
                    plugin_id="dtb",
                    display_name="Time Instance",
                    last_triggered_at=datetime(2026, 1, 1, 2, 0),
                    created_at=datetime(2026, 1, 1),
                    updated_at=datetime(2026, 1, 1, 1, 0),
                ),
                ProjectEvolutionConfig(
                    project_id="default",
                    enabled=True,
                    trace_source_plugin="langfuse",
                    schedule_cron="0 2 * * *",
                    timezone="Asia/Shanghai",
                    last_trace_timestamp=datetime(2026, 1, 1, 1, 0),
                    last_triggered_at=datetime(2026, 1, 1, 2, 0),
                    created_at=datetime(2026, 1, 1),
                    updated_at=datetime(2026, 1, 1),
                ),
                EvolutionCheckpoint(
                    project_id="default",
                    checkpoint_trajectory_id=1,
                    checkpoint_at=datetime(2026, 1, 1),
                    last_run_id="evo-time-1",
                    updated_at=datetime(2026, 1, 1),
                ),
                EvolutionRun(
                    id="evo-time-1",
                    project_id="default",
                    trigger_type="manual",
                    status="succeeded",
                    signal_summary=json.dumps(
                        {
                            "trace_source_plugin": "langfuse",
                            "traces_fetched": 2,
                            "proposals_created": 1,
                            "duplicates_skipped": 0,
                            "window_since": "2026-01-01T00:00:00",
                            "window_until": "2026-01-01T00:02:00",
                            "summary": "ok",
                        }
                    ),
                    started_at=datetime(2026, 1, 1),
                    finished_at=datetime(2026, 1, 1, 0, 2),
                    created_at=datetime(2026, 1, 1),
                ),
                EvolutionRound(
                    id="evo-round-1",
                    run_id="evo-time-1",
                    project_id="default",
                    round_number=1,
                    signal_kind="search_detour",
                    hypothesis="h",
                    decision="accept",
                    started_at=datetime(2026, 1, 1),
                    finished_at=datetime(2026, 1, 1, 0, 1),
                ),
                DiscoveryRun(
                    id="disc-time-1",
                    project_id="default",
                    status="completed",
                    mine_query_history=True,
                    probe_relationships=True,
                    seed_eval_drafts=False,
                    config_snapshot="{}",
                    created_by=owner.id,
                    started_at=datetime(2026, 1, 1),
                    finished_at=datetime(2026, 1, 1, 0, 2),
                    created_at=datetime(2026, 1, 1),
                ),
                DiscoveryCandidate(
                    id="disc-cand-1",
                    run_id="disc-time-1",
                    project_id="default",
                    candidate_kind="relationship",
                    target="relationship:shop.orders:shop.users:fk_orders_users",
                    evidence_json='{"overlap_ratio": 0.95, "join_matched": true}',
                    decision="proposed",
                    created_at=datetime(2026, 1, 1),
                ),
            ]
        )
        db.flush()
        build_id = build.id
        turn_id = (
            db.query(Turn.id)
            .filter(Turn.conversation_id == "conversation", Turn.sequence == 1)
            .scalar()
        )

    from semantica_dataagent import (
        crypto,
        discovery_runner,
        evolution_runner,
        project_context,
        pull_service,
    )

    with sessions().begin() as db:
        owner = db.get(Token, digest(admin_token))
        db.add(
            PersonalDtbToken(
                owner_id=owner.id,
                encrypted_token=crypto.encrypt("time-dtb-token"),
            )
        )
        project_context.upsert_plugin_config(
            db,
            "executor",
            "dtb",
            True,
            {"base_url": "http://dtb.local", "timeout_seconds": 30},
        )

    monkeypatch.setattr(
        discovery_runner,
        "execute_discovery_run",
        lambda run_id, **kwargs: None,
    )
    monkeypatch.setattr(
        evolution_runner, "execute_evolution_run", lambda run_id, **kwargs: None
    )
    monkeypatch.setattr(
        pull_service,
        "run_instance_pull",
        lambda project_id, instance_id, *, trigger_type="manual", force=False: (
            PullRecord(
                id=1000,
                project_id=project_id,
                instance_id=instance_id,
                plugin_id="dtb",
                trigger_type="forced" if force else trigger_type,
                status="succeeded",
                started_at=datetime(2026, 1, 1),
                finished_at=datetime(2026, 1, 1, 0, 1),
                tables=2,
                tasks=1,
                warnings='["dim_x carry-forward"]',
            )
        ),
    )
    issued = c.post(
        "/api/v1/tokens", headers=headers, json={"name": "time", "role": "user"}
    )
    switched = c.post("/api/v1/generations/naive/switch", headers=headers)
    assert switched.status_code == 200, switched.text
    feedback_resp = c.post(
        f"/api/v1/conversations/conversation/turns/{turn_id}/feedback",
        headers=headers,
        json={"rating": "accurate"},
    )
    assert feedback_resp.status_code == 201, feedback_resp.text
    eval_created = c.post(
        "/api/v1/eval-questions",
        headers=headers,
        json={
            "question": "时间测试评测题",
            "caliber": "测试口径",
            "reference_sql": "SELECT id FROM shop.orders",
            "dialect": "hive",
        },
    )
    assert eval_created.status_code == 201, eval_created.text
    eval_qid = eval_created.json()["id"]
    eval_patched = c.patch(
        f"/api/v1/eval-questions/{eval_qid}",
        headers=headers,
        json={"notes": "updated"},
    )
    assert eval_patched.status_code == 200, eval_patched.text
    evo_triggered = c.post("/api/v1/projects/default/evolution/runs", headers=headers)
    assert evo_triggered.status_code == 202, evo_triggered.text
    disc_triggered = c.post("/api/v1/discovery/runs", headers=headers)
    assert disc_triggered.status_code == 202, disc_triggered.text
    proj_disc_runs = c.get("/api/v1/projects/default/discovery/runs", headers=headers)
    assert proj_disc_runs.status_code == 200, proj_disc_runs.text
    proj_disc_run = c.get(
        "/api/v1/projects/default/discovery/runs/disc-time-1", headers=headers
    )
    assert proj_disc_run.status_code == 200, proj_disc_run.text
    gap_reviewed = c.post(
        "/api/v1/knowledge-gaps/time-gap/review",
        headers=headers,
        json={"status": "confirmed"},
    )
    assert gap_reviewed.status_code == 200, gap_reviewed.text
    payloads = {
        "token_issued": issued.json(),
        "tokens": c.get("/api/v1/tokens", headers=headers).json(),
        "corrections": c.get("/api/v1/corrections", headers=headers).json(),
        "correction": c.get(
            "/api/v1/corrections/time-proposal", headers=headers
        ).json(),
        "knowledge_gaps": c.get("/api/v1/knowledge-gaps", headers=headers).json(),
        "knowledge_gap_review": gap_reviewed.json(),
        "build": c.get(f"/api/v1/builds/{build_id}", headers=headers).json(),
        "builds": c.get("/api/v1/builds", headers=headers).json(),
        "pull_instance_trigger": c.post(
            "/api/v1/projects/default/pull-instances/time-inst/pull", headers=headers
        ).json(),
        "pulls": c.get("/api/v1/pulls", headers=headers).json(),
        "pull_instances": c.get(
            "/api/v1/projects/default/pull-instances", headers=headers
        ).json(),
        "generations": c.get("/api/v1/generations", headers=headers).json(),
        "providers": c.get("/api/v1/providers", headers=headers).json(),
        "conversations": c.get("/api/v1/conversations", headers=headers).json(),
        "conversation": c.get(
            "/api/v1/conversations/conversation", headers=headers
        ).json(),
        "projects": c.get("/api/v1/projects", headers=headers).json(),
        "project_audit": c.get(
            "/api/v1/projects/default/audit", headers=headers
        ).json(),
        "evolution_config": c.get(
            "/api/v1/projects/default/evolution/config", headers=headers
        ).json(),
        "discovery_config": c.get(
            "/api/v1/projects/default/discovery-config", headers=headers
        ).json(),
        "discovery_runs": c.get("/api/v1/discovery/runs", headers=headers).json(),
        "discovery_run": c.get(
            "/api/v1/discovery/runs/disc-time-1", headers=headers
        ).json(),
        "discovery_run_triggered": disc_triggered.json(),
        "project_discovery_runs": proj_disc_runs.json(),
        "project_discovery_run": proj_disc_run.json(),
        "evolution_runs": c.get(
            "/api/v1/projects/default/evolution/runs", headers=headers
        ).json(),
        "evolution_run": c.get(
            "/api/v1/projects/default/evolution/runs/evo-time-1", headers=headers
        ).json(),
        "evolution_run_triggered": evo_triggered.json(),
        "turn_feedback": feedback_resp.json(),
        "eval_question_created": eval_created.json(),
        "eval_questions": c.get("/api/v1/eval-questions", headers=headers).json(),
        "eval_question_patched": eval_patched.json(),
    }
    failures = {
        name: problems
        for name, payload in payloads.items()
        if (problems := utc_failures(payload))
    }
    assert not failures, f"time fields without UTC offset: {failures}"
    assert "fallback_reason" in payloads["build"]
    assert payloads["pull_instance_trigger"]["instance_id"] == "time-inst"
    assert payloads["pull_instance_trigger"]["warnings"] == ["dim_x carry-forward"]


def test_source_history_time_has_utc_offset(client, tmp_path):
    c, admin_token = client
    headers = {"Authorization": f"Bearer {admin_token}"}
    repo = tmp_path / "repository"
    repo.mkdir()
    (repo / "note.md").write_text("one\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "note.md"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "first",
        ],
        cwd=repo,
        check=True,
    )
    assert (
        c.put(
            "/api/v1/settings",
            headers=headers,
            json={"values": {"source_directory": str(repo)}},
        ).status_code
        == 200
    )
    history = c.get(
        "/api/v1/source/history", headers=headers, params={"path": "note.md"}
    )
    assert history.status_code == 200, history.text
    assert not utc_failures(history.json()), history.text
