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
        raise AssertionError(f"expected {count} ingestion requests, got {len(self.bodies)}")

    def payloads(self):
        return [body["batch"][0]["body"] for body in self.bodies]


class FakeRunner:
    agent_id = "pi"

    def available(self):
        return True

    async def run(self, request):
        yield AgentEvent(TEXT_DELTA, {"text": "推荐 "})
        yield AgentEvent(DONE, {"text": "推荐 shop.orders。", "sql": [], "citations": []})


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
    for secret in (SQL, "2025-01-01", "issue-234-literal", QUESTION, PROMPT, RESULT_ROW):
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
    assert body["sql"] == SQL


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
        json={"name": "Local", "base_url": "http://127.0.0.1:3425", "models": ["model"]},
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
        json={"name": "Local", "base_url": "http://127.0.0.1:3425", "models": ["model"]},
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
