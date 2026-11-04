"""Incremental build through HTTP against uniquely isolated real test stores."""

import base64
import json
import os
import time
import uuid
from pathlib import Path

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
    not all(os.getenv(x) for x in NEEDED),
    reason="set all TEST_* test storage variables",
)


def test_incremental_build_preserves_other_documents_and_removes_deleted(
    tmp_path, monkeypatch
):
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    generation = "test_" + uuid.uuid4().hex
    source = tmp_path / "source"
    source.mkdir()
    sample = Path(__file__).resolve().parents[2] / "samples/demo"
    tables = json.loads((sample / "tables.json").read_text())
    fields = json.loads((sample / "fields.json").read_text())
    table = next(
        row for row in tables if row["database"] == "shop" and row["name"] == "orders"
    )
    selected_fields = [
        row for row in fields if row["database"] == "shop" and row["table"] == "orders"
    ][:1]
    channel = next(
        row for row in tables if row["database"] == "shop" and row["name"] == "channels"
    )
    upstream = {"database": "staging", "name": "orders", "comment": ""}
    channel_field = next(
        row
        for row in fields
        if row["database"] == "shop"
        and row["table"] == "channels"
        and row["name"] == "id"
    )
    dimensions = [
        row
        for row in json.loads((sample / "dimension_values.json").read_text())
        if row["database"] == "shop"
        and row["table"] == "channels"
        and row["field"] == "id"
    ]
    lineage = json.loads((sample / "lineage.json").read_text())[:1]
    warehouse = source / "warehouse"
    warehouse.mkdir()
    (warehouse / "tables.json").write_text(json.dumps([table, channel, upstream]))
    (warehouse / "fields.json").write_text(
        json.dumps(selected_fields + [channel_field])
    )
    (warehouse / "dimension_values.json").write_text(json.dumps(dimensions))
    (warehouse / "lineage.json").write_text(json.dumps(lineage))
    docs = source / "knowledge/metrics"
    docs.mkdir(parents=True)
    generated_dir = source / "knowledge/unindexed"
    generated_dir.mkdir(parents=True)
    (generated_dir / "draft.md").write_bytes(
        (sample / "knowledge/unindexed/draft.md").read_bytes()
    )
    for filename in ("index.md", "sales.md", "orders.md"):
        (docs / filename).write_bytes(
            (sample / "knowledge/metrics" / filename).read_bytes()
        )
    (docs / "dictionaries").mkdir()
    (docs / "dictionaries/shop.orders.md").write_bytes(
        (sample / "knowledge/operations/dictionaries/shop.orders.md").read_bytes()
    )
    token = initialize()
    headers = {"Authorization": "Bearer " + token}
    driver = GraphDatabase.driver(
        os.environ["TEST_NEO4J_URI"],
        auth=(os.environ["TEST_NEO4J_USER"], os.environ["TEST_NEO4J_PASSWORD"]),
    )
    vectors = MilvusClient(uri=os.environ["TEST_MILVUS_URI"])
    collection = "sda_" + generation
    try:
        with TestClient(app, headers=headers) as client:
            values = {
                "neo4j_uri": os.environ["TEST_NEO4J_URI"],
                "neo4j_user": os.environ["TEST_NEO4J_USER"],
                "neo4j_password": os.environ["TEST_NEO4J_PASSWORD"],
                "milvus_uri": os.environ["TEST_MILVUS_URI"],
                "embedding_url": os.environ["TEST_EMBEDDING_URL"],
                "source_directory": str(source),
                "knowledge_generation": generation,
            }
            assert (
                client.put("/api/v1/settings", json={"values": values}).status_code
                == 200
            )

            def wait_build(build_id):
                deadline = time.monotonic() + 900
                while time.monotonic() < deadline:
                    build = client.get("/api/v1/builds/" + str(build_id)).json()
                    if build["status"] in {"succeeded", "failed", "cancelled"}:
                        return build
                    time.sleep(0.1)
                raise AssertionError(f"Build {build_id} did not finish: {build}")

            def build(kind):
                result = client.post("/api/v1/builds", json={"build_type": kind})
                assert result.status_code == 202, result.text
                build = wait_build(result.json()["id"])
                assert build["status"] == "succeeded", build
                return build

            first = build("full")

            def document_state(path):
                rows, _, _ = driver.execute_query(
                    "MATCH (d:SDADocument {generation:$generation, source_file:$source}) OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:SDAChunk) RETURN elementId(d) AS id, d.build_id AS build_id, collect(c.ref) AS chunks",
                    generation=generation,
                    source=path,
                )
                return dict(rows[0]) if rows else None

            a = "knowledge/metrics/sales.md"
            b = "knowledge/metrics/orders.md"
            before_a, before_b = document_state(a), document_state(b)
            assert before_a and before_b and before_a["chunks"] and before_b["chunks"]
            before_vector = vectors.get(
                collection_name=collection,
                ids=before_b["chunks"],
                output_fields=["metadata"],
            )
            assert len(before_vector) == len(before_b["chunks"])
            assert all(hit["metadata"]["source_file"] == b for hit in before_vector)

            def upload(files, replace=False, target="knowledge/metrics"):
                payload = [
                    {"path": name, "content_base64": base64.b64encode(content).decode()}
                    for name, content in files.items()
                ]
                response = client.post(
                    "/api/v1/source/upload",
                    json={
                        "target": target,
                        "files": payload,
                        "replace": replace,
                    },
                )
                assert response.status_code == 200, response.text

            upload(
                {"sales.md": (docs / "sales.md").read_bytes() + b"\nnew paragraph\n"}
            )
            second = build("incremental")
            assert second["counts"]["changed"] == 1
            assert document_state(a)["build_id"] != before_a["build_id"]
            assert document_state(b) == before_b
            after_vector = vectors.get(
                collection_name=collection,
                ids=before_b["chunks"],
                output_fields=["metadata"],
            )
            assert {hit["id"]: hit["metadata"] for hit in after_vector} == {
                hit["id"]: hit["metadata"] for hit in before_vector
            }
            upload(
                {
                    "index.md": (docs / "index.md").read_bytes(),
                    "sales.md": (docs / "sales.md").read_bytes(),
                    "dictionaries/shop.orders.md": (
                        docs / "dictionaries/shop.orders.md"
                    ).read_bytes(),
                },
                replace=True,
            )
            third = build("incremental")
            assert third["counts"]["deleted"] == 1
            assert document_state(b) is None
            assert (
                vectors.get(
                    collection_name=collection,
                    ids=before_b["chunks"],
                    output_fields=["metadata"],
                )
                == []
            )
            assert first["triggered_by"] == "initial admin"
            assert second["source_commit"]
            original_draft = (generated_dir / "draft.md").read_bytes()
            upload(
                {"draft.md": original_draft + b"\nnew draft\n"},
                target="knowledge/unindexed",
            )
            draft_build = build("incremental")
            assert draft_build["counts"]["changed"] == 1
            rows, _, _ = driver.execute_query(
                "MATCH (d:SDADocument {generation:$generation, path:'index.md', category:'unindexed'}) RETURN d.source_file AS source_file, d.generated AS generated",
                generation=generation,
            )
            assert (
                rows[0]["source_file"] == "knowledge/unindexed/draft.md"
                and rows[0]["generated"]
            )

            def relations():
                rows, _, _ = driver.execute_query(
                    "MATCH (t:SDATable {generation:$generation, database:'shop', name:'orders'}) OPTIONAL MATCH (t)-[:HAS_FIELD]->(f:SDAField) OPTIONAL MATCH (t)-[:HAS_DOCUMENT]->(d:SDADocument) OPTIONAL MATCH (t)-[:DEPENDS_ON]->(u:SDATable) RETURN count(DISTINCT f) AS fields, count(DISTINCT d) AS docs, count(DISTINCT u) AS upstream",
                    generation=generation,
                )
                return dict(rows[0]) if rows else None

            def dimension_link():
                rows, _, _ = driver.execute_query(
                    "MATCH (:SDAField {generation:$generation, database:'shop', table:'channels', name:'id'})-[:HAS_VALUE]->(v:SDADimensionValue) RETURN count(v) AS count",
                    generation=generation,
                )
                return rows[0]["count"]

            assert relations() == {"fields": 1, "docs": 1, "upstream": 1}
            assert dimension_link() == len(dimensions)
            changed = [
                {**table, "comment": table["comment"] + " changed"},
                channel,
                upstream,
            ]
            upload({"tables.json": json.dumps(changed).encode()}, target="warehouse")
            build("incremental")
            assert relations() == {"fields": 1, "docs": 1, "upstream": 1}
            assert dimension_link() == len(dimensions)
            original_dimension, _, _ = driver.execute_query(
                "MATCH (v:SDADimensionValue {generation:$generation, database:'shop', table:'channels'}) RETURN v.ref AS ref, elementId(v) AS id",
                generation=generation,
            )
            original_dimension = {row["ref"]: row["id"] for row in original_dimension}
            changed_fields = [
                {
                    **selected_fields[0],
                    "comment": selected_fields[0]["comment"] + " changed",
                },
                {**channel_field, "comment": channel_field["comment"] + " changed"},
            ]
            upload(
                {"fields.json": json.dumps(changed_fields).encode()}, target="warehouse"
            )
            build("incremental")
            assert relations() == {"fields": 1, "docs": 1, "upstream": 1}
            assert dimension_link() == len(dimensions)
            current_dimension, _, _ = driver.execute_query(
                "MATCH (v:SDADimensionValue {generation:$generation, database:'shop', table:'channels'}) RETURN v.ref AS ref, elementId(v) AS id",
                generation=generation,
            )
            assert {
                row["ref"]: row["id"] for row in current_dimension
            } == original_dimension
            upload(
                {"dimension_values.json": json.dumps(dimensions[:1]).encode()},
                target="warehouse",
            )
            build("incremental")
            assert dimension_link() == 1
            upload(
                {"tables.json": json.dumps([channel, upstream]).encode()},
                target="warehouse",
            )
            build("incremental")
            assert relations() == {"fields": 0, "docs": 0, "upstream": 0}
            rows, _, _ = driver.execute_query(
                "MATCH (t:SDATable {generation:$generation, database:'shop', name:'orders'}) RETURN count(t) AS count",
                generation=generation,
            )
            assert rows[0]["count"] == 0
            assert dimension_link() == 1

            from semantica_dataagent import knowledge

            original_embed = knowledge.embed

            def fail_embedding(values, texts):
                raise RuntimeError("Injected embedding failure")

            upload(
                {"sales.md": (docs / "sales.md").read_bytes() + b"\nfailed update\n"}
            )
            monkeypatch.setattr(knowledge, "embed", fail_embedding)
            response = client.post("/api/v1/builds", json={"build_type": "incremental"})
            failed = wait_build(response.json()["id"])
            assert failed["status"] == "failed"
            monkeypatch.setattr(knowledge, "embed", original_embed)
            upload(
                {
                    "sales.md": (sample / "knowledge/metrics/sales.md").read_bytes()
                    + b"\nnew paragraph\n"
                }
            )
            recovered = build("incremental")
            assert recovered["counts"]["changed"] == 0
            assert document_state(a)["build_id"] == recovered["id"]
            refs = document_state(a)["chunks"]
            assert len(
                vectors.get(
                    collection_name=collection, ids=refs, output_fields=["metadata"]
                )
            ) == len(refs)
            upload(
                {"sales.md": (docs / "sales.md").read_bytes() + b"\nrollback failure\n"}
            )
            monkeypatch.setattr(knowledge, "embed", fail_embedding)
            result = client.post("/api/v1/builds", json={"build_type": "incremental"})
            failed = wait_build(result.json()["id"])
            assert failed["status"] == "failed"
            monkeypatch.setattr(knowledge, "embed", original_embed)
            upload(
                {
                    "sales.md": (sample / "knowledge/metrics/sales.md").read_bytes()
                    + b"\nnew paragraph\n"
                }
            )
            recovered = build("incremental")
            assert recovered["counts"]["changed"] == 0
            assert document_state(a)["build_id"] == recovered["id"]

            missing, _, _ = driver.execute_query(
                "MATCH (n:SDAEntity {generation:$generation}) WHERE n.source_file IS NULL RETURN count(n) AS missing",
                generation=generation,
            )
            assert missing[0]["missing"] == 0
            before_noop = document_state(a)
            empty_build = build("incremental")
            assert (
                empty_build["counts"]["added"]
                == empty_build["counts"]["changed"]
                == empty_build["counts"]["deleted"]
                == 0
            )
            assert document_state(a) == before_noop
            categories = client.get("/api/v1/categories").json()
            assert sorted(item["name"] for item in categories) == [
                "metrics",
                "unindexed",
            ]
            assert all(item["documents"] for item in categories)
            upload({"tables.json": json.dumps(changed).encode()}, target="warehouse")
            rebuild = build("full")
            assert rebuild["build_type"] == "full"
            assert document_state(a)["build_id"] == rebuild["id"]
            assert dimension_link() == 1
            assert (
                client.put(
                    "/api/v1/settings", json={"values": {"dimension_value_limit": "2"}}
                ).status_code
                == 200
            )
            rejected = client.post("/api/v1/builds", json={"build_type": "incremental"})
            rejected = wait_build(rejected.json()["id"])
            assert (
                rejected["status"] == "failed"
                and "configuration changed" in rejected["error"]
            )

    finally:
        driver.execute_query(
            "MATCH (n:SDAEntity {generation:$generation}) DETACH DELETE n",
            generation=generation,
        )
        driver.close()
        if vectors.has_collection(collection):
            vectors.drop_collection(collection)
        vectors.close()
