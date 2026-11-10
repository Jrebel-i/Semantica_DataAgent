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

from contextlib import contextmanager
from contextvars import ContextVar

from .auth import Requester
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


@contextmanager
def bind_mcp_context(
    requester: Requester,
    conversation_id: str | None = None,
    turn_id: str | None = None,
    *,
    project_id: str | None = None,
    project_error: str | None = None,
    project_error_status: int | None = None,
):
    requester_token = _requester.set(requester)
    conversation_token = _conversation_id.set(conversation_id or None)
    turn_token = _turn_id.set(turn_id)
    project_token = _project_id.set(project_id or None)
    project_error_token = _project_error.set(project_error or None)
    project_error_status_token = _project_error_status.set(project_error_status)
    try:
        yield
    finally:
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
