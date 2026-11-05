"""Main seam against isolated test services; set all TEST_* values to enable."""

import asyncio
import json
import os
import shutil
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
        storage.execute(
            "CREATE (:SDAEntity:SDATable {generation:$generation, database:'shop', name:'isolated', comment:'Isolated test table', ref:'table:shop.isolated'})",
            {"generation": generation},
        )
        storage.add_vectors(
            ["test:extra"],
            ["independent stage"],
            [{"scope": "tests", "ref": "test:extra"}],
        )
        return StageResult(counts={"test_stage": 1})

    def join_fixture(records, generation, storage):
        storage.execute(
            """UNWIND range(1, 25) AS n CREATE (:SDAEntity:SDATable {generation:$generation, database:'fixture', name:toString(n), comment:'test', ref:'table:fixture.' + toString(n)})""",
            {"generation": generation},
        )
        storage.execute(
            """MATCH (a:SDALineageTable {generation:$generation, name:'shop.orders'}), (b:SDALineageTable {generation:$generation, name:'shop.customers'}) CREATE (a)-[:DEPENDS_ON {upstream:'shop.orders', downstream:'shop.customers', ref:'task:test', task_id:'test', evidence:'declared', parse_status:'failed'}]->(b)""",
            {"generation": generation},
        )
        storage.execute(
            """MATCH (t:SDATable {generation:$generation, database:'shop'}) WHERE t.name IN ['orders','products'] CREATE (f:SDAEntity:SDAField {generation:$generation, name:'special_id', type:'string', comment:'test', partition:false, ref:'field:shop.' + t.name + '.special_id'}) CREATE (t)-[:HAS_FIELD]->(f)""",
            {"generation": generation},
        )
        return StageResult()

    # Run the additional stage first: table writes must preserve its data.
    monkeypatch.setattr(stages, "STAGES", [extra_stage, *stages.STAGES, join_fixture])
    source = tmp_path / "source"
    shutil.copytree(Path(__file__).resolve().parents[2] / "samples/demo", source)

    generation = "test_" + uuid.uuid4().hex
    print(f"test generation: {generation}", flush=True)
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    token = initialize()
    entered, release = threading.Event(), threading.Event()
    original_embed = knowledge.embed

    def delayed_embed(values, texts, stats=None):
        if len(texts) > 1:
            entered.set()
            if not release.wait(timeout=180):
                raise TimeoutError("Test did not release build")
        return original_embed(values, texts, stats)

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
                "source_directory": str(source),
                "knowledge_generation": generation,
            }
            assert c.put("/api/v1/settings", json={"values": values}).status_code == 200
            response = c.post("/api/v1/builds")
            assert response.status_code == 202, response.text
            ident = response.json()["id"]
            try:
                assert entered.wait(timeout=120), c.get(
                    f"/api/v1/builds/{ident}"
                ).json()
                assert c.post("/api/v1/builds").status_code == 409
            finally:
                release.set()
            build_started = time.monotonic()
            deadline = build_started + 900
            while time.monotonic() < deadline:
                build = c.get(f"/api/v1/builds/{ident}").json()
                if build["status"] != "running":
                    break
                time.sleep(0.2)
            print("build_elapsed_ms=", round((time.monotonic() - build_started) * 1000))
            assert build["status"] == "succeeded", build.get("error")
            assert build["counts"]["embedding_requests"] > 0
            assert (
                build["counts"]["embedding_successful_batches"]
                == build["counts"]["embedding_requests"]
            )
            assert build["counts"]["embedding_retries"] >= 0
            assert build["counts"]["embedding_elapsed_ms"] > 0
            print(
                "embedding_stats=",
                {
                    key: build["counts"][key]
                    for key in (
                        "embedding_requests",
                        "embedding_successful_batches",
                        "embedding_retries",
                        "embedding_elapsed_ms",
                    )
                },
            )
            assert build["counts"]["test_stage"] == 1
            assert any(
                f["task_id"] == "bad_sql" and f["reason"]
                for f in build["parse_failures"]
            )
            lineage = c.get(
                "/api/v1/table-lineage", params={"table": "shop.orders"}
            ).json()
            assert any(
                e["upstream"] == "staging.orders" and e["evidence"] == "both"
                for e in lineage["upstream"]
            )
            assert any(t["name"] == "load_orders" for t in lineage["tasks"])
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
            deadline = time.monotonic() + 900
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

            async def check_cache_field():
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
                    response = await session.call_tool(
                        "validate_sql",
                        {
                            "sql": "SELECT cache_probe FROM shop.orders",
                            "dialect": "hive",
                        },
                    )
                    return json.loads(response.content[0].text)

            calls.update(neo4j=0, milvus=0)
            assert asyncio.run(check_cache_field())["fields"][0]["status"] == "error"
            assert calls["neo4j"] <= 3
            calls.update(neo4j=0, milvus=0)
            assert asyncio.run(check_cache_field())["fields"][0]["status"] == "error"
            assert calls["neo4j"] == 0
            fields_path = source / "fields.json"
            fields = json.loads(fields_path.read_text())
            fields.append(
                {
                    "database": "shop",
                    "table": "orders",
                    "name": "cache_probe",
                    "type": "string",
                    "comment": "test",
                    "partition": False,
                }
            )
            fields_path.write_text(json.dumps(fields))
            third = c.post("/api/v1/builds").json()["id"]
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                final_build = c.get(f"/api/v1/builds/{third}").json()
                if final_build["status"] != "running":
                    break
                time.sleep(0.2)
            assert final_build["status"] == "succeeded", final_build.get("error")
            calls.update(neo4j=0, milvus=0)
            assert asyncio.run(check_cache_field())["fields"][0]["status"] == "pass"
            assert calls["neo4j"] <= 3
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
                categories = await session.call_tool("list_categories", {})
                assert {c["name"] for c in categories.structuredContent["result"]} == {
                    "metrics",
                    "operations",
                    "unindexed",
                }
                index = await session.call_tool(
                    "read_category_index", {"category": "unindexed"}
                )
                assert not index.isError, index.content
                assert json.loads(index.content[0].text)["generated"]
                document = await session.call_tool(
                    "read_document",
                    {
                        "doc_id": "document:metrics/definitions/sales.md",
                        "section": "退款处理",
                    },
                )
                assert "部分退款" in json.loads(document.content[0].text)["content"]
                assert json.loads(document.content[0].text)["start_line"] > 1
                docs = await session.call_tool(
                    "search_knowledge",
                    {
                        "query": "支付成功后退款金额如何计算",
                        "scope": "docs",
                        "category": "metrics",
                        "top_k": 10,
                    },
                )
                results = docs.structuredContent["result"]
                assert any("退款" in d["content"] for d in results)
                assert all(
                    d["category"] == "metrics"
                    and d["path"] != "index.md"
                    and d["ref"].startswith("document:")
                    for d in results
                )
                assert any(
                    d["path"] == "dictionaries/shop.orders.md"
                    for d in table["documents"]
                )
                assert (
                    await session.call_tool("list_categories", {"extra": 1})
                ).isError
                upstream = await session.call_tool(
                    "get_lineage",
                    {"table": "shop.orders", "direction": "up", "depth": 1},
                )
                assert "staging.orders" in str(upstream) and "both" in str(upstream)
                sql = await session.call_tool(
                    "read_task_sql", {"task_id": "load_orders"}
                )
                assert "INSERT OVERWRITE" in str(sql)
                invalid = await session.call_tool(
                    "get_lineage", {"table": "shop.orders", "depth": 4}
                )
                assert invalid.isError
                for dialect in ("hive", "spark", "doris"):
                    checked = await session.call_tool(
                        "validate_sql",
                        {"sql": "SELECT o.id FROM shop.orders o", "dialect": dialect},
                    )
                    assert json.loads(checked.content[0].text)["status"] == "pass", (
                        checked
                    )
                checked = await session.call_tool(
                    "validate_sql", {"sql": "SELECT FROM", "dialect": "hive"}
                )
                assert (
                    json.loads(checked.content[0].text)["syntax"]["status"] == "error"
                )
                checked = await session.call_tool(
                    "validate_sql",
                    {"sql": "SELECT missing FROM shop.orders", "dialect": "hive"},
                )
                assert (
                    json.loads(checked.content[0].text)["fields"][0]["status"]
                    == "error"
                )
                checked = await session.call_tool(
                    "validate_sql",
                    {"sql": "SELECT id FROM shop.ordrs", "dialect": "hive"},
                )
                assert (
                    json.loads(checked.content[0].text)["tables"][0]["status"]
                    == "error"
                )
                checked = await session.call_tool(
                    "validate_sql",
                    {
                        "sql": "SELECT o.id FROM shop.orders o JOIN shop.products c ON o.created_at = c.id",
                        "dialect": "hive",
                    },
                )
                assert (
                    json.loads(checked.content[0].text)["joins"][0]["status"]
                    == "unsupported"
                )
                path = await session.call_tool(
                    "find_join_path", {"tables": ["shop.orders", "shop.customers"]}
                )
                result = json.loads(path.content[0].text)
                assert result["status"] == "found" and result["strength"] == "strong"
                assert result["steps"][0]["evidence"][0]["parse_status"] == "failed"
                weak = await session.call_tool(
                    "validate_sql",
                    {
                        "sql": "SELECT o.id FROM shop.orders o JOIN shop.products p ON o.special_id = p.special_id",
                        "dialect": "hive",
                    },
                )
                assert json.loads(weak.content[0].text)["joins"][0]["status"] == "weak"
                path = await session.call_tool(
                    "find_join_path", {"tables": ["shop.orders", "shop.products"]}
                )
                assert json.loads(path.content[0].text)["strength"] == "weak"
                generic = await session.call_tool(
                    "validate_sql",
                    {
                        "sql": "SELECT o.id FROM shop.orders o JOIN shop.products p ON o.id = p.id",
                        "dialect": "hive",
                    },
                )
                assert (
                    json.loads(generic.content[0].text)["joins"][0]["status"]
                    == "unsupported"
                )
                path = await session.call_tool(
                    "find_join_path", {"tables": ["shop.orders", "shop.isolated"]}
                )
                assert json.loads(path.content[0].text)["status"] == "no_path"
                calls.update(neo4j=0, milvus=0)
                before = knowledge.round_trip_counts()
                start = time.monotonic()
                exact = await session.call_tool(
                    "search_knowledge",
                    {"query": "orders", "scope": "tables", "top_k": 5},
                )
                print("search_elapsed_ms=", round((time.monotonic() - start) * 1000))
                after = knowledge.round_trip_counts()
                assert sum(after[k] - before[k] for k in ("neo4j", "milvus")) <= 5
                assert sum(calls.values()) <= 5
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
