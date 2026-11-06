"""Public behavior of the common MCP registration boundary."""

import asyncio
import json
import socket

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolRequest, CallToolRequestParams
from neo4j.exceptions import ServiceUnavailable
from pymilvus.exceptions import (
    CollectionNotExistException,
    ConnectError,
    MilvusException,
    MilvusUnavailableException,
)
from semantica.utils.exceptions import ProcessingError

from semantica_dataagent.mcp_contract import install_contract


def test_execute_query_is_available_to_mcp_clients():
    from semantica_dataagent.app import create_mcp

    async def run():
        async with create_connected_server_and_client_session(create_mcp()) as client:
            tools = (await client.list_tools()).tools
        assert "execute_query" in {tool.name for tool in tools}

    asyncio.run(run())


def test_all_mcp_tool_input_schemas_are_self_contained():
    from semantica_dataagent.app import create_mcp

    def forbidden_paths(value, path):
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}"
                if key in {"$ref", "$defs"}:
                    yield child_path
                yield from forbidden_paths(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                yield from forbidden_paths(child, f"{path}[{index}]")

    async def run():
        async with create_connected_server_and_client_session(create_mcp()) as client:
            tools = (await client.list_tools()).tools
        assert tools, "MCP client returned no tools"
        violations = [
            f"{tool.name}: {path}"
            for tool in tools
            for path in forbidden_paths(tool.inputSchema, "inputSchema")
        ]
        assert not violations, "MCP schemas contain $ref or $defs:\n" + "\n".join(
            violations
        )

    asyncio.run(run())


def call_registered(server, name, arguments):
    async def run():
        server.get_context = lambda: None
        handler = server._mcp_server.request_handlers[CallToolRequest]
        return (
            await handler(
                CallToolRequest(
                    method="tools/call",
                    params=CallToolRequestParams(name=name, arguments=arguments),
                )
            )
        ).root

    return asyncio.run(run())


@pytest.fixture
def unavailable_port():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()


def storage_values(port):
    return {
        "neo4j_uri": f"bolt://127.0.0.1:{port}",
        "neo4j_user": "neo4j",
        "neo4j_password": "private-password",
        "milvus_uri": f"http://127.0.0.1:{port}",
        "embedding_url": f"http://127.0.0.1:{port}",
        "embedding_model": "test/model",
        "knowledge_generation": "default",
    }


@pytest.mark.parametrize(
    ("failure", "code", "retryable"),
    [
        (ValueError("private invalid data"), "invalid_input", False),
        (httpx.ConnectError("private host"), "unavailable", True),
        (httpx.ReadTimeout("private host"), "unavailable", True),
        (ServiceUnavailable("private host"), "unavailable", True),
        (ConnectError(message="private host"), "unavailable", True),
        (MilvusUnavailableException(message="private host"), "unavailable", True),
        (RuntimeError("private stack"), "internal_error", False),
    ],
)
def test_registered_tool_errors_are_classified_and_sanitized(failure, code, retryable):
    async def run():
        server = FastMCP("test")

        @server.tool()
        def added_tool(value: str) -> list[str]:
            raise failure

        install_contract(server)
        server.get_context = lambda: None
        handler = server._mcp_server.request_handlers[CallToolRequest]
        result = (
            await handler(
                CallToolRequest(
                    method="tools/call",
                    params=CallToolRequestParams(
                        name="added_tool", arguments={"value": "x"}
                    ),
                )
            )
        ).root
        assert result.isError
        assert result.structuredContent["code"] == code
        assert result.structuredContent["retryable"] is retryable
        assert result.structuredContent["next_action"]
        assert "private" not in json.dumps(result.model_dump())

    asyncio.run(run())


def test_newly_registered_tool_gets_validation_pagination_and_error_contract():
    async def run():
        server = FastMCP("test")

        @server.tool()
        def added_tool(value: str) -> list[str]:
            if value == "invalid":
                raise ValueError("private detail")
            return [str(i) for i in range(30)]

        install_contract(server)
        server.get_context = lambda: None
        handler = server._mcp_server.request_handlers[CallToolRequest]

        async def call(arguments):
            return (
                await handler(
                    CallToolRequest(
                        method="tools/call",
                        params=CallToolRequestParams(
                            name="added_tool", arguments=arguments
                        ),
                    )
                )
            ).root

        invalid = await call({"value": "x", "extra": 1})
        assert invalid.isError and invalid.structuredContent["code"] == "invalid_input"
        first = await call({"value": "x"})
        assert len(first.structuredContent["result"]) == 25
        second = await call(
            {"value": "x", "cursor": first.structuredContent["next_cursor"]}
        )
        assert second.structuredContent["result"] == [str(i) for i in range(25, 30)]
        error = await call({"value": "invalid"})
        assert error.isError and error.structuredContent["code"] == "invalid_input"
        assert error.structuredContent["retryable"] is False
        assert "private" not in json.dumps(error.model_dump())

    asyncio.run(run())


def test_existing_lineage_validation_and_registration_order():
    from semantica_dataagent.app import create_mcp

    async def run():
        server = create_mcp()
        server.get_context = lambda: None
        for tool in server._tool_manager.list_tools():
            assert tool.parameters["additionalProperties"] is False
            assert "cursor" in tool.parameters["properties"]
        handler = server._mcp_server.request_handlers[CallToolRequest]
        result = (
            await handler(
                CallToolRequest(
                    method="tools/call",
                    params=CallToolRequestParams(
                        name="get_lineage",
                        arguments={"table": "shop.orders", "depth": 4},
                    ),
                )
            )
        ).root
        assert result.isError
        assert result.structuredContent["code"] == "invalid_input"
        assert result.structuredContent["retryable"] is False

    asyncio.run(run())


def test_sql_tools_follow_the_uniform_registration_contract(monkeypatch):
    from semantica_dataagent import sql_validation
    from semantica_dataagent.app import create_mcp

    monkeypatch.setattr(
        sql_validation, "load_snapshot", lambda: sql_validation.Snapshot([], [], [])
    )
    server = create_mcp()
    names = {tool.name for tool in server._tool_manager.list_tools()}
    assert {"validate_sql", "find_join_path"} <= names

    valid = call_registered(server, "validate_sql", {"sql": "SELECT 1"})
    assert not valid.isError

    undeclared = call_registered(
        server,
        "validate_sql",
        {"sql": "SELECT 1", "database": "shop"},
    )
    assert undeclared.isError
    assert undeclared.structuredContent["code"] == "invalid_input"
    assert undeclared.structuredContent["retryable"] is False

    too_few = call_registered(server, "find_join_path", {"tables": ["shop.orders"]})
    assert too_few.isError
    assert too_few.structuredContent["code"] == "invalid_input"
    assert too_few.structuredContent["retryable"] is False


def test_find_join_path_rejects_table_count_without_loading_snapshot(monkeypatch):
    from semantica_dataagent import sql_validation
    from semantica_dataagent.app import create_mcp

    def fail():
        raise AssertionError("snapshot should not be loaded for invalid arguments")

    monkeypatch.setattr(sql_validation, "load_snapshot", fail)
    result = call_registered(
        create_mcp(), "find_join_path", {"tables": ["shop.orders"]}
    )

    assert result.isError
    assert result.structuredContent["code"] == "invalid_input"
    assert result.structuredContent["retryable"] is False


def test_get_capabilities_lists_sql_tools(monkeypatch):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import create_mcp

    monkeypatch.setattr(
        knowledge, "settings", lambda: {"knowledge_generation": "default"}
    )
    result = call_registered(create_mcp(), "get_capabilities", {})

    assert not result.isError
    assert {"validate_sql", "find_join_path"} <= set(result.structuredContent["tools"])


def test_embedding_retry_exhaustion_is_unavailable_through_registered_tool(
    unavailable_port, monkeypatch
):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import create_mcp

    values = storage_values(unavailable_port)
    values["embedding_url"] = f"http://127.0.0.1:{unavailable_port}"
    values.update(
        {
            "embedding_retries": "0",
            "embedding_backoff_seconds": "0",
            "embedding_timeout_seconds": "0.2",
        }
    )
    monkeypatch.setattr(knowledge, "settings", lambda: values)
    result = call_registered(
        create_mcp(),
        "search_knowledge",
        {"query": "orders", "scope": "tables", "top_k": 1},
    )

    assert result.isError
    assert result.structuredContent["code"] == "unavailable"
    assert result.structuredContent["retryable"] is True
    assert "private" not in json.dumps(result.model_dump())


def test_embedding_4xx_is_internal_error_through_registered_tool(monkeypatch):
    from test_embedding_client import FakeEmbedding

    from semantica_dataagent import knowledge
    from semantica_dataagent.app import create_mcp

    embedding = FakeEmbedding(actions=[{"status": 400}]).start()
    try:
        values = storage_values(1)
        values["embedding_url"] = embedding.url
        values.update(
            {
                "embedding_retries": "3",
                "embedding_backoff_seconds": "0",
                "embedding_timeout_seconds": "0.2",
            }
        )
        monkeypatch.setattr(knowledge, "settings", lambda: values)
        result = call_registered(
            create_mcp(),
            "search_knowledge",
            {"query": "orders", "scope": "tables", "top_k": 1},
        )
    finally:
        embedding.stop()

    assert result.isError
    assert result.structuredContent["code"] == "internal_error"
    assert result.structuredContent["retryable"] is False
    assert embedding.requests == 1
    assert "private" not in json.dumps(result.model_dump())


def test_semantica_wrapped_connection_failure_is_unavailable():
    async def run():
        server = FastMCP("test")

        @server.tool()
        def wrapped() -> str:
            try:
                raise ServiceUnavailable("private host")
            except ServiceUnavailable:
                raise ProcessingError("private query") from None

        install_contract(server)
        server.get_context = lambda: None
        handler = server._mcp_server.request_handlers[CallToolRequest]
        result = (
            await handler(
                CallToolRequest(
                    method="tools/call",
                    params=CallToolRequestParams(name="wrapped", arguments={}),
                )
            )
        ).root
        assert result.structuredContent["code"] == "unavailable"
        assert result.structuredContent["retryable"] is True
        assert "private" not in json.dumps(result.model_dump())

    asyncio.run(run())


def test_storage_constructors_wrap_unreachable_services(unavailable_port):
    from semantica_dataagent import knowledge
    from semantica_dataagent.storage_errors import StorageUnavailable

    values = storage_values(unavailable_port)
    with pytest.raises(StorageUnavailable) as graph_failure:
        knowledge.graph(values)
    assert graph_failure.value.__cause__ is not None

    with pytest.raises(StorageUnavailable) as milvus_failure:
        knowledge.milvus(values)
    assert milvus_failure.value.__cause__ is not None


def test_neo4j_connect_failure_is_unavailable_through_registered_tool(
    unavailable_port, monkeypatch
):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import create_mcp

    monkeypatch.setattr(knowledge, "settings", lambda: storage_values(unavailable_port))
    result = call_registered(create_mcp(), "describe_tables", {"names": []})

    assert result.isError
    assert result.structuredContent["code"] == "unavailable"
    assert result.structuredContent["retryable"] is True
    assert "private" not in json.dumps(result.model_dump())


def test_neo4j_query_disconnect_is_unavailable_through_registered_tool(monkeypatch):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import create_mcp
    from semantica_dataagent.storage_errors import StorageUnavailable

    class DisconnectedDriver:
        def verify_connectivity(self):
            return False

    class DisconnectedGraph:
        _store_backend = type("Backend", (), {"_driver": DisconnectedDriver()})()

        def execute_query(self, query, parameters):
            raise ProcessingError("Query execution failed: connection lost")

    values = storage_values(1)
    monkeypatch.setattr(knowledge, "settings", lambda: values)
    monkeypatch.setattr(knowledge, "read_graph", lambda _: DisconnectedGraph())
    result = call_registered(create_mcp(), "describe_tables", {"names": []})

    assert result.isError
    assert result.structuredContent["code"] == "unavailable"
    assert result.structuredContent["retryable"] is True
    assert "private" not in json.dumps(result.model_dump())

    with pytest.raises(StorageUnavailable) as caught:
        knowledge.execute(DisconnectedGraph(), "RETURN 1", {})
    assert caught.value.__cause__ is not None


def test_neo4j_query_error_with_live_connection_is_internal_error(monkeypatch):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import create_mcp

    class LiveDriver:
        def verify_connectivity(self):
            return True

    class InvalidQueryGraph:
        _store_backend = type("Backend", (), {"_driver": LiveDriver()})()

        def execute_query(self, query, parameters):
            raise ProcessingError("Query execution failed: invalid parameter")

    values = storage_values(1)
    monkeypatch.setattr(knowledge, "settings", lambda: values)
    monkeypatch.setattr(knowledge, "read_graph", lambda _: InvalidQueryGraph())
    result = call_registered(create_mcp(), "describe_tables", {"names": []})

    assert result.isError
    assert result.structuredContent["code"] == "internal_error"
    assert result.structuredContent["retryable"] is False
    assert "private" not in json.dumps(result.model_dump())

    with pytest.raises(ProcessingError):
        knowledge.execute(InvalidQueryGraph(), "RETURN 1", {})


def test_milvus_connect_failure_is_unavailable_through_registered_tool(
    unavailable_port, monkeypatch
):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import create_mcp
    from semantica_dataagent.storage_errors import StorageUnavailable

    values = storage_values(unavailable_port)
    monkeypatch.setattr(knowledge, "settings", lambda: values)
    monkeypatch.setattr(knowledge, "read_graph", lambda _: object())
    monkeypatch.setattr(knowledge, "execute", lambda *_: [])
    monkeypatch.setattr(
        knowledge, "embed", lambda _values, texts: [[0.0, 1.0] for _ in texts]
    )

    with pytest.raises(StorageUnavailable) as caught:
        knowledge.read_vectors(values)
    assert caught.value.__cause__ is not None

    result = call_registered(
        create_mcp(),
        "search_knowledge",
        {"query": "orders", "scope": "all", "top_k": 1},
    )
    assert result.isError
    assert result.structuredContent["code"] == "unavailable"
    assert result.structuredContent["retryable"] is True
    assert "private" not in json.dumps(result.model_dump())


def test_milvus_timeout_is_unavailable_through_registered_tool(monkeypatch):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import create_mcp

    class TimedOutVectors:
        def search(self, **kwargs):
            try:
                raise TimeoutError("private timeout")
            except TimeoutError as exc:
                raise MilvusException(message="deadline exceeded") from exc

    monkeypatch.setattr(knowledge, "settings", lambda: storage_values(1))
    monkeypatch.setattr(knowledge, "read_graph", lambda _: object())
    monkeypatch.setattr(knowledge, "execute", lambda *_: [])
    monkeypatch.setattr(
        knowledge, "embed", lambda _values, texts: [[0.0, 1.0] for _ in texts]
    )
    monkeypatch.setattr(knowledge, "read_vectors", lambda _: TimedOutVectors())
    result = call_registered(
        create_mcp(),
        "search_knowledge",
        {"query": "orders", "scope": "all", "top_k": 1},
    )

    assert result.isError
    assert result.structuredContent["code"] == "unavailable"
    assert result.structuredContent["retryable"] is True
    assert "private" not in json.dumps(result.model_dump())


def test_milvus_non_connectivity_failure_is_internal_error(monkeypatch):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import create_mcp

    class MissingCollectionVectors:
        def search(self, **kwargs):
            raise CollectionNotExistException(message="private collection")

    monkeypatch.setattr(knowledge, "settings", lambda: storage_values(1))
    monkeypatch.setattr(knowledge, "read_graph", lambda _: object())
    monkeypatch.setattr(knowledge, "execute", lambda *_: [])
    monkeypatch.setattr(
        knowledge, "embed", lambda _values, texts: [[0.0, 1.0] for _ in texts]
    )
    monkeypatch.setattr(knowledge, "read_vectors", lambda _: MissingCollectionVectors())
    result = call_registered(
        create_mcp(),
        "search_knowledge",
        {"query": "orders", "scope": "all", "top_k": 1},
    )

    assert result.isError
    assert result.structuredContent["code"] == "internal_error"
    assert result.structuredContent["retryable"] is False
    assert "private" not in json.dumps(result.model_dump())


def test_read_vectors_collection_error_is_not_unavailable(monkeypatch):
    from semantica_dataagent import knowledge

    class MissingCollectionClient:
        def __init__(self, uri):
            self.uri = uri

        def load_collection(self, name):
            raise CollectionNotExistException(message="private collection")

        def close(self):
            pass

    monkeypatch.setattr(knowledge, "MilvusClient", MissingCollectionClient)
    values = storage_values(7)
    values["knowledge_generation"] = "regression"

    with pytest.raises(CollectionNotExistException):
        knowledge.read_vectors(values)


def test_search_argument_errors_remain_invalid_input():
    from semantica_dataagent.app import create_mcp

    result = call_registered(
        create_mcp(),
        "search_knowledge",
        {"query": "orders", "scope": "all", "top_k": 0},
    )

    assert result.isError
    assert result.structuredContent["code"] == "invalid_input"
    assert result.structuredContent["retryable"] is False
