"""Delegate a complete warehouse question to the online agent over MCP."""

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, text

from .agent_runner import DONE, ERROR, AgentEvent, AgentTurnRequest, get_runner
from .auth import issue_agent_credential
from .conversation_api import (
    _finish_turn,
    _record_event,
    agent_root,
    cleanup_expired_sessions,
    public_base_url,
)
from .db import Conversation, Provider, Setting, Turn, sessions
from .mcp_context import current_requester
from .mcp_contract import ToolInvalidInput, ToolUnavailable
from .provider_api import provider_config

DEFAULT_TIMEOUT_SECONDS = 300.0
SETTING_KEYS = {
    "assistant_agent": "DATAAGENT_ASK_AGENT",
    "assistant_provider_id": "DATAAGENT_ASK_PROVIDER_ID",
    "assistant_model": "DATAAGENT_ASK_MODEL",
    "assistant_timeout_seconds": "DATAAGENT_ASK_TIMEOUT_SECONDS",
}


def _configured(key: str) -> str | None:
    with sessions()() as db:
        row = db.get(Setting, key)
    value = row.value if row is not None else None
    if value:
        return value
    return os.environ.get(SETTING_KEYS[key]) or None


def _timeout() -> float:
    raw = _configured("assistant_timeout_seconds")
    if raw is None:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        value = 0
    if not 0.1 <= value <= 3600:
        raise RuntimeError("assistant_timeout_seconds must be between 0.1 and 3600")
    return value


def _choices() -> tuple[str, str, str]:
    agent_id = _configured("assistant_agent") or "pi"
    provider_id = _configured("assistant_provider_id")
    model = _configured("assistant_model")
    with sessions()() as db:
        provider = db.get(Provider, provider_id) if provider_id else None
        if provider is None and provider_id is None:
            provider = db.query(Provider).order_by(Provider.name, Provider.id).first()
        if provider is None:
            raise RuntimeError("The default assistant provider is not configured")
        models = json.loads(provider.models)
        if not model:
            model = models[0]
        if model not in models:
            raise RuntimeError("The default assistant model is not configured")
        return agent_id, provider.id, model


def _start_turn(
    question: str,
    conversation_id: str | None,
    owner_id: str,
    project_id: str,
):
    now = datetime.now(UTC)
    cleanup_expired_sessions()
    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        if conversation_id:
            conversation = db.get(Conversation, conversation_id)
            if conversation is None or conversation.owner_id != owner_id:
                raise ValueError("Conversation not found")
            if conversation.project_id != project_id:
                raise ToolInvalidInput("对话已绑定其它项目，请新建对话")
            if not conversation.resumable:
                raise ValueError("Conversation session expired and cannot be resumed")
            agent_id = conversation.agent
            provider_id = conversation.provider_id
            model = conversation.model
            sequence = (
                db.query(func.max(Turn.sequence))
                .filter(Turn.conversation_id == conversation.id)
                .scalar()
                or 0
            ) + 1
        else:
            agent_id, provider_id, model = _choices()
            conversation = Conversation(
                id=uuid.uuid4().hex,
                owner_id=owner_id,
                project_id=project_id,
                title=question.strip()[:100],
                agent=agent_id,
                provider_id=provider_id,
                model=model,
                created_at=now,
                updated_at=now,
            )
            db.add(conversation)
            db.flush()
            sequence = 1
        provider = db.get(Provider, provider_id)
        if provider is None:
            raise RuntimeError("The conversation provider no longer exists")
        models = json.loads(provider.models)
        if model not in models:
            raise RuntimeError("The conversation model is no longer configured")
        runner = get_runner(agent_id)
        if runner is None:
            raise RuntimeError("The conversation agent is no longer available")
        if not runner.available():
            raise RuntimeError("The online agent runtime is unavailable")
        conversation.updated_at = now
        turn = Turn(
            conversation_id=conversation.id,
            provider_id=provider_id,
            model=model,
            sequence=sequence,
            question=question.strip(),
            status="running",
            answer="",
            sql="[]",
            citations="[]",
            started_at=now,
        )
        db.add(turn)
        db.flush()
        return {
            "conversation_id": conversation.id,
            "turn_id": turn.id,
            "agent": agent_id,
            "provider": provider_config(provider),
            "model": model,
            "runner": runner,
            "project_id": project_id,
        }


async def _run_turn(context: dict, question: str, timeout: float) -> dict:
    request = AgentTurnRequest(
        conversation_id=context["conversation_id"],
        prompt=question.strip(),
        session_id=context["conversation_id"],
        workspace=agent_root() / context["conversation_id"],
        provider=context["provider"],
        model=context["model"],
        mcp_url=public_base_url() + "/mcp/",
        mcp_token=issue_agent_credential(
            current_requester().id,
            context["conversation_id"],
            context["turn_id"],
            project_id=context["project_id"],
        ),
        turn_id=context["turn_id"],
    )
    seq = 0
    terminal = False
    try:
        async with asyncio.timeout(timeout):
            async for event in context["runner"].run(request):
                _record_event(context["turn_id"], seq, event)
                seq += 1
                if event.type == DONE:
                    terminal = True
                    _finish_turn(context["turn_id"], "completed", event)
                    return {
                        "status": "completed",
                        "conversation_id": context["conversation_id"],
                        "turn_id": context["turn_id"],
                        "answer": event.data.get("text", ""),
                        "sql": event.data.get("sql", []),
                        "citations": event.data.get("citations", []),
                    }
                if event.type == ERROR:
                    terminal = True
                    message = event.data.get("message", "Agent failed")
                    _finish_turn(context["turn_id"], "failed", event, message)
                    raise RuntimeError(message)
        terminal = True
        message = "Agent ended without a final event"
        _finish_turn(context["turn_id"], "failed", None, message)
        raise RuntimeError(message)
    except TimeoutError:
        message = f"Online agent did not finish within {timeout:g} seconds"
        event = AgentEvent(ERROR, {"message": message})
        _record_event(context["turn_id"], seq, event)
        _finish_turn(context["turn_id"], "failed", event, message)
        raise ToolUnavailable(
            message,
            conversation_id=context["conversation_id"],
            turn_id=context["turn_id"],
        ) from None
    except Exception as exc:
        if not terminal:
            _finish_turn(
                context["turn_id"],
                "failed",
                None,
                str(exc) or type(exc).__name__,
            )
        raise


async def ask_dataagent(
    question: str,
    conversation_id: str | None = None,
    project_id: str | None = None,
) -> dict:
    """Ask the configured online agent and wait for its final answer.

    Reuse ``conversation_id`` to continue the same conversation and agent
    session.  The conversation belongs to the MCP caller's token and stays
    bound to the project resolved for the MCP request.  A timeout is reported
    as ``unavailable`` together with that conversation id so the caller can
    continue instead of losing the work.
    """
    if not question or not question.strip():
        raise ValueError("question is required")
    from .project_context import DEFAULT_PROJECT_ID

    requester = current_requester()
    timeout = _timeout()
    context = _start_turn(
        question,
        conversation_id,
        requester.id,
        project_id or DEFAULT_PROJECT_ID,
    )
    return await _run_turn(context, question, timeout)
