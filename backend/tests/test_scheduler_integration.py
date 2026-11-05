"""Scheduled builds through REST using isolated real test stores."""

import os
import shutil
import threading
import time
import uuid
from pathlib import Path

import pytest
from conftest import cleanup_test_generations
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from neo4j import GraphDatabase
from pymilvus import MilvusClient

NEEDED = (
    "TEST_NEO4J_URI",
    "TEST_NEO4J_USER",
    "TEST_NEO4J_PASSWORD",
    "TEST_MILVUS_URI",
    "TEST_EMBEDDING_URL",
)
pytestmark = pytest.mark.skipif(
    not all(os.getenv(key) for key in NEEDED), reason="test storage variables required"
)


def test_scheduled_incremental_uses_queue_and_live_interval(tmp_path, monkeypatch):
    from semantica_dataagent import stages
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    generation = "sched_" + uuid.uuid4().hex[:12]
    source = tmp_path / "source"
    shutil.copytree(Path(__file__).resolve().parents[2] / "samples/demo", source)
    token = initialize()
    driver = GraphDatabase.driver(
        os.environ["TEST_NEO4J_URI"],
        auth=(os.environ["TEST_NEO4J_USER"], os.environ["TEST_NEO4J_PASSWORD"]),
    )
    vectors = MilvusClient(uri=os.environ["TEST_MILVUS_URI"])
    entered, release = threading.Event(), threading.Event()
    try:
        with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
            values = {
                "neo4j_uri": os.environ["TEST_NEO4J_URI"],
                "neo4j_user": os.environ["TEST_NEO4J_USER"],
                "neo4j_password": os.environ["TEST_NEO4J_PASSWORD"],
                "milvus_uri": os.environ["TEST_MILVUS_URI"],
                "embedding_url": os.environ["TEST_EMBEDDING_URL"],
                "embedding_model": os.environ["TEST_EMBEDDING_MODEL"],
                "source_directory": str(source),
                "knowledge_generation": generation,
            }
            assert (
                client.put("/api/v1/settings", json={"values": values}).status_code
                == 200
            )
            first = client.post("/api/v1/builds").json()["id"]

            def wait_for(predicate, description):
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    rows = client.get("/api/v1/builds").json()
                    result = predicate(rows)
                    if result:
                        return result
                    time.sleep(0.1)
                pytest.fail(f"timed out waiting for {description}: {rows}")

            initial = wait_for(
                lambda rows: next(
                    (
                        r
                        for r in rows
                        if r["id"] == first and r["status"] in {"succeeded", "failed"}
                    ),
                    None,
                ),
                "initial build",
            )
            assert initial["status"] == "succeeded", initial
            original = stages.STAGES[0]

            def paused(*args, **kwargs):
                entered.set()
                if not release.wait(120):
                    raise RuntimeError("test did not release build")
                return original(*args, **kwargs)

            monkeypatch.setattr(stages, "STAGES", (paused, *stages.STAGES[1:]))
            manual = client.post(
                "/api/v1/builds", json={"build_type": "incremental"}
            ).json()["id"]
            assert entered.wait(120)
            assert (
                client.put(
                    "/api/v1/settings",
                    json={"values": {"schedule_interval_seconds": "1"}},
                ).status_code
                == 200
            )
            scheduled = wait_for(
                lambda rows: next(
                    (r for r in rows if r["triggered_by"] == "scheduler"), None
                ),
                "scheduled build",
            )
            assert scheduled["status"] == "queued"
            assert scheduled["build_type"] == "incremental"
            assert (
                client.put(
                    "/api/v1/settings",
                    json={"values": {"schedule_interval_seconds": "60"}},
                ).status_code
                == 200
            )
            release.set()
            complete = wait_for(
                lambda rows: next(
                    (
                        r
                        for r in rows
                        if r["id"] == scheduled["id"]
                        and r["status"] in {"succeeded", "failed"}
                    ),
                    None,
                ),
                "scheduled completion",
            )
            manual_row = client.get(f"/api/v1/builds/{manual}").json()
            assert manual_row["status"] == complete["status"] == "succeeded", complete
            assert complete["started_at"] >= manual_row["finished_at"]
            assert (
                client.get("/api/v1/settings").json()["schedule_interval_seconds"]
                == "60"
            )
    finally:
        release.set()
        try:
            cleanup_test_generations(driver, vectors, generation)
        finally:
            driver.close()
            vectors.close()
