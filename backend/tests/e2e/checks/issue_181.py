"""Probe provider models through the real REST API and a fake gateway.

The fake gateway validates the wire shape for each provider type, so a probe
that sends the wrong Anthropic / OpenAI / Google request fails instead of
silently reporting a capability. It also reproduces accepted, rejected,
server-error, and timeout outcomes. #181 must not persist probe results; the
check saves them explicitly with the existing provider update endpoint.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

SECRET = "sk-issue-181-probe-secret"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
EFFORT_VALUES = {"low", "medium", "high", "xhigh", "max"}


class _FakeGateway(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    gateway: _GatewayState

    def log_message(self, *_args):
        return None

    def do_POST(self):
        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            return self._send(400, _error("invalid JSON"))
        self.gateway.requests.append(
            {"path": self.path, "headers": dict(self.headers), "body": body}
        )
        try:
            status, payload = self._handle(body)
        except _Rejected as exc:
            self.gateway.rejections.append((self.path, exc.status, exc.message))
            status, payload = exc.status, _error(exc.message)
        except Exception as exc:  # noqa: BLE001 - fake gateway diagnostics
            self.gateway.rejections.append((self.path, 500, str(exc)))
            status, payload = 500, _error(f"fake gateway error: {exc}")
        return self._send(status, payload)

    def _handle(self, body):
        if not isinstance(body, dict):
            raise _Rejected(400, "request body must be an object")
        path = self.path.split("?", 1)[0]
        model = body.get("model")
        if not isinstance(model, str) or not model:
            prefix, suffix = "/v1/models/", ":generateContent"
            if path.startswith(prefix) and path.endswith(suffix):
                model = path[len(prefix) : -len(suffix)]
        if not isinstance(model, str) or not model:
            raise _Rejected(400, "model is required")
        if path == "/v1/messages":
            kind = _anthropic_kind(body)
        elif path == "/v1/chat/completions":
            kind = _completions_kind(body)
        elif path == "/v1/responses":
            kind = _responses_kind(body)
        elif path.startswith("/v1/models/") and path.endswith(":generateContent"):
            kind = _google_kind(body)
        else:
            raise _Rejected(404, f"unexpected path {path}")

        if model == "no-vision-model" and kind == "vision":
            raise _Rejected(400, "image input is not supported")
        if model in {"partial-effort-model", "slow-model"} and kind != "vision":
            level = _effort_level(body)
            if model == "partial-effort-model" and level == "high":
                raise _Rejected(400, "unsupported reasoning effort high")
            if model == "partial-effort-model" and level == "extra":
                return 500, _error("upstream failed")
            if model == "partial-effort-model" and level == "max":
                time.sleep(15.5)
        if model == "slow-model" and kind == "vision":
            self.gateway.slow_started.set()
            time.sleep(3.0)
        return 200, _success(path)

    def _send(self, status, payload):
        data = json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


class _GatewayState:
    def __init__(self):
        self.requests = []
        self.rejections = []
        self.slow_started = threading.Event()


class _Rejected(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _error(message):
    return {"error": {"type": "invalid_request_error", "message": message}}


def _success(path):
    if path == "/v1/messages":
        return {
            "id": "msg_probe",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "OK"}],
            "stop_reason": "end_turn",
        }
    if path == "/v1/chat/completions":
        return {
            "id": "chatcmpl-probe",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "OK"},
                    "finish_reason": "stop",
                }
            ],
        }
    if path == "/v1/responses":
        return {
            "id": "resp_probe",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "OK"}],
                }
            ],
        }
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": "OK"}]}}]}


def _check_png(data):
    try:
        raw = base64.b64decode(data, validate=True)
    except (TypeError, ValueError) as exc:
        raise _Rejected(400, f"invalid base64 image: {exc}") from None
    if not raw.startswith(PNG_SIGNATURE):
        raise _Rejected(400, "image is not a real PNG")


def _anthropic_kind(body):
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise _Rejected(400, "messages is required")
    if not isinstance(body.get("max_tokens"), int):
        raise _Rejected(400, "max_tokens is required")
    content = messages[0].get("content")
    if isinstance(content, list):
        image = next((item for item in content if item.get("type") == "image"), None)
        if image is None:
            raise _Rejected(400, "image block is required")
        source = image.get("source") or {}
        if source.get("type") != "base64" or source.get("media_type") != "image/png":
            raise _Rejected(400, "base64 PNG source is required")
        _check_png(source.get("data"))
        if not any(item.get("type") == "text" for item in content):
            raise _Rejected(400, "text prompt is required")
        return "vision"
    if not isinstance(content, str) or not content:
        raise _Rejected(400, "text content is required")
    thinking = body.get("thinking")
    effort = (body.get("output_config") or {}).get("effort")
    if not isinstance(thinking, dict) or thinking.get("type") != "adaptive":
        raise _Rejected(400, "adaptive thinking is required")
    if effort not in EFFORT_VALUES:
        raise _Rejected(400, "output_config.effort is required")
    return "effort"


def _completions_kind(body):
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise _Rejected(400, "messages is required")
    if not isinstance(body.get("max_completion_tokens"), int):
        raise _Rejected(400, "max_completion_tokens is required")
    content = messages[0].get("content")
    if isinstance(content, list):
        image = next(
            (item for item in content if item.get("type") == "image_url"), None
        )
        if image is None or not isinstance(image.get("image_url"), dict):
            raise _Rejected(400, "image_url block is required")
        url = image["image_url"].get("url", "")
        if not str(url).startswith("data:image/png;base64,"):
            raise _Rejected(400, "inline PNG data URL is required")
        _check_png(str(url).split(",", 1)[1])
        return "vision"
    if not isinstance(content, str) or not content:
        raise _Rejected(400, "text content is required")
    if body.get("reasoning_effort") not in EFFORT_VALUES:
        raise _Rejected(400, "reasoning_effort is required")
    return "effort"


def _responses_kind(body):
    if body.get("store") is not False:
        raise _Rejected(400, "store must be false")
    if not isinstance(body.get("max_output_tokens"), int):
        raise _Rejected(400, "max_output_tokens is required")
    items = body.get("input")
    if not isinstance(items, list) or not items:
        raise _Rejected(400, "input is required")
    content = items[0].get("content")
    if isinstance(content, list):
        image = next(
            (item for item in content if item.get("type") == "input_image"), None
        )
        if image is None:
            raise _Rejected(400, "input_image block is required")
        url = str(image.get("image_url", ""))
        if not url.startswith("data:image/png;base64,"):
            raise _Rejected(400, "inline PNG data URL is required")
        _check_png(url.split(",", 1)[1])
        return "vision"
    if not isinstance(content, str) or not content:
        raise _Rejected(400, "text content is required")
    reasoning = body.get("reasoning") or {}
    if reasoning.get("effort") not in EFFORT_VALUES:
        raise _Rejected(400, "reasoning.effort is required")
    if reasoning.get("summary") != "auto":
        raise _Rejected(400, "reasoning.summary is required")
    return "effort"


def _google_kind(body):
    contents = body.get("contents")
    if not isinstance(contents, list) or not contents:
        raise _Rejected(400, "contents is required")
    parts = contents[0].get("parts")
    if not isinstance(parts, list) or not parts:
        raise _Rejected(400, "parts is required")
    image = next((part for part in parts if "inlineData" in part), None)
    if image is not None:
        inline = image["inlineData"]
        if inline.get("mimeType") != "image/png":
            raise _Rejected(400, "image/png inlineData is required")
        _check_png(inline.get("data"))
        return "vision"
    if not any(part.get("text") for part in parts):
        raise _Rejected(400, "text part is required")
    config = body.get("generationConfig") or {}
    thinking = config.get("thinkingConfig") or {}
    if not (
        isinstance(thinking.get("thinkingBudget"), int)
        or thinking.get("thinkingLevel") in {"LOW", "MEDIUM", "HIGH"}
    ):
        raise _Rejected(400, "thinkingConfig is required")
    return "effort"


def _effort_level(body):
    if "output_config" in body:
        value = (body.get("output_config") or {}).get("effort")
    elif "reasoning_effort" in body:
        value = body.get("reasoning_effort")
    elif "reasoning" in body:
        value = (body.get("reasoning") or {}).get("effort")
    else:
        thinking = (body.get("generationConfig") or {}).get("thinkingConfig") or {}
        value = thinking.get("thinkingLevel") or thinking.get("thinkingBudget")
    return {
        "low": "low",
        "medium": "medium",
        "high": "high",
        "xhigh": "extra",
        "max": "max",
        "LOW": "low",
        "MEDIUM": "medium",
        "HIGH": "high",
    }.get(value, str(value))


def _start_gateway():
    state = _GatewayState()

    class Handler(_FakeGateway):
        gateway = state

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, state, f"http://127.0.0.1:{server.server_port}"


def _create_provider(ctx, name, provider_type, base_url, models, api_key=SECRET):
    response = ctx.rest.post(
        "/api/v1/providers",
        json={
            "name": name,
            "type": provider_type,
            "base_url": base_url,
            "api_key": api_key,
            "models": models,
        },
    )
    ctx.require(
        response.status_code == 201,
        f"creating {provider_type} provider failed: {response.status_code} {response.text}",
    )
    return response.json()


def _probe(ctx, provider_id, model):
    return ctx.rest.post(
        f"/api/v1/providers/{provider_id}/probe", json={"model": model}
    )


def _table_counts(db_path):
    with sqlite3.connect(db_path) as db:
        return {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("conversations", "turns", "turn_events")
        }


def check_provider_probe(ctx):
    server, state, base_url = _start_gateway()
    before = _table_counts(ctx.runtime.db_path)
    try:
        anthropic = _create_provider(
            ctx,
            "issue 181 anthropic",
            "anthropic-messages",
            base_url,
            ["no-vision-model", "partial-effort-model", "slow-model"],
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            slow = pool.submit(_probe, ctx, anthropic["id"], "slow-model")
            ctx.require(
                state.slow_started.wait(10),
                "fake gateway never received the slow probe",
            )
            busy = _probe(ctx, anthropic["id"], "slow-model")
            ctx.require(
                busy.status_code == 409,
                f"concurrent probe returned {busy.status_code}, expected 409",
            )
            slow_result = slow.result(timeout=60)
        ctx.require(
            slow_result.status_code == 200,
            f"first concurrent probe failed: {slow_result.status_code} {slow_result.text}",
        )

        no_vision = _probe(ctx, anthropic["id"], "no-vision-model")
        ctx.require(
            no_vision.status_code == 200,
            f"probe failed: {no_vision.status_code} {no_vision.text}",
        )
        no_vision_body = no_vision.json()
        ctx.require(
            no_vision_body["vision"] == "no",
            f"rejected image did not report no: {no_vision_body}",
        )
        ctx.require(
            set(no_vision_body["effort"].values()) == {"yes"},
            f"accepted efforts were not reported yes: {no_vision_body}",
        )

        partial = _probe(ctx, anthropic["id"], "partial-effort-model")
        ctx.require(
            partial.status_code == 200,
            f"probe failed: {partial.status_code} {partial.text}",
        )
        partial_body = partial.json()
        ctx.require(
            partial_body["vision"] == "yes",
            f"accepted image was not reported yes: {partial_body}",
        )
        ctx.require(
            partial_body["effort"]
            == {
                "low": "yes",
                "medium": "yes",
                "high": "no",
                "extra": "unknown",
                "max": "unknown",
            },
            f"effort outcomes are wrong: {partial_body['effort']}",
        )
        ctx.require(
            "HTTP 500" in partial_body["notes"] and "timeout" in partial_body["notes"],
            f"failure reasons are missing from notes: {partial_body['notes']}",
        )

        saved = ctx.rest.get("/api/v1/providers").json()
        stored = next(row for row in saved if row["id"] == anthropic["id"])
        ctx.require(
            stored["model_capabilities"]["partial-effort-model"]
            == {"description": "", "vision": False, "effort_levels": []},
            "probe wrote capabilities without the administrator applying them",
        )

        applied = ctx.rest.put(
            f"/api/v1/providers/{anthropic['id']}",
            json={
                "model_capabilities": {
                    "partial-effort-model": {
                        "description": "探测后人工确认",
                        "vision": True,
                        "effort_levels": [
                            level
                            for level, outcome in partial_body["effort"].items()
                            if outcome == "yes"
                        ],
                    }
                }
            },
        )
        ctx.require(
            applied.status_code == 200,
            f"saving probe results failed: {applied.status_code} {applied.text}",
        )
        ctx.require(
            applied.json()["model_capabilities"]["partial-effort-model"]
            == {
                "description": "探测后人工确认",
                "vision": True,
                "effort_levels": ["low", "medium"],
            },
            f"applied capabilities did not persist: {applied.json()['model_capabilities']}",
        )

        for provider_type, model in (
            ("openai-completions", "gpt-5-completions"),
            ("openai-responses", "gpt-5-responses"),
            ("google-generative-ai", "gemini-3-flash"),
        ):
            provider = _create_provider(
                ctx,
                f"issue 181 {provider_type}",
                provider_type,
                f"{base_url}/v1",
                [model],
            )
            response = _probe(ctx, provider["id"], model)
            ctx.require(
                response.status_code == 200,
                f"{provider_type} probe failed: {response.status_code} {response.text}",
            )
            body = response.json()
            ctx.require(
                body["vision"] == "yes",
                f"{provider_type} vision request shape was rejected: {body}",
            )
            ctx.require(
                set(body["effort"].values()) == {"yes"},
                f"{provider_type} effort request shape was rejected: {body}",
            )
            ctx.require(SECRET not in response.text, "probe leaked the API key")

        bad_model = _probe(ctx, anthropic["id"], "not-configured")
        ctx.require(
            bad_model.status_code == 422,
            f"unknown model returned {bad_model.status_code}, expected 422",
        )
        bad_provider = ctx.rest.post(
            "/api/v1/providers/does-not-exist/probe", json={"model": "slow-model"}
        )
        ctx.require(
            bad_provider.status_code == 404,
            f"unknown provider returned {bad_provider.status_code}, expected 404",
        )

        token_response = ctx.rest.post(
            "/api/v1/tokens", json={"name": "issue 181 user", "role": "user"}
        )
        ctx.require(
            token_response.status_code == 201,
            f"creating a non-admin token failed: {token_response.status_code}",
        )
        with httpx.Client(
            base_url=str(ctx.rest.base_url),
            headers={"Authorization": f"Bearer {token_response.json()['token']}"},
            timeout=30,
            trust_env=False,
        ) as user:
            forbidden = user.post(
                f"/api/v1/providers/{anthropic['id']}/probe",
                json={"model": "slow-model"},
            )
        ctx.require(
            forbidden.status_code == 403,
            f"non-admin probe returned {forbidden.status_code}, expected 403",
        )

        after = _table_counts(ctx.runtime.db_path)
        ctx.require(
            after == before,
            f"probe created conversations/turns/events: before={before} after={after}",
        )

        headers_seen = [item["headers"] for item in state.requests]
        ctx.require(
            any(headers.get("x-api-key") == SECRET for headers in headers_seen),
            "fake gateway never received the decrypted provider key",
        )
        ctx.require(
            all(SECRET not in json.dumps(item["body"]) for item in state.requests),
            "probe request body leaked the API key",
        )
        log_text = ctx.runtime.service_log.read_text(errors="replace")
        ctx.require(SECRET not in log_text, "service log contains the API key")

        return {
            "ok": True,
            "info": {
                "providers_probed": 4,
                "requests": len(state.requests),
                "concurrency": "slow probe returned 409 for the concurrent request",
                "effort": partial_body["effort"],
            },
        }
    finally:
        server.shutdown()
        server.server_close()
