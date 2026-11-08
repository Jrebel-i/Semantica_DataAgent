"""Verify provider/model switching keeps the agent session history."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REQUESTS: list[dict] = []


class _ProviderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        if self.path != "/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        REQUESTS.append(
            {
                "model": body.get("model"),
                "messages": body.get("messages", []),
                "authorization": self.headers.get("authorization"),
            }
        )
        model = body.get("model") or "unknown"
        text = f"answer from {model}"
        chunk = json.dumps(
            {
                "id": "chatcmpl-e2e",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": text},
                        "finish_reason": None,
                    }
                ],
            }
        )
        final = json.dumps(
            {
                "id": "chatcmpl-e2e",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        )
        payload = (f"data: {chunk}\n\ndata: {final}\n\ndata: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def _events(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.split("\n\n"):
        name = None
        data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name is not None:
            events.append((name, data or {}))
    return events


def _ask(ctx, payload: dict) -> list[tuple[str, dict]]:
    with ctx.rest.stream(
        "POST",
        "/api/v1/conversations/ask",
        json=payload,
        timeout=180,
    ) as response:
        ctx.require(
            response.status_code == 200,
            f"ask: HTTP {response.status_code} {response.read()[:400]!r}",
        )
        body = "".join(response.iter_text())
    events = _events(body)
    ctx.require(
        any(name == "done" for name, _ in events),
        f"answer did not finish: {events[-3:]}",
    )
    return events


def _provider(ctx, name: str, model: str, key: str, port: int) -> str:
    response = ctx.rest.post(
        "/api/v1/providers",
        json={
            "name": name,
            "type": "openai-completions",
            "base_url": f"http://127.0.0.1:{port}",
            "api_key": key,
            "models": [model],
        },
    )
    ctx.require(
        response.status_code == 201,
        f"provider: {response.status_code} {response.text}",
    )
    return response.json()["id"]


def check_switch_provider_and_model_keeps_history(ctx):
    REQUESTS.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    model_a = "e2e-switch-model-a"
    model_b = "e2e-switch-model-b"
    key_a = "e2e-switch-key-a"
    key_b = "e2e-switch-key-b"
    try:
        provider_a = _provider(ctx, "issue145 provider A", model_a, key_a, port)
        provider_b = _provider(ctx, "issue145 provider B", model_b, key_b, port)
        profiles = {
            "pi": {
                "providers": [provider_a, provider_b],
                "default_provider": provider_a,
                "default_model": model_a,
            }
        }
        saved = ctx.rest.put(
            "/api/v1/settings",
            json={"values": {"assistant_agent_profiles": json.dumps(profiles)}},
        )
        ctx.require(saved.status_code == 200, saved.text)

        first = _ask(
            ctx,
            {
                "question": "first question marker",
                "agent": "pi",
                "provider_id": provider_a,
                "model": model_a,
            },
        )
        conversation_id = first[0][1]["conversation_id"]
        ctx.require(
            first[-1][1].get("text") == f"answer from {model_a}",
            f"first answer: {first[-1]}",
        )

        second = _ask(
            ctx,
            {
                "question": "second question marker",
                "conversation_id": conversation_id,
                "provider_id": provider_b,
                "model": model_b,
            },
        )
        ctx.require(
            second[-1][1].get("text") == f"answer from {model_b}",
            f"second answer: {second[-1]}",
        )

        detail = ctx.rest.get(f"/api/v1/conversations/{conversation_id}")
        ctx.require(detail.status_code == 200, detail.text)
        row = detail.json()
        ctx.require(
            (row["provider_id"], row["model"]) == (provider_b, model_b),
            "conversation did not keep the latest provider/model",
        )
        turns = [(turn["provider_id"], turn["model"]) for turn in row["turns"]]
        ctx.require(
            turns == [(provider_a, model_a), (provider_b, model_b)],
            f"turn provider/model history: {turns}",
        )

        ctx.require(len(REQUESTS) == 2, f"unexpected provider calls: {REQUESTS}")
        first_request, second_request = REQUESTS
        ctx.require(
            first_request["model"] == model_a
            and first_request["authorization"] == f"Bearer {key_a}",
            "first turn did not use provider A credentials",
        )
        ctx.require(
            second_request["model"] == model_b
            and second_request["authorization"] == f"Bearer {key_b}",
            "second turn did not use provider B credentials",
        )
        serialized = json.dumps(second_request, ensure_ascii=False)
        ctx.require(key_a not in serialized, "old provider key leaked into new turn")
        ctx.require(
            "first question marker" in serialized
            and f"answer from {model_a}" in serialized,
            "previous turn history was not carried into the new model",
        )

        changed = ctx.rest.post(
            "/api/v1/conversations/ask",
            json={
                "question": "change agent",
                "conversation_id": conversation_id,
                "agent": "claude",
            },
        )
        ctx.require(
            changed.status_code == 409,
            f"agent change was accepted: {changed.status_code} {changed.text}",
        )
        return {
            "conversation_id": conversation_id,
            "provider_calls": len(REQUESTS),
            "turns": turns,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
