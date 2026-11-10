"""Common validation, pagination and error handling for registered MCP tools."""

import base64
import hashlib
import json
import logging

import httpx
import jsonschema
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, ListToolsRequest, TextContent
from neo4j.exceptions import DatabaseUnavailable, ServiceUnavailable, SessionExpired
from pymilvus.exceptions import (
    ConnectError,
    ConnectionNotExistException,
    MilvusUnavailableException,
)
from semantica.utils.exceptions import ProcessingError

from .embedding_client import EmbeddingError
from .storage_errors import StorageUnavailable

PAGE_ITEMS = 25
PAGE_CHARS = 12000


def _audit_bound_project_rejection() -> None:
    """Audit a tool call rejected by the project error bound by the transport."""
    from .mcp_context import current_project_error_status, current_requester

    status_code = current_project_error_status()
    if status_code is None:
        return
    try:
        from .query_api import audit_mcp_project_rejection

        audit_mcp_project_rejection(current_requester().id, status_code)
    except Exception:
        logging.getLogger(__name__).exception(
            "failed to audit an MCP project rejection"
        )


class ToolUnavailable(Exception):
    """A retryable tool failure that carries resumable state for the caller."""

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.details = details


class ToolInvalidInput(ValueError):
    """A request the caller can fix, with a message safe to return verbatim."""


def error(code, message, retryable, next_action, **details):
    body = {
        "code": code,
        "message": message,
        "retryable": retryable,
        "next_action": next_action,
        **details,
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
    if isinstance(exc, ToolUnavailable):
        return error(
            "unavailable",
            str(exc),
            True,
            "Retry the call, reusing conversation_id when present",
            **exc.details,
        )
    if isinstance(exc, ToolInvalidInput):
        return error(
            "invalid_input",
            str(exc),
            False,
            "Check the tool parameters and allowed values",
        )
    if isinstance(exc, EmbeddingError):
        # Retries exhausted (cause present) mean the service stayed unreachable.
        # A 4xx or missing configuration raises directly and is a request/config
        # failure, which must not be reported as retryable.
        if exc.__cause__ is not None:
            return error(
                "unavailable",
                "A required service is unavailable",
                True,
                "Check service connectivity and retry",
            )
        return error(
            "internal_error",
            "The tool could not complete the request",
            False,
            "Contact the service administrator",
        )
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
    from .mcp_context import current_project_type
    from .project_types import ensure_tool_supported, supports_tool
    from .telemetry import register_tool_names

    tools = {tool.name: tool for tool in server._tool_manager.list_tools()}
    register_tool_names(tools)
    for tool in tools.values():
        tool.parameters["additionalProperties"] = False
        tool.parameters.setdefault("properties", {})["cursor"] = {"type": "string"}

    original_list_tools = server._mcp_server.request_handlers[ListToolsRequest]

    async def list_tools(request):
        result = await original_list_tools(request)
        try:
            project_type = current_project_type()
        except Exception:  # noqa: BLE001 - the capability guard fails closed
            project_type = None
        result.root.tools = (
            []
            if project_type is None
            else [
                tool
                for tool in result.root.tools
                if supports_tool(project_type, tool.name)
            ]
        )
        return result

    server._mcp_server.request_handlers[ListToolsRequest] = list_tools

    async def call_impl(name, arguments):
        tool = tools.get(name)
        if tool is None:
            return error(
                "invalid_input", "Unknown tool", False, "Check available tools"
            )
        try:
            ensure_tool_supported(current_project_type(), name)
        except (ToolInvalidInput, LookupError, ValueError) as exc:
            _audit_bound_project_rejection()
            return classify_error(exc)
        try:
            jsonschema.validate(arguments, tool.parameters)
            args = dict(arguments)
            cursor = args.pop("cursor", None)
            if name == "execute_query" and cursor is not None:
                raise ValueError("Query execution does not support cursors")
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
        ) as exc:
            message = "Invalid tool arguments or cursor"
            if name == "propose_query":
                if isinstance(exc, jsonschema.ValidationError):
                    path = ".".join(str(part) for part in exc.absolute_path)
                    message = (
                        "propose_query 参数不合法"
                        + (f": {path}" if path else "")
                        + f": {exc.message}"
                    )
                else:
                    message = f"propose_query 参数不合法: {exc}"
            return error(
                "invalid_input",
                message,
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
            if name == "execute_query" and isinstance(structured, dict):
                return CallToolResult(
                    content=result[0] if isinstance(result, tuple) else result,
                    structuredContent=structured,
                )
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

    @server._mcp_server.call_tool(validate_input=False)
    async def call(name, arguments):
        import time
        import uuid
        from datetime import UTC, datetime

        from .crypto import decrypt
        from .db import Conversation, Provider, Token, Turn, sessions
        from .mcp_context import (
            current_conversation_id,
            current_requester,
            current_turn_id,
        )
        from .telemetry import UNKNOWN_TOOL_NAME, submit, trace_id

        started = datetime.now(UTC)
        clock = time.monotonic()
        result = await call_impl(name, arguments)
        trusted_name = name if name in tools else UNKNOWN_TOOL_NAME
        try:
            requester = current_requester()
            conversation_id = current_conversation_id()
            supplied_turn = current_turn_id()
            active_turn = None
            token_name = ""
            provider_secret = ""
            with sessions()() as db:
                owner = db.query(Token).filter_by(id=requester.id).first()
                token_name = owner.name if owner else ""
                if (
                    conversation_id
                    and supplied_turn
                    and supplied_turn.isascii()
                    and supplied_turn.isdecimal()
                ):
                    conversation = db.get(Conversation, conversation_id)
                    if conversation and conversation.owner_id == requester.id:
                        provider = db.get(Provider, conversation.provider_id)
                        if provider and provider.api_key:
                            provider_secret = decrypt(provider.api_key)
                        active_turn = (
                            db.query(Turn)
                            .filter_by(
                                id=int(supplied_turn),
                                conversation_id=conversation_id,
                                status="running",
                            )
                            .first()
                        )
                        if active_turn:
                            active_turn = active_turn.id
            current_trace = (
                trace_id(conversation_id, active_turn)
                if active_turn
                else uuid.uuid4().hex
            )
            secrets = (requester.token, provider_secret)
            if not active_turn:
                submit(
                    "trace-create",
                    {
                        "id": current_trace,
                        "name": "local-mcp",
                        "metadata": {"source": "local", "token_name": token_name},
                    },
                    secrets,
                )
            data = (
                result.structuredContent
                if hasattr(result, "structuredContent")
                else result
            )
            submit(
                "span-create",
                {
                    "id": uuid.uuid4().hex[:16],
                    "traceId": current_trace,
                    "name": trusted_name,
                    "startTime": started.isoformat(),
                    "endTime": datetime.now(UTC).isoformat(),
                    "input": arguments,
                    "output": data,
                    "is_error": bool(getattr(result, "isError", False)),
                    "status": "error" if getattr(result, "isError", False) else "ok",
                    "metadata": {"duration_ms": int((time.monotonic() - clock) * 1000)},
                },
                secrets,
            )
        except Exception:  # noqa: BLE001 - reporting must never change a tool result
            import logging

            logging.getLogger(__name__).warning("MCP telemetry failed")
        return result
