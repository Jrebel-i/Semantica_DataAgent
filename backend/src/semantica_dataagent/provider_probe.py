"""Probe a provider model's wire-level capabilities without persisting them."""

from __future__ import annotations

import base64
import json
import os
import struct
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
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
VISION_PROMPT = "这张图片是什么颜色？只回答一个词"
VISION_IMAGE_SIZE = 64
VISION_COLORS = (
    ("red", ("红", "red"), (255, 0, 0)),
    ("blue", ("蓝", "blue"), (0, 0, 255)),
)
VISION_UNRECOGNIZED = "请求被接受但模型未能识别图片内容"

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


@lru_cache(maxsize=2)
def _solid_png_base64(width: int, height: int, rgb: tuple[int, int, int]) -> str:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    row = b"\x00" + bytes(rgb) * width
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = zlib.compress(row * height)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", pixels)
        + chunk(b"IEND", b"")
    )
    return base64.b64encode(png).decode()


@lru_cache(maxsize=1)
def _vision_images() -> tuple[tuple[str, tuple[str, ...], str], ...]:
    return tuple(
        (
            name,
            keywords,
            _solid_png_base64(VISION_IMAGE_SIZE, VISION_IMAGE_SIZE, rgb),
        )
        for name, keywords, rgb in VISION_COLORS
    )


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


def _vision_payload(provider: ProviderConfig, model: str, image: str) -> dict:
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
        return isinstance(payload.get("output"), list) and not payload.get("error")
    return isinstance(payload.get("candidates"), list) and bool(payload["candidates"])


def _response_text(provider: ProviderConfig, payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    if provider.type == "anthropic-messages":
        content = payload.get("content")
        if not isinstance(content, list):
            return ""
        return "\n".join(
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
    if provider.type == "openai-completions":
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                block["text"]
                for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            )
        return ""
    if provider.type == "openai-responses":
        output = payload.get("output")
        if not isinstance(output, list):
            return ""
        texts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, str):
                texts.append(content)
                continue
            if not isinstance(content, list):
                continue
            texts.extend(
                block["text"]
                for block in content
                if isinstance(block, dict)
                and block.get("type") == "output_text"
                and isinstance(block.get("text"), str)
            )
        return "\n".join(texts)
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return ""
    content = candidates[0].get("content") if isinstance(candidates[0], dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        return ""
    return "\n".join(
        part["text"]
        for part in parts
        if isinstance(part, dict)
        and part.get("thought") is not True
        and isinstance(part.get("text"), str)
    )


def _error_message(payload: object, secret: str) -> str:
    message = ""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            message = error["message"]
        elif isinstance(error, str):
            message = error
        elif isinstance(payload.get("message"), str):
            message = payload["message"]
    message = " ".join(message.split())
    if secret:
        message = message.replace(secret, "***")
    return message[:200]


def _recognizes_color(text: str, keywords: tuple[str, ...]) -> bool:
    folded = text.casefold()
    return any(keyword.casefold() in folded for keyword in keywords)


def _post_with_deadline(
    url: str,
    headers: dict[str, str],
    body: dict,
    request_timeout: float,
    deadline: float,
    aborted: threading.Event,
) -> httpx.Response:
    client = httpx.Client(
        timeout=request_timeout,
        trust_env=False,
        follow_redirects=False,
    )

    def abort() -> None:
        aborted.set()
        client.close()

    timer = threading.Timer(max(0.0, deadline - time.monotonic()), abort)
    timer.daemon = True
    timer.start()
    try:
        return client.post(url, headers=headers, json=body)
    finally:
        timer.cancel()
        client.close()


@dataclass(frozen=True)
class _WireResult:
    kind: str
    status: int | None = None
    text: str = ""
    detail: str = ""


def _send_once(
    provider: ProviderConfig, model: str, body: dict, deadline: float
) -> _WireResult:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return _WireResult("timeout", detail="timeout")
    request_timeout = min(
        _seconds(
            "DATAAGENT_PROVIDER_PROBE_REQUEST_TIMEOUT_SECONDS",
            DEFAULT_REQUEST_TIMEOUT_SECONDS,
        ),
        remaining,
    )
    aborted = threading.Event()
    try:
        response = _post_with_deadline(
            _url(provider, model),
            _headers(provider),
            body,
            request_timeout,
            deadline,
            aborted,
        )
    except httpx.TimeoutException:
        return _WireResult("timeout", detail="timeout")
    except (httpx.HTTPError, RuntimeError):
        if aborted.is_set():
            return _WireResult("timeout", detail="timeout")
        return _WireResult("network", detail="network error")

    status = response.status_code
    payload: object
    try:
        payload = json.loads(response.content)
    except (ValueError, UnicodeDecodeError):
        payload = None
    if 200 <= status < 300:
        if payload is None:
            return _WireResult(
                "invalid", status, detail=f"HTTP {status} with an invalid response body"
            )
        if not _success_shape(provider, payload):
            return _WireResult(
                "invalid",
                status,
                detail=f"HTTP {status} with an unexpected response body",
            )
        return _WireResult("ok", status, _response_text(provider, payload))
    detail = f"HTTP {status}"
    reason = _error_message(payload, provider.api_key or "")
    if reason:
        detail += f": {reason}"
    return _WireResult("http", status, detail=detail)


def _outcome(
    provider: ProviderConfig, model: str, body: dict, deadline: float
) -> tuple[str, str]:
    result = _send_once(provider, model, body, deadline)
    if result.kind == "ok":
        return "yes", ""
    if result.kind == "http" and result.status in {400, 413, 415, 422}:
        return "no", result.detail
    return "unknown", result.detail or result.kind


def _vision_outcome(
    provider: ProviderConfig, model: str, deadline: float
) -> tuple[str, str]:
    images = _vision_images()
    bodies = [_vision_payload(provider, model, image) for _, _, image in images]
    with ThreadPoolExecutor(max_workers=len(bodies)) as pool:
        results = list(
            pool.map(lambda body: _send_once(provider, model, body, deadline), bodies)
        )

    rejected = [
        result
        for result in results
        if result.kind == "http"
        and result.status is not None
        and 400 <= result.status < 500
    ]
    if rejected:
        return "no", rejected[0].detail

    unknown = [result for result in results if result.kind != "ok"]
    if unknown:
        details = " / ".join(
            dict.fromkeys(result.detail for result in unknown if result.detail)
        )
        return "unknown", details or "unknown"

    recognized = all(
        _recognizes_color(result.text, keywords)
        for result, (_, keywords, _) in zip(results, images, strict=True)
    )
    if recognized:
        return "yes", ""
    return "no", VISION_UNRECOGNIZED


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
    text = "思考强度：yes = 网关接受该参数，不保证生效幅度"
    if mapping:
        text += f"；映射：{mapping}"
    if details:
        text += "; " + "; ".join(details)
    return text


def probe_model(provider: ProviderConfig, model: str) -> ModelProbeResult:
    total_timeout = _seconds(
        "DATAAGENT_PROVIDER_PROBE_TIMEOUT_SECONDS", DEFAULT_TOTAL_TIMEOUT_SECONDS
    )
    deadline = time.monotonic() + total_timeout
    vision, vision_reason = _vision_outcome(provider, model, deadline)
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
