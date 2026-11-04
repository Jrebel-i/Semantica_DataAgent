"""Common validation, pagination and error handling for registered MCP tools."""

import base64
import hashlib
import json

import httpx
import jsonschema
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent
from neo4j.exceptions import DatabaseUnavailable, ServiceUnavailable, SessionExpired
from pymilvus.exceptions import (
    ConnectError,
    ConnectionNotExistException,
    MilvusUnavailableException,
)
from semantica.utils.exceptions import ProcessingError

from .storage_errors import StorageUnavailable

PAGE_ITEMS = 25
PAGE_CHARS = 12000


def error(code, message, retryable, next_action):
    body = {
        "code": code,
        "message": message,
        "retryable": retryable,
        "next_action": next_action,
    }
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(body))],
        structuredContent=body,
        isError=True,
    )


def classify_error(exc):
    # FastMCP and Semantica wrap errors; inspect only their known wrappers.
    while isinstance(exc, (ToolError, ProcessingError)):
        cause = exc.__cause__ or exc.__context__
        if cause is None or cause is exc:
            break
        exc = cause
    if isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.TimeoutException,
            ServiceUnavailable,
            SessionExpired,
            DatabaseUnavailable,
            ConnectError,
            ConnectionNotExistException,
            MilvusUnavailableException,
            StorageUnavailable,
            ConnectionError,
            TimeoutError,
        ),
    ):
        return error(
            "unavailable",
            "A required service is unavailable",
            True,
            "Check service connectivity and retry",
        )
    if isinstance(exc, ValueError):
        return error(
            "invalid_input",
            "Invalid tool arguments",
            False,
            "Check the tool parameters and allowed values",
        )
    return error(
        "internal_error",
        "The tool could not complete the request",
        False,
        "Contact the service administrator",
    )


def install_contract(server):
    tools = {tool.name: tool for tool in server._tool_manager.list_tools()}
    for tool in tools.values():
        tool.parameters["additionalProperties"] = False
        tool.parameters.setdefault("properties", {})["cursor"] = {"type": "string"}

    @server._mcp_server.call_tool(validate_input=False)
    async def call(name, arguments):
        tool = tools.get(name)
        if tool is None:
            return error(
                "invalid_input", "Unknown tool", False, "Check available tools"
            )
        try:
            jsonschema.validate(arguments, tool.parameters)
            args = dict(arguments)
            cursor = args.pop("cursor", None)
            fingerprint = hashlib.sha256(
                json.dumps([name, args], sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            offset = 0
            if cursor:
                data = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                if (
                    data["fingerprint"] != fingerprint
                    or not isinstance(data["offset"], int)
                    or data["offset"] < 0
                ):
                    raise ValueError("Invalid cursor")
                offset = data["offset"]
        except (
            jsonschema.ValidationError,
            ValueError,
            KeyError,
            TypeError,
            UnicodeError,
        ):
            return error(
                "invalid_input",
                "Invalid tool arguments or cursor",
                False,
                "Check the tool schema and cursor",
            )
        try:
            result = await server.call_tool(name, args)
            if isinstance(result, tuple):
                structured = result[1]
            elif (
                isinstance(result, list)
                and len(result) == 1
                and isinstance(result[0], TextContent)
            ):
                structured = json.loads(result[0].text)
            else:
                structured = result
            value = (
                structured.get("result", structured)
                if isinstance(structured, dict)
                else structured
            )
            oversized_item = isinstance(value, list) and any(
                len(json.dumps(item, ensure_ascii=False)) > PAGE_CHARS for item in value
            )
            if isinstance(value, list) and not oversized_item:
                page = value[offset : offset + PAGE_ITEMS]
                next_offset = offset + len(page)
                while (
                    len(page) > 1
                    and len(json.dumps(page, ensure_ascii=False)) > PAGE_CHARS
                ):
                    page.pop()
                    next_offset -= 1
                truncated = next_offset < len(value)
                body = {"result": page}
            else:
                raw = json.dumps(value, ensure_ascii=False)
                if len(raw) > PAGE_CHARS or offset:
                    body = {"result_fragment": raw[offset : offset + PAGE_CHARS]}
                    next_offset = offset + len(body["result_fragment"])
                    truncated = next_offset < len(raw)
                else:
                    if isinstance(result, tuple):
                        return CallToolResult(
                            content=result[0], structuredContent=result[1]
                        )
                    if isinstance(result, list) and isinstance(structured, dict):
                        return CallToolResult(
                            content=result, structuredContent=structured
                        )
                    return result
            if truncated:
                body["truncated"] = True
                body["next_cursor"] = base64.urlsafe_b64encode(
                    json.dumps(
                        {"fingerprint": fingerprint, "offset": next_offset}
                    ).encode()
                ).decode()
            return CallToolResult(
                content=[
                    TextContent(type="text", text=json.dumps(body, ensure_ascii=False))
                ],
                structuredContent=body,
            )
        except Exception as exc:  # noqa: BLE001 - sanitize failures at the boundary
            return classify_error(exc)
