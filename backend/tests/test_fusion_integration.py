"""Exercise fusion and the MCP contract against isolated test storage."""

import asyncio
import json
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
    reason="isolated TEST_* storage variables required",
)


def test_fusion_over_mcp(tmp_path, monkeypatch):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from semantica_dataagent import knowledge
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    generation = "test_" + uuid.uuid4().hex
    print(f"test generation: {generation}", flush=True)
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    token = initialize()
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
        ) as client:
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
            assert (
                client.put("/api/v1/settings", json={"values": values}).status_code
                == 200
            )
            build_id = client.post("/api/v1/builds").json()["id"]
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                build = client.get(f"/api/v1/builds/{build_id}").json()
                if build["status"] != "running":
                    break
                time.sleep(0.2)
            assert build["status"] == "succeeded", build.get("error")
            response = client.get(
                "/api/v1/search",
                params={
                    "query": "销售额",
                    "scope": "docs",
                    "category": "metrics",
                    "top_k": 20,
                },
            )
            assert response.status_code == 200, response.text
            assert any(hit["ambiguous"] and hit["sources"] for hit in response.json())

        async def run():
            async with (
                httpx.AsyncClient(
                    headers=headers, timeout=180, trust_env=False
                ) as client,
                streamable_http_client(base + "/mcp/", http_client=client) as (
                    read,
                    write,
                    _,
                ),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                capabilities = await session.call_tool("get_capabilities", {})
                assert (
                    capabilities.structuredContent["current_generation"] == generation
                )
                assert "search_knowledge" in capabilities.structuredContent["tools"]
                bad = await session.call_tool(
                    "describe_tables", {"names": [], "extra": 1}
                )
                assert bad.isError and bad.structuredContent["code"] == "invalid_input"
                failure = await session.call_tool(
                    "read_document", {"doc_id": "missing"}
                )
                assert (
                    failure.isError
                    and failure.structuredContent["code"] == "invalid_input"
                )
                for query, scope, expected in [
                    ("shop.orders", "tables", "table:shop.orders"),
                    ("交易订单", "tables", "table:shop.orders"),
                    ("顾客购买交易记录", "tables", "table:shop.orders"),
                ]:
                    before = knowledge.round_trip_counts()
                    result = await session.call_tool(
                        "search_knowledge",
                        {"query": query, "scope": scope, "top_k": 10},
                    )
                    after = knowledge.round_trip_counts()
                    assert not result.isError, result.content
                    assert sum(after[k] - before[k] for k in ("neo4j", "milvus")) <= 5
                    assert expected in json.dumps(
                        result.structuredContent, ensure_ascii=False
                    )
                result = await session.call_tool(
                    "search_knowledge",
                    {
                        "query": "销售额",
                        "scope": "docs",
                        "category": "metrics",
                        "top_k": 20,
                    },
                )
                assert not result.isError, result.content
                assert any(
                    item["ambiguous"] and len(item["ambiguity_refs"]) >= 2
                    for item in result.structuredContent["result"]
                )
                from semantica_dataagent import mcp_contract

                monkeypatch.setattr(mcp_contract, "PAGE_ITEMS", 2)
                page = await session.call_tool("list_categories", {})
                assert page.structuredContent["truncated"]
                next_page = await session.call_tool(
                    "list_categories", {"cursor": page.structuredContent["next_cursor"]}
                )
                assert (
                    len(page.structuredContent["result"])
                    + len(next_page.structuredContent["result"])
                    == 3
                )
                monkeypatch.setattr(mcp_contract, "PAGE_CHARS", 100)
                args = {"doc_id": "document:metrics/definitions/sales.md"}
                fragments = []
                while True:
                    page = await session.call_tool("read_document", args)
                    fragments.append(page.structuredContent["result_fragment"])
                    cursor = page.structuredContent.get("next_cursor")
                    if not cursor:
                        break
                    args["cursor"] = cursor
                assert "部分退款" in json.loads("".join(fragments))["content"]

        asyncio.run(run())
    finally:
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
