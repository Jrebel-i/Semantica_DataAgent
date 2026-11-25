"""Tests for standalone DTB (AI Gov gateway) executor and puller plugins."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from semantica_dataagent.dtb_ai_gateway import (
    DEFAULT_AI_GOV_DTB_URL,
    DtbAiGatewayExecutor,
    DtbAiGatewayFetcher,
    DtbAiGatewayPuller,
)
from semantica_dataagent.dtb_executor import DtbAuthenticationError, DtbError
from semantica_dataagent.dtb_puller import (
    DtbAuthenticationError as DtbPullAuthenticationError,
)
from semantica_dataagent.plugins import PullContext, discover


def test_discover_registers_standalone_dtb_ai_gateway_plugins():
    found = discover()
    assert ("executor", "dtb") not in found
    assert ("puller", "dtb") not in found
    assert ("parser", "dtb") in found
    assert ("executor", "dtb_ai_gateway") in found
    assert ("puller", "dtb_ai_gateway") in found
    assert found[("executor", "dtb_ai_gateway")].display_name == "DTB (AI Gov 网关)"
    assert found[("puller", "dtb_ai_gateway")].display_name == "DTB (AI Gov 网关)"


def test_gateway_executor_protocol_headers_envelopes_and_download():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/api/v1/gateway/call/ai-dtb/async-execute":
            return httpx.Response(200, json={"data": {"queryKey": "gw-q1"}})
        if request.url.path == "/api/v1/gateway/call/ai-dtb/result/gw-q1":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "status": "200",
                        "data": {
                            "progress": 1,
                            "columns": [{"name": "c", "type": "bigint"}],
                            "data": [[42]],
                        },
                    }
                },
            )
        if request.url.path == "/api/v1/gateway/call/ai-dtb/download/gw-q1":
            return httpx.Response(
                200,
                content=b"c\n42\n",
                headers={
                    "Content-Disposition": 'attachment; filename="gw-q1.csv"',
                    "Content-Type": "text/csv",
                },
            )
        return httpx.Response(404)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            executor = DtbAiGatewayExecutor(
                DEFAULT_AI_GOV_DTB_URL,
                "dev-key-123",
                env=2,
                data_source="presto",
                client=client,
                retry_delays=(),
                poll_interval=0,
            )
            key = await executor.submit("SELECT 42", 500)
            assert key == "gw-q1"
            result = await executor.wait(key)
            assert result["data"] == [[42]]
            content, content_type = await executor.download(key, "csv")
            assert content == b"c\n42\n"
            assert content_type == "text/csv"
            assert await executor.estimate("SELECT 42") == {
                "partitions": None,
                "rows": None,
            }

    asyncio.run(run())
    assert json.loads(calls[0].content) == {
        "env": 2,
        "dataSource": "presto",
        "sql": "SELECT 42",
        "defaultLimit": 500,
    }
    for call in calls:
        assert call.headers["X-Agent-Key"] == "dev-key-123"
        assert "token" not in call.headers
        assert "cookie" not in call.headers


def test_gateway_executor_rejects_auth_errors_bad_downloads_and_missing_table():
    async def run():
        # 1. Invalid key format rejected before network call
        bad_key_exec = DtbAiGatewayExecutor(
            DEFAULT_AI_GOV_DTB_URL, "bad key with space", retry_delays=()
        )
        with pytest.raises(DtbAuthenticationError):
            await bad_key_exec.submit("SELECT 1", 10)

        # 2. HTTP 401 / 403 and business 401 / 403 / 503 map to DtbAuthenticationError
        for resp in [
            httpx.Response(401),
            httpx.Response(403),
            httpx.Response(200, json={"data": {"status": "403", "message": "denied"}}),
            httpx.Response(
                200,
                json={"data": {"status": "503", "message": "roleId cannot be null"}},
            ),
        ]:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _, r=resp: r)
            ) as client:
                ex = DtbAiGatewayExecutor(
                    DEFAULT_AI_GOV_DTB_URL, "dev-key", client=client, retry_delays=()
                )
                with pytest.raises(DtbAuthenticationError):
                    await ex.poll("q1")

        # 3. Missing Content-Disposition with error JSON raises error
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, json={"data": {"status": "500", "message": "failed"}}
                )
            )
        ) as client:
            ex = DtbAiGatewayExecutor(
                DEFAULT_AI_GOV_DTB_URL, "dev-key", client=client, retry_delays=()
            )
            with pytest.raises(DtbError):
                await ex.download("q1", "csv")

        # 4. Small JSON error even when Content-Disposition exists must NOT pass through
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={"data": {"status": "403", "message": "forbidden"}},
                    headers={"Content-Disposition": 'attachment; filename="err.json"'},
                )
            )
        ) as client:
            ex = DtbAiGatewayExecutor(
                DEFAULT_AI_GOV_DTB_URL, "dev-key", client=client, retry_delays=()
            )
            with pytest.raises(DtbAuthenticationError):
                await ex.download("q1", "csv")

        # 5. Missing table message classifies as table_or_catalog
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "data": {
                            "status": "200",
                            "data": {
                                "progress": 1.0,
                                "errorMessage": "Table[foo_bar] does not exist in database[dm]",
                            },
                        }
                    },
                )
            )
        ) as client:
            ex = DtbAiGatewayExecutor(
                DEFAULT_AI_GOV_DTB_URL, "dev-key", client=client, retry_delays=()
            )
            with pytest.raises(DtbError) as exc_info:
                await ex.poll("q1")
            assert exc_info.value.failure_category == "table_or_catalog"

    asyncio.run(run())


def test_gateway_puller_fetcher_and_plugin_pull(tmp_path, monkeypatch):
    submitted_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Agent-Key"] == "sync-dev-key"
        assert "token" not in request.headers
        path = request.url.path
        if path == "/api/v1/gateway/call/ai-dtb/async-execute":
            payload = json.loads(request.content)
            submitted_payloads.append(payload)
            sql = payload["sql"]
            if "information_schema.columns" in sql or ("ORDER BY a.table_id" in sql):
                key = "meta-1"
            elif "task_version" in sql:
                key = "task-1"
            elif "metadata_partition" in sql:
                key = "stats-1"
            elif "hive.dimensions.dim_flag" in sql:
                key = "dim-1"
            else:
                key = "tbl-1"
            return httpx.Response(200, json={"data": {"queryKey": key}})
        if path.startswith("/api/v1/gateway/call/ai-dtb/result/"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "status": "200",
                        "data": {"progress": 1.0, "totalCount": 1},
                    }
                },
            )
        if path == "/api/v1/gateway/call/ai-dtb/download/meta-1":
            return httpx.Response(
                200,
                json={
                    "columns": [
                        {"name": col}
                        for col in (
                            "table_id",
                            "table_name",
                            "database_name",
                            "create_time",
                            "modify_time",
                            "comment",
                            "output_task_id",
                            "output_task_name",
                            "use_direction",
                            "column_name",
                            "seq_no",
                            "column_type",
                            "column_comment",
                            "column_business_comment",
                        )
                    ],
                    "data": [
                        [
                            "1",
                            "dim_flag",
                            "dimensions",
                            None,
                            None,
                            "标记维表",
                            None,
                            None,
                            None,
                            "flag_code",
                            1,
                            "string",
                            "标记编码",
                            None,
                        ]
                    ],
                },
                headers={"Content-Disposition": 'attachment; filename="meta.json"'},
            )
        if path == "/api/v1/gateway/call/ai-dtb/download/stats-1":
            return httpx.Response(
                200,
                content=(
                    b"table_id,table_name,database_name,create_time,modify_time,comment,"
                    b"partition_id,partition_name,records,partition_create_time,rk\n"
                ),
                headers={"Content-Disposition": 'attachment; filename="stats.csv"'},
            )
        if path == "/api/v1/gateway/call/ai-dtb/download/dim-1":
            return httpx.Response(
                200,
                json={
                    "columns": [{"name": "flag_code", "type": "VARCHAR"}],
                    "data": [["Y"], ["N"]],
                },
                headers={"Content-Disposition": 'attachment; filename="dim.json"'},
            )
        if path == "/api/v1/gateway/call/ai-dtb/download/task-1":
            return httpx.Response(
                200,
                json={
                    "columns": [
                        {"name": "task_name"},
                        {"name": "task_type"},
                        {"name": "task_src"},
                    ],
                    "data": [],
                },
                headers={"Content-Disposition": 'attachment; filename="tasks.json"'},
            )
        if path == "/api/v1/gateway/call/ai-dtb/download/tbl-1":
            import openpyxl

            wb = openpyxl.Workbook()
            ws = wb.active
            ws.append(
                [
                    "table_id",
                    "table_name",
                    "database_name",
                    "comment",
                    "output_task_id",
                    "output_task_name",
                    "column_name",
                    "seq_no",
                    "column_type",
                    "column_comment",
                    "column_business_comment",
                ]
            )
            ws.append(
                [
                    1,
                    "dim_flag",
                    "dimensions",
                    "标记维表",
                    "",
                    "",
                    "flag_code",
                    1,
                    "string",
                    "标记编码",
                    "",
                ]
            )
            wb_path = tmp_path / "tables.xlsx"
            wb.save(wb_path)
            return httpx.Response(
                200,
                content=wb_path.read_bytes(),
                headers={
                    "Content-Disposition": 'attachment; filename="tables.xlsx"',
                },
            )
        return httpx.Response(404)

    fetcher = DtbAiGatewayFetcher(
        env=2, data_source="doris_ai", metadata_row_limit=5000
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        # Read-only SQL guard
        with pytest.raises(ValueError, match="not permitted"):
            fetcher.query_download(
                client,
                DEFAULT_AI_GOV_DTB_URL,
                "sync-dev-key",
                "DROP TABLE foo",
                "json",
            )
        # Missing / invalid key maps to DtbPullAuthenticationError
        with pytest.raises(DtbPullAuthenticationError):
            fetcher.query_download(
                client,
                DEFAULT_AI_GOV_DTB_URL,
                "bad key",
                "SELECT 1",
                "json",
            )
        # Normal fetch verifies metadata_row_limit override when row_limit=0
        raw = fetcher.query_download(
            client,
            DEFAULT_AI_GOV_DTB_URL,
            "sync-dev-key",
            "SELECT * FROM information_schema.columns",
            "json",
            poll_interval=0,
            row_limit=0,
        )
        assert len(json.loads(raw)["data"]) == 1
        assert submitted_payloads[-1]["defaultLimit"] == 5000

    # Verify full DtbAiGatewayPuller.pull orchestration producing a PullArtifact
    from semantica_dataagent import dtb_ossie

    real_client = httpx.Client
    monkeypatch.setattr(
        dtb_ossie.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler)),
    )
    puller = DtbAiGatewayPuller(
        fetcher=DtbAiGatewayFetcher(
            env=2, data_source="doris_ai", metadata_row_limit=100
        )
    )
    assert "url" in puller.parameters_schema["properties"]
    assert "metadata_row_limit" in puller.parameters_schema["properties"]
    with pytest.raises(ValueError, match="token is required"):
        puller.pull(
            PullContext(
                project_id="default",
                instance_id="inst-1",
                parameters={"url": DEFAULT_AI_GOV_DTB_URL, "env": 2},
                secrets={},
            )
        )
    artifact = puller.pull(
        PullContext(
            project_id="default",
            instance_id="inst-1",
            parameters={
                "url": DEFAULT_AI_GOV_DTB_URL,
                "env": 2,
                "data_source": "doris_ai",
                "metadata_row_limit": 100,
                "model_name": "gw_model",
            },
            secrets={"token": "sync-dev-key"},
        )
    )
    assert "gw_model.yaml" in artifact.ossie_models
    assert artifact.tasks == []
    assert artifact.table_count == 1


def test_project_executor_switching_to_dtb_ai_gateway(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))

    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    token = initialize()
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)

    listing = client.get("/api/v1/projects/default/executors", headers=headers)
    assert listing.status_code == 200
    by_id = {item["id"]: item for item in listing.json()}
    assert "dtb" not in by_id
    assert "dtb_ai_gateway" in by_id
    assert by_id["dtb_ai_gateway"]["name"] == "DTB (AI Gov 网关)"

    legacy_req = client.put(
        "/api/v1/plugins/executor/dtb",
        headers=headers,
        json={"enabled": True, "parameters": {"base_url": "http://dtb.local"}},
    )
    assert legacy_req.status_code in (404, 422)

    switched = client.put(
        "/api/v1/projects/default/executors",
        headers=headers,
        json={
            "executors": [
                {
                    "id": "dtb_ai_gateway",
                    "enabled": True,
                    "parameters": {
                        "base_url": DEFAULT_AI_GOV_DTB_URL,
                        "env": 2,
                        "data_source": "doris_ai",
                    },
                },
            ]
        },
    )
    assert switched.status_code == 200
    updated = {item["id"]: item for item in switched.json()}
    assert updated["dtb_ai_gateway"]["enabled"] is True
    assert updated["dtb_ai_gateway"]["parameters"]["base_url"] == DEFAULT_AI_GOV_DTB_URL
