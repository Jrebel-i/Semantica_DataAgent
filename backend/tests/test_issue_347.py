from test_conversation_project import CapturingRunner, _headers, _provider, _sse
from test_conversation_project import client as client  # noqa: PLC0414


def test_eval_token_issue_requires_admin_and_revoke(client, monkeypatch):
    c, admin = client
    user = c.post(
        "/api/v1/tokens",
        headers=_headers(admin),
        json={"name": "i347_user", "role": "user"},
    ).json()["token"]
    assert (
        c.post(
            "/api/v1/eval-tokens",
            headers=_headers(user),
            json={"project_id": "default"},
        ).status_code
        == 403
    )
    issued = c.post(
        "/api/v1/eval-tokens", headers=_headers(admin), json={"project_id": "default"}
    )
    assert issued.status_code == 201
    token = issued.json()["token"]
    from semantica_dataagent.mcp_context import resolve_eval_binding

    assert resolve_eval_binding(token)[0] == "default"
    assert (
        c.post(
            "/api/v1/eval-tokens/revoke", headers=_headers(admin), json={"token": token}
        ).status_code
        == 204
    )
    assert resolve_eval_binding(token) is None


def test_turn_sources_and_feedback(client, monkeypatch):
    from semantica_dataagent import agent_runner, telemetry
    from semantica_dataagent.db import Turn, sessions
    from semantica_dataagent.mcp_context import resolve_eval_binding

    c, admin = client
    provider = _provider(c, admin)
    observed = []

    class Runner(CapturingRunner):
        async def run(self, request):
            observed.append(resolve_eval_binding(request.mcp_token))
            async for event in super().run(request):
                yield event

    monkeypatch.setitem(agent_runner.RUNNERS, "pi", Runner())
    traces = []
    monkeypatch.setattr(
        telemetry,
        "submit",
        lambda kind, data, secrets=(): traces.append((kind, data, secrets)),
    )
    token = c.post(
        "/api/v1/eval-tokens", headers=_headers(admin), json={"project_id": "default"}
    ).json()["token"]
    payload = {
        "question": "i347_请澄清",
        "agent": "pi",
        "provider_id": provider["id"],
        "model": "project-model",
    }
    headers = {**_headers(admin), "X-Dataagent-Eval-Token": token}
    first = c.post("/api/v1/conversations/ask", headers=headers, json=payload)
    assert first.status_code == 200, first.text
    first_id = _sse(first.text)[0][1]
    second = c.post(
        "/api/v1/conversations/ask",
        headers=headers,
        json={
            **payload,
            "question": "i347_按月",
            "conversation_id": first_id["conversation_id"],
        },
    )
    assert second.status_code == 200, second.text
    second_id = _sse(second.text)[0][1]
    assert observed == [("default", None), ("default", None)]
    with sessions()() as db:
        assert db.get(Turn, first_id["turn_id"]).source == "eval"
        assert db.get(Turn, second_id["turn_id"]).source == "eval"
    assert [
        item[1]["metadata"]["source"] for item in traces if item[0] == "trace-create"
    ] == ["eval"] * 4
    assert all(token in item[2] for item in traces if item[0] == "trace-create")
    feedback = c.post(
        f"/api/v1/conversations/{first_id['conversation_id']}/turns/{first_id['turn_id']}/feedback",
        headers=_headers(admin),
        json={},
    )
    assert feedback.status_code == 409
    c.post("/api/v1/eval-tokens/revoke", headers=_headers(admin), json={"token": token})
    after = c.post(
        "/api/v1/conversations/ask",
        headers=headers,
        json={**payload, "conversation_id": first_id["conversation_id"]},
    )
    assert after.status_code == 200
    with sessions()() as db:
        assert db.get(Turn, _sse(after.text)[0][1]["turn_id"]).source == "online"
    mixed_feedback = c.post(
        f"/api/v1/conversations/{first_id['conversation_id']}/turns/{_sse(after.text)[0][1]['turn_id']}/feedback",
        headers=_headers(admin),
        json={},
    )
    assert mixed_feedback.status_code == 409
    member = c.post(
        "/api/v1/tokens",
        headers=_headers(admin),
        json={"name": "i347_member", "role": "user"},
    ).json()["token"]
    forged = c.post(
        "/api/v1/conversations/ask",
        headers={**_headers(member), "X-Source": "eval"},
        json={**payload, "source": "eval"},
    )
    assert forged.status_code == 200
    with sessions()() as db:
        assert db.get(Turn, _sse(forged.text)[0][1]["turn_id"]).source == "online"


def test_evaluation_session_revokes_on_exception():
    import httpx
    import pytest

    from semantica_dataagent.eval_exec import _evaluation_session

    requests = []

    def handler(request):
        requests.append(request)
        return (
            httpx.Response(201, json={"token": "i347_secret"})
            if len(requests) == 1
            else httpx.Response(204)
        )

    with (
        httpx.Client(
            base_url="http://test", transport=httpx.MockTransport(handler)
        ) as client,
        pytest.raises(RuntimeError),
        _evaluation_session(client, "default"),
    ):
        assert client.headers["X-Dataagent-Eval-Token"] == "i347_secret"
        raise RuntimeError("interrupted")
    assert requests[-1].url.path == "/api/v1/eval-tokens/revoke"


def test_invalid_expired_and_other_project_tokens_are_online(client, monkeypatch):
    from semantica_dataagent import agent_runner, mcp_context
    from semantica_dataagent.db import Turn, sessions

    c, admin = client
    provider = _provider(c, admin)
    monkeypatch.setitem(agent_runner.RUNNERS, "pi", CapturingRunner())
    payload = {
        "question": "i347_问题",
        "agent": "pi",
        "provider_id": provider["id"],
        "model": "project-model",
    }
    other = c.post(
        "/api/v1/eval-tokens",
        headers=_headers(admin),
        json={"project_id": "i347_other"},
    ).json()["token"]
    expired = c.post(
        "/api/v1/eval-tokens",
        headers=_headers(admin),
        json={"project_id": "default", "ttl_seconds": 1},
    ).json()["token"]
    clock = mcp_context.time.monotonic()
    monkeypatch.setattr(mcp_context.time, "monotonic", lambda: clock + 2)
    for candidate in (None, "i347_invalid", other, expired):
        headers = _headers(admin)
        if candidate:
            headers["X-Dataagent-Eval-Token"] = candidate
        response = c.post("/api/v1/conversations/ask", headers=headers, json=payload)
        assert response.status_code == 200, response.text
        with sessions()() as db:
            assert db.get(Turn, _sse(response.text)[0][1]["turn_id"]).source == "online"


def test_real_http_two_turns_and_revocation(client, monkeypatch):
    import socket
    import threading
    import time

    import httpx
    import uvicorn

    from semantica_dataagent import agent_runner, telemetry
    from semantica_dataagent.agent_runner import AgentEvent
    from semantica_dataagent.app import app
    from semantica_dataagent.db import Turn, sessions
    from semantica_dataagent.evolution_runner import _filter_and_scrub_conversations
    from semantica_dataagent.plugins import TraceConversation, TraceMessage

    c, admin = client
    provider = _provider(c, admin)
    traces = []
    monkeypatch.setattr(
        telemetry, "submit", lambda kind, data, secrets=(): traces.append((kind, data))
    )

    class ClarifyingRunner(CapturingRunner):
        async def run(self, request):
            if request.prompt.startswith("i347_请澄清"):
                yield AgentEvent(
                    "clarification", {"questions": [{"question": "按月吗？"}]}
                )
            async for event in super().run(request):
                yield event

    monkeypatch.setitem(agent_runner.RUNNERS, "pi", ClarifyingRunner())
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]})
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with httpx.Client(
            base_url=f"http://127.0.0.1:{sock.getsockname()[1]}",
            headers=_headers(admin),
            trust_env=False,
            timeout=20,
        ) as remote:
            issued = remote.post("/api/v1/eval-tokens", json={"project_id": "default"})
            assert issued.status_code == 201
            token = issued.json()["token"]
            headers = {"X-Dataagent-Eval-Token": token}
            payload = {
                "question": "i347_请澄清",
                "agent": "pi",
                "provider_id": provider["id"],
                "model": "project-model",
            }
            one = remote.post(
                "/api/v1/conversations/ask", headers=headers, json=payload
            )
            events = _sse(one.text)
            assert one.status_code == 200 and any(
                name == "clarification" for name, _ in events
            )
            first = events[0][1]
            two = remote.post(
                "/api/v1/conversations/ask",
                headers=headers,
                json={
                    **payload,
                    "question": "i347_按月",
                    "conversation_id": first["conversation_id"],
                },
            )
            assert two.status_code == 200
            second = _sse(two.text)[0][1]
            assert (
                remote.post(
                    "/api/v1/eval-tokens/revoke", json={"token": token}
                ).status_code
                == 204
            )
            third = remote.post(
                "/api/v1/conversations/ask", headers=headers, json=payload
            )
            fourth = remote.post(
                "/api/v1/conversations/ask",
                headers={"X-Source": "eval"},
                json={**payload, "source": "eval"},
            )
            assert third.status_code == fourth.status_code == 200
            ids = [
                first["turn_id"],
                second["turn_id"],
                _sse(third.text)[0][1]["turn_id"],
                _sse(fourth.text)[0][1]["turn_id"],
            ]
            with sessions()() as db:
                assert [db.get(Turn, turn).source for turn in ids] == [
                    "eval",
                    "eval",
                    "online",
                    "online",
                ]
            sources = [data for kind, data in traces if kind == "trace-create"]
            assert [entry["metadata"]["source"] for entry in sources] == [
                "eval",
                "eval",
                "eval",
                "eval",
                "online",
                "online",
                "online",
                "online",
            ]
            candidates = [
                TraceConversation(
                    trace_id=entry["id"],
                    project_id="default",
                    messages=[TraceMessage(role="user", content="i347_question")],
                    metadata=entry["metadata"],
                )
                for entry in sources[::2]
            ]
            kept = _filter_and_scrub_conversations(
                candidates, project_id="default", secrets={}
            )
            assert {item.trace_id for item in kept} == {
                item.trace_id for item in candidates[2:]
            }
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()


def test_eval_mcp_call_suppresses_telemetry(client, monkeypatch):
    import asyncio

    from mcp.server.fastmcp import FastMCP
    from mcp.types import CallToolRequest, CallToolRequestParams

    from semantica_dataagent import telemetry
    from semantica_dataagent.auth import Requester
    from semantica_dataagent.mcp_context import bind_mcp_context
    from semantica_dataagent.mcp_contract import install_contract

    _, admin = client
    emitted = []
    monkeypatch.setattr(telemetry, "submit", lambda *args: emitted.append(args))
    server = FastMCP("i347")

    @server.tool()
    def i347_probe() -> str:
        return "ok"

    install_contract(server)
    server.get_context = lambda: None
    handler = server._mcp_server.request_handlers[CallToolRequest]

    async def call(suppressed):
        with bind_mcp_context(
            Requester(id="i347", role="admin", token=admin),
            project_id="default",
            suppress_trajectory=suppressed,
        ):
            result = await handler(
                CallToolRequest(
                    method="tools/call",
                    params=CallToolRequestParams(name="i347_probe", arguments={}),
                )
            )
            assert not result.root.isError

    asyncio.run(call(True))
    assert emitted == []
    asyncio.run(call(False))
    assert {entry[0] for entry in emitted} == {"trace-create", "span-create"}


def test_migration_preserves_existing_turns_and_can_downgrade(tmp_path):
    import sqlite3

    from alembic import command
    from test_cli_migrate import _config

    path = tmp_path / "i347.db"
    cfg = _config(path)
    command.upgrade(cfg, "0050")
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO turns (id, conversation_id, sequence, question, status, answer, sql, citations, started_at) VALUES (1, 'i347', 1, 'old', 'completed', 'answer', '[]', '[]', '2026-01-01')"
        )
        before = db.execute("SELECT * FROM turns").fetchall()
        columns_before = db.execute("PRAGMA table_info(turns)").fetchall()
    command.upgrade(cfg, "0051")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM turns").fetchall() == [before[0] + (None,)]
        columns_after = db.execute("PRAGMA table_info(turns)").fetchall()
        assert columns_after[:-1] == columns_before
        assert columns_after[-1][1:5] == ("source", "VARCHAR", 0, None)
    command.downgrade(cfg, "0050")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM turns").fetchall() == before
        assert db.execute("PRAGMA table_info(turns)").fetchall() == columns_before


def test_langfuse_source_filters_eval_traces():
    import httpx

    from semantica_dataagent.plugins import TraceQuery
    from semantica_dataagent.trace_sources import LangfuseTraceSource

    def handler(request):
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "i347_eval",
                        "metadata": {"project_id": "default", "source": "eval"},
                        "input": "i347_q",
                        "output": "i347_a",
                    },
                    {
                        "id": "i347_online",
                        "metadata": {"project_id": "default", "source": "online"},
                        "input": "i347_q",
                        "output": "i347_a",
                    },
                ],
                "meta": {"page": 1, "totalPages": 1},
            },
        )

    source = LangfuseTraceSource(
        client_factory=lambda **kwargs: httpx.Client(
            transport=httpx.MockTransport(handler), **kwargs
        )
    )
    found = source.fetch_traces(
        TraceQuery(
            project_id="default",
            parameters={"base_url": "https://example.test", "public_key": "i347_pk"},
            secrets={"secret_key": "i347_sk"},
        )
    )
    assert [row.trace_id for row in found] == ["i347_online"]


def test_feedback_uses_one_snapshot_when_eval_turn_arrives_during_validation(
    client, monkeypatch
):
    import json

    from semantica_dataagent import (
        agent_runner,
        corrections,
        eval_questions,
        historical_tickets,
    )
    from semantica_dataagent.db import Clarification, Conversation, Turn, sessions

    c, admin = client
    provider = _provider(c, admin)
    monkeypatch.setitem(agent_runner.RUNNERS, "pi", CapturingRunner())
    response = c.post(
        "/api/v1/conversations/ask",
        headers=_headers(admin),
        json={
            "question": "i347_online",
            "agent": "pi",
            "provider_id": provider["id"],
            "model": "project-model",
        },
    )
    assert response.status_code == 200
    identifiers = _sse(response.text)[0][1]
    with sessions().begin() as db:
        db.get(Turn, identifiers["turn_id"]).source = None
    captured = {}

    def insert_during_validation(*args):
        with sessions().begin() as db:
            conv = db.get(Conversation, identifiers["conversation_id"])
            db.add(
                Turn(
                    conversation_id=conv.id,
                    sequence=2,
                    question="i347_eval_secret",
                    source="eval",
                    status="completed",
                    answer="i347_eval_answer",
                    sql="[]",
                    citations="[]",
                )
            )
            db.add(
                Clarification(
                    owner_id=conv.owner_id,
                    conversation_id=conv.id,
                    questions=json.dumps([{"question": "i347_eval_clarification"}]),
                    status="pending",
                )
            )
        return True, None

    def capture_synthesis(*args, **kwargs):
        captured.update(kwargs)
        return {
            "title": "i347_ticket",
            "target": "document:knowledge/historical-tickets/i347.md",
            "patch": "i347_patch",
            "reason": "i347_reason",
            "synthesis_mode": "fallback",
        }

    monkeypatch.setattr(
        eval_questions, "validate_sql_if_available", insert_during_validation
    )
    monkeypatch.setattr(
        historical_tickets, "check_eval_benchmark_leakage", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        historical_tickets, "synthesize_historical_ticket", capture_synthesis
    )
    monkeypatch.setattr(
        corrections,
        "propose_in_project",
        lambda *a, **kw: {
            "proposal_id": "i347",
            "status": "pending",
            "target": kw["target"],
        },
    )
    result = c.post(
        f"/api/v1/conversations/{identifiers['conversation_id']}/turns/{identifiers['turn_id']}/feedback",
        headers=_headers(admin),
        json={"reference_sql": "SELECT 1"},
    )
    assert result.status_code == 201, result.text
    assert captured["context_turns"][0]["question"] == "i347_online"
    assert "i347_eval" not in json.dumps(captured, ensure_ascii=False)
