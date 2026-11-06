import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from semantica_dataagent.agent_runner import (
    CLARIFICATION,
    DONE,
    TEXT_DELTA,
    TOOL_END,
    TOOL_START,
    AgentEvent,
    AgentTurnRequest,
    ProviderConfig,
    list_agents,
)
from semantica_dataagent.pi_runner import PiEventParser, PiRunner
from semantica_dataagent.skill import read_skill

FIXTURES = Path(__file__).parent / "fixtures"


def _records(name: str) -> list[dict]:
    return [
        json.loads(line)
        for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _parse(records: list[dict]) -> tuple[list[AgentEvent], AgentEvent]:
    parser = PiEventParser()
    events = [event for record in records for event in parser.feed(record)]
    return events, parser.finish()


def test_pi_event_parser_streams_tools_and_final_text_from_real_output():
    events, final = _parse(_records("pi_rpc_tool.jsonl"))
    starts = [event for event in events if event.type == TOOL_START]
    ends = [event for event in events if event.type == TOOL_END]
    assert len(starts) == 1 and len(ends) == 1
    assert starts[0].data["name"] == "bash"
    assert "echo fixture-ok" in starts[0].data["summary"]
    assert ends[0].data["is_error"] is False
    assert "fixture-ok" in ends[0].data["summary"]
    text = "".join(event.data["text"] for event in events if event.type == TEXT_DELTA)
    assert text == "DONE"
    assert final.type == DONE
    assert final.data["text"] == "DONE"
    assert final.data["sql"] == []
    assert final.data["citations"] == []


def test_pi_event_parser_extracts_sql_and_citations_from_real_output():
    _, final = _parse(_records("pi_rpc_sql.jsonl"))
    assert final.type == DONE
    assert final.data["sql"] == [
        {
            "sql": "SELECT channel, SUM(amount) AS sales FROM shop.orders GROUP BY channel",
            "dialect": "hive",
        }
    ]
    assert final.data["citations"] == [
        {"ref": "table:shop.orders", "type": "table"},
        {
            "ref": "document:metrics/sales.md#chunk-1",
            "type": "document",
        },
    ]
    assert "table:shop.orders" in final.data["text"]


def test_pi_event_parser_keeps_sql_validated_before_a_summary_only_answer():
    sql = "SELECT dt, COUNT(*) FROM shop.orders GROUP BY dt"
    _, final = _parse(
        [
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"```sql\n{sql}\n```"}],
                    "stopReason": "toolUse",
                },
            },
            {
                "type": "tool_execution_start",
                "toolCallId": "call-validate",
                "toolName": "mcp__dataagent__validate_sql",
                "args": {"sql": sql, "dialect": "hive"},
            },
            {
                "type": "tool_execution_end",
                "toolCallId": "call-validate",
                "toolName": "mcp__dataagent__validate_sql",
                "isError": False,
                "result": {"content": [{"type": "text", "text": '{"status": "pass"}'}]},
            },
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "SQL 已校验通过，引用 table:shop.orders",
                        }
                    ],
                    "stopReason": "stop",
                },
            },
        ]
    )
    assert final.type == DONE
    assert final.data["text"] == "SQL 已校验通过，引用 table:shop.orders"
    assert final.data["sql"] == [{"sql": sql, "dialect": "hive"}]


def test_pi_event_parser_emits_clarification_placeholder():
    parser = PiEventParser()
    events = parser.feed(
        {
            "type": "tool_execution_end",
            "toolCallId": "call-1",
            "toolName": "mcp__dataagent__request_clarification",
            "isError": False,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "questions": [
                                    {
                                        "question": "按支付日还是下单日？",
                                        "options": [
                                            {
                                                "label": "支付日",
                                                "ref": "document:metrics/sales.md#chunk-1",
                                            }
                                        ],
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        ),
                    }
                ]
            },
        }
    )
    assert [event.type for event in events] == [TOOL_END, CLARIFICATION]
    assert events[1].data["questions"][0]["question"] == "按支付日还是下单日？"


def test_pi_event_parser_emits_query_plan_from_tool_result():
    parser = PiEventParser()
    plan = {
        "id": "plan-1",
        "sql": "SELECT 1",
        "data_source": "doris_ai",
        "tables": [],
        "estimated_partitions": None,
        "estimated_rows": None,
        "row_limit": 1000,
    }
    events = parser.feed(
        {
            "type": "tool_execution_end",
            "toolCallId": "call-1",
            "toolName": "mcp__dataagent__propose_query",
            "isError": False,
            "result": {"content": [{"type": "text", "text": json.dumps(plan)}]},
        }
    )
    assert [event.type for event in events] == [TOOL_END, "query_plan"]
    assert events[1].data == plan


def test_online_query_plan_uses_persisted_tool_result_when_runner_summary_is_opaque(
    client, monkeypatch
):
    from semantica_dataagent import agent_runner, knowledge, query_api, sql_validation
    from semantica_dataagent.auth import Requester
    from semantica_dataagent.db import Token, sessions
    from semantica_dataagent.mcp_context import bind_mcp_context

    class Executor:
        def bind(self, config, token):
            return self

        async def estimate(self, sql):
            return {"partitions": 3, "rows": 42}

    monkeypatch.setattr(knowledge, "describe", lambda **_: [])
    monkeypatch.setattr(
        sql_validation, "validate_sql", lambda *a, **kw: {"status": "pass"}
    )
    monkeypatch.setattr(query_api, "_config", lambda: (Executor(), {}))
    c, token = client
    with sessions()() as db:
        owner = db.query(Token).first()
        requester = Requester(id=owner.id, role=owner.role, token=token)

    class Runner:
        agent_id = "pi"

        def available(self):
            return True

        async def run(self, request):
            with bind_mcp_context(
                requester, request.conversation_id, str(request.turn_id)
            ):
                await query_api.propose_query("SELECT 1")
            yield AgentEvent(
                TOOL_END,
                {
                    "name": "mcp__dataagent__propose_query",
                    "is_error": False,
                    "summary": "opaque",
                },
            )
            yield AgentEvent(TEXT_DELTA, {"text": "must stop"})

    monkeypatch.setitem(agent_runner.RUNNERS, "pi", Runner())
    headers = {"Authorization": f"Bearer {token}"}
    provider = c.post(
        "/api/v1/providers",
        headers=headers,
        json={
            "name": "Local",
            "base_url": "http://127.0.0.1:3425",
            "models": ["model"],
        },
    ).json()
    response = c.post(
        "/api/v1/conversations/ask",
        headers=headers,
        json={
            "question": "统计",
            "agent": "pi",
            "provider_id": provider["id"],
            "model": "model",
        },
    )
    emitted = _sse(response.text)
    assert [name for name, _ in emitted] == ["conversation", "tool_end", "query_plan"]
    assert emitted[-1][1]["estimated_rows"] == 42
    assert emitted[-1][1]["sql"] == "SELECT 1"


def test_query_plan_stream_waits_for_confirmation_and_persists(client, monkeypatch):
    from semantica_dataagent import agent_runner

    class PlanRunner:
        agent_id = "pi"

        def available(self):
            return True

        async def run(self, request):
            yield AgentEvent("query_plan", {"id": "plan-1", "sql": "SELECT 1"})
            yield AgentEvent(TEXT_DELTA, {"text": "should not run"})

    monkeypatch.setitem(agent_runner.RUNNERS, "pi", PlanRunner())
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    provider = c.post(
        "/api/v1/providers",
        headers=headers,
        json={
            "name": "Local",
            "base_url": "http://127.0.0.1:3425",
            "models": ["model"],
        },
    ).json()
    response = c.post(
        "/api/v1/conversations/ask",
        headers=headers,
        json={
            "question": "查询",
            "agent": "pi",
            "provider_id": provider["id"],
            "model": "model",
        },
    )
    events = _sse(response.text)
    assert [kind for kind, _ in events] == ["conversation", "query_plan"]
    conversation_id = events[0][1]["conversation_id"]
    detail = c.get(f"/api/v1/conversations/{conversation_id}", headers=headers).json()
    assert detail["turns"][0]["status"] == "awaiting_confirmation"
    assert detail["turns"][0]["events"][0]["data"]["id"] == "plan-1"


def test_agent_mcp_credential_is_bound_to_running_turn(client, monkeypatch):
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials

    from semantica_dataagent import agent_runner
    from semantica_dataagent.auth import authenticate_mcp

    seen = []

    class Runner:
        agent_id = "pi"

        def available(self):
            return True

        async def run(self, request):
            credentials = HTTPAuthorizationCredentials(
                scheme="Bearer", credentials=request.mcp_token
            )
            identity = authenticate_mcp(
                credentials, request.conversation_id, str(request.turn_id)
            )
            seen.append(identity.id)
            with pytest.raises(HTTPException):
                authenticate_mcp(
                    credentials, request.conversation_id, str(request.turn_id + 1)
                )
            yield AgentEvent(DONE, {"text": "ok", "sql": [], "citations": []})

    monkeypatch.setitem(agent_runner.RUNNERS, "pi", Runner())
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    provider = c.post(
        "/api/v1/providers",
        headers=headers,
        json={
            "name": "Local",
            "base_url": "http://127.0.0.1:3425",
            "models": ["model"],
        },
    ).json()
    response = c.post(
        "/api/v1/conversations/ask",
        headers=headers,
        json={
            "question": "问题",
            "agent": "pi",
            "provider_id": provider["id"],
            "model": "model",
        },
    )
    assert _sse(response.text)[-1][0] == "done"
    assert seen


@pytest.mark.parametrize("interpret", [True, False])
def test_confirmed_query_resumes_agent_and_respects_interpretation_setting(
    client, monkeypatch, interpret
):
    from semantica_dataagent import (
        agent_runner,
        knowledge,
        query_api,
        sql_validation,
        telemetry,
    )
    from semantica_dataagent.auth import Requester
    from semantica_dataagent.db import Token, sessions
    from semantica_dataagent.mcp_context import bind_mcp_context

    executions = []
    traces = []
    monkeypatch.setattr(
        telemetry, "submit", lambda kind, payload, secrets: traces.append(payload)
    )

    class Executor:
        def bind(self, config, token):
            return self

        async def estimate(self, sql):
            return {"partitions": None, "rows": None}

        async def submit(self, sql, limit):
            executions.append(sql)
            return "fake-key"

        async def wait(self, key):
            return {"columns": ["sales"], "data": [["private-value-81"]]}

    monkeypatch.setattr(knowledge, "describe", lambda **_: [])
    monkeypatch.setattr(
        sql_validation, "validate_sql", lambda *a, **kw: {"status": "pass"}
    )
    monkeypatch.setattr(
        query_api, "_config", lambda: (Executor(), {"interpret_results": interpret})
    )
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    c.put("/api/v1/me/dtb-token", headers=headers, json={"token": "fake"})
    with sessions()() as db:
        owner = db.query(Token).first()
        requester = Requester(id=owner.id, role=owner.role, token=token)

    class Runner:
        agent_id = "pi"

        def __init__(self):
            self.requests = []

        def available(self):
            return True

        async def run(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                with bind_mcp_context(
                    requester, request.conversation_id, str(request.turn_id)
                ):
                    plan = await query_api.propose_query("SELECT 1")
                yield AgentEvent("query_plan", plan)
            else:
                yield AgentEvent(DONE, {"text": "结论", "sql": [], "citations": []})

    runner = Runner()
    monkeypatch.setitem(agent_runner.RUNNERS, "pi", runner)
    provider = c.post(
        "/api/v1/providers",
        headers=headers,
        json={
            "name": "Local",
            "base_url": "http://127.0.0.1:3425",
            "models": ["model"],
        },
    ).json()
    response = c.post(
        "/api/v1/conversations/ask",
        headers=headers,
        json={
            "question": "销售额",
            "agent": "pi",
            "provider_id": provider["id"],
            "model": "model",
        },
    )
    initial = _sse(response.text)
    plan = next(data for name, data in initial if name == "query_plan")
    conversation_id = initial[0][1]["conversation_id"]
    turn_id = initial[0][1]["turn_id"]
    path = f"/api/v1/conversations/{conversation_id}/turns/{turn_id}/queries/{plan['id']}/execute"
    other_token = c.post(
        "/api/v1/tokens", headers=headers, json={"name": "other", "role": "user"}
    ).json()["token"]
    assert (
        c.post(path, headers={"Authorization": f"Bearer {other_token}"}).status_code
        == 404
    )
    assert executions == []
    response = c.post(path, headers=headers)
    assert response.status_code == 200, response.text
    events = _sse(response.text)
    assert events[0][0] == "query_result"
    assert events[0][1]["rows"] == [["private-value-81"]]
    assert events[-1][0] == "done"
    assert len(executions) == 1
    assert "sales" in runner.requests[1].prompt
    assert ("private-value-81" in runner.requests[1].prompt) is interpret
    assert all("private-value-81" not in json.dumps(trace) for trace in traces)
    assert c.post(path, headers=headers).status_code == 409
    detail = c.get(f"/api/v1/conversations/{conversation_id}", headers=headers).json()
    assert detail["turns"][0]["answer"] == "结论"
    assert any(
        event["type"] == "query_result" for event in detail["turns"][0]["events"]
    )


def test_deleting_conversation_removes_its_query_results(client, monkeypatch):
    from semantica_dataagent import agent_runner, knowledge, query_api, sql_validation
    from semantica_dataagent.auth import Requester
    from semantica_dataagent.db import Conversation, Token, Turn, sessions
    from semantica_dataagent.mcp_context import bind_mcp_context

    class Executor:
        def bind(self, config, token):
            return self

        async def estimate(self, sql):
            return {"partitions": None, "rows": None}

        async def submit(self, sql, limit):
            return "key"

        async def wait(self, key):
            return {"columns": ["value"], "data": [[123]]}

    monkeypatch.setattr(knowledge, "describe", lambda **_: [])
    monkeypatch.setattr(
        sql_validation, "validate_sql", lambda *a, **kw: {"status": "pass"}
    )
    monkeypatch.setattr(query_api, "_config", lambda: (Executor(), {}))
    runner = FakeRunner()
    monkeypatch.setitem(agent_runner.RUNNERS, "pi", runner)
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    c.put("/api/v1/me/dtb-token", headers=headers, json={"token": "fake"})
    with sessions().begin() as db:
        owner = db.query(Token).first()
        requester = Requester(id=owner.id, role=owner.role, token=token)
        db.add(
            Conversation(
                id="conv-delete",
                owner_id=owner.id,
                title="q",
                agent="pi",
                provider_id="p",
                model="m",
            )
        )
        turn = Turn(
            conversation_id="conv-delete",
            sequence=1,
            question="q",
            status="running",
            answer="",
            sql="[]",
            citations="[]",
        )
        db.add(turn)
        db.flush()
        turn_id = turn.id
    with bind_mcp_context(requester, "conv-delete", str(turn_id)):
        plan = asyncio.run(query_api.propose_query("SELECT 1"))
    assert (
        c.post(
            f"/api/v1/queries/{plan['id']}/execute", headers=headers, json={}
        ).status_code
        == 403
    )
    assert asyncio.run(
        query_api.execute_for_turn(plan["id"], requester, "conv-delete", turn_id)
    )["rows"] == [[123]]
    with sessions().begin() as db:
        db.get(Turn, turn_id).status = "completed"
    assert c.get(f"/api/v1/queries/{plan['id']}", headers=headers).status_code == 200
    c.put("/api/v1/me/dtb-token", headers=headers, json={"token": ""})
    download = f"/api/v1/queries/{plan['id']}/download"
    assert c.get(download + "?format=csv", headers=headers).text == "value\n123\n"
    assert c.get(download + "?format=json", headers=headers).json() == {
        "columns": ["value"],
        "rows": [[123]],
    }
    from io import BytesIO

    from openpyxl import load_workbook

    workbook = load_workbook(
        BytesIO(c.get(download + "?format=xlsx", headers=headers).content)
    )
    assert list(workbook.active.values) == [("value",), (123,)]
    assert (
        c.delete("/api/v1/conversations/conv-delete", headers=headers).status_code
        == 204
    )
    assert c.get(f"/api/v1/queries/{plan['id']}", headers=headers).status_code == 404
    assert (
        c.get("/api/v1/conversations/conv-delete", headers=headers).status_code == 404
    )


def test_pi_runner_reads_jsonl_records_past_the_default_stream_limit(tmp_path: Path):
    script = tmp_path / "fake-pi.py"
    script.write_text(
        """#!/usr/bin/env python3
import json
import sys

text = "x" * 100000
print(json.dumps({"type": "message_end", "message": {
    "role": "assistant",
    "content": [{"type": "text", "text": text}],
    "stopReason": "stop",
}}), flush=True)
print(json.dumps({"type": "agent_settled"}), flush=True)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    request = AgentTurnRequest(
        conversation_id="conv-large",
        prompt="question",
        session_id="conv-large",
        workspace=tmp_path / "large-workspace",
        provider=ProviderConfig(
            id="provider-one",
            name="Local",
            type="anthropic-messages",
            base_url="http://127.0.0.1:3425",
            models=["model"],
        ),
        model="model",
        mcp_url="http://127.0.0.1:8000/mcp/",
        mcp_token="token",
    )

    async def collect():
        return [event async for event in PiRunner(binary=str(script)).run(request)]

    events = asyncio.run(collect())
    assert events[-1].type == DONE
    assert events[-1].data["text"] == "x" * 100000


def test_pi_runner_redacts_tokens_from_events(tmp_path: Path):
    script = tmp_path / "leaky-pi.py"
    script.write_text(
        """#!/usr/bin/env python3
import json

print(json.dumps({"type": "tool_execution_start", "toolCallId": "call-1",
                  "toolName": "bash",
                  "args": {"command": "echo token-value key-value"}}), flush=True)
print(json.dumps({"type": "message_end", "message": {
    "role": "assistant",
    "content": [{"type": "text", "text": "token-value key-value"}],
    "stopReason": "stop",
}}), flush=True)
print(json.dumps({"type": "agent_settled"}), flush=True)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    request = AgentTurnRequest(
        conversation_id="conv-redact",
        prompt="question",
        session_id="conv-redact",
        workspace=tmp_path / "redact-workspace",
        provider=ProviderConfig(
            id="provider-one",
            name="Local",
            type="anthropic-messages",
            base_url="http://127.0.0.1:3425",
            models=["model"],
            api_key="key-value",
        ),
        model="model",
        mcp_url="http://127.0.0.1:8000/mcp/",
        mcp_token="token-value",
    )

    async def collect():
        return [event async for event in PiRunner(binary=str(script)).run(request)]

    events = asyncio.run(collect())
    dumped = json.dumps([event.to_dict() for event in events])
    assert "token-value" not in dumped
    assert "key-value" not in dumped
    assert "[redacted]" in dumped


def test_pi_config_is_isolated_and_never_writes_secrets(tmp_path: Path):
    workspace = tmp_path / "pi-config-test"
    request = AgentTurnRequest(
        conversation_id="conv-one",
        prompt="question",
        session_id="conv-one",
        workspace=workspace,
        provider=ProviderConfig(
            id="provider-one",
            name="Local",
            type="anthropic-messages",
            base_url="http://127.0.0.1:3425",
            models=["deepseek-v4.1-flash#claude"],
            api_key="provider-secret-value",
        ),
        model="deepseek-v4.1-flash#claude",
        mcp_url="http://127.0.0.1:8000/mcp/",
        mcp_token="user-token-secret-value",
    )
    config, sessions, work = PiRunner().write_config(request)
    assert Path(work).is_dir() and sessions.is_dir()
    models = (config / "models.json").read_text()
    mcp = (config / "mcp.json").read_text()
    settings = json.loads((config / "settings.json").read_text())
    assert "$DATAAGENT_PROVIDER_KEY" in models
    assert "provider-secret-value" not in models
    assert "${DATAAGENT_TOKEN}" in mcp
    assert "user-token-secret-value" not in mcp
    assert '"exposure": "direct"' in mcp
    assert (
        json.loads(mcp)["mcpServers"]["dataagent"]["headers"][
            "x-dataagent-conversation-id"
        ]
        == "conv-one"
    )
    assert settings["compaction"] == {
        "enabled": True,
        "reserveTokens": 16384,
        "keepRecentTokens": 20000,
    }
    assert (config / "skills" / "dataagent-retrieval" / "SKILL.md").read_text() == (
        read_skill()
    )
    other = replace(
        request,
        conversation_id="conv-two",
        session_id="conv-two",
        workspace=tmp_path / "pi-config-test-two",
    )
    other_config, _, _ = PiRunner().write_config(other)
    assert other_config != config
    env = PiRunner()._environment(request, config, sessions)
    assert env["DATAAGENT_TOKEN"] == "user-token-secret-value"
    assert env["DATAAGENT_PROVIDER_KEY"] == "provider-secret-value"
    assert "ANTHROPIC_API_KEY" not in env


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_AGENT_ROOT", str(tmp_path / "agents"))
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    token = initialize()
    with TestClient(app) as test_client:
        yield test_client, token


class FakeRunner:
    agent_id = "pi"

    def __init__(self):
        self.requests: list[AgentTurnRequest] = []

    def available(self):
        return True

    async def run(self, request):
        self.requests.append(request)
        yield AgentEvent(TEXT_DELTA, {"text": "推荐 "})
        yield AgentEvent(
            TOOL_START,
            {
                "id": "call-1",
                "name": "mcp__dataagent__search_knowledge",
                "summary": "query=销售额 scope=docs",
                "args": {"query": "销售额", "scope": "docs"},
            },
        )
        yield AgentEvent(
            TOOL_END,
            {
                "id": "call-1",
                "name": "mcp__dataagent__search_knowledge",
                "is_error": False,
                "summary": "found sales metric",
            },
        )
        yield AgentEvent(TEXT_DELTA, {"text": "shop.orders。"})
        yield AgentEvent(
            DONE,
            {
                "text": (
                    "推荐 shop.orders。\n```sql\nSELECT 1\n```\n引用 table:shop.orders"
                ),
                "stop_reason": "stop",
                "sql": [{"sql": "SELECT 1", "dialect": "hive"}],
                "citations": [{"ref": "table:shop.orders", "type": "table"}],
            },
        )


def _sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name is not None:
            events.append((name, data))
    return events


def test_provider_key_is_encrypted_and_never_echoed(client):
    c, admin_token = client
    headers = {"Authorization": f"Bearer {admin_token}"}
    secret = "provider-api-key-for-test"
    created = c.post(
        "/api/v1/providers",
        headers=headers,
        json={
            "name": "Local gateway",
            "type": "anthropic-messages",
            "base_url": "http://127.0.0.1:3425",
            "api_key": secret,
            "models": ["deepseek-v4.1-flash#claude"],
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["has_api_key"] is True
    assert secret not in created.text
    provider_id = body["id"]
    database = Path(os.environ["DATAAGENT_DB_PATH"]).read_bytes()
    assert secret.encode() not in database
    listing = c.get("/api/v1/providers", headers=headers)
    assert listing.status_code == 200
    assert secret not in listing.text
    assert listing.json()[0]["has_api_key"] is True
    updated = c.put(
        f"/api/v1/providers/{provider_id}",
        headers=headers,
        json={"name": "Renamed"},
    )
    assert updated.json()["has_api_key"] is True
    cleared = c.put(
        f"/api/v1/providers/{provider_id}", headers=headers, json={"api_key": ""}
    )
    assert cleared.json()["has_api_key"] is False


def test_agents_endpoint_lists_the_registered_runners(client):
    c, admin_token = client
    response = c.get(
        "/api/v1/agents", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert response.status_code == 200
    assert response.json() == list_agents()


def test_ask_rejects_unknown_or_unavailable_agent_without_creating_history(
    client, monkeypatch
):
    c, admin_token = client
    headers = {"Authorization": f"Bearer {admin_token}"}
    provider = c.post(
        "/api/v1/providers",
        headers=headers,
        json={
            "name": "Local",
            "base_url": "http://127.0.0.1:3425",
            "models": ["deepseek-v4.1-flash#claude"],
        },
    ).json()
    body = {
        "question": "销售额",
        "provider_id": provider["id"],
        "model": "deepseek-v4.1-flash#claude",
    }
    assert (
        c.post(
            "/api/v1/conversations/ask",
            headers=headers,
            json={**body, "agent": "missing"},
        ).status_code
        == 422
    )
    assert c.get("/api/v1/conversations", headers=headers).json() == []

    from semantica_dataagent import agent_runner

    class UnavailableRunner:
        agent_id = "pi"

        def available(self):
            return False

        async def run(self, request):
            yield AgentEvent(TEXT_DELTA, {"text": "unused"})

    monkeypatch.setitem(agent_runner.RUNNERS, "pi", UnavailableRunner())
    assert (
        c.post(
            "/api/v1/conversations/ask",
            headers=headers,
            json={**body, "agent": "pi"},
        ).status_code
        == 503
    )
    assert c.get("/api/v1/conversations", headers=headers).json() == []


def test_ask_decrypts_provider_key_for_the_runner(client, monkeypatch):
    c, admin_token = client
    from semantica_dataagent import agent_runner

    runner = FakeRunner()
    monkeypatch.setitem(agent_runner.RUNNERS, "pi", runner)
    headers = {"Authorization": f"Bearer {admin_token}"}
    secret = "sk-decrypted-only-for-runner"
    provider = c.post(
        "/api/v1/providers",
        headers=headers,
        json={
            "name": "Local",
            "base_url": "http://127.0.0.1:3425",
            "api_key": secret,
            "models": ["deepseek-v4.1-flash#claude"],
        },
    ).json()
    with c.stream(
        "POST",
        "/api/v1/conversations/ask",
        headers=headers,
        json={
            "question": "销售额",
            "provider_id": provider["id"],
            "model": "deepseek-v4.1-flash#claude",
        },
    ) as response:
        text = "".join(response.iter_text())
    assert runner.requests[0].provider.api_key == secret
    assert secret not in text


def test_ask_streams_events_persists_history_and_reuses_session(client, monkeypatch):
    c, admin_token = client
    from semantica_dataagent import agent_runner

    runner = FakeRunner()
    monkeypatch.setitem(agent_runner.RUNNERS, "pi", runner)
    headers = {"Authorization": f"Bearer {admin_token}"}
    provider = c.post(
        "/api/v1/providers",
        headers=headers,
        json={
            "name": "Local",
            "base_url": "http://127.0.0.1:3425",
            "models": ["deepseek-v4.1-flash#claude"],
        },
    ).json()
    body = {
        "question": "渠道销售额用哪张表？",
        "agent": "pi",
        "provider_id": provider["id"],
        "model": "deepseek-v4.1-flash#claude",
    }
    with c.stream(
        "POST", "/api/v1/conversations/ask", headers=headers, json=body
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        text = "".join(response.iter_text())
    assert admin_token not in text
    events = _sse(text)
    assert events[0][0] == "conversation"
    conversation_id = events[0][1]["conversation_id"]
    assert [name for name, _ in events[1:]] == [
        "text_delta",
        "tool_start",
        "tool_end",
        "text_delta",
        "done",
    ]
    done = events[-1][1]
    assert done["sql"] == [{"sql": "SELECT 1", "dialect": "hive"}]
    assert done["citations"] == [{"ref": "table:shop.orders", "type": "table"}]
    assert runner.requests[0].session_id == conversation_id
    agent_token = runner.requests[0].mcp_token
    assert agent_token != admin_token
    assert (
        c.post(
            "/api/v1/queries/plan",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={"sql": "SELECT 1"},
        ).status_code
        == 401
    )
    assert (
        c.get(
            f"/api/v1/conversations/{conversation_id}",
            headers={"Authorization": f"Bearer {agent_token}"},
        ).status_code
        == 401
    )
    assert runner.requests[0].mcp_url.endswith("/mcp/")

    detail = c.get(f"/api/v1/conversations/{conversation_id}", headers=headers)
    assert detail.status_code == 200
    turn = detail.json()["turns"][0]
    assert turn["status"] == "completed"
    assert turn["answer"].startswith("推荐 shop.orders")
    assert turn["sql"] == [{"sql": "SELECT 1", "dialect": "hive"}]
    assert turn["citations"] == [{"ref": "table:shop.orders", "type": "table"}]
    assert [event["type"] for event in turn["events"]] == [
        "text_delta",
        "tool_start",
        "tool_end",
        "text_delta",
        "done",
    ]
    listing = c.get("/api/v1/conversations", headers=headers)
    assert listing.json()[0]["id"] == conversation_id
    assert listing.json()[0]["turn_count"] == 1

    with c.stream(
        "POST",
        "/api/v1/conversations/ask",
        headers=headers,
        json={"question": "再按月份拆分", "conversation_id": conversation_id},
    ) as response:
        follow_up = "".join(response.iter_text())
    assert _sse(follow_up)[-1][0] == "done"
    assert runner.requests[1].session_id == conversation_id
    assert runner.requests[1].workspace == runner.requests[0].workspace
    detail = c.get(f"/api/v1/conversations/{conversation_id}", headers=headers).json()
    assert [turn["sequence"] for turn in detail["turns"]] == [1, 2]
    assert detail["turn_count"] == 2


def test_another_user_cannot_read_a_conversation(client, monkeypatch):
    c, admin_token = client
    from semantica_dataagent import agent_runner

    monkeypatch.setitem(agent_runner.RUNNERS, "pi", FakeRunner())
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    provider = c.post(
        "/api/v1/providers",
        headers=admin_headers,
        json={
            "name": "Local",
            "base_url": "http://127.0.0.1:3425",
            "models": ["deepseek-v4.1-flash#claude"],
        },
    ).json()
    with c.stream(
        "POST",
        "/api/v1/conversations/ask",
        headers=admin_headers,
        json={
            "question": "销售额",
            "provider_id": provider["id"],
            "model": "deepseek-v4.1-flash#claude",
        },
    ) as response:
        text = "".join(response.iter_text())
    conversation_id = _sse(text)[0][1]["conversation_id"]
    user = c.post(
        "/api/v1/tokens",
        headers=admin_headers,
        json={"name": "reader", "role": "user"},
    ).json()
    other_headers = {"Authorization": f"Bearer {user['token']}"}
    assert (
        c.get(
            f"/api/v1/conversations/{conversation_id}", headers=other_headers
        ).status_code
        == 404
    )
    assert c.get("/api/v1/conversations", headers=other_headers).json() == []


def test_expired_conversation_retains_history_but_cannot_resume(
    client, monkeypatch, tmp_path
):
    from datetime import UTC, datetime, timedelta

    from semantica_dataagent import agent_runner, conversation_api
    from semantica_dataagent.db import Conversation, Turn, sessions

    c, token = client
    monkeypatch.setenv("DATAAGENT_AGENT_ROOT", str(tmp_path))
    monkeypatch.setitem(agent_runner.RUNNERS, "pi", FakeRunner())
    headers = {"Authorization": f"Bearer {token}"}
    provider = c.post(
        "/api/v1/providers",
        headers=headers,
        json={
            "name": "Local",
            "base_url": "http://127.0.0.1:3425",
            "models": ["model"],
        },
    ).json()
    response = c.post(
        "/api/v1/conversations/ask",
        headers=headers,
        json={"question": "hello", "provider_id": provider["id"], "model": "model"},
    )
    conversation_id = _sse(response.text)[0][1]["conversation_id"]
    session = tmp_path / conversation_id / "sessions" / "pi" / "saved"
    session.parent.mkdir(parents=True)
    session.write_text("history")
    with sessions().begin() as db:
        db.get(Conversation, conversation_id).updated_at = datetime.now(
            UTC
        ) - timedelta(days=2)
    assert (
        c.put(
            "/api/v1/settings",
            headers=headers,
            json={"values": {"session_retention_days": "3"}},
        ).status_code
        == 200
    )
    assert hasattr(conversation_api, "cleanup_expired_sessions"), (
        "session cleanup is missing"
    )
    conversation_api.cleanup_expired_sessions()
    assert session.exists()
    assert (
        c.put(
            "/api/v1/settings",
            headers=headers,
            json={"values": {"session_retention_days": "1"}},
        ).status_code
        == 200
    )
    with sessions().begin() as db:
        db.query(Turn).filter_by(
            conversation_id=conversation_id
        ).first().status = "running"
    conversation_api.cleanup_expired_sessions()
    assert session.exists(), "active conversation must not be cleaned"
    with sessions().begin() as db:
        db.query(Turn).filter_by(
            conversation_id=conversation_id
        ).first().status = "completed"
    assert hasattr(conversation_api, "cleanup_expired_sessions"), (
        "session cleanup is missing"
    )
    conversation_api.cleanup_expired_sessions()
    assert not session.exists()
    detail = c.get(f"/api/v1/conversations/{conversation_id}", headers=headers).json()
    assert detail["resumable"] is False
    assert detail["turns"][0]["question"] == "hello"
    assert (
        c.post(
            "/api/v1/conversations/ask",
            headers=headers,
            json={"question": "again", "conversation_id": conversation_id},
        ).status_code
        == 409
    )


def test_unavailable_langfuse_does_not_interrupt_an_online_answer(client, monkeypatch):
    from semantica_dataagent import agent_runner, telemetry

    monkeypatch.setitem(agent_runner.RUNNERS, "pi", FakeRunner())
    monkeypatch.setattr(
        telemetry, "configuration", lambda: ("http://invalid", "public", "private")
    )

    def fail(*args, **kwargs):
        raise TimeoutError("unavailable")

    monkeypatch.setattr(telemetry.httpx, "post", fail)
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    provider = c.post(
        "/api/v1/providers",
        headers=headers,
        json={
            "name": "Local",
            "base_url": "http://127.0.0.1:3425",
            "models": ["model"],
        },
    ).json()
    response = c.post(
        "/api/v1/conversations/ask",
        headers=headers,
        json={
            "question": "销售额",
            "agent": "pi",
            "provider_id": provider["id"],
            "model": "model",
        },
    )
    assert response.status_code == 200
    assert _sse(response.text)[-1][0] == "done"
    telemetry._queue.join()


def test_online_turn_reports_one_trace_with_final_answer(client, monkeypatch):
    from semantica_dataagent import agent_runner, telemetry

    monkeypatch.setitem(agent_runner.RUNNERS, "pi", FakeRunner())
    events = []
    monkeypatch.setattr(
        telemetry,
        "submit",
        lambda kind, event, secrets=(): events.append((kind, event)),
    )
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    provider = c.post(
        "/api/v1/providers",
        headers=headers,
        json={
            "name": "Local",
            "base_url": "http://127.0.0.1:3425",
            "models": ["model"],
        },
    ).json()
    response = c.post(
        "/api/v1/conversations/ask",
        headers=headers,
        json={
            "question": "销售额",
            "agent": "pi",
            "provider_id": provider["id"],
            "model": "model",
        },
    )
    assert _sse(response.text)[-1][0] == "done"
    assert [event[0] for event in events] == ["trace-create", "trace-create"]
    assert events[0][1]["id"] == events[1][1]["id"]
    assert "shop.orders" in events[1][1]["output"]
    assert events[0][1]["metadata"]["agent"] == "pi"


def test_local_mcp_tool_reports_name_without_token(client, monkeypatch):
    from semantica_dataagent import telemetry

    c, admin = client
    headers = {"Authorization": f"Bearer {admin}"}
    issued = c.post(
        "/api/v1/tokens",
        headers=headers,
        json={"name": "analytics client", "role": "user"},
    ).json()
    events = []
    monkeypatch.setattr(
        telemetry,
        "submit",
        lambda kind, event, secrets=(): events.append((kind, event)),
    )
    response = c.post(
        "/mcp/",
        headers={
            "Authorization": f"Bearer {issued['token']}",
            "Accept": "application/json, text/event-stream",
            "Host": "localhost:8000",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_capabilities", "arguments": {}},
        },
    )
    assert response.status_code == 200
    assert [event[0] for event in events] == ["trace-create", "span-create"]
    assert events[0][1]["metadata"]["token_name"] == "analytics client"
    assert events[0][1]["id"] == events[1][1]["traceId"]
    assert issued["token"] not in str(events)


def test_mcp_turn_header_selects_owned_turn_and_rejects_forgery(client, monkeypatch):
    from datetime import UTC, datetime

    from semantica_dataagent import telemetry
    from semantica_dataagent.db import Conversation, Token, Turn, digest, sessions

    c, admin = client
    auth = {"Authorization": f"Bearer {admin}"}
    other = c.post(
        "/api/v1/tokens", headers=auth, json={"name": "other", "role": "user"}
    ).json()["token"]
    with sessions().begin() as db:
        owner = db.get(Token, digest(admin))
        now = datetime.now(UTC)
        db.add(
            Conversation(
                id="trace-test",
                owner_id=owner.id,
                title="test",
                agent="pi",
                provider_id="missing",
                model="model",
                created_at=now,
                updated_at=now,
            )
        )
        db.flush()
        turns = [
            Turn(
                conversation_id="trace-test",
                sequence=i,
                question="q",
                status="running",
                answer="",
                sql="[]",
                citations="[]",
                started_at=now,
            )
            for i in (1, 2)
        ]
        db.add_all(turns)
        db.flush()
        ids = [turn.id for turn in turns]
    events = []
    monkeypatch.setattr(
        telemetry,
        "submit",
        lambda kind, event, secrets=(): events.append((kind, event)),
    )
    for token, turn in [(admin, ids[0]), (admin, ids[1]), (other, ids[0])]:
        response = c.post(
            "/mcp/",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
                "Host": "localhost:8000",
                "x-dataagent-conversation-id": "trace-test",
                "x-dataagent-turn-id": str(turn),
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "get_capabilities", "arguments": {}},
            },
        )
        assert response.status_code == 200
    spans = [body for kind, body in events if kind == "span-create"]
    assert [span["traceId"] for span in spans[:2]] == [
        telemetry.trace_id("trace-test", turn) for turn in ids
    ]
    assert spans[2]["traceId"] not in {
        telemetry.trace_id("trace-test", turn) for turn in ids
    }


def test_existing_conversation_rejects_changed_agent_or_model(client):
    from semantica_dataagent.db import Conversation, Token, digest, sessions

    c, token = client
    with sessions().begin() as db:
        owner_id = db.get(Token, digest(token)).id
        db.add(
            Conversation(
                id="fixed-settings",
                owner_id=owner_id,
                title="test",
                agent="pi",
                provider_id="original",
                model="model-a",
            )
        )
    headers = {"Authorization": f"Bearer {token}"}
    for override in (
        {"agent": "claude"},
        {"provider_id": "other"},
        {"model": "model-b"},
    ):
        response = c.post(
            "/api/v1/conversations/ask",
            headers=headers,
            json={"conversation_id": "fixed-settings", "question": "继续", **override},
        )
        assert response.status_code == 409, response.text
