"""Probe a provider model's wire-level capabilities without persisting them."""

from __future__ import annotations

import base64
import json
import os
import struct
import threading
import time
import zlib
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import quote

import httpx

from .agent_types import ProviderConfig

EFFORT_LEVELS = ("low", "medium", "high", "extra", "max")
EFFORT_VALUES = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "extra": "xhigh",
    "max": "max",
}
GOOGLE_BUDGETS = {
    "low": 1024,
    "medium": 2048,
    "high": 4096,
    "extra": 8192,
    "max": 16384,
}
DEFAULT_TOTAL_TIMEOUT_SECONDS = 60.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0
MAX_TOKENS = 32
PROBE_PROMPT = "Reply with OK."
VISION_PROMPT = "Describe this image in one short sentence."

_active_lock = threading.Lock()
_active_providers: set[str] = set()


@dataclass(frozen=True)
class ModelProbeResult:
    model: str
    vision: str
    effort: dict[str, str]
    notes: str


def begin_probe(provider_id: str) -> bool:
    with _active_lock:
        if provider_id in _active_providers:
            return False
        _active_providers.add(provider_id)
        return True


def end_probe(provider_id: str) -> None:
    with _active_lock:
        _active_providers.discard(provider_id)


def _seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


@lru_cache(maxsize=1)
def _image_base64() -> str:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    pixels = zlib.compress(b"\x00\xff\x00\x00\xff")
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", pixels)
        + chunk(b"IEND", b"")
    )
    return base64.b64encode(png).decode()


def _url(provider: ProviderConfig, model: str) -> str:
    base = provider.base_url.rstrip("/")
    if provider.type == "anthropic-messages":
        return f"{base}/v1/messages"
    if provider.type == "openai-completions":
        return f"{base}/chat/completions"
    if provider.type == "openai-responses":
        return f"{base}/responses"
    resource = model if model.startswith("models/") else f"models/{model}"
    return f"{base}/{quote(resource, safe='/')}:generateContent"


def _headers(provider: ProviderConfig) -> dict[str, str]:
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "user-agent": "semantica-dataagent-probe",
    }
    if provider.type == "anthropic-messages":
        headers["anthropic-version"] = "2023-06-01"
        if provider.api_key:
            headers["x-api-key"] = provider.api_key
    elif provider.type == "google-generative-ai":
        if provider.api_key:
            headers["x-goog-api-key"] = provider.api_key
    elif provider.api_key:
        headers["authorization"] = f"Bearer {provider.api_key}"
    return headers


def _google_uses_thinking_level(model: str) -> bool:
    name = model.lower()
    return (
        name.startswith("gemini-3")
        and ("pro" in name or "flash" in name)
        or name in {"gemini-flash-latest", "gemini-flash-lite-latest"}
        or "gemma-4" in name
        or "gemma4" in name
    )


def _vision_payload(provider: ProviderConfig, model: str) -> dict:
    image = _image_base64()
    if provider.type == "anthropic-messages":
        return {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "stream": False,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": image,
                            },
                        },
                        {"type": "text", "text": VISION_PROMPT},
                    ],
                }
            ],
        }
    if provider.type == "openai-completions":
        return {
            "model": model,
            "max_completion_tokens": MAX_TOKENS,
            "stream": False,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image}"},
                        },
                        {"type": "text", "text": VISION_PROMPT},
                    ],
                }
            ],
        }
    if provider.type == "openai-responses":
        return {
            "model": model,
            "max_output_tokens": MAX_TOKENS,
            "store": False,
            "stream": False,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": VISION_PROMPT},
                        {
                            "type": "input_image",
                            "image_url": f"data:image/png;base64,{image}",
                        },
                    ],
                }
            ],
        }
    return {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": VISION_PROMPT},
                    {"inlineData": {"mimeType": "image/png", "data": image}},
                ],
            }
        ],
        "generationConfig": {"maxOutputTokens": MAX_TOKENS},
    }


def _effort_payload(provider: ProviderConfig, model: str, level: str) -> dict:
    value = EFFORT_VALUES[level]
    if provider.type == "anthropic-messages":
        return {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "stream": False,
            "messages": [{"role": "user", "content": PROBE_PROMPT}],
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": value},
        }
    if provider.type == "openai-completions":
        return {
            "model": model,
            "max_completion_tokens": MAX_TOKENS,
            "stream": False,
            "messages": [{"role": "user", "content": PROBE_PROMPT}],
            "reasoning_effort": value,
        }
    if provider.type == "openai-responses":
        return {
            "model": model,
            "max_output_tokens": MAX_TOKENS,
            "store": False,
            "stream": False,
            "input": [{"role": "user", "content": PROBE_PROMPT}],
            "reasoning": {"effort": value, "summary": "auto"},
            "include": ["reasoning.encrypted_content"],
        }
    thinking: dict[str, object] = {"includeThoughts": True}
    if _google_uses_thinking_level(model):
        thinking["thinkingLevel"] = {
            "low": "LOW",
            "medium": "MEDIUM",
            "high": "HIGH",
            "extra": "HIGH",
            "max": "HIGH",
        }[level]
    else:
        thinking["thinkingBudget"] = GOOGLE_BUDGETS[level]
    return {
        "contents": [{"role": "user", "parts": [{"text": PROBE_PROMPT}]}],
        "generationConfig": {
            "maxOutputTokens": MAX_TOKENS,
            "thinkingConfig": thinking,
        },
    }


def _success_shape(provider: ProviderConfig, payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    if provider.type == "anthropic-messages":
        return payload.get("type") == "message" and isinstance(
            payload.get("content"), list
        )
    if provider.type == "openai-completions":
        return isinstance(payload.get("choices"), list) and bool(payload["choices"])
    if provider.type == "openai-responses":
        return (
            isinstance(payload.get("output"), list)
            or payload.get("status") == "completed"
        )
    return isinstance(payload.get("candidates"), list) and bool(payload["candidates"])


def _outcome(
    provider: ProviderConfig, model: str, body: dict, deadline: float
) -> tuple[str, str]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return "unknown", "timeout"
    request_timeout = min(
        _seconds(
            "DATAAGENT_PROVIDER_PROBE_REQUEST_TIMEOUT_SECONDS",
            DEFAULT_REQUEST_TIMEOUT_SECONDS,
        ),
        remaining,
    )
    try:
        response = httpx.post(
            _url(provider, model),
            headers=_headers(provider),
            json=body,
            timeout=request_timeout,
            trust_env=False,
            follow_redirects=False,
        )
    except httpx.TimeoutException:
        return "unknown", "timeout"
    except httpx.HTTPError:
        return "unknown", "network error"

    status = response.status_code
    if 200 <= status < 300:
        try:
            payload = json.loads(response.content)
        except (ValueError, UnicodeDecodeError):
            return "unknown", f"HTTP {status} with an invalid response body"
        if not _success_shape(provider, payload):
            return "unknown", f"HTTP {status} with an unexpected response body"
        return "yes", ""
    if status in {400, 413, 415, 422}:
        return "no", f"HTTP {status}"
    return "unknown", f"HTTP {status}"


def _notes(
    provider: ProviderConfig, vision_reason: str, effort_reasons: dict[str, str]
) -> str:
    mapping = ", ".join(f"{level}->{value}" for level, value in EFFORT_VALUES.items())
    if provider.type == "google-generative-ai":
        mapping += "; google extra/max use the highest discrete level or larger budgets"
    details = []
    if vision_reason:
        details.append(f"vision: {vision_reason}")
    for level in EFFORT_LEVELS:
        if effort_reasons[level]:
            details.append(f"{level}: {effort_reasons[level]}")
    text = f"effort mapping: {mapping}"
    if details:
        text += "; " + "; ".join(details)
    return text


def probe_model(provider: ProviderConfig, model: str) -> ModelProbeResult:
    total_timeout = _seconds(
        "DATAAGENT_PROVIDER_PROBE_TIMEOUT_SECONDS", DEFAULT_TOTAL_TIMEOUT_SECONDS
    )
    deadline = time.monotonic() + total_timeout
    vision, vision_reason = _outcome(
        provider, model, _vision_payload(provider, model), deadline
    )
    effort: dict[str, str] = {}
    effort_reasons: dict[str, str] = {}
    for level in EFFORT_LEVELS:
        outcome, reason = _outcome(
            provider, model, _effort_payload(provider, model, level), deadline
        )
        effort[level] = outcome
        effort_reasons[level] = reason
    return ModelProbeResult(
        model=model,
        vision=vision,
        effort=effort,
        notes=_notes(provider, vision_reason, effort_reasons),
    )
