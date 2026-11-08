import asyncio
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from mcp.types import CallToolRequest, CallToolRequestParams


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    token = initialize()
    with TestClient(app) as test_client:
        yield test_client, token


def test_skill_download_requires_authentication_and_is_an_attachment(client):
    from semantica_dataagent.skill import SKILL_FILENAME, read_skill

    c, token = client
    assert c.get("/api/v1/skill").status_code == 401
    assert (
        c.get("/api/v1/skill", headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )
    response = c.get("/api/v1/skill", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert (
        response.headers["content-disposition"]
        == f'attachment; filename="{SKILL_FILENAME}"'
    )
    assert response.text == read_skill()


def test_mcp_prompt_and_capabilities_expose_the_same_skill(client):
    from semantica_dataagent.app import create_mcp
    from semantica_dataagent.skill import SKILL_NAME, read_skill

    server = create_mcp()

    async def run():
        prompts = await server.list_prompts()
        assert [prompt.name for prompt in prompts] == [SKILL_NAME]
        prompt = prompts[0]
        assert prompt.description
        assert prompt.arguments in (None, [])
        server.get_context = lambda: None
        rendered = await server.get_prompt(SKILL_NAME)
        return prompts, rendered

    prompts, rendered = asyncio.run(run())
    assert rendered.messages[0].content.text == read_skill()

    async def capabilities():
        handler = server._mcp_server.request_handlers[CallToolRequest]
        response = await handler(
            CallToolRequest(
                method="tools/call",
                params=CallToolRequestParams(name="get_capabilities", arguments={}),
            )
        )
        return response.root

    result = asyncio.run(capabilities())
    assert not result.isError
    assert result.structuredContent["prompts"] == [prompts[0].name]


def test_skill_source_is_a_valid_agent_skill():
    from semantica_dataagent.skill import read_skill

    text = read_skill()
    lines = text.splitlines()
    assert lines[0] == "---"
    assert any(line.startswith("name: dataagent-retrieval") for line in lines[1:])
    assert any(line.startswith("description: ") for line in lines[1:])
    assert len(lines) <= 300
    for tool in (
        "list_categories",
        "read_category_index",
        "search_knowledge",
        "read_document",
        "describe_tables",
        "sample_table_data",
        "propose_query",
        "lookup_dim_values",
        "validate_sql",
        "next_cursor",
        "invalid_input",
        "unavailable",
        "internal_error",
        "request_clarification",
    ):
        assert tool in text
    assert "Every option must contain" in text
    assert "Do not pick a definition" in text
    normalized = " ".join(text.split())
    assert "INSERT OVERWRITE" in text
    assert "treat the table as a snapshot" in normalized
    assert "never the partition column of a snapshot table" in normalized
    assert "business time column" in normalized
    assert "Field-level ambiguity" in normalized
    assert "two or more fields" in normalized
    assert "field:db.table.column" in normalized
    assert "one option per candidate field" in normalized
    assert "at most one ambiguous word per turn" in normalized
    assert "do not write SQL or call `propose_query` in that turn" in normalized
    assert "different representations of the same field" in normalized
    assert "Do not clarify when" in normalized
