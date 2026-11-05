"""Recovery of a full build interrupted after writing to both stores."""

import os
import uuid

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from neo4j import GraphDatabase
from pymilvus import MilvusClient

NEEDED = ("TEST_NEO4J_URI", "TEST_NEO4J_USER", "TEST_NEO4J_PASSWORD", "TEST_MILVUS_URI")
pytestmark = pytest.mark.skipif(
    not all(os.getenv(k) for k in NEEDED), reason="test storage variables required"
)


def test_startup_cleans_interrupted_full_build(tmp_path, monkeypatch):
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.db import (
        Build,
        GenerationPointer,
        KnowledgeGeneration,
        sessions,
    )

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    token = initialize()
    prefix = "test_" + uuid.uuid4().hex[:12]
    current, orphan = prefix + "_current", prefix + "_orphan"
    driver = GraphDatabase.driver(
        os.environ["TEST_NEO4J_URI"],
        auth=(os.environ["TEST_NEO4J_USER"], os.environ["TEST_NEO4J_PASSWORD"]),
    )
    vectors = MilvusClient(uri=os.environ["TEST_MILVUS_URI"])
    try:
        with TestClient(app, headers={"Authorization": "Bearer " + token}) as client:
            response = client.put(
                "/api/v1/settings",
                json={
                    "values": {
                        "neo4j_uri": os.environ["TEST_NEO4J_URI"],
                        "neo4j_user": os.environ["TEST_NEO4J_USER"],
                        "neo4j_password": os.environ["TEST_NEO4J_PASSWORD"],
                        "milvus_uri": os.environ["TEST_MILVUS_URI"],
                        "knowledge_generation": current,
                    }
                },
            )
            assert response.status_code == 200, response.text
        for generation in (current, orphan):
            driver.execute_query(
                "CREATE (:SDAEntity {generation:$generation})", generation=generation
            )
            vectors.create_collection("sda_" + generation, dimension=2)
        with sessions().begin() as db:
            db.add(KnowledgeGeneration(name=current))
            db.add(GenerationPointer(id=1, generation=current))
            build = Build(
                status="running",
                started_at="2026-01-01",
                counts="{}",
                build_type="full",
                generation=orphan,
            )
            db.add(build)
            db.flush()
            build_id = build.id
        from semantica_dataagent import knowledge

        original_delete = knowledge.delete_generation_data

        def unavailable(_values):
            raise ConnectionError("injected storage outage")

        monkeypatch.setattr(knowledge, "delete_generation_data", unavailable)
        with TestClient(app, headers={"Authorization": "Bearer " + token}) as client:
            assert client.get(f"/api/v1/builds/{build_id}").json()["status"] == "failed"
            assert vectors.has_collection("sda_" + orphan), (
                "failed cleanup should retry"
            )
        monkeypatch.setattr(knowledge, "delete_generation_data", original_delete)
        with TestClient(app, headers={"Authorization": "Bearer " + token}) as client:
            record = client.get(f"/api/v1/builds/{build_id}").json()
            assert (
                record["status"] == "failed"
                and "interrupted" in record["error"].lower()
            )
            assert not vectors.has_collection("sda_" + orphan), (
                "orphan collection remains"
            )
            rows, _, _ = driver.execute_query(
                "MATCH (n:SDAEntity {generation:$generation}) RETURN count(n) AS n",
                generation=orphan,
            )
            assert rows[0]["n"] == 0, "orphan nodes remain"
            assert vectors.has_collection("sda_" + current)
            rows, _, _ = driver.execute_query(
                "MATCH (n:SDAEntity {generation:$generation}) RETURN count(n) AS n",
                generation=current,
            )
            assert rows[0]["n"] == 1
            with sessions()() as db:
                assert db.get(GenerationPointer, 1).generation == current
    finally:
        for generation in (current, orphan):
            driver.execute_query(
                "MATCH (n:SDAEntity {generation:$generation}) DETACH DELETE n",
                generation=generation,
            )
            if vectors.has_collection("sda_" + generation):
                vectors.drop_collection("sda_" + generation)
        vectors.close()
        driver.close()
