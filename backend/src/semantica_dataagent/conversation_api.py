"""Conversation REST + SSE endpoints for the online agent."""

import asyncio
import json
import logging
import os
import shutil
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, text

from .agent_runner import DONE, ERROR, AgentEvent, AgentTurnRequest, get_runner
from .api_time import UtcDatetime
from .auth import Requester, current_requester
from .db import Conversation, Provider, Setting, Turn, TurnEvent, sessions
from .provider_api import provider_config

router = APIRouter()
logger = logging.getLogger(__name__)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=20000, pattern=r"\S")
    agent: str | None = None
    provider_id: str | None = None
    model: str | None = None
    conversation_id: str | None = None


class EventView(BaseModel):
    seq: int
    type: str
    data: dict


class TurnView(BaseModel):
    id: int
    sequence: int
    question: str
    status: str
    answer: str
    sql: list[dict]
    citations: list[dict]
    error: str | None
    started_at: UtcDatetime
    finished_at: UtcDatetime | None
    events: list[EventView] = []


class ConversationView(BaseModel):
    id: str
    title: str
    agent: str
    provider_id: str
    model: str
    created_at: UtcDatetime
    updated_at: UtcDatetime
    turn_count: int
    resumable: bool


class ConversationDetail(ConversationView):
    turns: list[TurnView]


def agent_root() -> Path:
    configured = os.environ.get("DATAAGENT_AGENT_ROOT")
    if configured:
        return Path(configured)
    database = Path(os.environ["DATAAGENT_DB_PATH"])
    return database.parent / "agent-workspaces"


def cleanup_expired_sessions() -> None:
    """Serialize cleanup with turn creation so active work cannot be removed."""
    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        setting = db.get(Setting, "session_retention_days")
        cutoff = datetime.now(UTC) - timedelta(
            days=int(setting.value) if setting else 30
        )
        active = db.query(Turn.conversation_id).filter(Turn.status == "running")
        rows = (
            db.query(Conversation)
            .filter(
                Conversation.resumable.is_(True),
                Conversation.updated_at < cutoff,
                ~Conversation.id.in_(active),
            )
            .all()
        )
        for row in rows:
            workspace = agent_root() / row.id
            if workspace.exists():
                shutil.rmtree(workspace)
            row.resumable = False


def public_base_url() -> str:
    configured = os.environ.get("DATAAGENT_PUBLIC_URL")
    if configured:
        return configured.rstrip("/")
    listen = os.environ.get("DATAAGENT_LISTEN", "127.0.0.1:8000")
    host, _, port = listen.rpartition(":")
    if host in {"", "0.0.0.0", "::", "[::]"}:
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def _summarize(value, depth: int = 0):
    if depth > 4:
        return "…"
    if isinstance(value, str):
        return value if len(value) <= 4000 else value[:3999] + "…"
    if isinstance(value, list):
        return [_summarize(item, depth + 1) for item in value[:100]]
    if isinstance(value, dict):
        return {str(key): _summarize(item, depth + 1) for key, item in value.items()}
    return value


def _event(row: TurnEvent) -> EventView:
    return EventView(seq=row.seq, type=row.type, data=json.loads(row.data))


def _turn(row: Turn, events: list[TurnEvent]) -> TurnView:
    return TurnView(
        id=row.id,
        sequence=row.sequence,
        question=row.question,
        status=row.status,
        answer=row.answer,
        sql=json.loads(row.sql),
        citations=json.loads(row.citations),
        error=row.error,
        started_at=row.started_at,
        finished_at=row.finished_at,
        events=[_event(item) for item in events],
    )


def _view(row: Conversation, turn_count: int) -> ConversationView:
    return ConversationView(
        id=row.id,
        title=row.title,
        agent=row.agent,
        provider_id=row.provider_id,
        model=row.model,
        created_at=row.created_at,
        updated_at=row.updated_at,
        turn_count=turn_count,
        resumable=row.resumable,
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _record_event(turn_id: int, seq: int, event: AgentEvent) -> None:
    with sessions().begin() as db:
        db.add(
            TurnEvent(
                turn_id=turn_id,
                seq=seq,
                type=event.type,
                data=json.dumps(_summarize(event.data), ensure_ascii=False),
            )
        )


def _finish_turn(turn_id: int, status: str, event: AgentEvent | None, error=None):
    with sessions().begin() as db:
        row = db.get(Turn, turn_id)
        if row is None:
            return
        row.status = status
        row.finished_at = datetime.now(UTC)
        if error is not None:
            row.error = error
        if event is not None and event.type == DONE:
            row.answer = event.data.get("text", "")
            row.sql = json.dumps(event.data.get("sql", []), ensure_ascii=False)
            row.citations = json.dumps(
                event.data.get("citations", []), ensure_ascii=False
            )
        conversation = db.get(Conversation, row.conversation_id)
        if conversation is not None:
            conversation.updated_at = datetime.now(UTC)


@router.get("/api/v1/conversations", response_model=list[ConversationView])
def conversations(requester: Annotated[Requester, Depends(current_requester)]):
    cleanup_expired_sessions()
    with sessions()() as db:
        rows = (
            db.query(Conversation)
            .filter(Conversation.owner_id == requester.id)
            .order_by(Conversation.updated_at.desc(), Conversation.id)
            .all()
        )
        counts = (
            dict(
                db.query(Turn.conversation_id, func.count(Turn.id))
                .filter(Turn.conversation_id.in_([row.id for row in rows]))
                .group_by(Turn.conversation_id)
                .all()
            )
            if rows
            else {}
        )
        return [_view(row, counts.get(row.id, 0)) for row in rows]


@router.get(
    "/api/v1/conversations/{conversation_id}", response_model=ConversationDetail
)
def conversation_detail(
    conversation_id: str, requester: Annotated[Requester, Depends(current_requester)]
):
    cleanup_expired_sessions()
    with sessions()() as db:
        row = db.get(Conversation, conversation_id)
        if row is None or row.owner_id != requester.id:
            raise HTTPException(404, "Conversation not found")
        turns = (
            db.query(Turn)
            .filter(Turn.conversation_id == conversation_id)
            .order_by(Turn.sequence)
            .all()
        )
        events = (
            (
                db.query(TurnEvent)
                .filter(TurnEvent.turn_id.in_([turn.id for turn in turns]))
                .order_by(TurnEvent.turn_id, TurnEvent.seq)
                .all()
            )
            if turns
            else []
        )
        by_turn: dict[int, list[TurnEvent]] = {turn.id: [] for turn in turns}
        for event in events:
            by_turn[event.turn_id].append(event)
        return ConversationDetail(
            **_view(row, len(turns)).model_dump(),
            turns=[_turn(turn, by_turn[turn.id]) for turn in turns],
        )


@router.post("/api/v1/conversations/{conversation_id}/end", status_code=204)
async def end_conversation(
    conversation_id: str, requester: Annotated[Requester, Depends(current_requester)]
):
    from .agent_sandbox import end_session

    with sessions()() as db:
        row = db.get(Conversation, conversation_id)
        if row is None or row.owner_id != requester.id:
            raise HTTPException(404, "Conversation not found")
    await asyncio.to_thread(end_session, conversation_id)


@router.post("/api/v1/conversations/ask")
async def ask(
    body: AskRequest,
    request: Request,
    requester: Annotated[Requester, Depends(current_requester)],
):
    now = datetime.now(UTC)
    cleanup_expired_sessions()
    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        if body.conversation_id:
            conversation = db.get(Conversation, body.conversation_id)
            if conversation is None or conversation.owner_id != requester.id:
                raise HTTPException(404, "Conversation not found")
            if not conversation.resumable:
                raise HTTPException(
                    409, "Conversation session expired and cannot be resumed"
                )
            if any(
                requested is not None and requested != saved
                for requested, saved in (
                    (body.agent, conversation.agent),
                    (body.provider_id, conversation.provider_id),
                    (body.model, conversation.model),
                )
            ):
                raise HTTPException(
                    409, "Start a new conversation to change agent or model"
                )
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
            if not body.provider_id or not body.model:
                raise HTTPException(422, "provider_id and model are required")
            agent_id = body.agent or "pi"
            provider_id = body.provider_id
            model = body.model
            conversation = Conversation(
                id=uuid.uuid4().hex,
                owner_id=requester.id,
                title=body.question.strip()[:100],
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
            raise HTTPException(404, "Provider not found")
        if model not in json.loads(provider.models):
            raise HTTPException(422, "Model is not configured for this provider")
        runner = get_runner(agent_id)
        if runner is None:
            raise HTTPException(422, "Unknown agent")
        if not runner.available():
            raise HTTPException(503, "Agent runtime is not installed")
        conversation.agent = agent_id
        conversation.provider_id = provider_id
        conversation.model = model
        conversation.updated_at = now
        turn = Turn(
            conversation_id=conversation.id,
            sequence=sequence,
            question=body.question.strip(),
            status="running",
            answer="",
            sql="[]",
            citations="[]",
            started_at=now,
        )
        db.add(turn)
        db.flush()
        turn_id = turn.id
        conversation_id = conversation.id
        provider_settings = provider_config(provider)

    workspace = agent_root() / conversation_id
    run_request = AgentTurnRequest(
        conversation_id=conversation_id,
        prompt=body.question.strip(),
        session_id=conversation_id,
        workspace=workspace,
        provider=provider_settings,
        model=model,
        mcp_url=public_base_url() + "/mcp/",
        mcp_token=requester.token,
        turn_id=turn_id,
    )
    return StreamingResponse(
        _stream(run_request, runner, turn_id, conversation_id, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _stream(
    run_request: AgentTurnRequest,
    runner,
    turn_id: int,
    conversation_id: str,
    request: Request,
) -> AsyncIterator[str]:
    yield _sse(
        "conversation",
        {"conversation_id": conversation_id, "turn_id": turn_id},
    )
    from .telemetry import submit, trace_id

    observation_id = trace_id(conversation_id, turn_id)
    secrets = (run_request.mcp_token, run_request.provider.api_key or "")
    trace = {
        "id": observation_id,
        "name": "online-conversation",
        "input": run_request.prompt,
        "metadata": {
            "agent": runner.agent_id,
            "provider": run_request.provider.name,
            "model": run_request.model,
            "source": "online",
        },
    }
    submit("trace-create", trace, secrets)
    seq = 0
    terminal = False
    try:
        async for event in runner.run(run_request):
            if await request.is_disconnected():
                break
            _record_event(turn_id, seq, event)
            seq += 1
            payload = {"conversation_id": conversation_id, "turn_id": turn_id}
            payload.update(event.data)
            yield _sse(event.type, payload)
            if event.type == DONE:
                submit(
                    "trace-create",
                    {**trace, "output": event.data.get("text", "")},
                    secrets,
                )
                _finish_turn(turn_id, "completed", event)
                terminal = True
            elif event.type == ERROR:
                _finish_turn(turn_id, "failed", event, event.data.get("message"))
                terminal = True
        if not terminal:
            message = "Agent ended without a final event"
            event = AgentEvent(ERROR, {"message": message})
            _record_event(turn_id, seq, event)
            _finish_turn(turn_id, "failed", event, message)
            terminal = True
            yield _sse(
                ERROR,
                {
                    "conversation_id": conversation_id,
                    "turn_id": turn_id,
                    "message": message,
                },
            )
    except Exception as exc:  # stream must report, not crash silently
        logger.exception("Agent stream failed for conversation %s", conversation_id)
        message = f"Agent failed: {type(exc).__name__}"
        event = AgentEvent(ERROR, {"message": message})
        _record_event(turn_id, seq, event)
        _finish_turn(turn_id, "failed", event, message)
        terminal = True
        yield _sse(
            ERROR,
            {
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                "message": message,
            },
        )
    finally:
        if not terminal:
            _finish_turn(turn_id, "failed", None, "Client disconnected")
