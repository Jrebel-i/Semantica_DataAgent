"""Per-request identity shared by MCP tools.

The MCP transport authenticates the bearer token in middleware.  Tools run
inside that middleware's context, so they can use the same caller identity and,
for requests made by an online agent, the conversation that owns the turn.
"""

from contextlib import contextmanager
from contextvars import ContextVar

from .auth import Requester

CONVERSATION_HEADER = "x-dataagent-conversation-id"

_requester: ContextVar[Requester | None] = ContextVar("mcp_requester", default=None)
_conversation_id: ContextVar[str | None] = ContextVar(
    "mcp_conversation_id", default=None
)


@contextmanager
def bind_mcp_context(requester: Requester, conversation_id: str | None = None):
    requester_token = _requester.set(requester)
    conversation_token = _conversation_id.set(conversation_id or None)
    try:
        yield
    finally:
        _conversation_id.reset(conversation_token)
        _requester.reset(requester_token)


def current_requester() -> Requester:
    requester = _requester.get()
    if requester is None:
        raise RuntimeError("MCP request identity is unavailable")
    return requester


def current_conversation_id() -> str | None:
    return _conversation_id.get()
