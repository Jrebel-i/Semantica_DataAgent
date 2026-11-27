"""Dialect contract for the MCP ``validate_sql`` entry point (#340)."""

import asyncio
import json
from pathlib import Path

import pytest
import sqlglot
from fastapi import HTTPException
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolRequest, CallToolRequestParams

from semantica_dataagent import query_api, sql_validation
from semantica_dataagent.sql_validation import Snapshot

NOTICE = "该方言仅用于语法检查，交付请使用 Doris SQL。"
DORIS_DELIVERABLE = (
    "SELECT id FROM shop.orders WHERE dt >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)"
)


@pytest.fixture
def snapshot(monkeypatch):
    root = Path(__file__).resolve().parents[2] / "samples" / "demo"
    tables = json.loads((root / "tables.json").read_text())
    fields = json.loads((root / "fields.json").read_text())
    loaded = Snapshot(
        [
            {
                **table,
                "ref": f"table:{table['database']}.{table['name']}",
                "fields": [
                    field
                    for field in fields
                    if field["database"] == table["database"]
                    and field["table"] == table["name"]
                ],
            }
            for table in tables
        ],
        [],
        [],
    )
    monkeypatch.setattr(sql_validation, "load_snapshot", lambda *a, **kw: loaded)
    return loaded


def call_tool(name, arguments):
    from semantica_dataagent.app import create_mcp

    async def run():
        server = create_mcp()
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

    result = asyncio.run(run())
    assert not result.isError, result
    return result.structuredContent


def test_hive_and_spark_carry_the_dialect_notice_but_doris_does_not(snapshot):
    results = {
        dialect: call_tool(
            "validate_sql", {"sql": DORIS_DELIVERABLE, "dialect": dialect}
        )
        for dialect in ("hive", "spark", "doris")
    }
    assert results["hive"]["dialect_notice"] == NOTICE
    assert results["spark"]["dialect_notice"] == NOTICE
    assert "dialect_notice" not in results["doris"]


def test_omitted_dialect_resolves_to_the_hive_default_and_carries_the_notice(snapshot):
    """An omitted dialect is hive, so it carries the same notice as explicit hive.

    The MCP signature default is "hive" and must stay that way, so the wrapper
    cannot tell an omitted dialect from an explicit hive request.
    """
    omitted = call_tool("validate_sql", {"sql": DORIS_DELIVERABLE})
    explicit_hive = call_tool(
        "validate_sql", {"sql": DORIS_DELIVERABLE, "dialect": "hive"}
    )
    assert omitted == explicit_hive
    assert omitted["dialect_notice"] == NOTICE


@pytest.mark.parametrize("sql", [DORIS_DELIVERABLE, "SELECT id FROM shop.orders"])
def test_the_notice_leaves_status_and_issues_unchanged(snapshot, sql):
    for dialect in ("hive", "spark", "doris"):
        from_mcp = call_tool("validate_sql", {"sql": sql, "dialect": dialect})
        direct = sql_validation.validate_sql(sql, dialect=dialect)
        for field in (
            "status",
            "syntax",
            "tables",
            "fields",
            "joins",
            "issues",
        ):
            assert from_mcp[field] == direct[field], (dialect, field)
        expected_extra = {"dialect_notice"} if dialect in ("hive", "spark") else set()
        assert set(from_mcp) - set(direct) == expected_extra


def test_the_default_call_matches_doris_delivery_validation(snapshot):
    """``propose_query`` validates with Doris; a deliverable statement must agree."""
    from_mcp = call_tool("validate_sql", {"sql": DORIS_DELIVERABLE})
    doris = sql_validation.validate_sql(DORIS_DELIVERABLE, dialect="doris")
    assert from_mcp["status"] == doris["status"] == "pass"
    assert from_mcp["issues"] == doris["issues"] == []


def test_validate_sql_schema_stays_self_contained_with_the_hive_default(snapshot):
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
        return next(tool for tool in tools if tool.name == "validate_sql")

    tool = asyncio.run(run())
    dialect = tool.inputSchema["properties"]["dialect"]
    assert dialect["default"] == "hive"
    assert dialect["enum"] == ["hive", "spark", "doris"]
    assert not list(forbidden_paths(tool.inputSchema, "inputSchema"))


@pytest.mark.parametrize(
    "predicate",
    [
        "dt >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)",
        "dt = DATE_SUB('2025-01-08', INTERVAL 7 DAY)",
        "dt = DATE_FORMAT('2025-01-01', '%Y-%m-%d')",
        "dt >= DATE_FORMAT(CURRENT_DATE(), '%Y-%m-%d')",
    ],
)
def test_a1_date_expressions_agree_across_validate_submit_and_rewrite(
    snapshot, predicate
):
    sql = f"SELECT id FROM shop.orders WHERE {predicate}"

    validation = {
        dialect: sql_validation.validate_sql(sql, dialect=dialect)
        for dialect in ("hive", "spark", "doris")
    }
    assert {result["status"] for result in validation.values()} == {"pass"}, validation

    submitted = query_api._validate_sql(sql)
    assert not query_api._partition_issues(submitted, snapshot.tables)[2]

    rewritten_text = submitted.sql(dialect="doris")
    rewritten = sqlglot.parse_one(rewritten_text, read="doris")
    assert rewritten == submitted
    assert not query_api._partition_issues(rewritten, snapshot.tables)[2]


def test_hive_numeric_date_sub_is_syntax_only_and_not_deliverable(snapshot):
    """Hive's numeric DATE_SUB parses there but not under the Doris submission parser.

    This is the difference the notice warns about; the Doris interval form is
    the deliverable spelling.
    """
    sql = "SELECT id FROM shop.orders WHERE dt >= DATE_SUB(CURRENT_DATE(), 7)"
    hive = sql_validation.validate_sql(sql, dialect="hive")
    assert hive["status"] == "pass", hive["issues"]
    assert sql_validation.validate_sql(sql, dialect="doris")["issues"][0]["kind"] == (
        "syntax"
    )
    with pytest.raises(HTTPException) as error:
        query_api._validate_sql(sql)
    assert error.value.status_code == 422
