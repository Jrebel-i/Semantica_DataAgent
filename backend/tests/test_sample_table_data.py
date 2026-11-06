"""MCP sampling through system-generated SQL and the caller's DTB token."""

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from mcp.types import CallToolRequest, CallToolRequestParams


FIELDS = [
    {"database": "shop", "table": "orders", "name": "id", "type": "string", "comment": "", "partition": False},
    {"database": "shop", "table": "orders", "name": "dt", "type": "string", "comment": "", "partition": True},
    {"database": "shop", "table": "orders", "name": "amount", "type": "decimal", "comment": "", "partition": False},
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    token = initialize()
    with TestClient(app) as c:
        yield c, {"Authorization": f"Bearer {token}"}, token


@pytest.fixture
def table_metadata(monkeypatch):
    from semantica_dataagent import knowledge

    def describe(names=None, **kwargs):
        if names and "shop.orders" not in names and "orders" not in names:
            return []
        return [
            {
                "database": "shop",
                "name": "orders",
                "comment": "orders",
                "fields": FIELDS,
            }
        ]

    monkeypatch.setattr(knowledge, "describe", describe)
    monkeypatch.setattr(
        knowledge,
        "dimension_values",
        lambda database, table, field: ["2025-01-01", "2025-01-03", "2025-01-02"],
        raising=False,
    )
    return describe


class FakeDtb:
    def __init__(self, status=200):
        self.calls = []
        self.status = status

    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.calls.append((self.headers.get("token"), body))
                if fake.status == 401:
                    self.send_response(401)
                    self.end_headers()
                    return
                payload = json.dumps(
                    {"status": 200, "data": {"queryKey": "sample-key"}}
                ).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                payload = json.dumps(
                    {
                        "status": 200,
                        "data": {
                            "progress": 1,
                            "columns": ["id", "dt"],
                            "data": [[1, "2025-01-03"], [2, "2025-01-03"]],
                        },
                    }
                ).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"


def configure_executor(client, url):
    c, headers, _ = client
    response = c.put(
        "/api/v1/plugins/executor/dtb",
        headers=headers,
        json={
            "enabled": True,
            "parameters": {
                "base_url": url,
                "row_limit": 100,
                "download_limit": 100,
                "timeout_seconds": 2,
            },
        },
    )
    assert response.status_code == 200, response.text


def set_token(client, token):
    c, headers, _ = client
    response = c.put("/api/v1/me/dtb-token", headers=headers, json={"token": token})
    assert response.status_code == 200, response.text


def requester(token):
    from semantica_dataagent.auth import Requester
    from semantica_dataagent.db import Token, sessions

    with sessions()() as db:
        row = db.query(Token).first()
        return Requester(id=row.id, role=row.role, token=token)


def call(client, name, arguments):
    from semantica_dataagent.app import create_mcp
    from semantica_dataagent.mcp_context import bind_mcp_context

    _, _, token = client
    server = create_mcp()

    async def run():
        server.get_context = lambda: None
        handler = server._mcp_server.request_handlers[CallToolRequest]
        with bind_mcp_context(requester(token)):
            response = await handler(
                CallToolRequest(
                    method="tools/call",
                    params=CallToolRequestParams(name=name, arguments=arguments),
                )
            )
        return response.root

    return asyncio.run(run())


def test_sample_uses_requested_columns_latest_partition_and_limit(
    client, table_metadata
):
    fake = FakeDtb().start()
    try:
        configure_executor(client, fake.url)
        set_token(client, "caller-token")
        result = call(
            client,
            "sample_table_data",
            {"table": "shop.orders", "columns": ["id", "dt"], "limit": 2},
        )
        assert not result.isError, result.model_dump()
        data = result.structuredContent
        assert data["rows"] == [[1, "2025-01-03"], [2, "2025-01-03"]]
        assert data["row_count"] == 2
        assert data["partition"] == [{"field": "dt", "value": "2025-01-03"}]
        assert fake.calls[-1][0] == "caller-token"
        request = fake.calls[-1][1]
        assert request["dataSource"] == "doris_ai"
        assert request["defaultLimit"] == 2
        sql = " ".join(request["sql"].split())
        assert "`id`" in sql and "`dt`" in sql
        assert "amount" not in sql
        assert "`dt` = '2025-01-03'" in sql
        assert "2025-01-01" not in sql and "2025-01-02" not in sql
        assert sql.endswith("LIMIT 2")
        c, headers, _ = client
        audit = c.get("/api/v1/query-audit", headers=headers).json()
        sample = [row for row in audit if row["kind"] == "sample"]
        assert sample and sample[0]["sql"] == data["sql"]
        assert sample[0]["status"] == "succeeded"
        assert sample[0]["row_count"] == 2
        assert sample[0]["type"] == "取样"
    finally:
        fake.close()


def test_sample_defaults_to_all_columns_and_caps_them_at_thirty(
    client, monkeypatch
):
    from semantica_dataagent import knowledge

    fields = [
        {
            "database": "wide",
            "table": "events",
            "name": f"c{index:02d}",
            "type": "string",
            "comment": "",
            "partition": False,
        }
        for index in range(35)
    ]
    monkeypatch.setattr(
        knowledge,
        "describe",
        lambda names=None, **kwargs: [
            {"database": "wide", "name": "events", "comment": "", "fields": fields}
        ],
    )
    fake = FakeDtb().start()
    try:
        configure_executor(client, fake.url)
        set_token(client, "caller-token")
        result = call(client, "sample_table_data", {"table": "wide.events"})
        assert not result.isError, result.model_dump()
        data = result.structuredContent
        assert data["columns_truncated"] is True
        assert data["limit"] == 20
        sql = fake.calls[-1][1]["sql"]
        assert "c29" in sql and "c30" not in sql
        assert sql.endswith("LIMIT 20")
    finally:
        fake.close()


def test_sample_without_dtb_token_is_invalid_input(client, table_metadata):
    fake = FakeDtb().start()
    try:
        configure_executor(client, fake.url)
        result = call(client, "sample_table_data", {"table": "shop.orders"})
        assert result.isError
        assert result.structuredContent["code"] == "invalid_input"
        assert "DTB token" in result.structuredContent["message"]
        assert not fake.calls
        c, headers, _ = client
        audit = c.get("/api/v1/query-audit", headers=headers).json()
        assert any(
            row["kind"] == "sample" and row["status"] == "rejected" for row in audit
        )
    finally:
        fake.close()


def test_sample_dtb_401_is_invalid_input(client, table_metadata):
    fake = FakeDtb(status=401).start()
    try:
        configure_executor(client, fake.url)
        set_token(client, "expired")
        result = call(client, "sample_table_data", {"table": "shop.orders"})
        assert result.isError
        assert result.structuredContent["code"] == "invalid_input"
        assert "token" in result.structuredContent["message"].lower()
    finally:
        fake.close()


def test_sample_table_outside_current_generation_is_invalid_input(
    client, table_metadata
):
    result = call(client, "sample_table_data", {"table": "missing.table"})
    assert result.isError
    assert result.structuredContent["code"] == "invalid_input"
    assert "知识代" in result.structuredContent["message"]


def test_sample_missing_latest_partition_is_unavailable(client, monkeypatch):
    from semantica_dataagent import knowledge

    monkeypatch.setattr(
        knowledge,
        "describe",
        lambda names=None, **kwargs: [
            {
                "database": "shop",
                "name": "orders",
                "comment": "orders",
                "fields": FIELDS,
            }
        ],
    )
    monkeypatch.setattr(
        knowledge, "dimension_values", lambda *args: [], raising=False
    )
    result = call(client, "sample_table_data", {"table": "shop.orders"})
    assert result.isError
    assert result.structuredContent["code"] == "unavailable"
    assert "最新分区" in result.structuredContent["message"]


def test_sample_schema_is_self_contained_and_caps_limit(client):
    from semantica_dataagent.app import create_mcp

    async def run():
        return await create_mcp().list_tools()

    tools = asyncio.run(run())
    sample = next(tool for tool in tools if tool.name == "sample_table_data")
    serialized = json.dumps(sample.inputSchema)
    assert "$ref" not in serialized and "$defs" not in serialized
    properties = sample.inputSchema["properties"]
    assert set(properties) >= {"table", "columns", "limit", "cursor"}
    assert properties["limit"]["type"] == "integer"
    assert properties["limit"]["maximum"] == 20
    assert properties["limit"]["default"] == 20
    assert properties["columns"]["type"] == "array"
