"""Telemetry content capture is opt-in and SQL fingerprints are literal-free."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from semantica_dataagent import telemetry
from semantica_dataagent.agent_runner import DONE, TEXT_DELTA, AgentEvent

SQL = "SELECT id FROM shop.orders WHERE dt = '2025-01-01' AND customer = 'issue-234-literal'"
QUESTION = "issue-234-question"
PROMPT = "issue-234-prompt"
RESULT_ROW = "issue-234-result-row"
ERROR_MESSAGE = "issue-234-error-message"
FORGED_TABLE = "issue-234-forged-table"
FORGED_NAME = "issue-234-forged-name"
FORGED_CODE = "issue-234-forged-code"
FORGED_PROMPT = "issue-234-forged natural language prompt"
UNREGISTERED_TOOL_NAME = "issue_234_private_customer_record_123"


class Receiver:
    """A local Langfuse ingestion endpoint that records request bodies."""

    def __init__(self):
        self.bodies = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                owner.bodies.append(json.loads(self.rfile.read(length)))
                payload = b'{"success": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def wait(self, count=1, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if len(self.bodies) >= count:
                telemetry._queue.join()
                return
            time.sleep(0.02)
        raise AssertionError(
            f"expected {count} ingestion requests, got {len(self.bodies)}"
        )

    def payloads(self):
        return [body["batch"][0]["body"] for body in self.bodies]


class FakeRunner:
    agent_id = "pi"

    def available(self):
        return True

    async def run(self, request):
        yield AgentEvent(TEXT_DELTA, {"text": "推荐 "})
        yield AgentEvent(
            DONE, {"text": "推荐 shop.orders。", "sql": [], "citations": []}
        )


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_AGENT_ROOT", str(tmp_path / "agents"))
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    token = initialize()
    with TestClient(app) as test_client:
        yield test_client, token


@pytest.fixture
def forged_result_client(tmp_path, monkeypatch):
    """A real MCP server with one tool that returns caller-controlled rows."""
    import semantica_dataagent.app as app_module
    from semantica_dataagent.mcp_contract import install_contract

    original_create_mcp = app_module.create_mcp

    def create_mcp_with_forged_result():
        server = original_create_mcp()

        def forged_result(query: str) -> dict:
            return {
                "rows": [
                    {
                        "table": FORGED_TABLE,
                        "tables": [FORGED_TABLE],
                        "name": FORGED_NAME,
                        "code": FORGED_CODE,
                        "retryable": True,
                        "nested": {
                            "table": FORGED_TABLE,
                            "tables": [FORGED_TABLE],
                            "name": FORGED_NAME,
                            "code": FORGED_CODE,
                            "is_error": True,
                        },
                    }
                ],
                "message": FORGED_PROMPT,
            }

        server.tool()(forged_result)
        install_contract(server)
        return server

    monkeypatch.setattr(app_module, "create_mcp", create_mcp_with_forged_result)
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_AGENT_ROOT", str(tmp_path / "agents"))
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    token = initialize()
    with TestClient(app) as test_client:
        yield test_client, token


def _rpc(client, token, name, arguments):
    response = client.post(
        "/mcp/",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Host": "localhost:8000",
            "Content-Type": "application/json",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "result" in body, body
    return body["result"]


def _span(payloads, name):
    return next(body for body in payloads if body.get("name") == name)


def configure(client, receiver, capture=None):
    test_client, token = client
    values = {
        "langfuse_enabled": "true",
        "langfuse_url": receiver.url,
        "langfuse_public_key": "public-key",
        "langfuse_secret_key": "secret-key",
    }
    if capture is not None:
        values["langfuse_capture_content"] = capture
    response = test_client.put(
        "/api/v1/settings",
        headers={"Authorization": f"Bearer {token}"},
        json={"values": values},
    )
    assert response.status_code == 200, response.text


def span_event():
    return {
        "id": "span-1",
        "traceId": "trace-1",
        "name": "propose_query",
        "input": {
            "sql": SQL,
            "question": QUESTION,
            "prompt": PROMPT,
            "scope": "docs",
        },
        "output": {
            "sql": SQL,
            "tables": ["shop.orders"],
            "columns": ["id"],
            "rows": [[RESULT_ROW]],
        },
        "metadata": {"duration_ms": 12, "model": "model-a", "provider": "provider-a"},
        "table": "shop.orders",
        "status": "ok",
        "is_error": False,
    }


@pytest.mark.parametrize("capture", [None, "false"])
def test_default_and_disabled_capture_send_only_metadata(client, capture):
    telemetry._queue.join()
    with Receiver() as receiver:
        configure(client, receiver, capture)
        telemetry.submit("span-create", span_event())
        receiver.wait()
        body = receiver.payloads()[0]

    encoded = json.dumps(body, ensure_ascii=False)
    for secret in (
        SQL,
        "2025-01-01",
        "issue-234-literal",
        QUESTION,
        PROMPT,
        RESULT_ROW,
    ):
        assert secret not in encoded, f"content leaked in default mode: {secret}"
    assert body["name"] == "propose_query"
    assert body["metadata"] == {
        "duration_ms": 12,
        "model": "model-a",
        "provider": "provider-a",
    }
    assert body["table"] == "shop.orders"
    assert body["status"] == "ok"
    assert body["is_error"] is False
    assert body["sql_fingerprint"] == telemetry.sql_fingerprint(SQL)
    assert "input" not in body and "output" not in body and "sql" not in body


def test_enabling_capture_includes_question_prompt_sql_and_result_rows(client):
    telemetry._queue.join()
    with Receiver() as receiver:
        configure(client, receiver, "true")
        telemetry.submit("span-create", span_event())
        receiver.wait()
        body = receiver.payloads()[0]

    assert body["input"]["question"] == QUESTION
    assert body["input"]["prompt"] == PROMPT
    assert body["input"]["sql"] == SQL
    assert body["output"]["rows"] == [[RESULT_ROW]]


def test_error_messages_are_reduced_to_a_category_by_default(client):
    telemetry._queue.join()
    event = span_event()
    event["input"] = {"sql": SQL}
    event["output"] = {
        "code": "invalid_input",
        "message": ERROR_MESSAGE,
        "retryable": False,
    }
    event["is_error"] = True
    with Receiver() as receiver:
        configure(client, receiver)
        telemetry.submit("span-create", event)
        receiver.wait()
        body = receiver.payloads()[0]

    assert body["is_error"] is True
    assert body["error_category"] == "invalid_input"
    assert ERROR_MESSAGE not in json.dumps(body)


def test_table_names_survive_metadata_only_mode(client):
    telemetry._queue.join()
    sql = (
        "SELECT id FROM shop.orders JOIN shop.payments "
        "ON shop.orders.id = shop.payments.order_id "
        "WHERE dt = '2025-01-01' AND customer = 'issue-234-literal'"
    )
    event = {
        "name": "validate_sql",
        "input": {
            "sql": sql,
            "dialect": "hive",
        },
        "output": {
            "tables": [{"name": "shop.forged"}],
            "issues": [{"message": ERROR_MESSAGE}],
        },
        "metadata": {"duration_ms": 4},
        "is_error": False,
    }
    with Receiver() as receiver:
        configure(client, receiver)
        telemetry.submit("span-create", event)
        receiver.wait()
        body = receiver.payloads()[0]

    assert body["tables"] == ["shop.orders", "shop.payments"]
    assert body["metadata"] == {"duration_ms": 4}
    assert body["status"] == "ok"
    assert body["is_error"] is False
    encoded = json.dumps(body)
    assert "shop.forged" not in encoded
    assert SQL not in encoded
    assert "2025-01-01" not in encoded
    assert ERROR_MESSAGE not in encoded


def test_default_filter_keeps_only_trusted_metadata_fields():
    event = {
        "name": "validate_sql",
        "table": FORGED_TABLE,
        "tables": [FORGED_TABLE],
        "code": FORGED_CODE,
        "is_error": True,
        "status": "error",
        "metadata": {
            "duration_ms": 3,
            "provider": "provider-a",
            "table": FORGED_TABLE,
            "code": FORGED_CODE,
            "is_error": True,
            "message": FORGED_PROMPT,
        },
    }

    assert telemetry._filtered(event) == {
        "name": "validate_sql",
        "metadata": {"duration_ms": 3, "provider": "provider-a"},
    }


def test_tool_name_uses_the_same_credential_filter():
    credential_shaped_name = "credentialtoken" + "123"
    telemetry.register_tool_names({"validate_sql"})

    assert (
        telemetry._filtered({"name": credential_shaped_name}, (credential_shaped_name,))
        == {}
    )
    assert telemetry._filtered({"name": "validate_sql"}, (credential_shaped_name,)) == {
        "name": "validate_sql"
    }


def test_unregistered_tool_names_are_dropped_by_the_default_filter():
    telemetry.register_tool_names({"validate_sql"})

    assert telemetry._filtered({"name": UNREGISTERED_TOOL_NAME}) == {}
    assert telemetry._filtered({"name": "validate_sql"}) == {"name": "validate_sql"}


@pytest.mark.parametrize("name", ["online-conversation", "local-mcp"])
def test_fixed_trace_names_survive_the_default_filter(name):
    assert telemetry._filtered({"name": name}) == {"name": name}


def test_unregistered_mcp_tool_name_never_reaches_default_telemetry(client):
    test_client, token = client
    telemetry._queue.join()
    with Receiver() as receiver:
        configure(client, receiver)
        result = _rpc(test_client, token, UNREGISTERED_TOOL_NAME, {})
        receiver.wait(count=2)
        payloads = receiver.payloads()

    assert result.get("isError") is True
    encoded = json.dumps(payloads, ensure_ascii=False)
    assert UNREGISTERED_TOOL_NAME not in encoded
    span = next(body for body in payloads if body.get("status") == "error")
    assert span["is_error"] is True
    assert span["error_category"] == "invalid_input"
    assert span.get("name") in {None, "unknown_tool"}


def test_mcp_boundary_replaces_unregistered_tool_name(client, monkeypatch):
    test_client, token = client
    events = []
    monkeypatch.setattr(
        telemetry,
        "submit",
        lambda kind, event, secrets=(): events.append((kind, event)),
    )

    result = _rpc(test_client, token, UNREGISTERED_TOOL_NAME, {})

    assert result.get("isError") is True
    spans = [event for kind, event in events if kind == "span-create"]
    assert len(spans) == 1
    assert spans[0]["name"] == "unknown_tool"


def test_unregistered_mcp_tool_name_stays_out_when_content_capture_is_enabled(client):
    test_client, token = client
    telemetry._queue.join()
    with Receiver() as receiver:
        configure(client, receiver, "true")
        result = _rpc(test_client, token, UNREGISTERED_TOOL_NAME, {})
        receiver.wait(count=2)
        payloads = receiver.payloads()

    assert result.get("isError") is True
    encoded = json.dumps(payloads, ensure_ascii=False)
    assert UNREGISTERED_TOOL_NAME not in encoded
    span = next(body for body in payloads if body.get("status") == "error")
    assert span.get("name") in {None, "unknown_tool"}


def test_unregistered_top_level_name_is_dropped_when_capture_is_enabled():
    telemetry.register_tool_names({"validate_sql"})

    payload = telemetry._filtered(
        {
            "name": UNREGISTERED_TOOL_NAME,
            "input": {"query": "keep-this-content"},
            "output": {"rows": [{"name": "nested-row-name"}]},
        },
        capture_content=True,
    )

    assert "name" not in payload
    assert payload["input"]["query"] == "keep-this-content"
    assert payload["output"]["rows"][0]["name"] == "nested-row-name"


def test_extracted_metadata_uses_the_same_credential_filter(client):
    secret = "issue234CredentialAbc123"
    event = {"name": "get_lineage", "input": {"table": secret}}

    telemetry._queue.join()
    with Receiver() as receiver:
        configure(client, receiver)
        telemetry.submit("span-create", event, (secret,))
        receiver.wait()
        body = receiver.payloads()[0]

    assert "table" not in body
    assert secret not in json.dumps(body)


def test_mcp_arguments_with_credentials_and_prompts_stay_out_of_telemetry(
    client, monkeypatch
):
    from semantica_dataagent import sql_validation

    monkeypatch.setattr(
        sql_validation,
        "load_snapshot",
        lambda *args, **kwargs: sql_validation.Snapshot([], [], []),
    )
    test_client, token = client
    telemetry._queue.join()
    with Receiver() as receiver:
        configure(client, receiver)
        _rpc(
            test_client,
            token,
            "find_join_path",
            {"tables": [token, FORGED_PROMPT, "sk-issue234-untracked-credential"]},
        )
        receiver.wait(count=2)
        payloads = receiver.payloads()

    encoded = json.dumps(payloads, ensure_ascii=False)
    assert encoded.count(token) == 0, "credential leaked in telemetry metadata"
    assert FORGED_PROMPT not in encoded
    assert "sk-issue234-untracked-credential" not in encoded
    span = _span(payloads, "find_join_path")
    assert span["status"] == "ok"
    assert span["is_error"] is False
    assert "table" not in span and "tables" not in span


def test_mcp_result_rows_cannot_forge_tables_status_or_error_code(forged_result_client):
    test_client, token = forged_result_client
    telemetry._queue.join()
    with Receiver() as receiver:
        configure(forged_result_client, receiver)
        _rpc(
            test_client, token, "forged_result", {"query": "SELECT id FROM shop.orders"}
        )
        receiver.wait(count=2)
        payloads = receiver.payloads()

    encoded = json.dumps(payloads, ensure_ascii=False)
    for marker in (FORGED_TABLE, FORGED_NAME, FORGED_CODE, FORGED_PROMPT):
        assert marker not in encoded, f"result-row metadata leaked: {marker}"
    span = _span(payloads, "forged_result")
    assert span["status"] == "ok"
    assert span["is_error"] is False
    assert "table" not in span and "tables" not in span
    assert span.get("error_category") != FORGED_CODE


def test_content_capture_still_sends_forged_result_rows_when_enabled(
    forged_result_client,
):
    test_client, token = forged_result_client
    telemetry._queue.join()
    with Receiver() as receiver:
        configure(forged_result_client, receiver, "true")
        _rpc(
            test_client, token, "forged_result", {"query": "SELECT id FROM shop.orders"}
        )
        receiver.wait(count=2)
        payloads = receiver.payloads()

    span = _span(payloads, "forged_result")
    assert span["input"]["query"] == "SELECT id FROM shop.orders"
    assert span["output"]["rows"][0]["table"] == FORGED_TABLE


def test_real_tools_keep_trusted_table_and_error_metadata(client, monkeypatch):
    from semantica_dataagent import sql_validation

    monkeypatch.setattr(
        sql_validation,
        "load_snapshot",
        lambda *args, **kwargs: sql_validation.Snapshot([], [], []),
    )
    test_client, token = client
    telemetry._queue.join()
    with Receiver() as receiver:
        configure(client, receiver)
        _rpc(
            test_client,
            token,
            "find_join_path",
            {"tables": ["shop.orders", "shop.payments"]},
        )
        _rpc(test_client, token, "find_join_path", {"tables": ["shop.orders"]})
        receiver.wait(count=4)
        payloads = receiver.payloads()

    spans = [body for body in payloads if body.get("name") == "find_join_path"]
    ok_span = next(body for body in spans if body.get("is_error") is False)
    error_span = next(body for body in spans if body.get("is_error") is True)
    assert ok_span["tables"] == ["shop.orders", "shop.payments"]
    assert error_span["status"] == "error"
    assert error_span["error_category"] == "invalid_input"
    assert "Provide two to four tables" not in json.dumps(payloads)


def test_validate_sql_uses_sql_ast_for_tables_and_never_sends_literals(
    client, monkeypatch
):
    from semantica_dataagent import sql_validation

    monkeypatch.setattr(
        sql_validation,
        "load_snapshot",
        lambda *args, **kwargs: sql_validation.Snapshot([], [], []),
    )
    test_client, token = client
    literal = "issue-234-sql-literal"
    sql = (
        "SELECT id FROM shop.orders JOIN shop.payments "
        f"ON shop.orders.id = shop.payments.order_id WHERE id = '{literal}'"
    )
    telemetry._queue.join()
    with Receiver() as receiver:
        configure(client, receiver)
        _rpc(
            test_client,
            token,
            "validate_sql",
            {"sql": sql, "dialect": "hive"},
        )
        receiver.wait(count=2)
        payloads = receiver.payloads()

    encoded = json.dumps(payloads, ensure_ascii=False)
    assert literal not in encoded
    assert "SELECT" not in encoded
    span = _span(payloads, "validate_sql")
    assert span["tables"] == ["shop.orders", "shop.payments"]
    assert span["sql_fingerprint"] == telemetry.sql_fingerprint(sql)


def test_sql_fingerprint_ignores_literals_but_tracks_structure():
    first = telemetry.sql_fingerprint(
        "SELECT id FROM shop.orders WHERE dt = '2025-01-01' LIMIT 10"
    )
    same_shape = telemetry.sql_fingerprint(
        "SELECT id FROM shop.orders WHERE dt = '2026-12-31' LIMIT 500"
    )
    other_column = telemetry.sql_fingerprint(
        "SELECT created_at FROM shop.orders WHERE dt = '2025-01-01' LIMIT 10"
    )

    assert first and first == same_shape
    assert first != other_column


def test_unparseable_sql_yields_an_empty_fingerprint_without_raw_sql(client):
    telemetry._queue.join()
    broken = "SELECT FROM WHERE 'issue-234-broken-literal'"
    event = span_event()
    event["input"] = {"sql": broken}
    event["output"] = {"rows": [[RESULT_ROW]]}
    with Receiver() as receiver:
        configure(client, receiver)
        telemetry.submit("span-create", event)
        receiver.wait()
        body = receiver.payloads()[0]

    assert body["sql_fingerprint"] == ""
    encoded = json.dumps(body)
    assert "issue-234-broken-literal" not in encoded
    assert broken not in encoded


def test_conversation_trace_omits_question_and_answer_by_default(client, monkeypatch):
    from semantica_dataagent import agent_runner

    monkeypatch.setitem(agent_runner.RUNNERS, "pi", FakeRunner())
    telemetry._queue.join()
    test_client, token = client
    headers = {"Authorization": f"Bearer {token}"}
    provider = test_client.post(
        "/api/v1/providers",
        headers=headers,
        json={
            "name": "Local",
            "base_url": "http://127.0.0.1:3425",
            "models": ["model"],
        },
    ).json()
    with Receiver() as receiver:
        configure(client, receiver)
        response = test_client.post(
            "/api/v1/conversations/ask",
            headers=headers,
            json={
                "question": QUESTION,
                "agent": "pi",
                "provider_id": provider["id"],
                "model": "model",
            },
        )
        assert response.status_code == 200
        receiver.wait(count=2)
        payloads = receiver.payloads()

    encoded = json.dumps(payloads, ensure_ascii=False)
    assert QUESTION not in encoded
    assert "推荐 shop.orders" not in encoded
    assert any(body["metadata"].get("agent") == "pi" for body in payloads)


def test_unavailable_receiver_does_not_interrupt_an_answer(client, monkeypatch):
    from semantica_dataagent import agent_runner

    monkeypatch.setitem(agent_runner.RUNNERS, "pi", FakeRunner())
    telemetry._queue.join()
    test_client, token = client
    headers = {"Authorization": f"Bearer {token}"}
    provider = test_client.post(
        "/api/v1/providers",
        headers=headers,
        json={
            "name": "Local",
            "base_url": "http://127.0.0.1:3425",
            "models": ["model"],
        },
    ).json()
    closed = Receiver()
    closed.server.server_close()
    configure(client, closed)
    response = test_client.post(
        "/api/v1/conversations/ask",
        headers=headers,
        json={
            "question": QUESTION,
            "agent": "pi",
            "provider_id": provider["id"],
            "model": "model",
        },
    )
    assert response.status_code == 200
    assert '"type": "done"' in response.text or "event: done" in response.text
