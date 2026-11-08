"""Provider capability probing through the real REST entrypoint.

The fake gateway speaks the concrete wire shape of each provider type and
records what the probe actually sent, so these tests catch shape drift as well
as status classification.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

EFFORT_LEVELS = ("low", "medium", "high", "extra", "max")
EFFORT_VALUES = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "extra": "xhigh",
    "max": "max",
}
REVERSE_EFFORT = {value: level for level, value in EFFORT_VALUES.items()}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SECRET = "probe-secret-a7c4e1"


def _success_body(provider_type: str, model: str) -> dict:
    if provider_type == "anthropic-messages":
        return {
            "id": "msg_probe",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": "OK"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 8, "output_tokens": 1},
        }
    if provider_type == "openai-completions":
        return {
            "id": "chatcmpl-probe",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "OK"},
                    "finish_reason": "stop",
                }
            ],
        }
    if provider_type == "openai-responses":
        return {
            "id": "resp_probe",
            "object": "response",
            "created_at": int(time.time()),
            "status": "completed",
            "model": model,
            "output": [
                {
                    "type": "message",
                    "id": "msg_probe",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "OK",
                            "annotations": [],
                        }
                    ],
                }
            ],
        }
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": "OK"}]},
                "finishReason": "STOP",
            }
        ],
        "modelVersion": model,
    }


def _error_body(provider_type: str, status: int, message: str) -> dict:
    if provider_type == "anthropic-messages":
        return {
            "type": "error",
            "error": {"type": "invalid_request_error", "message": message},
        }
    if provider_type in {"openai-completions", "openai-responses"}:
        return {
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "param": None,
                "code": "unsupported_parameter" if status == 400 else None,
            }
        }
    return {
        "error": {
            "code": status,
            "message": message,
            "status": "INVALID_ARGUMENT" if status == 400 else "INTERNAL",
        }
    }


def _content_blocks(body: dict) -> list[dict]:
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return []
    content = messages[0].get("content")
    return content if isinstance(content, list) else []


def _classify(provider_type: str, path: str, body: dict) -> tuple[str, str | None]:
    path = path.split("?", 1)[0]
    if provider_type == "anthropic-messages":
        content = _content_blocks(body)
        if any(block.get("type") == "image" for block in content):
            return "vision", None
        effort = (body.get("output_config") or {}).get("effort")
        return "effort", REVERSE_EFFORT.get(effort)
    if provider_type == "openai-completions":
        content = _content_blocks(body)
        if any(block.get("type") == "image_url" for block in content):
            return "vision", None
        return "effort", REVERSE_EFFORT.get(body.get("reasoning_effort"))
    if provider_type == "openai-responses":
        items = body.get("input")
        content = []
        if isinstance(items, list) and items:
            content = items[0].get("content") or []
        if any(block.get("type") == "input_image" for block in content):
            return "vision", None
        effort = (body.get("reasoning") or {}).get("effort")
        return "effort", REVERSE_EFFORT.get(effort)
    parts = ((body.get("contents") or [{}])[0]).get("parts") or []
    if any("inlineData" in part for part in parts):
        return "vision", None
    thinking = (body.get("generationConfig") or {}).get("thinkingConfig") or {}
    level = thinking.get("thinkingLevel")
    if level is not None:
        return "effort", {"LOW": "low", "MEDIUM": "medium", "HIGH": "high"}.get(level)
    budget = thinking.get("thinkingBudget")
    if budget is not None:
        return "effort", {
            1024: "low",
            2048: "medium",
            4096: "high",
            8192: "extra",
            16384: "max",
        }.get(budget)
    return "other", None


class FakeGateway:
    def __init__(
        self,
        provider_type: str,
        *,
        vision_status: int = 200,
        effort_status: dict[str, int] | None = None,
        delay: float = 0.0,
    ):
        self.provider_type = provider_type
        self.vision_status = vision_status
        self.effort_status = effort_status or {}
        self.delay = delay
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        self.received = threading.Event()

    def record(self, path: str, headers, body: dict) -> tuple[str, str | None]:
        kind, level = _classify(self.provider_type, path, body)
        with self.lock:
            self.requests.append(
                {
                    "path": path,
                    "headers": {key.lower(): value for key, value in headers.items()},
                    "body": body,
                    "kind": kind,
                    "level": level,
                }
            )
        self.received.set()
        return kind, level

    def status(self, kind: str, level: str | None) -> int:
        if kind == "vision":
            return self.vision_status
        if kind == "effort" and level is not None:
            return self.effort_status.get(level, 200)
        return 200


class _GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        gateway: FakeGateway = self.server.gateway
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {}
        kind, level = gateway.record(self.path, self.headers, body)
        if gateway.delay:
            time.sleep(gateway.delay)
        status = gateway.status(kind, level)
        if status == 200:
            payload = _success_body(gateway.provider_type, body.get("model") or "")
        else:
            payload = _error_body(
                gateway.provider_type,
                status,
                "image input is not supported"
                if kind == "vision"
                else f"unsupported reasoning parameter: {level}",
            )
        encoded = json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


@contextmanager
def _gateway(provider_type: str, **kwargs):
    gateway = FakeGateway(provider_type, **kwargs)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GatewayHandler)
    server.gateway = gateway
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield gateway, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    token = initialize()
    with TestClient(app) as test_client:
        yield test_client, token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_provider(
    client: TestClient,
    token: str,
    *,
    provider_type: str,
    base_url: str,
    models: list[str] | None = None,
    api_key: str = SECRET,
) -> str:
    response = client.post(
        "/api/v1/providers",
        headers=_headers(token),
        json={
            "name": f"probe {provider_type}",
            "type": provider_type,
            "base_url": base_url,
            "api_key": api_key,
            "models": models or ["model-a"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _probe(client: TestClient, token: str, provider_id: str, model: str = "model-a"):
    return client.post(
        f"/api/v1/providers/{provider_id}/probe",
        headers=_headers(token),
        json={"model": model},
    )


def _assert_effort_shape(provider_type: str, request: dict, level: str) -> None:
    body = request["body"]
    if provider_type == "anthropic-messages":
        assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert body["output_config"] == {"effort": EFFORT_VALUES[level]}
        assert body["max_tokens"] <= 64
    elif provider_type == "openai-completions":
        assert body["reasoning_effort"] == EFFORT_VALUES[level]
        assert body["max_completion_tokens"] <= 64
    elif provider_type == "openai-responses":
        assert body["reasoning"]["effort"] == EFFORT_VALUES[level]
        assert body["max_output_tokens"] <= 64
        assert body["store"] is False
    else:
        thinking = body["generationConfig"]["thinkingConfig"]
        assert thinking["includeThoughts"] is True
        assert (
            thinking.get("thinkingLevel") or thinking.get("thinkingBudget") is not None
        )
        assert body["generationConfig"]["maxOutputTokens"] <= 64


@pytest.mark.parametrize(
    ("provider_type", "expected_path"),
    [
        ("anthropic-messages", "/v1/messages"),
        ("openai-completions", "/chat/completions"),
        ("openai-responses", "/responses"),
        ("google-generative-ai", "/models/model-a:generateContent"),
    ],
)
def test_probe_sends_each_provider_wire_shape_and_accepts_all(
    client, provider_type, expected_path
):
    c, token = client
    with _gateway(provider_type) as (gateway, base_url):
        provider_id = _create_provider(
            c, token, provider_type=provider_type, base_url=base_url
        )
        response = _probe(c, token, provider_id)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "model": "model-a",
        "vision": "yes",
        "effort": {level: "yes" for level in EFFORT_LEVELS},
        "notes": response.json()["notes"],
    }
    assert len(gateway.requests) == 6
    assert {request["path"].split("?", 1)[0] for request in gateway.requests} == {
        expected_path
    }

    vision, *efforts = gateway.requests
    content = (
        vision["body"].get("messages", [{}])[0].get("content")
        or vision["body"].get("input", [{}])[0].get("content")
        or vision["body"].get("contents", [{}])[0].get("parts")
    )
    if provider_type == "anthropic-messages":
        image = next(block for block in content if block["type"] == "image")
        assert image["source"]["type"] == "base64"
        assert image["source"]["media_type"] == "image/png"
        assert base64.b64decode(image["source"]["data"]).startswith(PNG_SIGNATURE)
    elif provider_type == "openai-completions":
        image = next(block for block in content if block["type"] == "image_url")
        url = image["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        assert base64.b64decode(url.split(",", 1)[1]).startswith(PNG_SIGNATURE)
    elif provider_type == "openai-responses":
        image = next(block for block in content if block["type"] == "input_image")
        url = image["image_url"]
        assert url.startswith("data:image/png;base64,")
        assert base64.b64decode(url.split(",", 1)[1]).startswith(PNG_SIGNATURE)
    else:
        image = next(part for part in content if "inlineData" in part)
        assert image["inlineData"]["mimeType"] == "image/png"
        assert base64.b64decode(image["inlineData"]["data"]).startswith(PNG_SIGNATURE)

    for request in efforts:
        _assert_effort_shape(provider_type, request, request["level"])

    headers = vision["headers"]
    if provider_type == "anthropic-messages":
        assert headers["x-api-key"] == SECRET
        assert headers["anthropic-version"]
    elif provider_type == "google-generative-ai":
        assert headers["x-goog-api-key"] == SECRET
    else:
        assert headers["authorization"] == f"Bearer {SECRET}"

    assert SECRET not in response.text
    assert c.get("/api/v1/conversations", headers=_headers(token)).json() == []


def test_probe_reports_rejected_image_as_no(client):
    c, token = client
    with _gateway("anthropic-messages", vision_status=400) as (_, base_url):
        provider_id = _create_provider(
            c, token, provider_type="anthropic-messages", base_url=base_url
        )
        response = _probe(c, token, provider_id)

    assert response.status_code == 200, response.text
    assert response.json()["vision"] == "no"
    assert response.json()["effort"] == {level: "yes" for level in EFFORT_LEVELS}
    assert "vision" in response.json()["notes"]


def test_probe_reports_rejected_effort_parameters_as_no(client):
    c, token = client
    with _gateway("anthropic-messages", effort_status={"extra": 400, "max": 400}) as (
        gateway,
        base_url,
    ):
        provider_id = _create_provider(
            c, token, provider_type="anthropic-messages", base_url=base_url
        )
        response = _probe(c, token, provider_id)

    assert response.status_code == 200, response.text
    assert response.json()["vision"] == "yes"
    assert response.json()["effort"] == {
        "low": "yes",
        "medium": "yes",
        "high": "yes",
        "extra": "no",
        "max": "no",
    }
    values = {
        request["level"]: request["body"]["output_config"]["effort"]
        for request in gateway.requests
        if request["kind"] == "effort"
    }
    assert values == EFFORT_VALUES


def test_probe_reports_5xx_as_unknown(client):
    c, token = client
    with _gateway(
        "anthropic-messages",
        vision_status=503,
        effort_status={level: 503 for level in EFFORT_LEVELS},
    ) as (_, base_url):
        provider_id = _create_provider(
            c, token, provider_type="anthropic-messages", base_url=base_url
        )
        response = _probe(c, token, provider_id)

    assert response.status_code == 200, response.text
    assert response.json()["vision"] == "unknown"
    assert response.json()["effort"] == {level: "unknown" for level in EFFORT_LEVELS}
    assert "503" in response.json()["notes"]


def test_probe_reports_timeout_as_unknown(client, monkeypatch: pytest.MonkeyPatch):
    c, token = client
    monkeypatch.setenv("DATAAGENT_PROVIDER_PROBE_TIMEOUT_SECONDS", "1.2")
    monkeypatch.setenv("DATAAGENT_PROVIDER_PROBE_REQUEST_TIMEOUT_SECONDS", "0.2")
    started = time.monotonic()
    with _gateway("anthropic-messages", delay=1.0) as (_, base_url):
        provider_id = _create_provider(
            c, token, provider_type="anthropic-messages", base_url=base_url
        )
        response = _probe(c, token, provider_id)
    elapsed = time.monotonic() - started

    assert response.status_code == 200, response.text
    assert response.json()["vision"] == "unknown"
    assert response.json()["effort"] == {level: "unknown" for level in EFFORT_LEVELS}
    assert "timeout" in response.json()["notes"]
    assert elapsed < 5


def test_concurrent_probe_for_same_provider_returns_409(client):
    c, token = client
    with _gateway("anthropic-messages", delay=0.1) as (gateway, base_url):
        provider_id = _create_provider(
            c, token, provider_type="anthropic-messages", base_url=base_url
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(_probe, c, token, provider_id)
            assert gateway.received.wait(2), "first probe never reached the gateway"
            second = _probe(c, token, provider_id)
            assert second.status_code == 409, second.text
            assert first.result(timeout=10).status_code == 200

        after = _probe(c, token, provider_id)
        assert after.status_code == 200, after.text


def test_probe_requires_admin(client):
    c, token = client
    provider_id = _create_provider(
        c,
        token,
        provider_type="anthropic-messages",
        base_url="http://127.0.0.1:1",
    )
    issued = c.post(
        "/api/v1/tokens",
        headers=_headers(token),
        json={"name": "user", "role": "user"},
    ).json()

    response = _probe(c, issued["token"], provider_id)

    assert response.status_code == 403


def test_probe_rejects_model_outside_provider(client):
    c, token = client
    with _gateway("anthropic-messages") as (gateway, base_url):
        provider_id = _create_provider(
            c, token, provider_type="anthropic-messages", base_url=base_url
        )
        response = _probe(c, token, provider_id, model="model-b")

    assert response.status_code == 422
    assert gateway.requests == []


def test_probe_missing_provider_returns_404(client):
    c, token = client
    response = _probe(c, token, "missing-provider")
    assert response.status_code == 404


def test_probe_does_not_leak_api_key_in_response_or_logs(client, caplog):
    c, token = client
    caplog.set_level(logging.DEBUG)
    with _gateway("openai-completions") as (_, base_url):
        provider_id = _create_provider(
            c, token, provider_type="openai-completions", base_url=base_url
        )
        response = _probe(c, token, provider_id)
        listing = c.get("/api/v1/providers", headers=_headers(token))

    assert response.status_code == 200
    assert listing.status_code == 200
    assert SECRET not in response.text
    assert SECRET not in listing.text
    assert SECRET not in caplog.text
