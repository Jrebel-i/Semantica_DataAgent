"""Per-request identity shared by MCP tools.

The MCP transport authenticates the bearer token in middleware.  Tools run
inside that middleware's context, so they can use the same caller identity and,
for requests made by an online agent, the conversation that owns the turn.

The middleware also resolves one project for the request.  Credentials issued
to an online agent carry the project they are locked to; ordinary tokens may
select a project in the query string or the ``X-Dataagent-Project`` header.
The resolved id (or a safe tool error when selection is ambiguous) is bound
together with the identity so every tool reads the same project.
"""

import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock

from .auth import Requester
from .db import digest
from .project_context import DEFAULT_PROJECT_ID

CONVERSATION_HEADER = "x-dataagent-conversation-id"
TURN_HEADER = "x-dataagent-turn-id"
PROJECT_HEADER = "x-dataagent-project"
PROJECT_QUERY = "project"

_requester: ContextVar[Requester | None] = ContextVar("mcp_requester", default=None)
_conversation_id: ContextVar[str | None] = ContextVar(
    "mcp_conversation_id", default=None
)


_turn_id: ContextVar[str | None] = ContextVar("mcp_turn_id", default=None)
_project_id: ContextVar[str | None] = ContextVar("mcp_project_id", default=None)
_project_error: ContextVar[str | None] = ContextVar("mcp_project_error", default=None)
_project_error_status: ContextVar[int | None] = ContextVar(
    "mcp_project_error_status", default=None
)
_suppress_trajectory: ContextVar[bool] = ContextVar(
    "mcp_suppress_trajectory", default=False
)


@contextmanager
def bind_mcp_context(
    requester: Requester,
    conversation_id: str | None = None,
    turn_id: str | None = None,
    *,
    project_id: str | None = None,
    project_error: str | None = None,
    project_error_status: int | None = None,
    suppress_trajectory: bool = False,
):
    requester_token = _requester.set(requester)
    conversation_token = _conversation_id.set(conversation_id or None)
    turn_token = _turn_id.set(turn_id)
    project_token = _project_id.set(project_id or None)
    project_error_token = _project_error.set(project_error or None)
    project_error_status_token = _project_error_status.set(project_error_status)
    suppress_token = _suppress_trajectory.set(suppress_trajectory)
    try:
        yield
    finally:
        _suppress_trajectory.reset(suppress_token)
        _project_error_status.reset(project_error_status_token)
        _project_error.reset(project_error_token)
        _project_id.reset(project_token)
        _turn_id.reset(turn_token)
        _conversation_id.reset(conversation_token)
        _requester.reset(requester_token)


def current_requester() -> Requester:
    requester = _requester.get()
    if requester is None:
        raise RuntimeError("MCP request identity is unavailable")
    return requester


def current_conversation_id() -> str | None:
    return _conversation_id.get()


def current_turn_id() -> str | None:
    return _turn_id.get()


def current_project_id() -> str:
    """Return the project resolved for this request.

    Direct tool calls in tests and internal helpers that run outside the MCP
    middleware keep the historical default project.  A middleware resolution
    failure is raised as a tool error instead of silently falling back, so an
    ambiguous or forbidden selection can never read the default project.
    """
    error = _project_error.get()
    if error:
        from .mcp_contract import ToolInvalidInput

        raise ToolInvalidInput(error)
    return _project_id.get() or DEFAULT_PROJECT_ID


def current_project_error_status() -> int | None:
    """HTTP-style status for the rejection bound by the MCP middleware."""
    return _project_error_status.get()


def current_suppress_trajectory() -> bool:
    """Whether synthetic/evaluation MCP calls should skip trajectory persistence."""
    return bool(_suppress_trajectory.get())


_eval_lock = Lock()
_eval_bindings: dict[str, tuple[str, str | None, float]] = {}


def _reset_after_fork() -> None:
    global _eval_lock
    _eval_lock = Lock()
    _eval_bindings.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


def register_eval_token(
    token: str,
    project_id: str,
    generation: str | None = None,
    *,
    ttl_seconds: float = 86400,
) -> None:
    """Bind an ephemeral evaluation bearer token to a target project and generation."""
    token_digest = digest(token)
    with _eval_lock:
        _eval_bindings[token_digest] = (
            project_id,
            generation,
            time.monotonic() + ttl_seconds,
        )


def unregister_eval_token(token: str) -> None:
    """Remove an ephemeral evaluation token binding."""
    token_digest = digest(token)
    with _eval_lock:
        _eval_bindings.pop(token_digest, None)


def resolve_eval_binding(token: str | None) -> tuple[str, str | None] | None:
    """Return ``(project_id, generation)`` when ``token`` is a registered eval credential."""
    if not token:
        return None
    token_digest = digest(token)
    with _eval_lock:
        binding = _eval_bindings.get(token_digest)
        if binding is None:
            return None
        if binding[2] <= time.monotonic():
            _eval_bindings.pop(token_digest, None)
            return None
        return binding[:2]


def current_project_context():
    from . import project_context

    return project_context.current_project(current_project_id())


def current_project_type() -> str:
    """Return the immutable type of the project resolved for this request."""
    from . import project_context
    from .project_types import WAREHOUSE

    project_id = current_project_id()
    if project_id == project_context.DEFAULT_PROJECT_ID:
        return WAREHOUSE
    return project_context.project_type(project_id)
