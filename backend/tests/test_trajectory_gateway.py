"""Gateway capture, Langfuse project enrichment, scrubbing, and purge tests (#295)."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from semantica_dataagent import (
    agent_runner,
    conversation_api,
    knowledge,
    project_deletion,
    query_api,
    trajectories,
)
from semantica_dataagent.agent_runner import (
    CLARIFICATION,
    DONE,
    AgentEvent,
    AgentTurnRequest,
)


def _setup_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("DATAAGENT_DTB_CONFIG_DIR", str(tmp_path / "dtb"))

    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    admin_token = initialize()
    return TestClient(app), admin_token


def _create_provider_and_project(c: TestClient, admin_token: str, project_name: str):
    headers = {"Authorization": f"Bearer {admin_token}"}
    provider = c.post(
        "/api/v1/providers",
        headers=headers,
        json={
            "name": "LocalMock",
            "base_url": "http://127.0.0.1:18080",
            "api_key": "provider-secret-key-xyz",
            "models": ["model-a"],
        },
    )
    assert provider.status_code in (200, 201), provider.text
    provider_id = provider.json()["id"]

    proj = c.post(
        "/api/v1/projects",
        headers=headers,
        json={"name": project_name, "type": "warehouse"},
    )
    assert proj.status_code in (200, 201), proj.text
    return provider_id, proj.json()["id"], proj.json()["name"]


def _mcp_call(
    c: TestClient,
    token: str,
    name: str,
    arguments: dict,
    *,
    project_id: str | None = None,
    conversation_id: str | None = None,
    turn_id: str | None = None,
):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Host": "localhost:8000",
    }
    if project_id:
        headers["X-Dataagent-Project"] = project_id
    if conversation_id:
        headers["X-Dataagent-Conversation-Id"] = conversation_id
    if turn_id:
        headers["X-Dataagent-Turn-Id"] = turn_id
    return c.post(
        "/mcp/",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )


def _start_langfuse_receiver():
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            received.append(body)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, received


class _FakeOnlineRunner:
    agent_id = "pi"

    def __init__(self, long_sql: str, asgi_app=None):
        self._long_sql = long_sql
        self._asgi_app = asgi_app

    def available(self) -> bool:
        return True

    async def run(self, request: AgentTurnRequest):
        if "查询已执行" in request.prompt:
            yield AgentEvent(
                DONE,
                {
                    "text": "结果汇总完毕",
                    "sql": [{"sql": self._long_sql, "dialect": "doris"}],
                    "citations": [{"ref": "table:shop.orders"}],
                },
            )
            return
        if self._asgi_app is not None:
            import httpx

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self._asgi_app),
                base_url="http://localhost:8000",
            ) as ac:
                resp = await ac.post(
                    "/mcp/",
                    headers={
                        "Authorization": f"Bearer {request.mcp_token}",
                        "Accept": "application/json, text/event-stream",
                        "Host": "localhost:8000",
                        "X-Dataagent-Conversation-Id": request.conversation_id,
                        "X-Dataagent-Turn-Id": str(request.turn_id),
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "validate_sql",
                            "arguments": {"sql": self._long_sql, "dialect": "doris"},
                        },
                    },
                )
            assert resp.status_code == 200
        yield AgentEvent(
            CLARIFICATION,
            {
                "clarification_id": "clar-online-1",
                "status": "pending",
                "questions": [
                    {
                        "id": "q1",
                        "question": "口径？",
                        "options": [
                            {"id": "o1", "label": "支付", "ref": "document:m/paid.md"}
                        ],
                    }
                ],
            },
        )
        yield AgentEvent(
            DONE,
            {
                "text": "完成解答",
                "sql": [{"sql": self._long_sql, "dialect": "doris"}],
                "citations": [{"ref": "table:shop.orders"}],
            },
        )


def test_online_ask_attachments_confirm_query_and_ask_dataagent_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    c, admin_token = _setup_client(tmp_path, monkeypatch)
    with c:
        provider_id, sales_proj, _ = _create_provider_and_project(
            c, admin_token, "Sales Proj"
        )
        long_sql = (
            "SELECT " + ", ".join(f"col_{i}" for i in range(600)) + " FROM shop.orders"
        )
        runner = _FakeOnlineRunner(long_sql, c.app)
        monkeypatch.setitem(agent_runner.RUNNERS, "pi", runner)
        monkeypatch.setattr(knowledge, "search", lambda *a, **kw: {"result": []})

        headers = {"Authorization": f"Bearer {admin_token}"}
        # 1. ask-with-attachments: attachment file body must NEVER land in trajectory
        resp = c.post(
            "/api/v1/conversations/ask-with-attachments",
            headers=headers,
            data={
                "question": "查看订单总额",
                "agent": "pi",
                "provider_id": provider_id,
                "model": "model-a",
                "project": sales_proj,
            },
            files={
                "attachments": (
                    "notes.txt",
                    b"ATTACHMENT_RAW_FILE_BODY password=secret_in_file",
                    "text/plain",
                )
            },
        )
        assert resp.status_code == 200, resp.text
        conv_event = next(
            json.loads(line[6:])
            for line in resp.text.splitlines()
            if line.startswith("data: ")
        )
        conv_id = conv_event["conversation_id"]
        turn_id = conv_event["turn_id"]

        rows = trajectories.list_trajectories(sales_proj)
        assert len(rows) == 1
        row = rows[0]
        assert row["source_type"] == "online"
        assert row["question"] == "查看订单总额"
        assert row["final_sql"] == [{"sql": long_sql, "dialect": "doris"}]
        assert row["clarifications"][0]["questions"][0]["options"][0]["id"] == "o1"
        assert "document:m/paid.md" in row["matched_refs"]
        assert "table:shop.orders" in row["matched_refs"]
        assert "ATTACHMENT_RAW_FILE_BODY" not in str(row)
        assert "secret_in_file" not in str(row)
        assert "provider-secret-key-xyz" not in str(row)

        # 2. confirm_query execution: rows / raw errorMessage must NEVER land in trajectory
        from semantica_dataagent.db import (
            QueryExecution,
            Token,
            Turn,
            TurnEvent,
            digest,
            sessions,
        )

        with sessions().begin() as db:
            owner_row = db.get(Token, digest(admin_token))
            turn = db.get(Turn, turn_id)
            turn.status = "awaiting_confirmation"
            db.add(
                TurnEvent(
                    turn_id=turn_id,
                    seq=99,
                    type="query_plan",
                    data=json.dumps(
                        {"id": "qp-1", "sql": "SELECT 1", "dialect": "doris"}
                    ),
                )
            )
            db.add(
                QueryExecution(
                    id="qp-1",
                    owner_id=owner_row.id,
                    conversation_id=conv_id,
                    turn_id=turn_id,
                    sql="SELECT 1",
                    status="planned",
                    plan_data=json.dumps({"id": "qp-1", "sql": "SELECT 1"}),
                    created_at=datetime.now(UTC),
                )
            )

        async def _fake_execute_for_turn(query_id, requester, conversation_id, t_id):
            with sessions().begin() as db:
                qe = db.get(QueryExecution, query_id)
                qe.status = "executed"
                qe.rows = json.dumps(
                    {
                        "columns": ["id", "phone"],
                        "data": [["ord-1", "13900001111-CONFIRM-ROW-SECRET"]],
                        "row_count": 1,
                    }
                )
            return {
                "query_id": query_id,
                "status": "executed",
                "sql": "SELECT 1",
                "columns": ["id", "phone"],
                "rows": [["ord-1", "13900001111-CONFIRM-ROW-SECRET"]],
                "row_count": 1,
                "errorMessage": "RAW_CONFIRM_ERROR_LEAK",
            }

        monkeypatch.setattr(query_api, "execute_for_turn", _fake_execute_for_turn)
        monkeypatch.setattr(
            query_api,
            "_config",
            lambda: (None, {"interpret_results": True}),
        )
        confirm_resp = c.post(
            f"/api/v1/conversations/{conv_id}/turns/{turn_id}/queries/qp-1/execute",
            headers=headers,
        )
        assert confirm_resp.status_code == 200, confirm_resp.text
        updated = trajectories.list_trajectories(sales_proj)[0]
        assert "13900001111-CONFIRM-ROW-SECRET" not in str(updated)
        assert "RAW_CONFIRM_ERROR_LEAK" not in str(updated)

        # 3. MCP ask_dataagent records online trajectory under requested project
        monkeypatch.setattr(
            "semantica_dataagent.ask_api._choices",
            lambda: ("pi", provider_id, "model-a"),
        )
        ask_mcp = _mcp_call(
            c,
            admin_token,
            "ask_dataagent",
            {"question": "统计用户数"},
            project_id=sales_proj,
        )
        assert ask_mcp.status_code == 200, ask_mcp.text
        all_rows = trajectories.list_trajectories(sales_proj)
        assert len(all_rows) == 2
        assert all_rows[1]["question"] == "统计用户数"
        assert all_rows[1]["source_type"] == "online"


def test_local_mcp_gateway_capture_scrubbing_and_langfuse_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    server, received = _start_langfuse_receiver()
    try:
        c, admin_token = _setup_client(tmp_path, monkeypatch)
        with c:
            headers = {"Authorization": f"Bearer {admin_token}"}
            _, proj_a, _ = _create_provider_and_project(c, admin_token, "Proj A")
            host = f"http://127.0.0.1:{server.server_address[1]}"
            resp = c.put(
                "/api/v1/settings",
                headers=headers,
                json={
                    "values": {
                        "langfuse_enabled": "true",
                        "langfuse_url": host,
                        "langfuse_public_key": "pk-lf-test",
                        "langfuse_secret_key": "sk-lf-test",
                        "langfuse_capture_content": "false",
                    }
                },
            )
            assert resp.status_code == 200, resp.text

            user_tok = c.post(
                "/api/v1/tokens",
                headers=headers,
                json={"name": "local-cli", "role": "user"},
            ).json()["token"]
            from semantica_dataagent.db import ProjectMember, Token, digest, sessions

            with sessions().begin() as db:
                tok_row = db.get(Token, digest(user_tok))
                if (
                    db.query(ProjectMember)
                    .filter_by(project_id=proj_a, token_id=tok_row.id)
                    .first()
                    is None
                ):
                    db.add(
                        ProjectMember(
                            project_id=proj_a, token_id=tok_row.id, role="member"
                        )
                    )

            monkeypatch.setattr(
                knowledge,
                "search",
                lambda *a, **kw: {
                    "result": [
                        {"ref": "table:shop.orders", "type": "table", "score": 0.9}
                    ]
                },
            )
            from semantica_dataagent import sample_api

            monkeypatch.setattr(
                sample_api,
                "sample_table_data",
                lambda table, limit=10: {
                    "table": table,
                    "sql": f"SELECT * FROM {table} LIMIT {limit}",
                    "columns": ["id", "secret_col"],
                    "rows": [["r1", "SAMPLE_SECRET_ROW_VALUE"]],
                    "row_count": 1,
                    "errorMessage": "RAW_DTB_SAMPLE_ERROR",
                },
            )
            from semantica_dataagent import sql_validation

            monkeypatch.setattr(
                sql_validation,
                "validate_sql",
                lambda sql, dialect="doris", project_id="default": {
                    "status": "issues",
                    "issues": [{"kind": "missing_table", "table": "shop.orders"}],
                },
            )

            r1 = _mcp_call(
                c,
                user_tok,
                "search_knowledge",
                {"query": "订单表", "scope": "tables"},
                project_id=proj_a,
            )
            assert r1.status_code == 200
            r2 = _mcp_call(
                c,
                user_tok,
                "sample_table_data",
                {"table": "shop.orders", "limit": 5},
                project_id=proj_a,
            )
            assert r2.status_code == 200
            r3 = _mcp_call(
                c,
                user_tok,
                "validate_sql",
                {"sql": "SELECT id FROM shop.orders", "dialect": "doris"},
                project_id=proj_a,
            )
            assert r3.status_code == 200

            rows = trajectories.list_trajectories(proj_a)
            assert len(rows) == 1
            row = rows[0]
            assert row["source_type"] == "local_mcp"
            assert [tc["tool_name"] for tc in row["tool_calls"]] == [
                "search_knowledge",
                "sample_table_data",
                "validate_sql",
            ]
            assert "SAMPLE_SECRET_ROW_VALUE" not in str(row)
            assert "RAW_DTB_SAMPLE_ERROR" not in str(row)

            from semantica_dataagent import telemetry

            telemetry._queue.join()
            batch_events = [
                event for batch in received for event in batch.get("batch", [])
            ]
            trace_events = [e for e in batch_events if e.get("type") == "trace-create"]
            span_events = [e for e in batch_events if e.get("type") == "span-create"]
            assert len(trace_events) == 3
            assert len(span_events) == 3
            expected_trace_id = row["langfuse_trace_id"]
            assert all(e["body"]["id"] == expected_trace_id for e in trace_events)
            assert all(
                e["body"]["metadata"].get("project_id") == proj_a for e in trace_events
            )
            assert all(
                e["body"]["metadata"].get("traceId") == expected_trace_id
                for e in trace_events
            )
            assert all(
                e["body"]["traceId"] == expected_trace_id
                and e["body"]["metadata"].get("project_id") == proj_a
                and e["body"]["metadata"].get("traceId") == expected_trace_id
                for e in span_events
            )
            # capture_content is False -> raw query / answer must NOT be sent to Langfuse
            assert "订单表" not in str(batch_events)
            assert user_tok not in str(batch_events)
    finally:
        server.shutdown()


def test_trajectory_purge_on_conversation_delete_expiry_and_project_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    c, admin_token = _setup_client(tmp_path, monkeypatch)
    with c:
        provider_id, proj_del, proj_del_name = _create_provider_and_project(
            c, admin_token, "Proj Del"
        )
        headers = {"Authorization": f"Bearer {admin_token}"}
        runner = _FakeOnlineRunner("SELECT 1 FROM shop.orders")
        monkeypatch.setitem(agent_runner.RUNNERS, "pi", runner)

        # Create two conversations in default project
        r1 = c.post(
            "/api/v1/conversations/ask",
            headers=headers,
            json={
                "question": "问题一",
                "agent": "pi",
                "provider_id": provider_id,
                "model": "model-a",
            },
        )
        r2 = c.post(
            "/api/v1/conversations/ask",
            headers=headers,
            json={
                "question": "问题二",
                "agent": "pi",
                "provider_id": provider_id,
                "model": "model-a",
            },
        )
        c1 = json.loads(
            next(l[6:] for l in r1.text.splitlines() if l.startswith("data: "))
        )["conversation_id"]
        c2 = json.loads(
            next(l[6:] for l in r2.text.splitlines() if l.startswith("data: "))
        )["conversation_id"]
        assert len(trajectories.list_trajectories("default")) == 2

        # 1. DELETE /api/v1/conversations/{id} synchronously removes c1 trajectory only
        del_resp = c.delete(f"/api/v1/conversations/{c1}", headers=headers)
        assert del_resp.status_code == 204
        remaining = trajectories.list_trajectories("default")
        assert [r["conversation_id"] for r in remaining] == [c2]

        # 2. cleanup_expired_sessions purges expired online AND local_mcp trajectories
        old_time = datetime.now(UTC) - timedelta(days=40)
        trajectories.record_mcp_tool_call(
            project_id="default",
            token_id="tok-expired",
            conversation_id=None,
            turn_id=None,
            is_online_turn=False,
            tool_name="validate_sql",
            arguments={"sql": "SELECT 1", "dialect": "doris"},
            output={"status": "pass", "issues": []},
            is_error=False,
            now=old_time,
        )
        trajectories.record_mcp_tool_call(
            project_id="default",
            token_id="tok-fresh",
            conversation_id=None,
            turn_id=None,
            is_online_turn=False,
            tool_name="validate_sql",
            arguments={"sql": "SELECT 2", "dialect": "doris"},
            output={"status": "pass", "issues": []},
            is_error=False,
            now=datetime.now(UTC),
        )
        from semantica_dataagent.db import Conversation, sessions

        with sessions().begin() as db:
            conv = db.get(Conversation, c2)
            conv.updated_at = old_time

        conversation_api.cleanup_expired_sessions()
        after_expiry = trajectories.list_trajectories("default")
        assert [r["token_id"] for r in after_expiry] == ["tok-fresh"]

        # 3. project_deletion synchronously purges all project trajectories
        trajectories.record_mcp_tool_call(
            project_id=proj_del,
            token_id="tok-p",
            conversation_id=None,
            turn_id=None,
            is_online_turn=False,
            tool_name="validate_sql",
            arguments={"sql": "SELECT 3", "dialect": "doris"},
            output={"status": "pass", "issues": []},
            is_error=False,
        )
        assert len(trajectories.list_trajectories(proj_del)) == 1
        monkeypatch.setattr(
            project_deletion,
            "_delete_neo4j",
            lambda pid, s: SimpleNamespace(),
        )
        monkeypatch.setattr(
            project_deletion,
            "_delete_milvus",
            lambda pid, s: SimpleNamespace(),
        )
        arch = c.post(f"/api/v1/projects/{proj_del}/archive", headers=headers)
        assert arch.status_code == 200, arch.text
        del_proj = c.post(
            f"/api/v1/projects/{proj_del}/delete",
            headers=headers,
            json={"name": proj_del_name},
        )
        assert del_proj.status_code in (200, 202, 204), del_proj.text
        assert trajectories.list_trajectories(proj_del) == []
        # Default project trajectory remains untouched
        assert len(trajectories.list_trajectories("default")) == 1
