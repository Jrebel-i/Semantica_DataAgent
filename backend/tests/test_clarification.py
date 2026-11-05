"""Clarification and delegated online answers over the MCP boundary."""

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from mcp.types import CallToolRequest, CallToolRequestParams

from semantica_dataagent.agent_runner import DONE, TEXT_DELTA, AgentEvent
from semantica_dataagent.auth import Requester
from semantica_dataagent.db import (
    Clarification,
    Conversation,
    Token,
    Turn,
    digest,
    sessions,
)

SYNTHETIC_QUESTIONS = [
    {
        "question": "销售额按支付口径还是下单口径？",
        "options": [
            {
                "label": "支付销售额（支付成功时间）",
                "ref": "document:metrics/definitions/sales.md#chunk-1",
            },
            {
                "label": "下单销售额（订单创建时间）",
                "ref": "document:metrics/sales_order.md#chunk-1",
            },
        ],
    }
]


@contextmanager
def bound(requester: Requester, conversation_id: str | None = None):
    from semantica_dataagent.mcp_context import bind_mcp_context

    with bind_mcp_context(requester, conversation_id):
        yield


def call(
    server, name: str, arguments: dict, requester: Requester, conversation_id=None
):
    async def run():
        server.get_context = lambda: None
        handler = server._mcp_server.request_handlers[CallToolRequest]
        return (
            await handler(
                CallToolRequest(
                    method="tools/call",
                    params=CallToolRequestParams(name=name, arguments=arguments),
                )
            )
        ).root

    with bound(requester, conversation_id):
        return asyncio.run(run())


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_AGENT_ROOT", str(tmp_path / "agents"))
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    token = initialize()
    with TestClient(app) as test_client:
        yield test_client, token


def _requester(role: str = "admin", token: str = "admin-token") -> Requester:
    with sessions().begin() as db:
        row = Token(
            digest=digest(token),
            name=f"{role} caller",
            role=role,
        )
        db.add(row)
        db.flush()
        return Requester(id=row.id, role=role, token=token)


def test_request_clarification_records_caller_and_returns_structured_questions(
    client, monkeypatch
):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import create_mcp

    monkeypatch.setattr(
        knowledge,
        "existing_refs",
        lambda refs: (
            {
                "document:metrics/definitions/sales.md#chunk-1",
                "document:metrics/sales_order.md#chunk-1",
            }
            & set(refs)
        ),
    )
    requester = _requester()
    result = call(
        create_mcp(),
        "request_clarification",
        {"questions": SYNTHETIC_QUESTIONS},
        requester,
    )

    assert not result.isError, result.structuredContent
    body = result.structuredContent
    assert body["status"] == "pending"
    assert body["conversation_id"] is None
    assert body["questions"][0]["question"] == "销售额按支付口径还是下单口径？"
    assert [item["ref"] for item in body["questions"][0]["options"]] == [
        "document:metrics/definitions/sales.md#chunk-1",
        "document:metrics/sales_order.md#chunk-1",
    ]
    with sessions()() as db:
        row = db.get(Clarification, body["clarification_id"])
        assert row is not None
        assert row.owner_id == requester.id
        assert row.conversation_id is None
        assert (
            json.loads(row.questions)[0]["question"] == "销售额按支付口径还是下单口径？"
        )


def test_request_clarification_rejects_a_ref_outside_the_current_generation(
    client, monkeypatch
):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import create_mcp

    monkeypatch.setattr(knowledge, "existing_refs", lambda refs: set())
    requester = _requester()
    result = call(
        create_mcp(),
        "request_clarification",
        {"questions": SYNTHETIC_QUESTIONS},
        requester,
    )

    assert result.isError
    assert result.structuredContent["code"] == "invalid_input"
    assert result.structuredContent["retryable"] is False
    with sessions()() as db:
        assert db.query(Clarification).count() == 0


@pytest.mark.parametrize(
    "questions",
    [
        [],
        [
            {"question": f"问题{index}", "options": SYNTHETIC_QUESTIONS[0]["options"]}
            for index in range(4)
        ],
        [{"question": "只有一项", "options": SYNTHETIC_QUESTIONS[0]["options"][:1]}],
        [
            {
                "question": "选项太多",
                "options": [
                    {
                        "label": f"选项{index}",
                        "ref": "document:metrics/sales.md#chunk-1",
                    }
                    for index in range(6)
                ],
            }
        ],
    ],
)
def test_request_clarification_enforces_question_and_option_bounds(
    client, monkeypatch, questions
):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import create_mcp

    monkeypatch.setattr(knowledge, "existing_refs", lambda refs: set(refs))
    result = call(
        create_mcp(),
        "request_clarification",
        {"questions": questions},
        _requester(),
    )

    assert result.isError
    assert result.structuredContent["code"] == "invalid_input"


def test_request_clarification_records_the_current_conversation(client, monkeypatch):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import create_mcp

    monkeypatch.setattr(knowledge, "existing_refs", lambda refs: set(refs))
    requester = _requester(token="conversation-token")
    with sessions().begin() as db:
        conversation = Conversation(
            id="conversation-one",
            owner_id=requester.id,
            title="销售额",
            agent="pi",
            provider_id="provider",
            model="model",
        )
        db.add(conversation)

    result = call(
        create_mcp(),
        "request_clarification",
        {"questions": SYNTHETIC_QUESTIONS},
        requester,
        "conversation-one",
    )

    assert not result.isError, result.structuredContent
    assert result.structuredContent["conversation_id"] == "conversation-one"
    with sessions()() as db:
        row = db.get(Clarification, result.structuredContent["clarification_id"])
        assert row.owner_id == requester.id
        assert row.conversation_id == "conversation-one"


def test_request_clarification_rejects_another_users_conversation(client, monkeypatch):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import create_mcp

    monkeypatch.setattr(knowledge, "existing_refs", lambda refs: set(refs))
    with sessions().begin() as db:
        db.add(
            Conversation(
                id="someone-else",
                owner_id="other-owner",
                title="销售额",
                agent="pi",
                provider_id="provider",
                model="model",
            )
        )
    result = call(
        create_mcp(),
        "request_clarification",
        {"questions": SYNTHETIC_QUESTIONS},
        _requester(token="not-the-owner"),
        "someone-else",
    )

    assert result.isError
    assert result.structuredContent["code"] == "invalid_input"
    with sessions()() as db:
        assert db.query(Clarification).count() == 0


class FakeRunner:
    agent_id = "pi"

    def __init__(self):
        self.requests = []

    def available(self):
        return True

    async def run(self, request):
        self.requests.append(request)
        yield AgentEvent(TEXT_DELTA, {"text": "按支付口径。"})
        yield AgentEvent(
            DONE,
            {
                "text": (
                    "按支付口径，使用 shop.payments。\n```sql\n"
                    "SELECT SUM(amount) FROM shop.payments\n```\n"
                    "引用 document:metrics/definitions/sales.md#chunk-1"
                ),
                "sql": [
                    {
                        "sql": "SELECT SUM(amount) FROM shop.payments",
                        "dialect": "hive",
                    }
                ],
                "citations": [
                    {
                        "ref": "document:metrics/definitions/sales.md#chunk-1",
                        "type": "document",
                    }
                ],
            },
        )


def test_ask_dataagent_returns_answer_sql_citations_and_reuses_conversation(
    client, monkeypatch
):
    from semantica_dataagent import agent_runner
    from semantica_dataagent.app import create_mcp

    c, admin_token = client
    runner = FakeRunner()
    monkeypatch.setitem(agent_runner.RUNNERS, "pi", runner)
    requester = _requester(token="local-user-token")
    headers = {"Authorization": f"Bearer {requester.token}"}
    provider = c.post(
        "/api/v1/providers",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "name": "Default gateway",
            "base_url": "http://127.0.0.1:3425",
            "models": ["default-model"],
        },
    )
    assert provider.status_code == 201, provider.text

    first = call(
        create_mcp(),
        "ask_dataagent",
        {"question": "上个月销售额是多少？"},
        requester,
    )

    assert not first.isError, first.structuredContent
    body = first.structuredContent
    assert body["status"] == "completed"
    assert body["conversation_id"]
    assert "按支付口径" in body["answer"]
    assert body["sql"] == [
        {"sql": "SELECT SUM(amount) FROM shop.payments", "dialect": "hive"}
    ]
    assert body["citations"] == [
        {
            "ref": "document:metrics/definitions/sales.md#chunk-1",
            "type": "document",
        }
    ]
    conversation_id = body["conversation_id"]
    with sessions()() as db:
        conversation = db.get(Conversation, conversation_id)
        assert conversation.owner_id == requester.id
        assert db.query(Turn).filter_by(conversation_id=conversation_id).count() == 1
    assert runner.requests[0].session_id == conversation_id
    assert runner.requests[0].mcp_token == requester.token

    second = call(
        create_mcp(),
        "ask_dataagent",
        {"question": "再按月份拆分", "conversation_id": conversation_id},
        requester,
    )
    assert not second.isError, second.structuredContent
    assert second.structuredContent["conversation_id"] == conversation_id
    assert runner.requests[1].session_id == conversation_id
    assert runner.requests[1].prompt == "再按月份拆分"
    with sessions()() as db:
        assert db.query(Turn).filter_by(conversation_id=conversation_id).count() == 2

    history = c.get(
        f"/api/v1/conversations/{conversation_id}",
        headers=headers,
    )
    assert history.status_code == 200, history.text
    assert [turn["question"] for turn in history.json()["turns"]] == [
        "上个月销售额是多少？",
        "再按月份拆分",
    ]


class SlowRunner(FakeRunner):
    async def run(self, request):
        self.requests.append(request)
        yield AgentEvent(TEXT_DELTA, {"text": "正在检索…"})
        await asyncio.sleep(10)


def test_ask_dataagent_timeout_returns_unavailable_and_a_resumable_conversation(
    client, monkeypatch
):
    from semantica_dataagent import agent_runner
    from semantica_dataagent.app import create_mcp

    c, admin_token = client
    monkeypatch.setenv("DATAAGENT_ASK_TIMEOUT_SECONDS", "0.1")
    monkeypatch.setitem(agent_runner.RUNNERS, "pi", SlowRunner())
    requester = _requester(token="timeout-user-token")
    c.post(
        "/api/v1/providers",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "name": "Default gateway",
            "base_url": "http://127.0.0.1:3425",
            "models": ["default-model"],
        },
    )
    result = call(
        create_mcp(),
        "ask_dataagent",
        {"question": "上个月销售额是多少？"},
        requester,
    )

    assert result.isError
    body = result.structuredContent
    assert body["code"] == "unavailable"
    assert body["retryable"] is True
    assert body["conversation_id"]
    with sessions()() as db:
        conversation = db.get(Conversation, body["conversation_id"])
        assert conversation is not None and conversation.owner_id == requester.id
