"""Seam 1 tests for pluggable TraceSource protocol and LangfuseTraceSource."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from jsonschema import Draft202012Validator

from semantica_dataagent import plugins
from semantica_dataagent.plugins import (
    TraceConversation,
    TraceMessage,
    TraceQuery,
    TraceSourcePlugin,
    secret_parameter_keys,
)
from semantica_dataagent.trace_sources import LangfuseTraceSource


def _make_source(handler) -> LangfuseTraceSource:
    transport = httpx.MockTransport(handler)
    return LangfuseTraceSource(
        client_factory=lambda **kwargs: httpx.Client(transport=transport, **kwargs)
    )


def test_langfuse_trace_source_entry_point_and_schema():
    discovered = plugins.discover()
    assert ("trace_source", "langfuse") in discovered
    plugin = discovered[("trace_source", "langfuse")]
    assert isinstance(plugin, LangfuseTraceSource)
    assert plugin.id == "langfuse"
    assert plugin.display_name == "Langfuse"
    Draft202012Validator.check_schema(plugin.parameters_schema)
    assert secret_parameter_keys(plugin.parameters_schema) == {"secret_key"}
    # Structural check against TraceSourcePlugin protocol
    assert callable(getattr(plugin, "fetch_traces", None))
    assert isinstance(plugin, object)
    _: TraceSourcePlugin = plugin


def test_langfuse_enforces_project_id_isolation_and_ignores_other_projects():
    recorded_requests: list[tuple[str, dict[str, list[str]], str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        parsed = urlsplit(str(request.url))
        recorded_requests.append(
            (parsed.path, parse_qs(parsed.query), request.headers.get("authorization"))
        )
        return httpx.Response(
            200,
            json={
                "data": [
                    # Matches by metadata.project_id
                    {
                        "id": "trace-proj-a-1",
                        "timestamp": "2026-11-24T01:00:00Z",
                        "tags": ["project:proj_a"],
                        "metadata": {"project_id": "proj_a", "agent": "pi"},
                        "input": "Q1",
                        "output": "A1",
                    },
                    # Matches by project:<id> tag when metadata omits project_id
                    {
                        "id": "trace-proj-a-tag-only",
                        "timestamp": "2026-11-24T02:00:00Z",
                        "tags": ["project:proj_a"],
                        "metadata": {},
                        "input": "Q2",
                        "output": "A2",
                    },
                    # Conflicting metadata.project_id even with forged tag -> MUST reject
                    {
                        "id": "trace-forged-tag",
                        "timestamp": "2026-11-24T03:00:00Z",
                        "tags": ["project:proj_a"],
                        "metadata": {"project_id": "proj_b"},
                        "input": "Secret Q",
                        "output": "Secret A",
                    },
                    # Different project -> MUST reject
                    {
                        "id": "trace-proj-b",
                        "timestamp": "2026-11-24T04:00:00Z",
                        "tags": ["project:proj_b"],
                        "metadata": {"project_id": "proj_b"},
                        "input": "Other Q",
                        "output": "Other A",
                    },
                    # Unscoped trace -> MUST reject
                    {
                        "id": "trace-unscoped",
                        "timestamp": "2026-11-24T05:00:00Z",
                        "tags": [],
                        "metadata": {},
                        "input": "Unscoped Q",
                        "output": "Unscoped A",
                    },
                ],
                "meta": {"page": 1, "totalPages": 1},
            },
        )

    source = _make_source(handler)
    results = source.fetch_traces(
        TraceQuery(
            project_id="proj_a",
            parameters={
                "base_url": "https://langfuse.example.com/",
                "public_key": "pk-test",
            },
            secrets={"secret_key": "sk-lf-secret"},
        )
    )

    assert len(recorded_requests) == 1
    path, qs, auth_header = recorded_requests[0]
    assert path == "/api/public/traces"
    assert qs["tags"] == ["project:proj_a"]
    assert auth_header and auth_header.startswith("Basic ")

    assert [c.trace_id for c in results] == ["trace-proj-a-1", "trace-proj-a-tag-only"]
    assert all(
        isinstance(c, TraceConversation) and c.project_id == "proj_a" for c in results
    )
    assert results[0].messages == [
        TraceMessage(
            role="user",
            content="Q1",
            timestamp=datetime(2026, 11, 24, 1, 0, tzinfo=UTC),
        ),
        TraceMessage(
            role="assistant",
            content="A1",
            timestamp=datetime(2026, 11, 24, 1, 0, tzinfo=UTC),
        ),
    ]


def test_langfuse_extracts_only_human_ai_messages_and_scrubs_secrets_and_rows():
    runtime_secret = "super-secret-runtime-token-999"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "trace-messages",
                        "timestamp": "2026-11-24T02:00:00Z",
                        "tags": ["project:default"],
                        "metadata": {
                            "project_id": "default",
                            "messages": [
                                {"role": "system", "content": "Internal system prompt"},
                                {
                                    "role": "tool",
                                    "content": json.dumps({"rows": [["secret-row-1"]]}),
                                },
                                {
                                    "role": "user",
                                    "content": (
                                        f"查看订单明细 Bearer my-jwt-token {runtime_secret} "
                                        "password=hunter2 api_key=sk-abc123456789 "
                                        "Authorization: Bearer header-tok"
                                    ),
                                },
                                {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "name": "validate_sql",
                                            "arguments": {"sql": "SELECT 1"},
                                        }
                                    ],
                                    "content": [
                                        {
                                            "type": "thinking",
                                            "text": "hidden chain of thought",
                                        },
                                        {
                                            "type": "tool_use",
                                            "name": "describe_tables",
                                            "input": {},
                                        },
                                        {
                                            "type": "text",
                                            "text": "请使用 shop.orders 表统计成交额。",
                                        },
                                    ],
                                },
                                {
                                    "role": "user",
                                    "content": (
                                        "查询已执行，请根据以下结果信息给出结论：\n"
                                        + json.dumps(
                                            {
                                                "status": "executed",
                                                "row_count": 1,
                                                "columns": ["id"],
                                                "rows": [["SENSITIVE_FACT_ROW_VALUE"]],
                                            },
                                            ensure_ascii=False,
                                        )
                                    ),
                                },
                            ],
                        },
                        "observations": [
                            {
                                "type": "SPAN",
                                "name": "execute_query",
                                "output": {"rows": [["LEAK_ROW"]]},
                            }
                        ],
                    },
                    # Trace with only system/tool messages -> omitted
                    {
                        "id": "trace-tool-only",
                        "timestamp": "2026-11-24T03:00:00Z",
                        "tags": ["project:default"],
                        "metadata": {
                            "project_id": "default",
                            "messages": [
                                {"role": "system", "content": "Only system"},
                                {"role": "tool", "content": "Only tool"},
                            ],
                        },
                    },
                ],
                "meta": {"page": 1, "totalPages": 1},
            },
        )

    source = _make_source(handler)
    results = source.fetch_traces(
        TraceQuery(
            project_id="default",
            parameters={
                "base_url": "https://langfuse.example.com",
                "public_key": "pk-1",
            },
            secrets={"secret_key": "sk-1", "extra": runtime_secret},
        )
    )

    assert [c.trace_id for c in results] == ["trace-messages"]
    msgs = results[0].messages
    assert all(m.role in {"user", "assistant"} for m in msgs)
    assert len(msgs) == 3

    rendered = "\n".join(m.content for m in msgs)
    for forbidden in (
        "Internal system prompt",
        "secret-row-1",
        "my-jwt-token",
        runtime_secret,
        "hunter2",
        "sk-abc123456789",
        "header-tok",
        "hidden chain of thought",
        "describe_tables",
        "validate_sql",
        "SENSITIVE_FACT_ROW_VALUE",
        "LEAK_ROW",
    ):
        assert forbidden not in rendered, f"forbidden content leaked: {forbidden}"

    assert "查看订单明细" in msgs[0].content
    assert "[REDACTED]" in msgs[0].content
    assert msgs[1].role == "assistant"
    assert msgs[1].content == "请使用 shop.orders 表统计成交额。"
    assert "row_count" in msgs[2].content


def test_langfuse_supports_cursor_and_page_pagination_and_time_window():
    calls: list[dict[str, list[str]]] = []

    def cursor_handler(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlsplit(str(request.url)).query)
        calls.append(qs)
        cursor = qs.get("cursor", [None])[0]
        if cursor is None:
            return httpx.Response(
                200,
                json={
                    "data": [
                        # Before `since` -> client-side filtered out
                        {
                            "id": "t-too-old",
                            "timestamp": "2026-11-24T00:30:00Z",
                            "tags": ["project:default"],
                            "metadata": {"project_id": "default"},
                            "input": "too old",
                            "output": "too old",
                        },
                        {
                            "id": "t-2",
                            "timestamp": "2026-11-24T02:00:00Z",
                            "tags": ["project:default"],
                            "metadata": {"project_id": "default"},
                            "input": "Q2",
                            "output": "A2",
                        },
                    ],
                    "meta": {"nextCursor": "cur-2"},
                },
            )
        assert cursor == "cur-2"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "t-1",
                        "timestamp": "2026-11-24T01:30:00Z",
                        "tags": ["project:default"],
                        "metadata": {"project_id": "default"},
                        "input": "Q1",
                        "output": "A1",
                    },
                    # After `until` -> client-side filtered out
                    {
                        "id": "t-too-new",
                        "timestamp": "2026-11-24T05:00:00Z",
                        "tags": ["project:default"],
                        "metadata": {"project_id": "default"},
                        "input": "too new",
                        "output": "too new",
                    },
                ],
                "meta": {"nextCursor": None},
            },
        )

    since = datetime(2026, 11, 24, 1, 0, tzinfo=UTC)
    until = datetime(2026, 11, 24, 4, 0, tzinfo=UTC)
    source = _make_source(cursor_handler)
    results = source.fetch_traces(
        TraceQuery(
            project_id="default",
            parameters={
                "base_url": "https://langfuse.example.com",
                "public_key": "pk",
                "page_size": 2,
            },
            secrets={"secret_key": "sk"},
            since=since,
            until=until,
            limit=10,
        )
    )

    assert len(calls) == 2
    assert calls[0]["fromTimestamp"] == [since.isoformat()]
    assert calls[0]["toTimestamp"] == [until.isoformat()]
    assert calls[1]["cursor"] == ["cur-2"]
    # Chronologically sorted ascending
    assert [c.trace_id for c in results] == ["t-1", "t-2"]

    # Also test page/totalPages pagination + limit cap
    page_calls: list[dict[str, list[str]]] = []

    def page_handler(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlsplit(str(request.url)).query)
        page_calls.append(qs)
        page = int(qs.get("page", ["1"])[0])
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": f"p-{page}",
                        "timestamp": f"2026-11-24T0{page}:00:00Z",
                        "tags": ["project:default"],
                        "metadata": {"project_id": "default"},
                        "input": f"Q{page}",
                        "output": f"A{page}",
                    }
                ],
                "meta": {"page": page, "totalPages": 3},
            },
        )

    paged_source = _make_source(page_handler)
    paged_results = paged_source.fetch_traces(
        TraceQuery(
            project_id="default",
            parameters={
                "base_url": "https://langfuse.example.com",
                "public_key": "pk",
                "page_size": 1,
            },
            secrets={"secret_key": "sk"},
            limit=2,
        )
    )
    assert [c.trace_id for c in paged_results] == ["p-1", "p-2"]
    assert len(page_calls) == 2


def test_langfuse_page_limit_stays_fixed_when_earlier_page_filters_traces():
    page_calls: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlsplit(str(request.url)).query)
        page_calls.append(qs)
        page = int(qs.get("page", ["1"])[0])
        if page == 1:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "p1-wrong-proj",
                            "timestamp": "2026-11-24T01:00:00Z",
                            "tags": ["project:other"],
                            "metadata": {"project_id": "other"},
                            "input": "Q0",
                            "output": "A0",
                        },
                        {
                            "id": "p1-ok",
                            "timestamp": "2026-11-24T02:00:00Z",
                            "tags": ["project:default"],
                            "metadata": {"project_id": "default"},
                            "input": "Q1",
                            "output": "A1",
                        },
                    ],
                    "meta": {"page": 1, "totalPages": 2},
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "p2-ok-1",
                        "timestamp": "2026-11-24T03:00:00Z",
                        "tags": ["project:default"],
                        "metadata": {"project_id": "default"},
                        "input": "Q2",
                        "output": "A2",
                    },
                    {
                        "id": "p2-ok-2",
                        "timestamp": "2026-11-24T04:00:00Z",
                        "tags": ["project:default"],
                        "metadata": {"project_id": "default"},
                        "input": "Q3",
                        "output": "A3",
                    },
                ],
                "meta": {"page": 2, "totalPages": 2},
            },
        )

    source = _make_source(handler)
    results = source.fetch_traces(
        TraceQuery(
            project_id="default",
            parameters={
                "base_url": "https://langfuse.example.com",
                "public_key": "pk",
                "page_size": 2,
            },
            secrets={"secret_key": "sk"},
            limit=3,
        )
    )
    assert [c.trace_id for c in results] == ["p1-ok", "p2-ok-1", "p2-ok-2"]
    assert page_calls[0]["limit"] == ["2"]
    assert page_calls[1]["limit"] == ["2"]


def test_langfuse_strips_nested_and_truncated_query_rows_fail_closed():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "trace-nested-and-truncated",
                        "timestamp": "2026-11-24T01:00:00Z",
                        "tags": ["project:default"],
                        "metadata": {
                            "project_id": "default",
                            "messages": [
                                {
                                    "role": "user",
                                    "content": (
                                        "查询已执行，请根据以下结果信息给出结论：\n"
                                        + json.dumps(
                                            {
                                                "status": "executed",
                                                "result": {
                                                    "rows": [["NESTED_ROW_LEAK"]],
                                                    "summary": "ok",
                                                },
                                            },
                                            ensure_ascii=False,
                                        )
                                    ),
                                },
                                {
                                    "role": "user",
                                    "content": (
                                        '问题前缀\n查询已执行，请根据以下结果信息给出结论：\n{"rows":[["TRUNCATED_ROW_LEAK"'
                                    ),
                                },
                            ],
                        },
                    }
                ],
                "meta": {"page": 1, "totalPages": 1},
            },
        )

    source = _make_source(handler)
    results = source.fetch_traces(
        TraceQuery(
            project_id="default",
            parameters={"base_url": "https://langfuse.example.com", "public_key": "pk"},
            secrets={"secret_key": "sk"},
        )
    )
    assert len(results) == 1
    rendered = "\n".join(m.content for m in results[0].messages)
    assert "NESTED_ROW_LEAK" not in rendered
    assert "TRUNCATED_ROW_LEAK" not in rendered
    assert "问题前缀" in rendered


def test_plugins_api_excludes_trace_source_and_rejects_plaintext_update(
    tmp_path, monkeypatch
):
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient

    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    token = initialize()
    headers = {"Authorization": f"Bearer {token}"}

    assert not any(cat == "trace_source" for cat, *_ in plugins.configured())
    with TestClient(app) as client:
        listed = client.get("/api/v1/plugins", headers=headers)
        assert listed.status_code == 200
        assert not any(row["category"] == "trace_source" for row in listed.json())

        put_resp = client.put(
            "/api/v1/plugins/trace_source/langfuse",
            headers=headers,
            json={
                "enabled": True,
                "parameters": {
                    "base_url": "https://langfuse.example.com",
                    "public_key": "pk",
                    "secret_key": "sk-plaintext",
                },
            },
        )
        assert put_resp.status_code == 404


def test_langfuse_validates_config_and_propagates_http_errors():
    source = _make_source(
        lambda _req: httpx.Response(401, json={"error": "unauthorized"})
    )
    with pytest.raises(ValueError, match="base_url"):
        source.fetch_traces(
            TraceQuery(
                project_id="default",
                parameters={"base_url": "ftp://invalid", "public_key": "pk"},
                secrets={"secret_key": "sk"},
            )
        )
    with pytest.raises(ValueError, match="secret_key"):
        source.fetch_traces(
            TraceQuery(
                project_id="default",
                parameters={
                    "base_url": "https://langfuse.example.com",
                    "public_key": "pk",
                },
                secrets={},
            )
        )
    with pytest.raises(httpx.HTTPStatusError):
        source.fetch_traces(
            TraceQuery(
                project_id="default",
                parameters={
                    "base_url": "https://langfuse.example.com",
                    "public_key": "pk",
                },
                secrets={"secret_key": "sk"},
            )
        )
