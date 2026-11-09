"""Provider capability probing through the real REST entrypoint.

The fake gateway speaks the concrete wire shape of each provider type and
records what the probe actually sent, so these tests catch shape drift as well
as status classification.
"""

from __future__ import annotations

import base64
import json
import logging
import struct
import threading
import time
import zlib
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
MIN_IMAGE_SIZE = 64


def _success_body(
    provider_type: str,
    model: str,
    text: str = "OK",
    *,
    thinking: bool = False,
    truncated: bool = False,
    include_text: bool = False,
) -> dict:
    if provider_type == "anthropic-messages":
        content: list[dict] = []
        if thinking or truncated:
            content.append(
                {
                    "type": "thinking",
                    "thinking": "图片整体是红色",
                    "signature": "sig",
                }
            )
        if not truncated or include_text:
            content.append({"type": "text", "text": text})
        return {
            "id": "msg_probe",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": "max_tokens" if truncated else "end_turn",
            "usage": {"input_tokens": 8, "output_tokens": 1},
        }
    if provider_type == "openai-completions":
        message: dict = {
            "role": "assistant",
            "content": text if not truncated or include_text else "",
        }
        if thinking or truncated:
            message["reasoning_content"] = "图片整体是红色"
        return {
            "id": "chatcmpl-probe",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "length" if truncated else "stop",
                }
            ],
        }
    if provider_type == "openai-responses":
        output: list[dict] = []
        if thinking or truncated:
            output.append(
                {
                    "type": "reasoning",
                    "id": "rs_probe",
                    "summary": [],
                    "content": [{"type": "reasoning_text", "text": "图片整体是红色"}],
                }
            )
        if not truncated or include_text:
            output.append(
                {
                    "type": "message",
                    "id": "msg_probe",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": text,
                            "annotations": [],
                        }
                    ],
                }
            )
        body = {
            "id": "resp_probe",
            "object": "response",
            "created_at": int(time.time()),
            "status": "incomplete" if truncated else "completed",
            "model": model,
            "output": output,
        }
        if truncated:
            body["incomplete_details"] = {"reason": "max_output_tokens"}
        return body
    parts: list[dict] = []
    if thinking or truncated:
        parts.append({"text": "图片整体是红色", "thought": True})
    if not truncated or include_text:
        parts.append({"text": text})
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": parts},
                "finishReason": "MAX_TOKENS" if truncated else "STOP",
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


def _image_bytes(provider_type: str, body: dict) -> bytes:
    if provider_type == "anthropic-messages":
        content = _content_blocks(body)
        image = next(block for block in content if block.get("type") == "image")
        return base64.b64decode(image["source"]["data"])
    if provider_type == "openai-completions":
        content = _content_blocks(body)
        image = next(block for block in content if block.get("type") == "image_url")
        return base64.b64decode(image["image_url"]["url"].split(",", 1)[1])
    if provider_type == "openai-responses":
        content = body["input"][0]["content"]
        image = next(block for block in content if block.get("type") == "input_image")
        return base64.b64decode(image["image_url"].split(",", 1)[1])
    parts = body["contents"][0]["parts"]
    image = next(part for part in parts if "inlineData" in part)
    return base64.b64decode(image["inlineData"]["data"])


def _png_info(data: bytes) -> tuple[int, int, tuple[int, int, int]]:
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("not a PNG")
    width, height = struct.unpack(">II", data[16:24])
    bit_depth = data[24]
    color_type = data[25]
    channels = {2: 3, 6: 4}.get(color_type)
    if bit_depth != 8 or channels is None:
        raise ValueError("unsupported PNG pixel format")
    position = 8
    compressed = b""
    while position < len(data):
        length = struct.unpack(">I", data[position : position + 4])[0]
        kind = data[position + 4 : position + 8]
        chunk = data[position + 8 : position + 8 + length]
        if kind == b"IDAT":
            compressed += chunk
        position += 12 + length
    raw = zlib.decompress(compressed)
    if raw[0] != 0:
        raise ValueError("unexpected PNG filter")
    red, green, blue = raw[1 : 1 + channels][:3]
    return width, height, (red, green, blue)


def _color_answer(pixel: tuple[int, int, int]) -> str:
    red, green, blue = pixel
    if red > 200 and green < 50 and blue < 50:
        return "深红色。"
    if blue > 200 and red < 50 and green < 50:
        return "蓝色"
    return ""


def _prompt_text(provider_type: str, body: dict) -> str:
    if provider_type in {"anthropic-messages", "openai-completions"}:
        content = _content_blocks(body)
        return "".join(
            block["text"]
            for block in content
            if block.get("type") == "text" and isinstance(block.get("text"), str)
        )
    if provider_type == "openai-responses":
        content = body["input"][0]["content"]
        return "".join(
            block["text"]
            for block in content
            if block.get("type") == "input_text" and isinstance(block.get("text"), str)
        )
    parts = body["contents"][0]["parts"]
    return "".join(
        part["text"]
        for part in parts
        if isinstance(part.get("text"), str) and part.get("thought") is not True
    )


def _output_limit(provider_type: str, body: dict) -> int:
    if provider_type == "anthropic-messages":
        return body["max_tokens"]
    if provider_type == "openai-completions":
        return body["max_completion_tokens"]
    if provider_type == "openai-responses":
        return body["max_output_tokens"]
    return body["generationConfig"]["maxOutputTokens"]


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
        if not isinstance(content, list):
            content = []
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
        discard_image: bool = False,
        vision_text: str = "OK",
        min_vision_size: bool = False,
        fail_color: str | None = None,
        effort_status: dict[str, int] | None = None,
        delay: float = 0.0,
        drip: float | None = None,
        success_body: dict | None = None,
        vision_thinking: bool = False,
        vision_truncated: bool = False,
        vision_truncated_text: bool = False,
    ):
        self.provider_type = provider_type
        self.vision_status = vision_status
        self.discard_image = discard_image
        self.vision_text = vision_text
        self.min_vision_size = min_vision_size
        self.fail_color = fail_color
        self.effort_status = effort_status or {}
        self.delay = delay
        self.drip = drip
        self.success_body = success_body
        self.vision_thinking = vision_thinking
        self.vision_truncated = vision_truncated
        self.vision_truncated_text = vision_truncated_text
        self.requests: list[dict] = []
        self.vision_sizes: list[tuple[int, int]] = []
        self.vision_statuses: list[int] = []
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

    def vision_reply(self, body: dict) -> tuple[int, str]:
        if self.vision_status != 200:
            self.vision_statuses.append(self.vision_status)
            return self.vision_status, "image input is not supported"
        try:
            width, height, pixel = _png_info(_image_bytes(self.provider_type, body))
        except (KeyError, StopIteration, ValueError, TypeError):
            self.vision_statuses.append(400)
            return 400, "a valid PNG image is required"
        with self.lock:
            self.vision_sizes.append((width, height))
        if self.min_vision_size and (width < MIN_IMAGE_SIZE or height < MIN_IMAGE_SIZE):
            self.vision_statuses.append(400)
            return 400, "image dimensions must be at least 64x64"
        self.vision_statuses.append(200)
        if self.discard_image:
            return 200, self.vision_text
        red, green, blue = pixel
        if self.fail_color == "red" and red > 200 and green < 50 and blue < 50:
            return 200, self.vision_text
        if self.fail_color == "blue" and blue > 200 and red < 50 and green < 50:
            return 200, self.vision_text
        return 200, _color_answer(pixel) or self.vision_text


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
        text = "OK"
        if kind == "vision":
            status, text = gateway.vision_reply(body)
        else:
            status = gateway.status(kind, level)
        if status == 200:
            if gateway.success_body is not None:
                payload = gateway.success_body
            elif kind == "vision":
                payload = _success_body(
                    gateway.provider_type,
                    body.get("model") or "",
                    text,
                    thinking=gateway.vision_thinking,
                    truncated=gateway.vision_truncated,
                    include_text=gateway.vision_truncated_text,
                )
            else:
                payload = _success_body(
                    gateway.provider_type, body.get("model") or "", text
                )
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
            if gateway.drip:
                for byte in encoded:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(gateway.drip)
            else:
                self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


@contextmanager
def _gateway(provider_type: str, **kwargs):
    gateway = FakeGateway(provider_type, **kwargs)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GatewayHandler)
    server.daemon_threads = True
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
    assert len(gateway.requests) == 7, [
        (request["kind"], request["path"]) for request in gateway.requests
    ]
    assert {request["path"].split("?", 1)[0] for request in gateway.requests} == {
        expected_path
    }

    visions = [request for request in gateway.requests if request["kind"] == "vision"]
    efforts = [request for request in gateway.requests if request["kind"] == "effort"]
    assert len(visions) == 2
    image_info = [
        _png_info(_image_bytes(provider_type, item["body"])) for item in visions
    ]
    assert all(
        width >= MIN_IMAGE_SIZE and height >= MIN_IMAGE_SIZE
        for width, height, _ in image_info
    ), image_info
    assert sorted(gateway.vision_sizes) == [(MIN_IMAGE_SIZE, MIN_IMAGE_SIZE)] * 2
    assert sorted(gateway.vision_statuses) == [200, 200]
    assert {pixel for _, _, pixel in image_info} == {(255, 0, 0), (0, 0, 255)}
    assert [_prompt_text(provider_type, item["body"]) for item in visions] == [
        "这张图片是什么颜色？只回答一个词"
    ] * 2

    vision = visions[0]
    if provider_type == "anthropic-messages":
        image = next(
            block
            for block in _content_blocks(vision["body"])
            if block["type"] == "image"
        )
        assert image["source"]["type"] == "base64"
        assert image["source"]["media_type"] == "image/png"
    elif provider_type == "openai-completions":
        image = next(
            block
            for block in _content_blocks(vision["body"])
            if block["type"] == "image_url"
        )
        assert image["image_url"]["url"].startswith("data:image/png;base64,")
    elif provider_type == "openai-responses":
        image = next(
            block
            for block in vision["body"]["input"][0]["content"]
            if block["type"] == "input_image"
        )
        assert image["image_url"].startswith("data:image/png;base64,")
    else:
        image = next(
            part
            for part in vision["body"]["contents"][0]["parts"]
            if "inlineData" in part
        )
        assert image["inlineData"]["mimeType"] == "image/png"

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
    notes = response.json()["notes"]
    assert "vision" in notes
    assert "HTTP 400" in notes
    assert "image input is not supported" in notes
    assert SECRET not in response.text
    assert "x-api-key" not in response.text


@pytest.mark.parametrize("vision_text", ["OK", ""])
def test_probe_reports_accepted_but_unrecognized_image_as_no(client, vision_text):
    c, token = client
    with _gateway(
        "anthropic-messages", discard_image=True, vision_text=vision_text
    ) as (gateway, base_url):
        provider_id = _create_provider(
            c, token, provider_type="anthropic-messages", base_url=base_url
        )
        response = _probe(c, token, provider_id)

    assert response.status_code == 200, response.text
    assert response.json()["vision"] == "no"
    assert response.json()["effort"] == {level: "yes" for level in EFFORT_LEVELS}
    assert "请求被接受但模型未能识别图片内容" in response.json()["notes"]
    assert len(gateway.vision_sizes) == 2


def test_probe_requires_both_colors_to_be_recognized(client):
    c, token = client
    with _gateway("anthropic-messages", fail_color="red") as (_, base_url):
        provider_id = _create_provider(
            c, token, provider_type="anthropic-messages", base_url=base_url
        )
        response = _probe(c, token, provider_id)

    assert response.status_code == 200, response.text
    assert response.json()["vision"] == "no"
    assert "请求被接受但模型未能识别图片内容" in response.json()["notes"]


def test_probe_reports_truncated_vision_without_text_as_unknown(client):
    c, token = client
    with _gateway("anthropic-messages", vision_truncated=True) as (
        gateway,
        base_url,
    ):
        provider_id = _create_provider(
            c, token, provider_type="anthropic-messages", base_url=base_url
        )
        response = _probe(c, token, provider_id)

    assert response.status_code == 200, response.text
    assert response.json()["vision"] == "unknown"
    assert "截断" in response.json()["notes"]
    assert response.json()["effort"] == {level: "yes" for level in EFFORT_LEVELS}
    assert len(gateway.vision_sizes) == 2


@pytest.mark.parametrize(
    "provider_type",
    [
        "anthropic-messages",
        "openai-completions",
        "openai-responses",
        "google-generative-ai",
    ],
)
def test_probe_accepts_vision_answer_after_thinking_block(client, provider_type):
    c, token = client
    with _gateway(provider_type, vision_thinking=True) as (_, base_url):
        provider_id = _create_provider(
            c, token, provider_type=provider_type, base_url=base_url
        )
        response = _probe(c, token, provider_id)

    assert response.status_code == 200, response.text
    assert response.json()["vision"] == "yes"


def test_probe_reports_wrong_color_after_thinking_as_no(client):
    c, token = client
    with _gateway(
        "anthropic-messages",
        vision_thinking=True,
        discard_image=True,
        vision_text="绿色",
    ) as (_, base_url):
        provider_id = _create_provider(
            c, token, provider_type="anthropic-messages", base_url=base_url
        )
        response = _probe(c, token, provider_id)

    assert response.status_code == 200, response.text
    assert response.json()["vision"] == "no"
    assert "请求被接受但模型未能识别图片内容" in response.json()["notes"]


@pytest.mark.parametrize(
    "provider_type",
    [
        "anthropic-messages",
        "openai-completions",
        "openai-responses",
        "google-generative-ai",
    ],
)
def test_probe_uses_larger_output_budget_for_vision(client, provider_type):
    c, token = client
    with _gateway(provider_type) as (gateway, base_url):
        provider_id = _create_provider(
            c, token, provider_type=provider_type, base_url=base_url
        )
        response = _probe(c, token, provider_id)

    assert response.status_code == 200, response.text
    vision_limits = [
        _output_limit(provider_type, request["body"])
        for request in gateway.requests
        if request["kind"] == "vision"
    ]
    effort_limits = [
        _output_limit(provider_type, request["body"])
        for request in gateway.requests
        if request["kind"] == "effort"
    ]
    assert len(vision_limits) == 2
    assert all(limit >= 512 for limit in vision_limits), vision_limits
    assert len(effort_limits) == len(EFFORT_LEVELS)
    assert all(limit <= 64 for limit in effort_limits), effort_limits


@pytest.mark.parametrize(
    "provider_type",
    ["openai-completions", "openai-responses", "google-generative-ai"],
)
def test_probe_reports_truncated_vision_as_unknown(client, provider_type):
    c, token = client
    with _gateway(provider_type, vision_truncated=True) as (_, base_url):
        provider_id = _create_provider(
            c, token, provider_type=provider_type, base_url=base_url
        )
        response = _probe(c, token, provider_id)

    assert response.status_code == 200, response.text
    assert response.json()["vision"] == "unknown"
    assert "截断" in response.json()["notes"]
    assert response.json()["effort"] == {level: "yes" for level in EFFORT_LEVELS}


@pytest.mark.parametrize(
    "provider_type",
    [
        "anthropic-messages",
        "openai-completions",
        "openai-responses",
        "google-generative-ai",
    ],
)
def test_probe_accepts_correct_answer_from_truncated_vision(client, provider_type):
    c, token = client
    with _gateway(
        provider_type,
        vision_truncated=True,
        vision_truncated_text=True,
    ) as (_, base_url):
        provider_id = _create_provider(
            c, token, provider_type=provider_type, base_url=base_url
        )
        response = _probe(c, token, provider_id)

    assert response.status_code == 200, response.text
    assert response.json()["vision"] == "yes"


def test_probe_reports_wrong_answer_from_truncated_vision_as_no(client):
    c, token = client
    with _gateway(
        "anthropic-messages",
        vision_truncated=True,
        vision_truncated_text=True,
        discard_image=True,
        vision_text="绿色",
    ) as (_, base_url):
        provider_id = _create_provider(
            c, token, provider_type="anthropic-messages", base_url=base_url
        )
        response = _probe(c, token, provider_id)

    assert response.status_code == 200, response.text
    assert response.json()["vision"] == "no"
    assert "请求被接受但模型未能识别图片内容" in response.json()["notes"]


def test_probe_accepts_images_at_least_64_pixels_like_qwen(client):
    c, token = client
    with _gateway("anthropic-messages", min_vision_size=True) as (gateway, base_url):
        provider_id = _create_provider(
            c, token, provider_type="anthropic-messages", base_url=base_url
        )
        response = _probe(c, token, provider_id)

    assert response.status_code == 200, response.text
    assert response.json()["vision"] == "yes"
    assert sorted(gateway.vision_sizes) == [(MIN_IMAGE_SIZE, MIN_IMAGE_SIZE)] * 2
    assert sorted(gateway.vision_statuses) == [200, 200]


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


def test_probe_enforces_total_deadline_on_slow_responses(
    client, monkeypatch: pytest.MonkeyPatch
):
    c, token = client
    monkeypatch.setenv("DATAAGENT_PROVIDER_PROBE_TIMEOUT_SECONDS", "0.8")
    monkeypatch.setenv("DATAAGENT_PROVIDER_PROBE_REQUEST_TIMEOUT_SECONDS", "30")
    started = time.monotonic()
    with _gateway("anthropic-messages", drip=0.02) as (gateway, base_url):
        provider_id = _create_provider(
            c, token, provider_type="anthropic-messages", base_url=base_url
        )
        response = _probe(c, token, provider_id)
    elapsed = time.monotonic() - started

    assert response.status_code == 200, response.text
    assert response.json()["vision"] == "unknown"
    assert response.json()["effort"] == {level: "unknown" for level in EFFORT_LEVELS}
    assert "timeout" in response.json()["notes"]
    assert elapsed < 2.5
    assert len(gateway.requests) < 6


def test_probe_reports_responses_completion_without_output_as_unknown(client):
    c, token = client
    with _gateway("openai-responses", success_body={"status": "completed"}) as (
        _,
        base_url,
    ):
        provider_id = _create_provider(
            c, token, provider_type="openai-responses", base_url=base_url
        )
        response = _probe(c, token, provider_id)

    assert response.status_code == 200, response.text
    assert response.json()["vision"] == "unknown"
    assert response.json()["effort"] == {level: "unknown" for level in EFFORT_LEVELS}
    assert "unexpected response body" in response.json()["notes"]


def test_probe_maps_google_gemini_3_thinking_levels(client):
    c, token = client
    model = "gemini-3-pro-preview"
    with _gateway("google-generative-ai") as (gateway, base_url):
        provider_id = _create_provider(
            c,
            token,
            provider_type="google-generative-ai",
            base_url=base_url,
            models=[model],
        )
        response = _probe(c, token, provider_id, model=model)

    assert response.status_code == 200, response.text
    assert response.json()["vision"] == "yes"
    assert response.json()["effort"] == {level: "yes" for level in EFFORT_LEVELS}
    levels = [
        request["body"]["generationConfig"]["thinkingConfig"]["thinkingLevel"]
        for request in gateway.requests
        if request["kind"] == "effort"
    ]
    assert levels == ["LOW", "MEDIUM", "HIGH", "HIGH", "HIGH"]
    assert "google extra/max" in response.json()["notes"]


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
