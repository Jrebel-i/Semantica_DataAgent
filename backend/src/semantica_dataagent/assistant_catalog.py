"""Published assistant catalog, personal agent defaults, and unified model resolution (#329)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from .agent_profile_api import _catalog_set, _parse_model_map
from .agent_runner import get_runner
from .agent_runner_api import is_runner_available
from .api_time import UtcDatetime
from .auth import Requester, current_requester
from .db import (
    AgentProfile,
    AgentRunnerRecord,
    Conversation,
    Provider,
    UserAgentDefault,
    sessions,
)
from .provider_api import EffortLevel, ModelCapability, _capabilities_view

router = APIRouter()

_SEEDED_PROFILE_IDS = frozenset({"pi", "claude", "codex"})


class AssistantResolutionError(Exception):
    """Domain error raised when resolving an agent profile / provider / model."""

    def __init__(
        self, status_code: int, message: str, *, code: str = "invalid"
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ResolvedAssistantTarget:
    agent_profile_id: str | None
    agent_id: str
    provider: Provider
    model: str
    runner: Any
    runner_available: bool
    catalog_governed: bool


class AgentModelPair(BaseModel):
    provider_id: str = Field(min_length=1, max_length=200, pattern=r"\S")
    model: str = Field(min_length=1, max_length=500, pattern=r"\S")


class PublishedProviderView(BaseModel):
    id: str
    name: str
    type: str
    models: list[str]
    model_capabilities: dict[str, ModelCapability]


class PublishedAgentView(BaseModel):
    id: str
    name: str
    runner_id: str
    is_system_default: bool
    default_provider_id: str | None
    default_model: str | None
    effort_levels: list[EffortLevel] = Field(default_factory=list)
    supports_skills: bool = False
    providers: list[PublishedProviderView]


class AssistantCatalogView(BaseModel):
    system_default_profile_id: str | None
    agents: list[PublishedAgentView]
    personal_defaults: dict[str, AgentModelPair]


class UserAgentDefaultView(BaseModel):
    agent_profile_id: str
    provider_id: str
    model: str
    updated_at: UtcDatetime


def resolve_runner_adapter(db, runner_id: str):
    """Return the registered runner adapter for a runner_id (or adapter_id)."""
    record = db.get(AgentRunnerRecord, runner_id)
    adapter_id = record.adapter_id if record is not None else runner_id
    return get_runner(adapter_id)


def _parse_provider_models(provider: Provider) -> list[str]:
    try:
        parsed = json.loads(provider.models or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, str) and item]


def _published_provider_entries(
    profile: AgentProfile,
    providers_by_id: dict[str, Provider],
) -> list[tuple[Provider, list[str]]]:
    """Return ordered (Provider, valid_catalog_models) published on this profile."""
    selected_map = _parse_model_map(profile.provider_models)
    entries: list[tuple[Provider, list[str]]] = []
    ordered_providers = sorted(providers_by_id.values(), key=lambda p: (p.name, p.id))
    for provider in ordered_providers:
        selected = selected_map.get(provider.id)
        if not selected:
            continue
        catalog = _catalog_set(provider)
        if catalog is None:
            continue
        provider_models = set(_parse_provider_models(provider))
        valid = [m for m in selected if m in catalog and m in provider_models]
        if valid:
            entries.append((provider, valid))
    return entries


def _published_pairs_set(
    entries: list[tuple[Provider, list[str]]],
) -> set[tuple[str, str]]:
    return {(provider.id, model) for provider, models in entries for model in models}


def _require_published_pair(
    db,
    profile: AgentProfile,
    provider_id: str,
    model: str,
    providers_by_id: dict[str, Provider],
) -> None:
    if not profile.enabled:
        raise HTTPException(422, "Agent profile is disabled")
    if not is_runner_available(db, profile.runner_id):
        raise HTTPException(422, "Agent runner is not available")
    entries = _published_provider_entries(profile, providers_by_id)
    pairs = _published_pairs_set(entries)
    if (provider_id, model) not in pairs:
        raise HTTPException(
            422,
            "Selected provider and model are not published for this agent profile",
        )


def is_catalog_governed(db, *, explicit_profile_id: str | None = None) -> bool:
    """Return True when catalog/profile governance is active instead of unconfigured legacy seed."""
    if explicit_profile_id is not None:
        return True
    if db.query(UserAgentDefault.owner_id).first() is not None:
        return True
    if (
        db.query(Provider.id).filter(Provider.catalog_models.is_not(None)).first()
        is not None
    ):
        return True
    for profile in db.query(AgentProfile).all():
        if profile.id not in _SEEDED_PROFILE_IDS:
            return True
        if not profile.enabled:
            return True
        if profile.default_provider_id is not None or profile.default_model is not None:
            return True
        if (profile.provider_models or "{}") != "{}":
            return True
        if (profile.pending_verification_models or "{}") != "{}":
            return True
        if (profile.version or 1) > 1:
            return True
        if profile.id == "pi" and not profile.is_system_default:
            return True
        if profile.id != "pi" and profile.is_system_default:
            return True
    return False


def _find_profile_by_agent(db, agent_id: str) -> AgentProfile | None:
    sys_default = (
        db.query(AgentProfile).filter(AgentProfile.is_system_default.is_(True)).first()
    )
    if (
        sys_default is not None
        and sys_default.runner_id == agent_id
        and sys_default.enabled
    ):
        return sys_default
    by_id = db.get(AgentProfile, agent_id)
    if by_id is not None and by_id.runner_id == agent_id and by_id.enabled:
        return by_id
    enabled = (
        db.query(AgentProfile)
        .filter(
            AgentProfile.runner_id == agent_id,
            AgentProfile.enabled.is_(True),
        )
        .order_by(AgentProfile.name, AgentProfile.id)
        .first()
    )
    if enabled is not None:
        return enabled
    if by_id is not None and by_id.runner_id == agent_id:
        return by_id
    return (
        db.query(AgentProfile)
        .filter(AgentProfile.runner_id == agent_id)
        .order_by(AgentProfile.name, AgentProfile.id)
        .first()
    )


def preview_runner_for_request(
    db,
    *,
    conversation: Conversation | None = None,
    agent_profile_id: str | None = None,
    agent: str | None = None,
):
    """Resolve the runner before locking the DB transaction for skill mounting."""
    clean_pid = agent_profile_id.strip() if isinstance(agent_profile_id, str) else None
    clean_pid = clean_pid or None
    clean_agent = agent.strip() if isinstance(agent, str) else None
    clean_agent = clean_agent or None

    if conversation is not None:
        if clean_pid is not None:
            if conversation.agent_profile_id is not None:
                if clean_pid != conversation.agent_profile_id:
                    raise AssistantResolutionError(
                        409, "Start a new conversation to change agent"
                    )
            else:
                candidate = db.get(AgentProfile, clean_pid)
                if candidate is None:
                    raise AssistantResolutionError(422, "Unknown agent profile")
                if candidate.runner_id != conversation.agent:
                    raise AssistantResolutionError(
                        409, "Start a new conversation to change agent"
                    )
        if clean_agent is not None and clean_agent != conversation.agent:
            raise AssistantResolutionError(
                409, "Start a new conversation to change agent"
            )
        agent_id = conversation.agent
        runner = resolve_runner_adapter(db, agent_id)
        if runner is None:
            raise AssistantResolutionError(422, "Unknown agent")
        return agent_id, runner

    if clean_pid is not None:
        profile = db.get(AgentProfile, clean_pid)
        if profile is None:
            raise AssistantResolutionError(422, "Unknown agent profile")
        if clean_agent is not None and clean_agent != profile.runner_id:
            raise AssistantResolutionError(422, "agent does not match agent_profile_id")
        agent_id = profile.runner_id
        runner = resolve_runner_adapter(db, agent_id)
        if runner is None:
            raise AssistantResolutionError(422, "Unknown agent")
        return agent_id, runner

    if clean_agent is not None:
        runner = resolve_runner_adapter(db, clean_agent)
        if runner is None:
            raise AssistantResolutionError(422, "Unknown agent")
        return clean_agent, runner

    if is_catalog_governed(db):
        profile = (
            db.query(AgentProfile)
            .filter(AgentProfile.is_system_default.is_(True))
            .first()
        )
        if profile is None:
            raise AssistantResolutionError(
                422, "No system default agent profile is configured"
            )
        runner = resolve_runner_adapter(db, profile.runner_id)
        if runner is None:
            raise AssistantResolutionError(422, "Unknown agent")
        return profile.runner_id, runner

    runner = resolve_runner_adapter(db, "pi")
    if runner is None:
        raise AssistantResolutionError(422, "Unknown agent")
    return "pi", runner


def resolve_assistant_target(
    db,
    *,
    owner_id: str | None,
    conversation: Conversation | None = None,
    agent_profile_id: str | None = None,
    agent: str | None = None,
    provider_id: str | None = None,
    model: str | None = None,
    legacy_allowed_providers: Callable[[Any, str], set[str] | None] | None = None,
    check_runner_available: bool = True,
) -> ResolvedAssistantTarget:
    """Resolve agent profile, runner, provider, and model using one shared policy.

    Order for new targets: Explicit choice -> Valid personal default -> Profile company default.
    For resumed conversations without explicit replacement, re-validates the bound pair.
    """
    clean_pid = agent_profile_id.strip() if isinstance(agent_profile_id, str) else None
    clean_pid = clean_pid or None
    clean_agent = agent.strip() if isinstance(agent, str) else None
    clean_agent = clean_agent or None
    clean_provider = provider_id.strip() if isinstance(provider_id, str) else None
    clean_provider = clean_provider or None
    clean_model = model.strip() if isinstance(model, str) else None
    clean_model = clean_model or None

    governed = is_catalog_governed(db, explicit_profile_id=clean_pid)

    if conversation is not None:
        if clean_pid is not None:
            if conversation.agent_profile_id is not None:
                if clean_pid != conversation.agent_profile_id:
                    raise AssistantResolutionError(
                        409, "Start a new conversation to change agent"
                    )
            else:
                candidate = db.get(AgentProfile, clean_pid)
                if candidate is None:
                    raise AssistantResolutionError(422, "Unknown agent profile")
                if candidate.runner_id != conversation.agent:
                    raise AssistantResolutionError(
                        409, "Start a new conversation to change agent"
                    )
        if clean_agent is not None and clean_agent != conversation.agent:
            raise AssistantResolutionError(
                409, "Start a new conversation to change agent"
            )

    if not governed:
        return _resolve_legacy_target(
            db,
            conversation=conversation,
            agent=clean_agent,
            provider_id=clean_provider,
            model=clean_model,
            legacy_allowed_providers=legacy_allowed_providers,
            check_runner_available=check_runner_available,
        )

    # Catalog-governed resolution
    if conversation is not None:
        bound_pid = conversation.agent_profile_id or clean_pid
        if bound_pid:
            profile = db.get(AgentProfile, bound_pid)
        else:
            profile = _find_profile_by_agent(db, conversation.agent)
        if profile is None:
            raise AssistantResolutionError(
                422,
                "Bound agent profile no longer exists; please start a new conversation",
                code="profile_missing",
            )
    elif clean_pid is not None:
        profile = db.get(AgentProfile, clean_pid)
        if profile is None:
            raise AssistantResolutionError(
                422, "Unknown agent profile", code="profile_missing"
            )
        if clean_agent is not None and clean_agent != profile.runner_id:
            raise AssistantResolutionError(422, "agent does not match agent_profile_id")
    elif clean_agent is not None:
        profile = _find_profile_by_agent(db, clean_agent)
        if profile is None:
            raise AssistantResolutionError(422, "Unknown agent")
    else:
        profile = (
            db.query(AgentProfile)
            .filter(AgentProfile.is_system_default.is_(True))
            .first()
        )
        if profile is None:
            raise AssistantResolutionError(
                422,
                "No system default agent profile is configured",
                code="no_system_default",
            )

    if not profile.enabled:
        raise AssistantResolutionError(
            422,
            f"Agent profile '{profile.name}' is disabled",
            code="profile_disabled",
        )

    runner = resolve_runner_adapter(db, profile.runner_id)
    if runner is None:
        raise AssistantResolutionError(422, "Unknown agent")
    runner_available = is_runner_available(db, profile.runner_id)
    if check_runner_available and not runner_available:
        raise AssistantResolutionError(
            503,
            "Agent runtime is not installed",
            code="runner_unavailable",
        )

    providers_by_id = {p.id: p for p in db.query(Provider).all()}
    entries = _published_provider_entries(profile, providers_by_id)
    published_pairs = _published_pairs_set(entries)

    if conversation is not None:
        if clean_provider is not None or clean_model is not None:
            chosen_pid = clean_provider or conversation.provider_id
            chosen_model = clean_model or conversation.model
        else:
            chosen_pid = conversation.provider_id
            chosen_model = conversation.model
        if (chosen_pid, chosen_model) not in published_pairs:
            raise AssistantResolutionError(
                422,
                "Selected provider or model is not published for this conversation's agent",
                code="model_unpublished",
            )
    elif clean_provider is not None or clean_model is not None:
        if not clean_provider or not clean_model:
            raise AssistantResolutionError(
                422, "provider_id and model must be specified together"
            )
        chosen_pid, chosen_model = clean_provider, clean_model
        if (chosen_pid, chosen_model) not in published_pairs:
            raise AssistantResolutionError(
                422,
                "Selected provider or model is not published for this agent profile",
                code="model_unpublished",
            )
    else:
        chosen_pid = None
        chosen_model = None
        if owner_id:
            personal = db.get(UserAgentDefault, (owner_id, profile.id))
            if (
                personal is not None
                and (personal.provider_id, personal.model) in published_pairs
            ):
                chosen_pid, chosen_model = personal.provider_id, personal.model
        if (
            chosen_pid is None
            and profile.default_provider_id
            and profile.default_model
            and (profile.default_provider_id, profile.default_model) in published_pairs
        ):
            chosen_pid = profile.default_provider_id
            chosen_model = profile.default_model
        if not chosen_pid or not chosen_model:
            raise AssistantResolutionError(
                422,
                "No valid published default model is configured for this agent profile",
                code="no_default_model",
            )

    provider = providers_by_id[chosen_pid]
    return ResolvedAssistantTarget(
        agent_profile_id=profile.id,
        agent_id=profile.runner_id,
        provider=provider,
        model=chosen_model,
        runner=runner,
        runner_available=runner_available,
        catalog_governed=True,
    )


def _resolve_legacy_target(
    db,
    *,
    conversation: Conversation | None,
    agent: str | None,
    provider_id: str | None,
    model: str | None,
    legacy_allowed_providers: Callable[[Any, str], set[str] | None] | None,
    check_runner_available: bool = True,
) -> ResolvedAssistantTarget:
    if conversation is not None:
        agent_id = conversation.agent
        chosen_pid = provider_id or conversation.provider_id
        chosen_model = model or conversation.model
        bound_profile_id = conversation.agent_profile_id
    else:
        if not provider_id or not model:
            raise AssistantResolutionError(422, "provider_id and model are required")
        agent_id = agent or "pi"
        chosen_pid = provider_id
        chosen_model = model
        profile = db.get(AgentProfile, agent_id)
        bound_profile_id = profile.id if profile is not None else None

    provider = db.get(Provider, chosen_pid)
    if provider is None:
        raise AssistantResolutionError(404, "Provider not found")
    if legacy_allowed_providers is not None:
        allowed = legacy_allowed_providers(db, agent_id)
        if allowed is not None and chosen_pid not in allowed:
            raise AssistantResolutionError(
                422, "Provider is not available for this conversation's agent"
            )
    if chosen_model not in _parse_provider_models(provider):
        raise AssistantResolutionError(422, "Model is not configured for this provider")

    runner = resolve_runner_adapter(db, agent_id)
    if runner is None:
        raise AssistantResolutionError(422, "Unknown agent")
    runner_available = bool(runner.available())
    if check_runner_available and not runner_available:
        raise AssistantResolutionError(503, "Agent runtime is not installed")

    return ResolvedAssistantTarget(
        agent_profile_id=bound_profile_id,
        agent_id=agent_id,
        provider=provider,
        model=chosen_model,
        runner=runner,
        runner_available=runner_available,
        catalog_governed=False,
    )


def _build_published_agent_view(
    db,
    profile: AgentProfile,
    providers_by_id: dict[str, Provider],
) -> tuple[PublishedAgentView, set[tuple[str, str]]] | None:
    if not profile.enabled:
        return None
    if not is_runner_available(db, profile.runner_id):
        return None
    entries = _published_provider_entries(profile, providers_by_id)
    if not entries:
        return None
    published_pairs = _published_pairs_set(entries)
    dpid = profile.default_provider_id
    dmodel = profile.default_model
    if not (dpid and dmodel and (dpid, dmodel) in published_pairs):
        dpid, dmodel = None, None
    runner = resolve_runner_adapter(db, profile.runner_id)
    effort_levels = list(getattr(runner, "effort_levels", ()) or ())
    supports_skills = bool(getattr(runner, "supports_skills", False))
    view = PublishedAgentView(
        id=profile.id,
        name=profile.name,
        runner_id=profile.runner_id,
        is_system_default=bool(profile.is_system_default),
        default_provider_id=dpid,
        default_model=dmodel,
        effort_levels=effort_levels,
        supports_skills=supports_skills,
        providers=[
            PublishedProviderView(
                id=provider.id,
                name=provider.name,
                type=provider.type,
                models=valid_models,
                model_capabilities=_capabilities_view(provider, valid_models),
            )
            for provider, valid_models in entries
        ],
    )
    return view, published_pairs


@router.get("/api/v1/assistant-catalog", response_model=AssistantCatalogView)
def get_assistant_catalog(
    requester: Annotated[Requester, Depends(current_requester)],
):
    with sessions()() as db:
        providers_by_id = {p.id: p for p in db.query(Provider).all()}
        profiles = (
            db.query(AgentProfile)
            .order_by(
                AgentProfile.is_system_default.desc(),
                AgentProfile.name,
                AgentProfile.id,
            )
            .all()
        )
        agents: list[PublishedAgentView] = []
        published_by_profile: dict[str, set[tuple[str, str]]] = {}
        system_default_profile_id: str | None = None
        for profile in profiles:
            built = _build_published_agent_view(db, profile, providers_by_id)
            if built is None:
                continue
            view, pairs = built
            agents.append(view)
            published_by_profile[profile.id] = pairs
            if profile.is_system_default:
                system_default_profile_id = profile.id

        personal_rows = (
            db.query(UserAgentDefault)
            .filter(UserAgentDefault.owner_id == requester.id)
            .all()
        )
        personal_defaults: dict[str, AgentModelPair] = {}
        for row in personal_rows:
            pairs = published_by_profile.get(row.agent_profile_id)
            if pairs and (row.provider_id, row.model) in pairs:
                personal_defaults[row.agent_profile_id] = AgentModelPair(
                    provider_id=row.provider_id,
                    model=row.model,
                )

        return AssistantCatalogView(
            system_default_profile_id=system_default_profile_id,
            agents=agents,
            personal_defaults=personal_defaults,
        )


def resolve_system_default_llm(db) -> tuple[Provider, str] | None:
    """Resolve (Provider, model) from the active system default AgentProfile."""
    profile = (
        db.query(AgentProfile).filter(AgentProfile.is_system_default.is_(True)).first()
    )
    if profile is None or not profile.enabled:
        return None
    if not profile.default_provider_id or not profile.default_model:
        return None
    providers_by_id = {p.id: p for p in db.query(Provider).all()}
    entries = _published_provider_entries(profile, providers_by_id)
    pairs = _published_pairs_set(entries)
    if (profile.default_provider_id, profile.default_model) not in pairs:
        return None
    provider = providers_by_id.get(profile.default_provider_id)
    if provider is None:
        return None
    return provider, profile.default_model


@router.get(
    "/api/v1/me/agent-defaults/{agent_profile_id}",
    response_model=UserAgentDefaultView,
)
def get_personal_agent_default(
    agent_profile_id: str,
    requester: Annotated[Requester, Depends(current_requester)],
):
    with sessions()() as db:
        profile = db.get(AgentProfile, agent_profile_id)
        if profile is None:
            raise HTTPException(404, "Agent profile not found")
        row = db.get(UserAgentDefault, (requester.id, agent_profile_id))
        if row is None:
            raise HTTPException(404, "Personal agent default not found")
        providers_by_id = {p.id: p for p in db.query(Provider).all()}
        _require_published_pair(
            db, profile, row.provider_id, row.model, providers_by_id
        )
        return UserAgentDefaultView(
            agent_profile_id=row.agent_profile_id,
            provider_id=row.provider_id,
            model=row.model,
            updated_at=row.updated_at,
        )


@router.put(
    "/api/v1/me/agent-defaults/{agent_profile_id}",
    response_model=UserAgentDefaultView,
)
def put_personal_agent_default(
    agent_profile_id: str,
    body: AgentModelPair,
    requester: Annotated[Requester, Depends(current_requester)],
):
    provider_id = body.provider_id.strip()
    model = body.model.strip()
    now = datetime.now(UTC)
    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        profile = db.get(AgentProfile, agent_profile_id)
        if profile is None:
            raise HTTPException(404, "Agent profile not found")
        providers_by_id = {p.id: p for p in db.query(Provider).all()}
        _require_published_pair(db, profile, provider_id, model, providers_by_id)
        row = db.get(UserAgentDefault, (requester.id, agent_profile_id))
        if row is None:
            row = UserAgentDefault(
                owner_id=requester.id,
                agent_profile_id=agent_profile_id,
                provider_id=provider_id,
                model=model,
                updated_at=now,
            )
            db.add(row)
        else:
            row.provider_id = provider_id
            row.model = model
            row.updated_at = now
        db.flush()
        return UserAgentDefaultView(
            agent_profile_id=row.agent_profile_id,
            provider_id=row.provider_id,
            model=row.model,
            updated_at=row.updated_at,
        )


@router.delete(
    "/api/v1/me/agent-defaults/{agent_profile_id}",
    status_code=204,
)
def delete_personal_agent_default(
    agent_profile_id: str,
    requester: Annotated[Requester, Depends(current_requester)],
):
    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        profile = db.get(AgentProfile, agent_profile_id)
        if profile is None:
            raise HTTPException(404, "Agent profile not found")
        row = db.get(UserAgentDefault, (requester.id, agent_profile_id))
        if row is not None:
            db.delete(row)
