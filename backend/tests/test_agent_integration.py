"""Main seam: REST + SSE + real pi + real MCP tools against isolated test storage."""

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

NEO4J_URI_KEY = "neo4j" + "_uri"

STORAGE = (
    "TEST_NEO4J_URI",
    "TEST_NEO4J_USER",
    "TEST_NEO4J_PASSWORD",
    "TEST_MILVUS_URI",
    "TEST_EMBEDDING_URL",
)
pytestmark = pytest.mark.skipif(
    not all(os.getenv(x) for x in STORAGE)
    or not os.getenv("TEST_PI_BASE_URL")
    or not os.getenv("TEST_PI_MODEL"),
    reason="TEST_* storage and TEST_PI_BASE_URL/TEST_PI_MODEL required",
)


def _sse(block: str) -> tuple[str, dict] | None:
    event = data = None
    for line in block.splitlines():
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: "):
            data = json.loads(line[6:])
    return (event, data) if event is not None else None


def _ask(client, body, log):
    events = []
    with client.stream("POST", "/api/v1/conversations/ask", json=body) as response:
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if not line:
                continue
            log.append(line + "\n")
            if line.startswith("event: "):
                pending_event = line[7:]
            elif line.startswith("data: "):
                events.append((pending_event, json.loads(line[6:])))
    return events


def test_online_agent_with_real_pi_and_mcp(tmp_path, monkeypatch):
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    generation = "test_" + uuid.uuid4().hex
    print(f"test generation: {generation}", flush=True)
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_AGENT_ROOT", str(tmp_path / "agents"))
    token = initialize()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    base = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("DATAAGENT_PUBLIC_URL", base)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    headers = {"Authorization": f"Bearer {token}"}
    log: list[str] = []
    try:
        deadline = time.monotonic() + 15
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        with httpx.Client(
            base_url=base, headers=headers, timeout=900, trust_env=False
        ) as client:
            values = {
                NEO4J_URI_KEY: os.environ["TEST_NEO4J_URI"],
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
            build = {}
            while time.monotonic() < deadline:
                build = client.get(f"/api/v1/builds/{build_id}").json()
                if build["status"] != "running":
                    break
                time.sleep(0.2)
            assert build.get("status") == "succeeded", build.get("error")
            provider = client.post(
                "/api/v1/providers",
                json={
                    "name": "local gateway",
                    "type": "anthropic-messages",
                    "base_url": os.environ["TEST_PI_BASE_URL"],
                    "models": [os.environ["TEST_PI_MODEL"]],
                },
            )
            assert provider.status_code == 201, provider.text
            provider_id = provider.json()["id"]
            model = os.environ["TEST_PI_MODEL"]

            first = _ask(
                client,
                {
                    "question": (
                        "知识库里支付销售额的口径是什么？请用 shop.payments 表写一个"
                        "按 dt 统计记录数的 Hive SQL（单表查询，不要 JOIN 其他表）。"
                        "必须先用 search_knowledge 查口径和相关表，写出 SQL 后必须"
                        "调用 validate_sql，并在回答里附引用 id。"
                    ),
                    "agent": "pi",
                    "provider_id": provider_id,
                    "model": model,
                },
                log,
            )
            names = [name for name, _ in first]
            assert "conversation" in names and names[-1] == "done", names
            starts = [data for name, data in first if name == "tool_start"]
            ends = [data for name, data in first if name == "tool_end"]
            assert any("search_knowledge" in item.get("name", "") for item in starts), (
                starts
            )
            assert any(
                item.get("name", "").endswith("validate_sql") for item in ends
            ), ends
            assert any(
                item.get("name", "").endswith("validate_sql")
                and "pass" in item.get("summary", "")
                for item in ends
            ), ends
            done = [data for name, data in first if name == "done"][-1]
            assert done["sql"], done["text"]
            assert done["citations"], done["text"]
            conversation_id = first[0][1]["conversation_id"]
            assert token not in json.dumps(first, ensure_ascii=False)

            detail = client.get(f"/api/v1/conversations/{conversation_id}").json()
            assert detail["turns"][0]["status"] == "completed"
            assert detail["turns"][0]["sql"] == done["sql"]
            assert detail["turns"][0]["citations"] == done["citations"]

            second = _ask(
                client,
                {
                    "question": (
                        "在上一轮 SQL 的基础上只统计 dt >= '2024-01-01'，"
                        "其他口径不变；修改后的 SQL 仍必须调用 validate_sql 并附引用。"
                    ),
                    "conversation_id": conversation_id,
                },
                log,
            )
            assert [name for name, _ in second][-1] == "done"
            detail = client.get(f"/api/v1/conversations/{conversation_id}").json()
            assert [turn["sequence"] for turn in detail["turns"]] == [1, 2]
            assert all(turn["status"] == "completed" for turn in detail["turns"])

        session_files = list(
            (tmp_path / "agents" / conversation_id / "sessions").rglob("*.jsonl")
        )
        assert session_files, "pi session file was not persisted"
        session_text = "\n".join(
            path.read_text(encoding="utf-8", errors="replace") for path in session_files
        )
        assert "支付销售额的口径" in session_text
        assert "dt >= '2024-01-01'" in session_text
    finally:
        target = os.environ.get("TEST_AGENT_EVENT_LOG")
        if target:
            Path(target).write_text("".join(log), encoding="utf-8")
        server.should_exit = True
        server.force_exit = True
        thread.join(timeout=15)
        try:
            with (
                GraphDatabase.driver(
                    os.environ["TEST_NEO4J_URI"],
                    auth=(
                        os.environ["TEST_NEO4J_USER"],
                        os.environ["TEST_NEO4J_PASSWORD"],
                    ),
                ) as driver,
                driver.session() as session,
            ):
                session.run(
                    "MATCH (n:SDAEntity {generation:$generation}) DETACH DELETE n",
                    generation=generation,
                )
        finally:
            MilvusClient(os.environ["TEST_MILVUS_URI"]).drop_collection(
                "sda_" + generation
            )
