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
    {
        "database": "shop",
        "table": "orders",
        "name": "id",
        "type": "string",
        "comment": "",
        "partition": False,
    },
    {
        "database": "shop",
        "table": "orders",
        "name": "dt",
        "type": "string",
        "comment": "",
        "partition": True,
    },
    {
        "database": "shop",
        "table": "orders",
        "name": "amount",
        "type": "decimal",
        "comment": "",
        "partition": False,
    },
]

PARTITION_METADATA_COLUMNS = [{"name": "partition_name", "type": "char"}]


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
    def __init__(self, status=200, metadata=None):
        self.calls = []
        self.status = status
        self.metadata = (
            metadata
            if metadata is not None
            else {
                "columns": PARTITION_METADATA_COLUMNS,
                "data": [["dt=2025-01-03"]],
            }
        )
        self.queries = {}

    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.calls.append((self.headers.get("token"), body))
                key = f"sample-{len(fake.calls)}"
                fake.queries[key] = body.get("sql", "")
                if fake.status == 401:
                    self.send_response(401)
                    self.end_headers()
                    return
                payload = json.dumps(
                    {"status": 200, "data": {"queryKey": key}}
                ).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                key = self.path.rstrip("/").rsplit("/", 1)[-1]
                sql = fake.queries.get(key, "")
                if "metadata_table" in sql:
                    data = {"progress": 1, **fake.metadata}
                else:
                    data = {
                        "progress": 1,
                        "columns": [
                            {"name": "id", "type": "long"},
                            {"name": "dt", "type": "date"},
                        ],
                        "data": [[1, "2025-01-03"], [2, "2025-01-03"]],
                    }
                payload = json.dumps({"status": 200, "data": data}).encode()
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


def test_sample_defaults_to_all_columns_and_caps_them_at_thirty(client, monkeypatch):
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


def test_sample_select_list_does_not_include_partition_predicate_columns(
    client, table_metadata
):
    import sqlglot

    fake = FakeDtb().start()
    try:
        configure_executor(client, fake.url)
        set_token(client, "caller-token")
        result = call(
            client,
            "sample_table_data",
            {"table": "shop.orders", "columns": ["id"], "limit": 1},
        )
        assert not result.isError, result.model_dump()
        sql = fake.calls[-1][1]["sql"]
        tree = sqlglot.parse_one(sql, read="doris")
        assert [expression.name for expression in tree.expressions] == ["id"]
        assert "`dt` = '2025-01-03'" in sql
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
    monkeypatch.setattr(knowledge, "dimension_values", lambda *args: [], raising=False)
    set_token(client, "caller-token")
    result = call(client, "sample_table_data", {"table": "shop.orders"})
    assert result.isError
    assert result.structuredContent["code"] == "unavailable"
    assert "最新分区" in result.structuredContent["message"]


def test_sample_without_token_or_partition_reports_missing_token(client, fact_metadata):
    fake = FakeDtb().start()
    try:
        configure_executor(client, fake.url)
        result = call(client, "sample_table_data", {"table": "dm.sales_fact"})
        assert result.isError
        assert result.structuredContent["code"] == "invalid_input"
        assert "DTB token" in result.structuredContent["message"]
        assert not fake.calls
    finally:
        fake.close()


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
    columns_schema = properties["columns"]
    assert columns_schema.get("type") == "array" or any(
        option.get("type") == "array" for option in columns_schema.get("anyOf", [])
    )


FACT_FIELDS = [
    {
        "database": "dm",
        "table": "sales_fact",
        "name": "id",
        "type": "string",
        "comment": "",
        "partition": False,
    },
    {
        "database": "dm",
        "table": "sales_fact",
        "name": "dt",
        "type": "string",
        "comment": "",
        "partition": True,
    },
    {
        "database": "dm",
        "table": "sales_fact",
        "name": "amount",
        "type": "decimal",
        "comment": "",
        "partition": False,
    },
]


@pytest.fixture
def fact_metadata(monkeypatch):
    from semantica_dataagent import knowledge

    def describe(names=None, **kwargs):
        if names and "dm.sales_fact" not in names and "sales_fact" not in names:
            return []
        return [
            {
                "database": "dm",
                "name": "sales_fact",
                "comment": "sales fact",
                "fields": FACT_FIELDS,
            }
        ]

    monkeypatch.setattr(knowledge, "describe", describe)
    monkeypatch.setattr(knowledge, "dimension_values", lambda *args: [], raising=False)


def test_sample_uses_dtb_partition_metadata_for_fact_table(client, fact_metadata):
    fake = FakeDtb(
        metadata={
            "columns": PARTITION_METADATA_COLUMNS,
            "data": [["dt=2026-08-31"]],
        }
    ).start()
    try:
        configure_executor(client, fake.url)
        set_token(client, "caller-token")
        result = call(
            client,
            "sample_table_data",
            {"table": "dm.sales_fact", "columns": ["id"], "limit": 2},
        )
        assert not result.isError, result.model_dump()
        data = result.structuredContent
        assert data["partition"] == [{"field": "dt", "value": "2026-08-31"}]
        assert data["row_count"] == 2
        assert data["sql"].endswith("LIMIT 2")
        assert "`dt` = '2026-08-31'" in " ".join(data["sql"].split())

        metadata_calls = [
            (token, body)
            for token, body in fake.calls
            if "metadata_table" in body["sql"]
        ]
        assert len(metadata_calls) == 1
        token, request = metadata_calls[0]
        assert token == "caller-token"
        assert request["dataSource"] == "doris_ai"
        assert 1 <= request["defaultLimit"] <= 5
        metadata_sql = " ".join(request["sql"].split())
        assert "mysql.data_tool_box_emr.metadata_table" in metadata_sql
        assert "mysql.data_tool_box_emr.metadata_partition" in metadata_sql
        assert "database_name = 'dm'" in metadata_sql
        assert "table_name = 'sales_fact'" in metadata_sql
        assert "split_part(partition_name, '=', 2)" in metadata_sql
        assert metadata_sql.upper().startswith("SELECT ")
    finally:
        fake.close()


def test_metadata_accepts_plain_string_columns(client, fact_metadata):
    fake = FakeDtb(
        metadata={"columns": ["partition_name"], "data": [["dt=2026-08-31"]]}
    ).start()
    try:
        configure_executor(client, fake.url)
        set_token(client, "caller-token")
        result = call(client, "sample_table_data", {"table": "dm.sales_fact"})
        assert not result.isError, result.model_dump()
        assert result.structuredContent["partition"] == [
            {"field": "dt", "value": "2026-08-31"}
        ]
    finally:
        fake.close()


def test_metadata_column_object_without_name_is_format_error(client, fact_metadata):
    fake = FakeDtb(
        metadata={"columns": [{"type": "char"}], "data": [["dt=2026-08-31"]]}
    ).start()
    try:
        configure_executor(client, fake.url)
        set_token(client, "caller-token")
        result = call(client, "sample_table_data", {"table": "dm.sales_fact"})
        assert result.isError
        assert result.structuredContent["code"] == "unavailable"
        assert "格式无效" in result.structuredContent["message"]
    finally:
        fake.close()


def test_metadata_latest_partition_wins_over_knowledge(client, table_metadata):
    fake = FakeDtb(
        metadata={
            "columns": PARTITION_METADATA_COLUMNS,
            "data": [["dt=2026-08-31"]],
        }
    ).start()
    try:
        configure_executor(client, fake.url)
        set_token(client, "caller-token")
        result = call(
            client,
            "sample_table_data",
            {"table": "shop.orders", "columns": ["id"], "limit": 1},
        )
        assert not result.isError, result.model_dump()
        assert result.structuredContent["partition"] == [
            {"field": "dt", "value": "2026-08-31"}
        ]
        sample_sql = " ".join(result.structuredContent["sql"].split())
        assert "`dt` = '2026-08-31'" in sample_sql
        assert "2025-01-03" not in sample_sql
    finally:
        fake.close()


def test_metadata_multiple_rows_uses_latest_partition(client, fact_metadata):
    fake = FakeDtb(
        metadata={
            "columns": PARTITION_METADATA_COLUMNS,
            "data": [["dt=2026-08-01"], ["dt=2026-08-31"], ["dt=2026-08-15"]],
        }
    ).start()
    try:
        configure_executor(client, fake.url)
        set_token(client, "caller-token")
        result = call(client, "sample_table_data", {"table": "dm.sales_fact"})
        assert not result.isError, result.model_dump()
        assert result.structuredContent["partition"] == [
            {"field": "dt", "value": "2026-08-31"}
        ]
    finally:
        fake.close()


def test_metadata_empty_falls_back_to_knowledge(client, table_metadata):
    fake = FakeDtb(metadata={"columns": PARTITION_METADATA_COLUMNS, "data": []}).start()
    try:
        configure_executor(client, fake.url)
        set_token(client, "caller-token")
        result = call(
            client,
            "sample_table_data",
            {"table": "shop.orders", "columns": ["id"]},
        )
        assert not result.isError, result.model_dump()
        assert result.structuredContent["partition"] == [
            {"field": "dt", "value": "2025-01-03"}
        ]
        assert any("metadata_table" in body["sql"] for _, body in fake.calls)
    finally:
        fake.close()


def test_metadata_empty_without_knowledge_is_unavailable(client, fact_metadata):
    fake = FakeDtb(metadata={"columns": PARTITION_METADATA_COLUMNS, "data": []}).start()
    try:
        configure_executor(client, fake.url)
        set_token(client, "caller-token")
        result = call(client, "sample_table_data", {"table": "dm.sales_fact"})
        assert result.isError
        assert result.structuredContent["code"] == "unavailable"
        assert "最新分区" in result.structuredContent["message"]
        assert "元数据" in result.structuredContent["message"]
    finally:
        fake.close()


def test_metadata_malformed_falls_back_to_knowledge(client, table_metadata):
    fake = FakeDtb(
        metadata={
            "columns": PARTITION_METADATA_COLUMNS,
            "data": [["not-a-partition"], [123], [None]],
        }
    ).start()
    try:
        configure_executor(client, fake.url)
        set_token(client, "caller-token")
        result = call(
            client,
            "sample_table_data",
            {"table": "shop.orders", "columns": ["id"]},
        )
        assert not result.isError, result.model_dump()
        assert result.structuredContent["partition"] == [
            {"field": "dt", "value": "2025-01-03"}
        ]
    finally:
        fake.close()


@pytest.mark.parametrize(
    "metadata",
    [
        {"columns": ["other"], "data": [["dt=2026-08-31"]]},
        {"columns": ["partition_name"], "data": [["dt=2026-08-31", "extra"]]},
        {"columns": [], "data": []},
    ],
)
def test_metadata_format_error_falls_back_to_knowledge(
    client, table_metadata, metadata
):
    fake = FakeDtb(metadata=metadata).start()
    try:
        configure_executor(client, fake.url)
        set_token(client, "caller-token")
        result = call(
            client,
            "sample_table_data",
            {"table": "shop.orders", "columns": ["id"]},
        )
        assert not result.isError, result.model_dump()
        assert result.structuredContent["partition"] == [
            {"field": "dt", "value": "2025-01-03"}
        ]
    finally:
        fake.close()


def test_metadata_malformed_without_knowledge_is_unavailable(client, fact_metadata):
    fake = FakeDtb(
        metadata={
            "columns": PARTITION_METADATA_COLUMNS,
            "data": [["amount=9"], ["dt="], ["dt"]],
        }
    ).start()
    try:
        configure_executor(client, fake.url)
        set_token(client, "caller-token")
        result = call(client, "sample_table_data", {"table": "dm.sales_fact"})
        assert result.isError
        assert result.structuredContent["code"] == "unavailable"
        assert "最新分区" in result.structuredContent["message"]
        assert "partition_name" in result.structuredContent["message"]
    finally:
        fake.close()


@pytest.mark.parametrize(
    "database,table",
    [
        ("shop'; DROP TABLE x; --", "orders"),
        ("shop", 'orders"; DROP TABLE x; --'),
        ("shop`", "orders"),
        ("shop\n", "orders"),
        ("shop", "orders;--"),
        ("shop", "orders`"),
    ],
)
def test_metadata_partition_sql_rejects_injection(database, table):
    from semantica_dataagent.mcp_contract import ToolInvalidInput
    from semantica_dataagent.sample_api import _metadata_partition_sql

    with pytest.raises(ToolInvalidInput):
        _metadata_partition_sql(database, table)


def test_metadata_query_validator_accepts_generated_query():
    from semantica_dataagent.sample_api import (
        _metadata_partition_sql,
        _validate_metadata_sql,
    )

    sql = _metadata_partition_sql("shop", "orders")
    _validate_metadata_sql(sql, "shop", "orders")


def test_metadata_query_validator_rejects_other_tables_and_filters():
    from semantica_dataagent.mcp_contract import ToolUnavailable
    from semantica_dataagent.sample_api import (
        _metadata_partition_sql,
        _validate_metadata_sql,
    )

    sql = _metadata_partition_sql("shop", "orders")
    with pytest.raises(ToolUnavailable):
        _validate_metadata_sql(
            sql.replace("metadata_partition", "other_partition"), "shop", "orders"
        )
    with pytest.raises(ToolUnavailable):
        _validate_metadata_sql(
            sql.replace(
                "WHERE t.database_name", "WHERE t.owner_id = 'x' AND t.database_name"
            ),
            "shop",
            "orders",
        )
    with pytest.raises(ToolUnavailable):
        _validate_metadata_sql(
            sql.replace("AND t.status = '在用'", "OR t.status = '在用'"),
            "shop",
            "orders",
        )


def test_sample_rejects_injection_in_knowledge_table_metadata(client, monkeypatch):
    from semantica_dataagent import knowledge

    fields = [
        {
            "database": "shop'; DROP TABLE x; --",
            "table": "orders",
            "name": "dt",
            "type": "string",
            "comment": "",
            "partition": True,
        }
    ]
    monkeypatch.setattr(
        knowledge,
        "describe",
        lambda names=None, **kwargs: [
            {
                "database": "shop'; DROP TABLE x; --",
                "name": "orders",
                "comment": "",
                "fields": fields,
            }
        ],
    )
    monkeypatch.setattr(
        knowledge, "dimension_values", lambda *args: ["2025-01-03"], raising=False
    )
    fake = FakeDtb().start()
    try:
        configure_executor(client, fake.url)
        set_token(client, "caller-token")
        result = call(client, "sample_table_data", {"table": "shop.orders"})
        assert result.isError
        assert result.structuredContent["code"] == "invalid_input"
        assert "不合法" in result.structuredContent["message"]
        assert not fake.calls
    finally:
        fake.close()
