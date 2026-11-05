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
UTC_SUFFIX = re.compile(r"(Z|[+-]00:00)$")


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


def test_rest_time_fields_have_utc_offset(client):
    from semantica_dataagent.db import (
        Build,
        Conversation,
        GenerationPointer,
        KnowledgeGeneration,
        Provider,
        Token,
        Turn,
        digest,
        sessions,
    )

    c, admin_token = client
    headers = {"Authorization": f"Bearer {admin_token}"}
    with sessions().begin() as db:
        owner = db.get(Token, digest(admin_token))
        db.add_all(
            [
                Build(
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
                    sql="[]",
                    citations="[]",
                    started_at=datetime(2026, 1, 1),
                    finished_at=datetime(2026, 1, 1, 0, 1),
                ),
            ]
        )

    payloads = {
        "tokens": c.get("/api/v1/tokens", headers=headers).json(),
        "builds": c.get("/api/v1/builds", headers=headers).json(),
        "generations": c.get("/api/v1/generations", headers=headers).json(),
        "providers": c.get("/api/v1/providers", headers=headers).json(),
        "conversations": c.get("/api/v1/conversations", headers=headers).json(),
        "conversation": c.get(
            "/api/v1/conversations/conversation", headers=headers
        ).json(),
    }
    failures = {
        name: problems
        for name, payload in payloads.items()
        if (problems := utc_failures(payload))
    }
    assert not failures, f"time fields without UTC offset: {failures}"


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
