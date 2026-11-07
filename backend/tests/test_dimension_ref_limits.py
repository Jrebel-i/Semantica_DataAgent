"""Oversized dimension values must not enter the bounded ref indexes."""

import json
import os
import uuid

import pytest
from conftest import cleanup_test_generations

NEEDED = (
    "TEST_NEO4J_URI",
    "TEST_NEO4J_USER",
    "TEST_NEO4J_PASSWORD",
    "TEST_MILVUS_URI",
    "TEST_EMBEDDING_URL",
)
pytestmark = pytest.mark.skipif(
    not all(os.getenv(x) for x in NEEDED),
    reason="set TEST_* values for isolated test services",
)


def test_oversized_dimension_values_are_skipped(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    from neo4j import GraphDatabase
    from pymilvus import MilvusClient

    from semantica_dataagent import knowledge
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.db import Build, Setting, sessions

    source = tmp_path / "source"
    source.mkdir()
    (source / "tables.json").write_text(
        json.dumps(
            [
                {
                    "database": "synthetic",
                    "name": "long_values",
                    "comment": "synthetic table",
                }
            ]
        )
    )
    (source / "fields.json").write_text(
        json.dumps(
            [
                {
                    "database": "synthetic",
                    "table": "long_values",
                    "name": "short_code",
                    "type": "string",
                    "comment": "short values",
                    "partition": False,
                },
                {
                    "database": "synthetic",
                    "table": "long_values",
                    "name": "free_text",
                    "type": "string",
                    "comment": "long values",
                    "partition": False,
                },
            ]
        )
    )
    short_values = ["A", "B"]
    long_values = ["x" * 10_000, "y" * 200_000, "中" * 300]
    (source / "dimension_values.json").write_text(
        json.dumps(
            [
                {
                    "database": "synthetic",
                    "table": "long_values",
                    "field": "short_code",
                    "value": value,
                }
                for value in short_values
            ]
            + [
                {
                    "database": "synthetic",
                    "table": "long_values",
                    "field": "free_text",
                    "value": value,
                }
                for value in long_values
            ]
        )
    )

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv(
        "DATAAGENT_NEO4J_AUTH",
        os.environ["TEST_NEO4J_USER"] + "/" + os.environ["TEST_NEO4J_PASSWORD"],
    )
    initialize()
    generation = "oversize_" + uuid.uuid4().hex
    values = {
        "source_directory": str(source),
        "neo4j_uri": os.environ["TEST_NEO4J_URI"],
        "neo4j_user": os.environ["TEST_NEO4J_USER"],
        "milvus_uri": os.environ["TEST_MILVUS_URI"],
        "embedding_url": os.environ["TEST_EMBEDDING_URL"],
        "embedding_model": os.environ.get(
            "TEST_EMBEDDING_MODEL", "model/bge-small-zh-v1.5"
        ),
        "knowledge_generation": generation,
        "dimension_value_limit": "1000",
    }
    with sessions().begin() as db:
        for key, value in values.items():
            db.add(Setting(key=key, value=value))
        build = Build(
            status="running",
            started_at="2026-01-01T00:00:00+00:00",
            counts="{}",
            build_type="full",
        )
        db.add(build)
        db.flush()
        build_id = build.id

    driver = GraphDatabase.driver(
        os.environ["TEST_NEO4J_URI"],
        auth=(os.environ["TEST_NEO4J_USER"], os.environ["TEST_NEO4J_PASSWORD"]),
    )
    vectors = MilvusClient(uri=os.environ["TEST_MILVUS_URI"])
    try:
        knowledge.run_build(build_id)
        with sessions()() as db:
            build = db.get(Build, build_id)
            status, error, counts, built_generation = (
                build.status,
                build.error,
                json.loads(build.counts),
                build.generation,
            )
        assert status == "succeeded", error
        assert counts["dimension_value"] == len(short_values)
        assert counts["dimension_value_skipped"] == len(long_values)
        with driver.session() as session:
            fields = {
                row["name"]: row
                for row in session.run(
                    """MATCH (f:SDAField {generation:$generation, database:'synthetic',
                    table:'long_values'})
                    RETURN f.name AS name, f.dimension_count AS count,
                    f.dimension_samples AS samples""",
                    generation=built_generation,
                )
            }
            value_count = session.run(
                "MATCH (v:SDADimensionValue {generation:$generation}) RETURN count(v) AS count",
                generation=built_generation,
            ).single()["count"]
        assert value_count == len(short_values)
        assert fields["short_code"]["count"] == len(short_values)
        assert fields["short_code"]["samples"] == short_values
        assert fields["free_text"]["count"] == len(long_values)
        assert fields["free_text"]["samples"] == []
    finally:
        cleanup_test_generations(driver, vectors, generation)
        driver.close()
        vectors.close()
