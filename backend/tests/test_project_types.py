"""Project-type driven MCP tools, source adapters and skills (#179)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    token = initialize()
    with TestClient(app) as test_client:
        yield test_client, token


def _create_project(c: TestClient, admin: str, name: str, project_type: str) -> dict:
    response = c.post(
        "/api/v1/projects",
        headers={"Authorization": f"Bearer {admin}"},
        json={"name": name, "type": project_type},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_every_registered_tool_has_one_authoritative_project_type():
    from semantica_dataagent.app import create_mcp
    from semantica_dataagent.project_types import TOOL_PROJECT_TYPES, unclassified_tools

    names = {tool.name for tool in create_mcp()._tool_manager.list_tools()}
    assert unclassified_tools(names) == set()
    assert set(TOOL_PROJECT_TYPES) == names


def test_tool_sets_follow_the_project_type():
    from semantica_dataagent.project_types import (
        DOCUMENT_TOOL_NAMES,
        TOOL_PROJECT_TYPES,
        tools_for_project_type,
    )

    warehouse = tools_for_project_type("warehouse")
    document = tools_for_project_type("document")
    assert warehouse == set(TOOL_PROJECT_TYPES)
    assert document == DOCUMENT_TOOL_NAMES
    assert {
        "search_knowledge",
        "read_document",
        "list_categories",
        "read_category_index",
        "request_clarification",
        "propose_correction",
        "report_knowledge_gap",
        "get_capabilities",
    } == document


def test_document_project_selects_only_compatible_source_adapters(monkeypatch):
    from semantica_dataagent import plugins

    class Adapter:
        version = "1"
        parameters_schema: ClassVar[dict] = {"type": "object"}

        def __init__(self, plugin_id, project_types):
            self.id = plugin_id
            self.project_types = project_types

        def read(self, directory, parameters):
            from semantica_dataagent.canonical import ReadResult

            return ReadResult([], {})

    adapters = {
        ("source", "markdown"): Adapter("markdown", ("warehouse", "document")),
        ("source", "dtb"): Adapter("dtb", ("warehouse",)),
    }
    monkeypatch.setattr(plugins, "discover", lambda: adapters)
    monkeypatch.setattr(
        plugins.project_context,
        "plugin_configs",
        lambda: {("source", "dtb"): (True, {})},
    )

    selected = plugins.configured("docs", project_type="document")
    assert [plugin.id for _, plugin, _, _ in selected] == ["markdown"]

    warehouse = plugins.configured("warehouse-a", project_type="warehouse")
    assert {plugin.id for _, plugin, _, _ in warehouse} == {"markdown", "dtb"}


def test_read_sources_never_calls_an_incompatible_adapter(tmp_path):
    from semantica_dataagent.canonical import ReadResult
    from semantica_dataagent.knowledge import read_sources

    calls = []

    class Adapter:
        id = "dtb"
        version = "1"
        project_types = ("warehouse",)

        def read(self, directory, parameters):
            calls.append("dtb")
            return ReadResult([], {})

    values = {
        "source_directory": str(tmp_path),
        "_project_id": "docs",
        "_project_type": "document",
        "knowledge_generation": "docs_initial",
    }
    plugins = [("source", Adapter(), True, {})]
    assert read_sources(values, plugins)[:2] == ([], {})
    assert calls == []

    values["_project_type"] = "warehouse"
    read_sources(values, plugins)
    assert calls == ["dtb"]


def test_plugin_listing_reports_supported_project_types(client):
    c, admin = client
    response = c.get("/api/v1/plugins", headers={"Authorization": f"Bearer {admin}"})
    assert response.status_code == 200, response.text
    source = {row["id"]: row for row in response.json() if row["category"] == "source"}
    assert source["markdown"]["project_types"] == ["warehouse", "document"]
    assert source["dtb"]["project_types"] == ["warehouse"]
    assert source["canonical"]["project_types"] == ["warehouse"]


def test_adapter_compatibility_validation():
    from semantica_dataagent.project_types import (
        AdapterIncompatible,
        ensure_adapter_compatible,
    )

    ensure_adapter_compatible("document", "markdown")
    ensure_adapter_compatible("warehouse", "markdown")
    ensure_adapter_compatible("warehouse", "dtb")
    with pytest.raises(AdapterIncompatible, match="dtb"):
        ensure_adapter_compatible("document", "dtb")
    with pytest.raises(AdapterIncompatible, match="missing"):
        ensure_adapter_compatible("document", "missing")


def test_runner_writes_the_skill_selected_by_project_type(tmp_path, monkeypatch):
    from semantica_dataagent.agent_types import AgentTurnRequest, ProviderConfig
    from semantica_dataagent.pi_runner import PiRunner
    from semantica_dataagent.skill import read_skill

    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    request = AgentTurnRequest(
        conversation_id="conv-docs",
        prompt="question",
        session_id="conv-docs",
        workspace=tmp_path / "workspace",
        provider=ProviderConfig(
            id="provider",
            name="Local",
            type="anthropic-messages",
            base_url="http://127.0.0.1:3425",
            models=["model"],
        ),
        model="model",
        mcp_url="http://127.0.0.1:8000/mcp/",
        mcp_token="token",
        project_type="document",
    )
    config, _, _ = PiRunner(binary="/usr/bin/true").write_config(request)
    skill = config / "skills" / "dataagent-document-retrieval" / "SKILL.md"
    assert skill.read_text(encoding="utf-8") == read_skill("document")
    assert not (config / "skills" / "dataagent-retrieval").exists()


def test_document_skill_contains_only_document_retrieval_guidance():
    from semantica_dataagent.skill import read_skill

    text = read_skill("document")
    lines = text.splitlines()
    assert lines[0] == "---"
    assert any(
        line.startswith("name: dataagent-document-retrieval") for line in lines[1:]
    )
    assert any(line.startswith("description: ") for line in lines[1:])
    for forbidden in (
        "SQL",
        "describe_tables",
        "validate_sql",
        "propose_query",
        "execute_query",
        "sample_table_data",
        "lookup_dim_values",
        "find_join_path",
        "get_lineage",
        "read_task_sql",
        "dimension value",
    ):
        assert forbidden not in text, forbidden
    assert not re.search(r"\btables?\b", text, re.IGNORECASE)
    assert not re.search(r"\bfields?\b", text, re.IGNORECASE)
    for allowed in (
        "list_categories",
        "read_category_index",
        "search_knowledge",
        "read_document",
        "request_clarification",
        "next_cursor",
    ):
        assert allowed in text


def test_project_type_cannot_be_changed_after_creation(client):
    c, admin = client
    headers = {"Authorization": f"Bearer {admin}"}
    project = _create_project(c, admin, "Docs", "document")
    rejected = c.patch(
        f"/api/v1/projects/{project['id']}",
        headers=headers,
        json={"name": "Renamed", "type": "warehouse"},
    )
    assert rejected.status_code == 422, rejected.text
    renamed = c.patch(
        f"/api/v1/projects/{project['id']}",
        headers=headers,
        json={"name": "Renamed"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Renamed"
    assert renamed.json()["type"] == "document"
    listing = {
        row["id"]: row for row in c.get("/api/v1/projects", headers=headers).json()
    }
    assert listing[project["id"]]["type"] == "document"
