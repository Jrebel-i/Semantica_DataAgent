"""Main seam against isolated test services; set all TEST_* values to enable."""

import asyncio
import os
import socket
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest
import uvicorn
from cryptography.fernet import Fernet
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
    reason="set TEST_NEO4J_URI, TEST_NEO4J_USER, TEST_NEO4J_PASSWORD, TEST_MILVUS_URI and TEST_EMBEDDING_URL for isolated test services",
)


def test_build_and_retrieve_through_http(tmp_path, monkeypatch):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from neo4j._sync.work.session import Session

    from semantica_dataagent import knowledge, stages
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.stage_contract import StageResult

    calls = {"neo4j": 0, "milvus": 0}
    original_run = Session.run
    original_search = MilvusClient.search

    def counted_run(self, *args, **kwargs):
        calls["neo4j"] += 1
        return original_run(self, *args, **kwargs)

    def counted_search(self, *args, **kwargs):
        calls["milvus"] += 1
        return original_search(self, *args, **kwargs)

    monkeypatch.setattr(Session, "run", counted_run)
    monkeypatch.setattr(MilvusClient, "search", counted_search)

    def extra_stage(records, generation, storage):
        storage.execute(
            "CREATE (n:SDAEntity:SDATest {generation:$generation, ref:$ref})",
            {"generation": generation, "ref": "test:extra"},
        )
        storage.add_vectors(
            ["test:extra"],
            ["independent stage"],
            [{"scope": "tests", "ref": "test:extra"}],
        )
        return StageResult(counts={"test_stage": 1})

    # Run the additional stage first: table writes must preserve its data.
    monkeypatch.setattr(stages, "STAGES", [extra_stage, *stages.STAGES])

    generation = "test_" + uuid.uuid4().hex
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    token = initialize()
    entered, release = threading.Event(), threading.Event()
    original_embed = knowledge.embed

    def delayed_embed(values, texts):
        if len(texts) > 1:
            entered.set()
            if not release.wait(timeout=60):
                raise TimeoutError("Test did not release build")
        return original_embed(values, texts)

    monkeypatch.setattr(knowledge, "embed", delayed_embed)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        deadline = time.monotonic() + 15
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        with httpx.Client(
            base_url=base, headers=headers, timeout=180, trust_env=False
        ) as c:
            values = {
                "neo4j_uri": os.environ["TEST_NEO4J_URI"],
                "neo4j_user": os.environ["TEST_NEO4J_USER"],
                "neo4j_password": os.environ["TEST_NEO4J_PASSWORD"],
                "milvus_uri": os.environ["TEST_MILVUS_URI"],
                "embedding_url": os.environ["TEST_EMBEDDING_URL"],
                "source_directory": str(
                    Path(__file__).resolve().parents[2] / "samples/demo"
                ),
                "knowledge_generation": generation,
            }
            assert c.put("/api/v1/settings", json={"values": values}).status_code == 200
            response = c.post("/api/v1/builds")
            assert response.status_code == 202, response.text
            ident = response.json()["id"]
            try:
                assert entered.wait(timeout=30)
                assert c.post("/api/v1/builds").status_code == 409
            finally:
                release.set()
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                build = c.get(f"/api/v1/builds/{ident}").json()
                if build["status"] != "running":
                    break
                time.sleep(0.2)
            assert build["status"] == "succeeded", build.get("error")
            assert build["counts"]["test_stage"] == 1
            with GraphDatabase.driver(
                os.environ["TEST_NEO4J_URI"],
                auth=(os.environ["TEST_NEO4J_USER"], os.environ["TEST_NEO4J_PASSWORD"]),
            ) as driver:
                rows, _, _ = driver.execute_query(
                    "MATCH (n:SDAEntity {generation:$generation}) RETURN labels(n) AS labels",
                    generation=generation,
                )
                assert any("SDATest" in row["labels"] for row in rows)
                assert any("SDATable" in row["labels"] for row in rows)
            vector_client = MilvusClient(uri=os.environ["TEST_MILVUS_URI"])
            try:
                hits = vector_client.get(
                    collection_name="sda_" + generation,
                    ids=["test:extra", "table:shop.orders"],
                    output_fields=["metadata"],
                )
                assert {hit["metadata"]["scope"] for hit in hits} == {"tests", "tables"}
            finally:
                vector_client.close()
            assert build["counts"]["table"] == 20
            assert build["counts"]["filtered"] == 1
            assert c.get("/api/v1/tables", params={"database": "tmp"}).json() == []
            assert (
                c.put(
                    "/api/v1/filters",
                    json={
                        "database_include": [],
                        "database_exclude": [],
                        "task_include": [],
                        "task_exclude": [],
                    },
                ).status_code
                == 200
            )
            assert (
                c.put(
                    "/api/v1/settings", json={"values": {"dimension_value_limit": "2"}}
                ).status_code
                == 200
            )
            second = c.post("/api/v1/builds").json()["id"]
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                rebuilt = c.get(f"/api/v1/builds/{second}").json()
                if rebuilt["status"] != "running":
                    break
                time.sleep(0.2)
            assert rebuilt["status"] == "succeeded", rebuilt.get("error")
            assert rebuilt["counts"]["filtered"] == 0
            channel = next(
                t for t in c.get("/api/v1/tables").json() if t["name"] == "channels"
            )
            assert next(f for f in channel["fields"] if f["name"] == "os_type")[
                "dimension_too_many"
            ]
            assert (
                c.put(
                    "/api/v1/settings",
                    json={"values": {"dimension_value_limit": "1000"}},
                ).status_code
                == 200
            )
            third = c.post("/api/v1/builds").json()["id"]
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                final_build = c.get(f"/api/v1/builds/{third}").json()
                if final_build["status"] != "running":
                    break
                time.sleep(0.2)
            assert final_build["status"] == "succeeded", final_build.get("error")
            with GraphDatabase.driver(
                os.environ["TEST_NEO4J_URI"],
                auth=(os.environ["TEST_NEO4J_USER"], os.environ["TEST_NEO4J_PASSWORD"]),
            ) as driver:
                rows, _, _ = driver.execute_query(
                    "MATCH (n:SDATest {generation:$generation}) RETURN count(n) AS count",
                    generation=generation,
                )
                assert rows[0]["count"] == 1
            assert (
                c.get("/api/v1/tables", params={"database": "tmp"}).json()[0]["name"]
                == "scratch"
            )
            assert build["finished_at"]
            assert c.get("/api/v1/builds").json()[0]["id"] == third
            tables = c.get(
                "/api/v1/tables", params={"database": "shop", "search": "orders"}
            ).json()
            assert any(
                x["name"] == "orders" and any(f["partition"] for f in x["fields"])
                for x in tables
            )
            assert (
                c.put(
                    "/api/v1/settings",
                    json={"values": {"knowledge_generation": generation + "_other"}},
                ).status_code
                == 200
            )
            assert c.get("/api/v1/tables").json() == []
            assert (
                c.put(
                    "/api/v1/settings",
                    json={"values": {"knowledge_generation": generation}},
                ).status_code
                == 200
            )
            assert (
                httpx.post(base + "/mcp/", timeout=5, trust_env=False).status_code
                == 401
            )
            assert (
                httpx.post(
                    base + "/mcp/",
                    headers={"Authorization": "Bearer wrong"},
                    timeout=5,
                    trust_env=False,
                ).status_code
                == 401
            )

        async def use_mcp():
            async with (
                httpx.AsyncClient(
                    headers=headers, timeout=180, trust_env=False
                ) as http_client,
                streamable_http_client(base + "/mcp/", http_client=http_client) as (
                    read,
                    write,
                    _,
                ),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                described = await session.call_tool(
                    "describe_tables", {"names": ["shop.orders"]}
                )
                table = described.structuredContent["result"][0]
                assert table["database"] == "shop" and table["comment"] == "交易订单"
                assert any(
                    f["name"] == "dt"
                    and f["type"] == "string"
                    and f["comment"] == "日期分区"
                    and f["partition"]
                    for f in table["fields"]
                )
                for keyword, field, value in [
                    ("安卓", "os_type", "android"),
                    ("android", "os_type", "android"),
                    ("香港", "os_type", " HK "),
                ]:
                    found = await session.call_tool(
                        "lookup_dim_values",
                        {"table": "shop.channels", "field": field, "keyword": keyword},
                    )
                    assert any(
                        x["value"] == value for x in found.structuredContent["result"]
                    )
                partial = await session.call_tool(
                    "lookup_dim_values",
                    {"table": "shop.channels", "field": "id", "keyword": "移动"},
                )
                assert any(
                    x["value"] == "移动端" and x["match"] == "fulltext"
                    for x in partial.structuredContent["result"]
                )
                bad_dim = await session.call_tool(
                    "lookup_dim_values",
                    {"table": "channels", "keyword": "安卓", "extra": 1},
                )
                assert bad_dim.isError
                calls.update(neo4j=0, milvus=0)
                start = time.monotonic()
                exact = await session.call_tool(
                    "search_knowledge",
                    {"query": "orders", "scope": "tables", "top_k": 5},
                )
                print("search_elapsed_ms=", round((time.monotonic() - start) * 1000))
                assert calls == {"neo4j": 1, "milvus": 1}
                print("storage_round_trips=", sum(calls.values()))
                assert "table:shop.orders" in str(exact)
                fields = await session.call_tool(
                    "search_knowledge", {"query": "dt", "scope": "fields", "top_k": 5}
                )
                assert "field:shop." in str(fields)
                semantic = await session.call_tool(
                    "search_knowledge",
                    {"query": "顾客购买交易记录", "scope": "tables", "top_k": 5},
                )
                assert "table:shop.orders" in str(semantic)
                bad = await session.call_tool(
                    "search_knowledge",
                    {"query": "orders", "scope": "tables", "extra": 1},
                )
                assert bad.isError

        asyncio.run(use_mcp())
    finally:
        release.set()
        server.should_exit = True
        thread.join(timeout=20)
        with GraphDatabase.driver(
            os.environ["TEST_NEO4J_URI"],
            auth=(os.environ["TEST_NEO4J_USER"], os.environ["TEST_NEO4J_PASSWORD"]),
        ) as driver:
            driver.execute_query(
                "MATCH (n:SDAEntity {generation:$generation}) DETACH DELETE n",
                generation=generation,
            )
        client = MilvusClient(uri=os.environ["TEST_MILVUS_URI"])
        if client.has_collection("sda_" + generation):
            client.drop_collection("sda_" + generation)
        client.close()
        assert not thread.is_alive()
