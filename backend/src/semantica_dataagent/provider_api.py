"""Model provider configuration with encrypted API keys."""

import json
import os
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .agent_runner import ProviderConfig, list_agents
from .api_time import UtcDatetime
from .auth import admin, current_user
from .crypto import decrypt, encrypt
from .db import AgentProfile, Provider, sessions
from .provider_probe import (
    EFFORT_LEVELS,
    begin_probe,
    end_probe,
    probe_model,
)

router = APIRouter()
PROVIDER_TYPES = {
    "anthropic-messages",
    "openai-completions",
    "openai-responses",
    "google-generative-ai",
}
EffortLevel = Literal["low", "medium", "high", "extra", "max"]
DESCRIPTION_LIMIT = 200
ProbeOutcome = Literal["yes", "no", "unknown"]
DEFAULT_CATALOG_TIMEOUT_SECONDS = 15.0
MAX_CATALOG_BYTES = 2 * 1024 * 1024
_catalog_transport: httpx.BaseTransport | None = None

_PROTOCOL_BY_SUFFIX = {
    "claude": "Claude",
    "codex": "Codex",
    "oai": "OpenAI",
}


def model_protocol(model_id: str) -> str:
    """Map trailing #<suffix> tag to a display protocol badge."""
    if "#" not in model_id:
        return "未知"
    suffix = model_id.rsplit("#", 1)[1].strip().lower()
    return _PROTOCOL_BY_SUFFIX.get(suffix, "未知")


class ModelCapability(BaseModel):
    description: str = Field(default="", max_length=DESCRIPTION_LIMIT)
    vision: bool = False
    effort_levels: list[EffortLevel] = Field(default_factory=list)


class ProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200, pattern=r"\S")
    type: str = "anthropic-messages"
    base_url: str
    api_key: str | None = Field(default=None, max_length=10000)
    models: list[str] = Field(default_factory=list)
    model_capabilities: dict[str, ModelCapability] | None = None


class ProviderUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200, pattern=r"\S")
    type: str | None = None
    base_url: str | None = None
    api_key: str | None = Field(default=None, max_length=10000)
    models: list[str] | None = None
    model_capabilities: dict[str, ModelCapability] | None = None


class AgentView(BaseModel):
    id: str
    name: str
    available: bool
    effort_levels: list[EffortLevel]
    supports_skills: bool = False


class ProviderView(BaseModel):
    id: str
    name: str
    type: str
    base_url: str
    models: list[str]
    model_capabilities: dict[str, ModelCapability]
    catalog_models: list[str] = Field(default_factory=list)
    catalog_fetched_at: UtcDatetime | None = None
    catalog_status: str | None = None
    catalog_error: str | None = None
    has_api_key: bool
    created_at: UtcDatetime
    updated_at: UtcDatetime


class ProviderProbeRequest(BaseModel):
    model: str = Field(min_length=1, max_length=200)


class ProbeEffortResult(BaseModel):
    low: ProbeOutcome
    medium: ProbeOutcome
    high: ProbeOutcome
    extra: ProbeOutcome
    max: ProbeOutcome


class ProviderProbeView(BaseModel):
    model: str
    vision: ProbeOutcome
    effort: ProbeEffortResult
    notes: str


def _validate_address(value: str) -> str:
    value = value.strip().rstrip("/")
    try:
        address = urlsplit(value)
    except ValueError:
        raise HTTPException(422, "Invalid base URL") from None
    if (
        address.scheme not in {"http", "https"}
        or not address.hostname
        or address.username is not None
        or address.password is not None
        or address.query
        or address.fragment
    ):
        raise HTTPException(422, "Invalid base URL")
    return value


def _validate_type(value: str) -> str:
    if value not in PROVIDER_TYPES:
        raise HTTPException(422, "Unsupported provider type")
    return value


def _validate_models(values: list[str], *, allow_empty: bool = False) -> list[str]:
    models: list[str] = []
    for item in values:
        item = item.strip()
        if not item or len(item) > 200:
            raise HTTPException(422, "Invalid model id")
        if item not in models:
            models.append(item)
    if not models and not allow_empty:
        raise HTTPException(422, "At least one model is required")
    return models


def _stored_capabilities(row: Provider) -> dict[str, ModelCapability]:
    if not row.model_capabilities:
        return {}
    values = json.loads(row.model_capabilities)
    return {
        name: ModelCapability.model_validate(value) for name, value in values.items()
    }


def _validate_capabilities(
    values: dict[str, ModelCapability], models: list[str]
) -> dict[str, ModelCapability]:
    unknown = sorted(set(values) - set(models))
    if unknown:
        raise HTTPException(422, "Capability model is not in models")
    capabilities: dict[str, ModelCapability] = {}
    for name, value in values.items():
        selected = set(value.effort_levels)
        capabilities[name] = ModelCapability(
            description=value.description.strip(),
            vision=value.vision,
            effort_levels=[level for level in EFFORT_LEVELS if level in selected],
        )
    return capabilities


def _dump_capabilities(capabilities: dict[str, ModelCapability]) -> str | None:
    if not capabilities:
        return None
    return json.dumps(
        {name: value.model_dump() for name, value in capabilities.items()},
        ensure_ascii=False,
        sort_keys=True,
    )


def _capabilities_view(row: Provider, models: list[str]) -> dict[str, ModelCapability]:
    stored = _stored_capabilities(row)
    return {model: stored.get(model, ModelCapability()) for model in models}


def view(row: Provider) -> ProviderView:
    models = json.loads(row.models)
    catalog_models = json.loads(row.catalog_models) if row.catalog_models else []
    return ProviderView(
        id=row.id,
        name=row.name,
        type=row.type,
        base_url=row.base_url,
        models=models,
        model_capabilities=_capabilities_view(row, models),
        catalog_models=catalog_models,
        catalog_fetched_at=row.catalog_fetched_at,
        catalog_status=row.catalog_status,
        catalog_error=row.catalog_error,
        has_api_key=bool(row.api_key),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def provider_config(row: Provider) -> ProviderConfig:
    return ProviderConfig(
        id=row.id,
        name=row.name,
        type=row.type,
        base_url=row.base_url,
        models=json.loads(row.models),
        api_key=decrypt(row.api_key) if row.api_key else None,
    )


@router.get("/api/v1/agents", response_model=list[AgentView])
def agents(_: Annotated[str, Depends(current_user)]):
    return list_agents()


@router.get("/api/v1/providers", response_model=list[ProviderView])
def providers(_: Annotated[str, Depends(current_user)]):
    with sessions()() as db:
        return [
            view(row) for row in db.query(Provider).order_by(Provider.name, Provider.id)
        ]


def _catalog_timeout_seconds() -> float:
    try:
        value = float(
            os.environ.get(
                "DATAAGENT_PROVIDER_CATALOG_TIMEOUT_SECONDS",
                DEFAULT_CATALOG_TIMEOUT_SECONDS,
            )
        )
    except (TypeError, ValueError):
        return DEFAULT_CATALOG_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_CATALOG_TIMEOUT_SECONDS


def _catalog_url(provider: ProviderConfig) -> str:
    base = provider.base_url.rstrip("/")
    if provider.type == "anthropic-messages":
        return f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
    return f"{base}/models"


def _catalog_headers(provider: ProviderConfig) -> dict[str, str]:
    headers = {
        "accept": "application/json",
        "user-agent": "semantica-dataagent-catalog",
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


def _sanitize_error(message: str, secret: str) -> str:
    cleaned = " ".join(message.split())
    if secret:
        cleaned = cleaned.replace(secret, "***")
    return cleaned[:200]


def _extract_error_reason(payload: object, secret: str) -> str:
    reason = ""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            reason = error["message"]
        elif isinstance(error, str):
            reason = error
        elif isinstance(payload.get("message"), str):
            reason = payload["message"]
    return _sanitize_error(reason, secret)


def _parse_catalog_models(provider_type: str, payload: object) -> list[str] | None:
    if not isinstance(payload, dict):
        return None
    items: object
    if provider_type == "google-generative-ai" and isinstance(
        payload.get("models"), list
    ):
        items = payload["models"]
    elif isinstance(payload.get("data"), list):
        items = payload["data"]
    elif isinstance(payload.get("models"), list):
        items = payload["models"]
    else:
        return None

    models: list[str] = []
    for item in items:
        raw_id = ""
        if isinstance(item, str):
            raw_id = item
        elif isinstance(item, dict):
            candidate = (
                item.get("name")
                if provider_type == "google-generative-ai"
                else item.get("id")
            )
            if not isinstance(candidate, str):
                candidate = item.get("id") or item.get("name")
            if isinstance(candidate, str):
                raw_id = candidate
        model_id = raw_id.strip()
        if provider_type == "google-generative-ai" and model_id.startswith("models/"):
            model_id = model_id.removeprefix("models/").strip()
        if model_id and len(model_id) <= 500 and model_id not in models:
            models.append(model_id)
    return models


def _fetch_provider_catalog(
    provider: ProviderConfig,
) -> tuple[str, list[str] | None, str | None]:
    secret = provider.api_key or ""
    timeout = _catalog_timeout_seconds()
    url = _catalog_url(provider)
    headers = _catalog_headers(provider)
    try:
        with (
            httpx.Client(
                timeout=timeout,
                trust_env=False,
                follow_redirects=False,
                transport=_catalog_transport,
            ) as client,
            client.stream("GET", url, headers=headers) as response,
        ):
            status = response.status_code
            if 300 <= status < 400:
                return "error", None, "Redirects are not allowed"
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > MAX_CATALOG_BYTES:
                    return "error", None, "Catalog response exceeds size limit"
                chunks.append(chunk)
            raw_bytes = b"".join(chunks)
    except httpx.TimeoutException:
        return "timeout", None, "Request timed out"
    except httpx.HTTPError as exc:
        detail = _sanitize_error(str(exc) or type(exc).__name__, secret)
        return "error", None, detail or "Network error"

    payload: object
    try:
        payload = json.loads(raw_bytes)
    except (ValueError, UnicodeDecodeError):
        payload = None

    if status in (401, 403):
        reason = _extract_error_reason(payload, secret)
        detail = f"HTTP {status}: {reason}" if reason else f"HTTP {status}"
        return "auth_failed", None, _sanitize_error(detail, secret)

    if status in (404, 405, 501):
        reason = _extract_error_reason(payload, secret)
        detail = f"HTTP {status}: {reason}" if reason else f"HTTP {status}"
        return "unsupported_protocol", None, _sanitize_error(detail, secret)

    if not 200 <= status < 300:
        reason = _extract_error_reason(payload, secret)
        detail = f"HTTP {status}: {reason}" if reason else f"HTTP {status}"
        return "error", None, _sanitize_error(detail, secret)

    if payload is None:
        return "unsupported_protocol", None, "Invalid JSON catalog response"

    models = _parse_catalog_models(provider.type, payload)
    if models is None:
        return "unsupported_protocol", None, "Unsupported catalog response format"
    if not models:
        return "empty", [], None
    return "ok", models, None


def _clear_pending_verification_for_provider(db, provider_id: str) -> None:
    profiles = db.query(AgentProfile).all()
    for profile in profiles:
        try:
            pending = json.loads(profile.pending_verification_models or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(pending, dict) and provider_id in pending:
            pending.pop(provider_id, None)
            profile.pending_verification_models = json.dumps(
                pending, ensure_ascii=False
            )


@router.post(
    "/api/v1/providers/{provider_id}/refresh-models", response_model=ProviderView
)
def refresh_provider_models(provider_id: str, _: Annotated[str, Depends(admin)]):
    with sessions()() as db:
        row = db.get(Provider, provider_id)
        if row is None:
            raise HTTPException(404, "Provider not found")
        provider = provider_config(row)

    status, catalog_models, error = _fetch_provider_catalog(provider)
    now = datetime.now(UTC)
    with sessions().begin() as db:
        row = db.get(Provider, provider_id)
        if row is None:
            raise HTTPException(404, "Provider not found")
        row.catalog_status = status
        row.catalog_error = error
        row.updated_at = now
        if status in ("ok", "empty") and catalog_models is not None:
            serialized = json.dumps(catalog_models, ensure_ascii=False)
            row.catalog_models = serialized
            row.models = serialized
            row.catalog_fetched_at = now
            _clear_pending_verification_for_provider(db, provider_id)
        db.flush()
        return view(row)


@router.post("/api/v1/providers/{provider_id}/probe", response_model=ProviderProbeView)
def probe_provider(
    provider_id: str,
    body: ProviderProbeRequest,
    _: Annotated[str, Depends(admin)],
):
    with sessions()() as db:
        row = db.get(Provider, provider_id)
        if row is None:
            raise HTTPException(404, "Provider not found")
        provider = provider_config(row)
    model = body.model.strip()
    if model not in provider.models:
        raise HTTPException(422, "Model is not in provider models")
    if not begin_probe(provider_id):
        raise HTTPException(409, "A probe is already running for this provider")
    try:
        result = probe_model(provider, model)
    finally:
        end_probe(provider_id)
    return ProviderProbeView(
        model=result.model,
        vision=result.vision,
        effort=ProbeEffortResult(**result.effort),
        notes=result.notes,
    )


@router.post("/api/v1/providers", response_model=ProviderView, status_code=201)
def create_provider(body: ProviderCreate, _: Annotated[str, Depends(admin)]):
    now = datetime.now(UTC)
    models = _validate_models(body.models, allow_empty=True)
    capabilities = _validate_capabilities(body.model_capabilities or {}, models)
    row = Provider(
        id=uuid.uuid4().hex,
        name=body.name.strip(),
        type=_validate_type(body.type),
        base_url=_validate_address(body.base_url),
        api_key=encrypt(body.api_key) if body.api_key else None,
        models=json.dumps(models),
        model_capabilities=_dump_capabilities(capabilities),
        created_at=now,
        updated_at=now,
    )
    with sessions().begin() as db:
        db.add(row)
        db.flush()
        return view(row)


@router.put("/api/v1/providers/{provider_id}", response_model=ProviderView)
def update_provider(
    provider_id: str, body: ProviderUpdate, _: Annotated[str, Depends(admin)]
):
    with sessions().begin() as db:
        row = db.get(Provider, provider_id)
        if row is None:
            raise HTTPException(404, "Provider not found")
        if body.name is not None:
            row.name = body.name.strip()
        if body.type is not None:
            row.type = _validate_type(body.type)
        if body.base_url is not None:
            new_base_url = _validate_address(body.base_url)
            if new_base_url != row.base_url and "api_key" not in body.model_fields_set:
                raise HTTPException(
                    422,
                    "API key must be re-submitted or cleared when changing base URL",
                )
            row.base_url = new_base_url
        models: list[str] | None = None
        if body.models is not None:
            models = _validate_models(body.models)
            row.models = json.dumps(models)
        if body.model_capabilities is not None:
            current_models = models or json.loads(row.models)
            capabilities = _validate_capabilities(
                body.model_capabilities, current_models
            )
            row.model_capabilities = _dump_capabilities(capabilities)
        elif models is not None:
            stored = _stored_capabilities(row)
            kept = {model: value for model, value in stored.items() if model in models}
            row.model_capabilities = _dump_capabilities(kept)
        if "api_key" in body.model_fields_set:
            row.api_key = encrypt(body.api_key) if body.api_key else None
        row.updated_at = datetime.now(UTC)
        db.flush()
        return view(row)


@router.delete("/api/v1/providers/{provider_id}", status_code=204)
def delete_provider(provider_id: str, _: Annotated[str, Depends(admin)]):
    with sessions().begin() as db:
        row = db.get(Provider, provider_id)
        if row is None:
            raise HTTPException(404, "Provider not found")
        db.delete(row)
