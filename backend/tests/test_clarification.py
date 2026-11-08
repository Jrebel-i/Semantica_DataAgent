"""Clarification and delegated online answers over the MCP boundary."""

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

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

FIELD_QUESTIONS = [
    {
        "question": "“操作系统”指哪个字段？",
        "options": [
            {
                "label": "os_type（操作系统代码）",
                "ref": "field:shop.channels.os_type",
            },
            {
                "label": "os_name（操作系统名称）",
                "ref": "field:shop.channels.os_name",
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


def test_existing_refs_follow_the_switched_generation_not_the_setting(
    client, monkeypatch
):
    from semantica_dataagent import knowledge
    from semantica_dataagent.db import (
        GenerationPointer,
        KnowledgeGeneration,
        Setting,
        sessions,
    )

    c, token = client
    with sessions().begin() as db:
        db.add_all([KnowledgeGeneration(name="old"), KnowledgeGeneration(name="new")])
        db.add(Setting(key="knowledge_generation", value="old"))
        db.add(GenerationPointer(id=1, generation="old"))
    refs = {
        "old": {"document:metrics/only-old.md#chunk-1"},
        "new": {"document:metrics/only-new.md#chunk-1"},
    }
    seen = []

    def execute(store, query, parameters):
        seen.append(parameters["generation"])
        wanted = set(parameters["refs"])
        return [{"ref": ref} for ref in refs[parameters["generation"]] if ref in wanted]

    monkeypatch.setattr(knowledge, "read_graph", lambda values: object())
    monkeypatch.setattr(knowledge, "execute", execute)
    old_ref = "document:metrics/only-old.md#chunk-1"

    assert knowledge.existing_refs([old_ref]) == {old_ref}
    response = c.post(
        "/api/v1/generations/new/switch",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    assert knowledge.existing_refs([old_ref]) == set()
    assert seen == ["old", "new"]


def test_mcp_middleware_binds_the_caller_and_conversation_header(client):
    from semantica_dataagent.app import MCPAuthentication
    from semantica_dataagent.mcp_context import (
        current_conversation_id,
        current_requester,
    )

    requester = _requester(token="middleware-token")
    with sessions().begin() as db:
        db.add(
            Conversation(
                id="conversation-middleware",
                owner_id=requester.id,
                title="销售额",
                agent="pi",
                provider_id="provider",
                model="model",
            )
        )
    seen = {}

    async def mcp_app(scope, receive, send):
        seen["requester"] = current_requester()
        seen["conversation_id"] = current_conversation_id()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    inner = SimpleNamespace(state=SimpleNamespace(mcp_app=mcp_app))
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "root_path": "",
        "path": "/mcp/",
        "raw_path": b"/mcp/",
        "query_string": b"",
        "headers": [
            (b"authorization", f"Bearer {requester.token}".encode()),
            (b"x-dataagent-conversation-id", b"conversation-middleware"),
        ],
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(MCPAuthentication(inner)(scope, receive, send))

    assert sent[0]["status"] == 204
    assert seen["requester"] == requester
    assert seen["conversation_id"] == "conversation-middleware"


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


def test_request_clarification_accepts_field_refs_from_retrieval(client, monkeypatch):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import create_mcp

    monkeypatch.setattr(knowledge, "existing_refs", lambda refs: set(refs))
    requester = _requester(token="clarification-field-token")
    result = call(
        create_mcp(),
        "request_clarification",
        {"questions": FIELD_QUESTIONS},
        requester,
    )

    assert not result.isError, result.structuredContent
    body = result.structuredContent
    refs = [option["ref"] for option in body["questions"][0]["options"]]
    assert refs == [
        "field:shop.channels.os_type",
        "field:shop.channels.os_name",
    ]
    with sessions()() as db:
        row = db.get(Clarification, body["clarification_id"])
        assert row is not None
        stored = json.loads(row.questions)
        assert [option["ref"] for option in stored[0]["options"]] == refs


def test_request_clarification_rejects_a_fabricated_field_ref(client, monkeypatch):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import create_mcp

    existing = "field:shop.channels.os_type"
    monkeypatch.setattr(knowledge, "existing_refs", lambda refs: set(refs) & {existing})
    requester = _requester(token="clarification-field-reject-token")
    result = call(
        create_mcp(),
        "request_clarification",
        {
            "questions": [
                {
                    "question": "“操作系统”指哪个字段？",
                    "options": [
                        {"label": "os_type（操作系统代码）", "ref": existing},
                        {
                            "label": "os_name（操作系统名称）",
                            "ref": "field:shop.channels.os_missing",
                        },
                    ],
                }
            ]
        },
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
    assert runner.requests[0].mcp_token
    assert runner.requests[0].mcp_token != requester.token

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
    assert runner.requests[1].mcp_token != runner.requests[0].mcp_token
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


def test_ask_dataagent_uses_configured_default_agent_provider_and_model(
    client, monkeypatch
):
    from semantica_dataagent import agent_runner
    from semantica_dataagent.app import create_mcp

    c, admin_token = client
    runner = FakeRunner()
    monkeypatch.setitem(agent_runner.RUNNERS, "pi", runner)
    provider = c.post(
        "/api/v1/providers",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "name": "Configured gateway",
            "base_url": "http://127.0.0.1:3425",
            "models": ["first-model", "configured-model"],
        },
    ).json()
    settings = c.put(
        "/api/v1/settings",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "values": {
                "assistant_agent": "pi",
                "assistant_provider_id": provider["id"],
                "assistant_model": "configured-model",
            }
        },
    )
    assert settings.status_code == 200, settings.text

    result = call(
        create_mcp(),
        "ask_dataagent",
        {"question": "按支付口径统计上个月销售额"},
        _requester(token="configured-user-token"),
    )

    assert not result.isError, result.structuredContent
    assert runner.requests[0].provider.id == provider["id"]
    assert runner.requests[0].model == "configured-model"


def test_clarification_schema_survives_clients_that_rebuild_the_root():
    from jsonschema import Draft202012Validator

    from semantica_dataagent.app import create_mcp

    tool = next(
        item
        for item in create_mcp()._tool_manager.list_tools()
        if item.name == "request_clarification"
    )
    schema = {key: tool.parameters[key] for key in ("type", "properties", "required")}
    assert "#/$defs/" not in json.dumps(schema), (
        "tool schema contains dangling references"
    )
    validator = Draft202012Validator(schema)
    assert validator.is_valid({"questions": SYNTHETIC_QUESTIONS})
    assert not validator.is_valid({"questions": [{"question": "x", "options": []}]})


def _conversation_with_turn(requester: Requester, conversation_id: str) -> int:
    with sessions().begin() as db:
        db.add(
            Conversation(
                id=conversation_id,
                owner_id=requester.id,
                title="澄清持久化",
                agent="pi",
                provider_id="provider",
                model="model",
            )
        )
        db.flush()
        turn = Turn(
            conversation_id=conversation_id,
            sequence=1,
            question="销售额按哪个口径？",
            status="completed",
            answer="",
            sql="[]",
            citations="[]",
        )
        db.add(turn)
        db.flush()
        return turn.id


def _stored_turn(c, token: str, conversation_id: str) -> dict:
    response = c.get(
        f"/api/v1/conversations/{conversation_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["turns"][0]


def test_persisted_clarification_keeps_option_ids_labels_and_refs(client, monkeypatch):
    from semantica_dataagent import knowledge
    from semantica_dataagent.agent_runner import CLARIFICATION
    from semantica_dataagent.app import create_mcp
    from semantica_dataagent.conversation_api import _record_event

    c, _ = client
    monkeypatch.setattr(knowledge, "existing_refs", lambda refs: set(refs))
    requester = _requester(token="clarification-persist-token")
    conversation_id = "conversation-clarification-persist"
    turn_id = _conversation_with_turn(requester, conversation_id)

    result = call(
        create_mcp(),
        "request_clarification",
        {"questions": SYNTHETIC_QUESTIONS},
        requester,
        conversation_id,
    )
    assert not result.isError, result.structuredContent
    questions = result.structuredContent["questions"]
    assert questions[0]["id"] and all(
        option["id"] for option in questions[0]["options"]
    )

    _record_event(turn_id, 0, AgentEvent(CLARIFICATION, {"questions": questions}))

    turn = _stored_turn(c, requester.token, conversation_id)
    assert [event["type"] for event in turn["events"]] == ["clarification"]
    stored = turn["events"][0]["data"]["questions"]
    assert stored == questions
    assert stored[0]["id"] == questions[0]["id"]
    assert [option["label"] for option in stored[0]["options"]] == [
        option["label"] for option in questions[0]["options"]
    ]
    assert [option["ref"] for option in stored[0]["options"]] == [
        option["ref"] for option in questions[0]["options"]
    ]


def test_other_event_summaries_still_truncate_values_below_depth_five(client):
    from semantica_dataagent.conversation_api import _record_event

    c, _ = client
    requester = _requester(token="summary-token")
    conversation_id = "conversation-summary"
    turn_id = _conversation_with_turn(requester, conversation_id)

    _record_event(
        turn_id,
        0,
        AgentEvent(
            "tool_end",
            {
                "name": "mcp__dataagent__search_knowledge",
                "payload": {"a": {"b": {"c": {"d": "deep-value"}}}},
            },
        ),
    )

    turn = _stored_turn(c, requester.token, conversation_id)
    stored = turn["events"][0]["data"]
    assert stored["name"] == "mcp__dataagent__search_knowledge"
    assert stored["payload"]["a"]["b"]["c"] == {"d": "…"}


def test_clarification_summary_still_caps_strings_and_list_length(client):
    from semantica_dataagent.agent_runner import CLARIFICATION
    from semantica_dataagent.conversation_api import _record_event

    c, _ = client
    requester = _requester(token="clarification-limits-token")
    conversation_id = "conversation-clarification-limits"
    turn_id = _conversation_with_turn(requester, conversation_id)
    label = "口" * 4001

    _record_event(
        turn_id,
        0,
        AgentEvent(
            CLARIFICATION,
            {
                "questions": [
                    {
                        "id": "q1",
                        "question": "哪个口径？",
                        "options": [
                            {
                                "id": f"o{index}",
                                "label": label,
                                "ref": "document:metrics/sales.md#chunk-1",
                            }
                            for index in range(120)
                        ],
                    }
                ]
            },
        ),
    )

    turn = _stored_turn(c, requester.token, conversation_id)
    options = turn["events"][0]["data"]["questions"][0]["options"]
    assert len(options) == 100
    assert len(options[0]["label"]) == 4000
    assert options[0]["label"].endswith("…")
    assert options[0]["id"] == "o0"
