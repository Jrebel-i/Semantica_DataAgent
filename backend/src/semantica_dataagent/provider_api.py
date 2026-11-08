"""Model provider configuration with encrypted API keys."""

import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .agent_runner import ProviderConfig, list_agents
from .api_time import UtcDatetime
from .auth import admin, current_user
from .crypto import decrypt, encrypt
from .db import Provider, sessions

router = APIRouter()
PROVIDER_TYPES = {
    "anthropic-messages",
    "openai-completions",
    "openai-responses",
    "google-generative-ai",
}
EFFORT_LEVELS = ("low", "medium", "high", "extra", "max")
EffortLevel = Literal["low", "medium", "high", "extra", "max"]
DESCRIPTION_LIMIT = 200


class ModelCapability(BaseModel):
    description: str = Field(default="", max_length=DESCRIPTION_LIMIT)
    vision: bool = False
    effort_levels: list[EffortLevel] = Field(default_factory=list)


class ProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200, pattern=r"\S")
    type: str = "anthropic-messages"
    base_url: str
    api_key: str | None = Field(default=None, max_length=10000)
    models: list[str] = Field(min_length=1)
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


class ProviderView(BaseModel):
    id: str
    name: str
    type: str
    base_url: str
    models: list[str]
    model_capabilities: dict[str, ModelCapability]
    has_api_key: bool
    created_at: UtcDatetime
    updated_at: UtcDatetime


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


def _validate_models(values: list[str]) -> list[str]:
    models: list[str] = []
    for item in values:
        item = item.strip()
        if not item or len(item) > 200:
            raise HTTPException(422, "Invalid model id")
        if item not in models:
            models.append(item)
    if not models:
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
    return ProviderView(
        id=row.id,
        name=row.name,
        type=row.type,
        base_url=row.base_url,
        models=models,
        model_capabilities=_capabilities_view(row, models),
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


@router.post("/api/v1/providers", response_model=ProviderView, status_code=201)
def create_provider(body: ProviderCreate, _: Annotated[str, Depends(admin)]):
    now = datetime.now(UTC)
    models = _validate_models(body.models)
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
            row.base_url = _validate_address(body.base_url)
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
