"""Public behavior of the common MCP registration boundary."""

import asyncio
import json

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolRequest, CallToolRequestParams
from neo4j.exceptions import ServiceUnavailable
from pymilvus.exceptions import ConnectError, MilvusUnavailableException

from semantica_dataagent.mcp_contract import install_contract


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
    from semantica.utils.exceptions import ProcessingError

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
