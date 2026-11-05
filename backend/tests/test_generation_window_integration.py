"""Real-store regression for reads while a full rebuild is in progress."""

import os
import shutil
from pathlib import Path
import threading
import time
import uuid

import pytest
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
    not all(os.getenv(k) for k in NEEDED), reason="test storage variables required"
)


def test_full_rebuild_keeps_searching_previous_generation(tmp_path, monkeypatch):
    from semantica_dataagent import stages
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.table_api import search_knowledge

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    prefix = "test_" + uuid.uuid4().hex
    source = tmp_path / "source"
    docs = source / "knowledge" / "metrics"
    docs.mkdir(parents=True)
    (docs / "sales.md").write_text(
        "# Sales\n\nThe quokkawindow metric counts orders.\n"
    )
    warehouse = source / "warehouse"
    warehouse.mkdir()
    shutil.copy(
        Path(__file__).resolve().parents[2] / "samples/demo/tables.json",
        warehouse / "tables.json",
    )
    token = initialize()
    driver = GraphDatabase.driver(
        os.environ["TEST_NEO4J_URI"],
        auth=(os.environ["TEST_NEO4J_USER"], os.environ["TEST_NEO4J_PASSWORD"]),
    )
    vectors = MilvusClient(uri=os.environ["TEST_MILVUS_URI"])
    entered, release = threading.Event(), threading.Event()
    try:
        with TestClient(app, headers={"Authorization": "Bearer " + token}) as client:
            values = {
                "neo4j_uri": os.environ["TEST_NEO4J_URI"],
                "neo4j_user": os.environ["TEST_NEO4J_USER"],
                "neo4j_password": os.environ["TEST_NEO4J_PASSWORD"],
                "milvus_uri": os.environ["TEST_MILVUS_URI"],
                "embedding_url": os.environ["TEST_EMBEDDING_URL"],
                "embedding_model": os.getenv(
                    "TEST_EMBEDDING_MODEL", "model/bge-small-zh-v1.5"
                ),
                "source_directory": str(source),
                "knowledge_generation": prefix,
            }
            response = client.put("/api/v1/settings", json={"values": values})
            assert response.status_code == 200, response.text

            def submit():
                response = client.post("/api/v1/builds", json={"build_type": "full"})
                assert response.status_code == 202, response.text
                return response.json()["id"]

            def wait(build_id):
                deadline = time.monotonic() + 300
                while time.monotonic() < deadline:
                    record = client.get(f"/api/v1/builds/{build_id}").json()
                    if record["status"] in {"succeeded", "failed", "cancelled"}:
                        return record
                    time.sleep(0.1)
                pytest.fail(f"build {build_id} timed out")

            first = wait(submit())
            assert first["status"] == "succeeded", first.get("error")
            assert any(
                "quokkawindow" in (hit.get("content") or "")
                for hit in search_knowledge("quokkawindow", "docs", 10)
            )
            original = stages.STAGES[0]

            def paused(*args, **kwargs):
                entered.set()
                if not release.wait(120):
                    raise RuntimeError("test did not release build")
                return original(*args, **kwargs)

            monkeypatch.setattr(stages, "STAGES", (paused, *stages.STAGES[1:]))
            build_id = submit()
            assert entered.wait(120), "rebuild did not reach the write stage"
            try:
                for _ in range(3):
                    hits = search_knowledge("quokkawindow", "docs", 10)
                    assert any(
                        "quokkawindow" in (hit.get("content") or "") for hit in hits
                    ), "old generation disappeared during full rebuild"
            finally:
                release.set()
                result = wait(build_id)
            assert result["status"] == "succeeded", result
    finally:
        release.set()
        rows, _, _ = driver.execute_query(
            "MATCH (n:SDAEntity) WHERE n.generation STARTS WITH $prefix RETURN DISTINCT n.generation AS generation",
            prefix=prefix,
        )
        for row in rows:
            driver.execute_query(
                "MATCH (n:SDAEntity {generation:$generation}) DETACH DELETE n",
                generation=row["generation"],
            )
        driver.close()
        for name in vectors.list_collections():
            if name.startswith("sda_" + prefix):
                vectors.drop_collection(name)
        vectors.close()
