"""Public behavior of the common MCP registration boundary."""

import asyncio
import json
import socket

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolRequest, CallToolRequestParams
from neo4j.exceptions import ServiceUnavailable
from pymilvus.exceptions import (
    ConnectError,
    MilvusException,
    MilvusUnavailableException,
)
from semantica.utils.exceptions import ProcessingError

from semantica_dataagent.mcp_contract import install_contract


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
    assert isinstance(graph_failure.value.__cause__, ProcessingError)

    with pytest.raises(StorageUnavailable) as milvus_failure:
        knowledge.milvus(values)
    assert isinstance(milvus_failure.value.__cause__, ProcessingError)


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

    class DisconnectedGraph:
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
    assert isinstance(caught.value.__cause__, ProcessingError)


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
            raise MilvusException(code=2, message="deadline exceeded")

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
