"""Tests for published assistant catalog, personal agent defaults, and unified resolution (#329)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_AGENT_ROOT", str(tmp_path / "agents"))
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    admin_token = initialize()
    with TestClient(app) as test_client:
        member_resp = test_client.post(
            "/api/v1/tokens",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"name": "member-a", "role": "user"},
        )
        assert member_resp.status_code == 201, member_resp.text
        member_b_resp = test_client.post(
            "/api/v1/tokens",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"name": "member-b", "role": "user"},
        )
        assert member_b_resp.status_code == 201, member_b_resp.text
        yield (
            test_client,
            admin_token,
            member_resp.json()["token"],
            member_b_resp.json()["token"],
        )


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class StubRunner:
    effort_levels = ("low", "medium", "high")
    supports_skills = True

    def __init__(self, agent_id: str = "claude", *, ok: bool = True):
        self.agent_id = agent_id
        self._ok = ok
        self.requests = []

    def available(self) -> bool:
        return self._ok

    async def run(self, request):
        from semantica_dataagent.agent_runner import DONE, AgentEvent

        self.requests.append(request)
        yield AgentEvent(
            DONE,
            {
                "text": f"reply from {self.agent_id}:{request.model}",
                "sql": [],
                "citations": [],
            },
        )


def _create_catalog_provider(
    c: TestClient,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str,
    models: list[str],
    api_key: str = "sk-top-secret-key",
    capabilities: dict | None = None,
) -> dict:
    from semantica_dataagent import provider_api

    created = c.post(
        "/api/v1/providers",
        headers=_headers(admin_token),
        json={
            "name": name,
            "type": "anthropic-messages",
            "base_url": f"https://{name.lower().replace(' ', '-')}.example.test",
            "api_key": api_key,
        },
    )
    assert created.status_code == 201, created.text
    pid = created.json()["id"]

    monkeypatch.setattr(
        provider_api,
        "_catalog_transport",
        httpx.MockTransport(
            lambda _req: httpx.Response(200, json={"data": [{"id": m} for m in models]})
        ),
    )
    refreshed = c.post(
        f"/api/v1/providers/{pid}/refresh-models",
        headers=_headers(admin_token),
    )
    assert refreshed.status_code == 200, refreshed.text

    if capabilities:
        updated = c.put(
            f"/api/v1/providers/{pid}",
            headers=_headers(admin_token),
            json={"model_capabilities": capabilities},
        )
        assert updated.status_code == 200, updated.text
        return updated.json()
    return refreshed.json()


def test_published_assistant_catalog_filters_and_redacts(client, monkeypatch):
    from semantica_dataagent import agent_runner, provider_api

    c, admin_token, member_token, _ = client
    monkeypatch.setitem(agent_runner.RUNNERS, "claude", StubRunner("claude", ok=True))
    monkeypatch.setitem(agent_runner.RUNNERS, "pi", StubRunner("pi", ok=True))
    monkeypatch.setitem(agent_runner.RUNNERS, "codex", StubRunner("codex", ok=True))

    # Unauthenticated request rejected
    assert c.get("/api/v1/assistant-catalog").status_code == 401

    secret = "sk-never-leak-to-members"
    p_refreshed = _create_catalog_provider(
        c,
        admin_token,
        monkeypatch,
        name="Gateway One",
        models=["m-pub-1#claude", "m-pub-2#claude", "m-unpub#claude"],
        api_key=secret,
        capabilities={
            "m-pub-1#claude": {
                "description": "Fast",
                "vision": True,
                "effort_levels": ["low", "high"],
            },
            "m-unpub#claude": {
                "description": "Hidden",
                "vision": False,
                "effort_levels": ["max"],
            },
        },
    )
    pid1 = p_refreshed["id"]

    # Unrefreshed manual provider (catalog_models=None) must never be published
    p_manual = c.post(
        "/api/v1/providers",
        headers=_headers(admin_token),
        json={
            "name": "Unrefreshed Manual",
            "base_url": "https://manual.example.test",
            "api_key": "sk-manual-secret",
            "models": ["m-manual"],
        },
    )
    assert p_manual.status_code == 201, p_manual.text
    pid_manual = p_manual.json()["id"]

    # Published system default profile on claude
    prof_default = c.post(
        "/api/v1/agent-profiles",
        headers=_headers(admin_token),
        json={
            "name": "Claude · 日常",
            "runner_id": "claude",
            "enabled": True,
            "is_system_default": True,
            "default_provider_id": pid1,
            "default_model": "m-pub-1#claude",
            "provider_models": {pid1: ["m-pub-1#claude", "m-pub-2#claude"]},
        },
    )
    assert prof_default.status_code == 201, prof_default.text
    default_id = prof_default.json()["id"]

    # Disabled profile must not appear
    prof_disabled = c.post(
        "/api/v1/agent-profiles",
        headers=_headers(admin_token),
        json={
            "name": "Claude · 停用",
            "runner_id": "claude",
            "enabled": False,
            "default_provider_id": pid1,
            "default_model": "m-pub-1#claude",
            "provider_models": {pid1: ["m-pub-1#claude"]},
        },
    )
    assert prof_disabled.status_code == 201, prof_disabled.text

    # Profile whose runner later becomes unavailable must not appear
    prof_codex = c.post(
        "/api/v1/agent-profiles",
        headers=_headers(admin_token),
        json={
            "name": "Codex · 不可用",
            "runner_id": "codex",
            "enabled": True,
            "default_provider_id": pid1,
            "default_model": "m-pub-1#claude",
            "provider_models": {pid1: ["m-pub-1#claude"]},
        },
    )
    assert prof_codex.status_code == 201, prof_codex.text
    monkeypatch.setitem(agent_runner.RUNNERS, "codex", StubRunner("codex", ok=False))

    # Profile whose only selected model later disappears from catalog must not appear
    p_vanishing = _create_catalog_provider(
        c,
        admin_token,
        monkeypatch,
        name="Vanishing GW",
        models=["m-gone#claude", "m-other#claude"],
        api_key="sk-vanishing",
    )
    pid_van = p_vanishing["id"]
    prof_vanished = c.post(
        "/api/v1/agent-profiles",
        headers=_headers(admin_token),
        json={
            "name": "Claude · 失效目录",
            "runner_id": "claude",
            "enabled": True,
            "default_provider_id": pid_van,
            "default_model": "m-gone#claude",
            "provider_models": {pid_van: ["m-gone#claude"]},
        },
    )
    assert prof_vanished.status_code == 201, prof_vanished.text
    monkeypatch.setattr(
        provider_api,
        "_catalog_transport",
        httpx.MockTransport(
            lambda _req: httpx.Response(200, json={"data": [{"id": "m-other#claude"}]})
        ),
    )
    assert (
        c.post(
            f"/api/v1/providers/{pid_van}/refresh-models",
            headers=_headers(admin_token),
        ).status_code
        == 200
    )

    # Seeded pi profile has empty provider_models -> must not appear
    # Manually inject a pending_verification entry on pi pointing to p_manual -> still must not appear
    from semantica_dataagent.db import AgentProfile, sessions

    with sessions().begin() as db:
        pi_row = db.get(AgentProfile, "pi")
        pi_row.provider_models = json.dumps({pid_manual: ["m-manual"]})
        pi_row.pending_verification_models = json.dumps({pid_manual: ["m-manual"]})

    resp = c.get("/api/v1/assistant-catalog", headers=_headers(member_token))
    assert resp.status_code == 200, resp.text
    assert secret not in resp.text
    assert "sk-manual-secret" not in resp.text
    assert "m-unpub#claude" not in resp.text
    assert "m-manual" not in resp.text
    assert "m-gone#claude" not in resp.text

    body = resp.json()
    assert body["system_default_profile_id"] == default_id
    assert body["personal_defaults"] == {}
    assert [a["id"] for a in body["agents"]] == [default_id]

    agent = body["agents"][0]
    assert agent["name"] == "Claude · 日常"
    assert agent["runner_id"] == "claude"
    assert agent["is_system_default"] is True
    assert agent["default_provider_id"] == pid1
    assert agent["default_model"] == "m-pub-1#claude"
    assert agent["effort_levels"] == ["low", "medium", "high"]
    assert agent["supports_skills"] is True
    assert len(agent["providers"]) == 1

    pub_prov = agent["providers"][0]
    assert set(pub_prov.keys()) == {
        "id",
        "name",
        "type",
        "models",
        "model_capabilities",
    }
    assert pub_prov["id"] == pid1
    assert pub_prov["name"] == "Gateway One"
    assert pub_prov["type"] == "anthropic-messages"
    assert pub_prov["models"] == ["m-pub-1#claude", "m-pub-2#claude"]
    assert set(pub_prov["model_capabilities"].keys()) == {
        "m-pub-1#claude",
        "m-pub-2#claude",
    }
    assert pub_prov["model_capabilities"]["m-pub-1#claude"]["vision"] is True


def test_personal_agent_defaults_crud_and_isolation(client, monkeypatch):
    from semantica_dataagent import agent_runner, provider_api

    c, admin_token, user_a_token, user_b_token = client
    monkeypatch.setitem(agent_runner.RUNNERS, "claude", StubRunner("claude", ok=True))
    monkeypatch.setitem(agent_runner.RUNNERS, "codex", StubRunner("codex", ok=True))

    p1 = _create_catalog_provider(
        c,
        admin_token,
        monkeypatch,
        name="GW1",
        models=["m-1#claude", "m-2#claude", "m-unpub#claude"],
    )
    pid1 = p1["id"]

    prof = c.post(
        "/api/v1/agent-profiles",
        headers=_headers(admin_token),
        json={
            "name": "Claude · 团队",
            "runner_id": "claude",
            "enabled": True,
            "is_system_default": True,
            "default_provider_id": pid1,
            "default_model": "m-1#claude",
            "provider_models": {pid1: ["m-1#claude", "m-2#claude"]},
        },
    ).json()
    prof_id = prof["id"]

    disabled_prof = c.post(
        "/api/v1/agent-profiles",
        headers=_headers(admin_token),
        json={
            "name": "Claude · 已停用",
            "runner_id": "claude",
            "enabled": False,
            "default_provider_id": pid1,
            "default_model": "m-1#claude",
            "provider_models": {pid1: ["m-1#claude"]},
        },
    ).json()

    # Unauthenticated -> 401
    assert c.get(f"/api/v1/me/agent-defaults/{prof_id}").status_code == 401
    assert (
        c.put(
            f"/api/v1/me/agent-defaults/{prof_id}",
            json={"provider_id": pid1, "model": "m-2#claude"},
        ).status_code
        == 401
    )
    assert c.delete(f"/api/v1/me/agent-defaults/{prof_id}").status_code == 401

    # Missing profile -> 404
    assert (
        c.get(
            "/api/v1/me/agent-defaults/nonexistent",
            headers=_headers(user_a_token),
        ).status_code
        == 404
    )
    assert (
        c.put(
            "/api/v1/me/agent-defaults/nonexistent",
            headers=_headers(user_a_token),
            json={"provider_id": pid1, "model": "m-1#claude"},
        ).status_code
        == 404
    )

    # Not yet configured -> 404
    assert (
        c.get(
            f"/api/v1/me/agent-defaults/{prof_id}",
            headers=_headers(user_a_token),
        ).status_code
        == 404
    )

    # Reject unpublished model, unknown provider, disabled profile -> 422
    assert (
        c.put(
            f"/api/v1/me/agent-defaults/{prof_id}",
            headers=_headers(user_a_token),
            json={"provider_id": pid1, "model": "m-unpub#claude"},
        ).status_code
        == 422
    )
    assert (
        c.put(
            f"/api/v1/me/agent-defaults/{prof_id}",
            headers=_headers(user_a_token),
            json={"provider_id": "missing-pid", "model": "m-1#claude"},
        ).status_code
        == 422
    )
    assert (
        c.put(
            f"/api/v1/me/agent-defaults/{disabled_prof['id']}",
            headers=_headers(user_a_token),
            json={"provider_id": pid1, "model": "m-1#claude"},
        ).status_code
        == 422
    )

    # User A saves valid personal default
    saved = c.put(
        f"/api/v1/me/agent-defaults/{prof_id}",
        headers=_headers(user_a_token),
        json={"provider_id": pid1, "model": "m-2#claude"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["agent_profile_id"] == prof_id
    assert saved.json()["provider_id"] == pid1
    assert saved.json()["model"] == "m-2#claude"

    # User A reads it via GET and catalog; User B cannot see User A's default
    got_a = c.get(
        f"/api/v1/me/agent-defaults/{prof_id}",
        headers=_headers(user_a_token),
    )
    assert got_a.status_code == 200
    assert got_a.json()["model"] == "m-2#claude"

    cat_a = c.get("/api/v1/assistant-catalog", headers=_headers(user_a_token)).json()
    assert cat_a["personal_defaults"] == {
        prof_id: {"provider_id": pid1, "model": "m-2#claude"}
    }

    assert (
        c.get(
            f"/api/v1/me/agent-defaults/{prof_id}",
            headers=_headers(user_b_token),
        ).status_code
        == 404
    )
    cat_b = c.get("/api/v1/assistant-catalog", headers=_headers(user_b_token)).json()
    assert cat_b["personal_defaults"] == {}

    # User B deletes -> does not affect User A
    assert (
        c.delete(
            f"/api/v1/me/agent-defaults/{prof_id}",
            headers=_headers(user_b_token),
        ).status_code
        == 204
    )
    assert (
        c.get(
            f"/api/v1/me/agent-defaults/{prof_id}",
            headers=_headers(user_a_token),
        ).status_code
        == 200
    )

    # User A deletes -> resets to company default
    assert (
        c.delete(
            f"/api/v1/me/agent-defaults/{prof_id}",
            headers=_headers(user_a_token),
        ).status_code
        == 204
    )
    assert (
        c.get(
            f"/api/v1/me/agent-defaults/{prof_id}",
            headers=_headers(user_a_token),
        ).status_code
        == 404
    )
    assert (
        c.get("/api/v1/assistant-catalog", headers=_headers(user_a_token)).json()[
            "personal_defaults"
        ]
        == {}
    )

    # If saved personal default becomes invalid after catalog removes model, catalog omits it & GET rejects
    assert (
        c.put(
            f"/api/v1/me/agent-defaults/{prof_id}",
            headers=_headers(user_a_token),
            json={"provider_id": pid1, "model": "m-2#claude"},
        ).status_code
        == 200
    )
    monkeypatch.setattr(
        provider_api,
        "_catalog_transport",
        httpx.MockTransport(
            lambda _req: httpx.Response(200, json={"data": [{"id": "m-1#claude"}]})
        ),
    )
    c.post(f"/api/v1/providers/{pid1}/refresh-models", headers=_headers(admin_token))
    cat_after = c.get(
        "/api/v1/assistant-catalog", headers=_headers(user_a_token)
    ).json()
    assert cat_after["personal_defaults"] == {}
    assert (
        c.get(
            f"/api/v1/me/agent-defaults/{prof_id}",
            headers=_headers(user_a_token),
        ).status_code
        == 422
    )


def _sse_events(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name is not None:
            events.append((name, data))
    return events


def test_conversation_unified_resolution_enforcement_and_history_readability(
    client, monkeypatch
):
    from semantica_dataagent import agent_runner, provider_api

    c, admin_token, user_a_token, user_b_token = client
    claude_runner = StubRunner("claude", ok=True)
    pi_runner = StubRunner("pi", ok=True)
    monkeypatch.setitem(agent_runner.RUNNERS, "claude", claude_runner)
    monkeypatch.setitem(agent_runner.RUNNERS, "pi", pi_runner)

    p1 = _create_catalog_provider(
        c,
        admin_token,
        monkeypatch,
        name="Shared GW",
        models=["m-comp#claude", "m-pers#claude", "m-extra#claude", "m-unpub#claude"],
        api_key="sk-conv-secret",
    )
    pid1 = p1["id"]

    prof_default = c.post(
        "/api/v1/agent-profiles",
        headers=_headers(admin_token),
        json={
            "name": "Claude · 默认",
            "runner_id": "claude",
            "enabled": True,
            "is_system_default": True,
            "default_provider_id": pid1,
            "default_model": "m-comp#claude",
            "provider_models": {
                pid1: ["m-comp#claude", "m-pers#claude", "m-extra#claude"]
            },
        },
    ).json()
    prof_id = prof_default["id"]

    prof_disabled = c.post(
        "/api/v1/agent-profiles",
        headers=_headers(admin_token),
        json={
            "name": "Claude · 停用",
            "runner_id": "claude",
            "enabled": False,
            "default_provider_id": pid1,
            "default_model": "m-comp#claude",
            "provider_models": {pid1: ["m-comp#claude"]},
        },
    ).json()

    # User A saves personal default m-pers#claude
    assert (
        c.put(
            f"/api/v1/me/agent-defaults/{prof_id}",
            headers=_headers(user_a_token),
            json={"provider_id": pid1, "model": "m-pers#claude"},
        ).status_code
        == 200
    )

    # 1) User A omits provider_id & model -> resolves personal default (m-pers#claude)
    resp_a = c.post(
        "/api/v1/conversations/ask",
        headers=_headers(user_a_token),
        json={"question": "Q1", "agent_profile_id": prof_id},
    )
    assert resp_a.status_code == 200, resp_a.text
    conv_a_id = _sse_events(resp_a.text)[0][1]["conversation_id"]
    assert claude_runner.requests[-1].model == "m-pers#claude"

    detail_a = c.get(
        f"/api/v1/conversations/{conv_a_id}", headers=_headers(user_a_token)
    ).json()
    assert detail_a["agent_profile_id"] == prof_id
    assert detail_a["agent"] == "claude"
    assert detail_a["provider_id"] == pid1
    assert detail_a["model"] == "m-pers#claude"

    # 2) User B omits agent_profile_id, provider_id, model -> resolves system default & company default (m-comp#claude)
    resp_b = c.post(
        "/api/v1/conversations/ask",
        headers=_headers(user_b_token),
        json={"question": "Q2"},
    )
    assert resp_b.status_code == 200, resp_b.text
    conv_b_id = _sse_events(resp_b.text)[0][1]["conversation_id"]
    assert claude_runner.requests[-1].model == "m-comp#claude"
    detail_b = c.get(
        f"/api/v1/conversations/{conv_b_id}", headers=_headers(user_b_token)
    ).json()
    assert detail_b["agent_profile_id"] == prof_id
    assert detail_b["model"] == "m-comp#claude"

    # 3) Explicit choice overrides personal/company default
    resp_exp = c.post(
        "/api/v1/conversations/ask",
        headers=_headers(user_a_token),
        json={
            "question": "Q3",
            "agent_profile_id": prof_id,
            "provider_id": pid1,
            "model": "m-extra#claude",
        },
    )
    assert resp_exp.status_code == 200, resp_exp.text
    assert claude_runner.requests[-1].model == "m-extra#claude"

    # 4) Reject unpublished catalog model, disabled profile, and switching profile on resume
    rej_unpub = c.post(
        "/api/v1/conversations/ask",
        headers=_headers(user_a_token),
        json={
            "question": "Bad",
            "agent_profile_id": prof_id,
            "provider_id": pid1,
            "model": "m-unpub#claude",
        },
    )
    assert rej_unpub.status_code == 422, rej_unpub.text
    assert "sk-conv-secret" not in rej_unpub.text

    rej_disabled = c.post(
        "/api/v1/conversations/ask",
        headers=_headers(user_a_token),
        json={"question": "Bad", "agent_profile_id": prof_disabled["id"]},
    )
    assert rej_disabled.status_code == 422, rej_disabled.text

    rej_switch_prof = c.post(
        "/api/v1/conversations/ask",
        headers=_headers(user_a_token),
        json={
            "question": "Switch",
            "conversation_id": conv_a_id,
            "agent_profile_id": prof_disabled["id"],
        },
    )
    assert rej_switch_prof.status_code == 409, rej_switch_prof.text

    # 5) Catalog refresh removes m-pers#claude -> conv_a remains readable, resume without switching fails 422, resume with valid switch succeeds
    monkeypatch.setattr(
        provider_api,
        "_catalog_transport",
        httpx.MockTransport(
            lambda _req: httpx.Response(
                200,
                json={"data": [{"id": "m-comp#claude"}, {"id": "m-extra#claude"}]},
            )
        ),
    )
    assert (
        c.post(
            f"/api/v1/providers/{pid1}/refresh-models",
            headers=_headers(admin_token),
        ).status_code
        == 200
    )

    hist_a = c.get(f"/api/v1/conversations/{conv_a_id}", headers=_headers(user_a_token))
    assert hist_a.status_code == 200, hist_a.text
    assert hist_a.json()["model"] == "m-pers#claude"
    assert len(hist_a.json()["turns"]) == 1

    # User A's personal default is now invalid -> new conversation falls back to company default (m-comp#claude), NOT unpublished m-pers#claude
    resp_fallback = c.post(
        "/api/v1/conversations/ask",
        headers=_headers(user_a_token),
        json={"question": "New after invalid personal", "agent_profile_id": prof_id},
    )
    assert resp_fallback.status_code == 200, resp_fallback.text
    assert claude_runner.requests[-1].model == "m-comp#claude"

    # Resuming conv_a without explicit valid replacement rejects 422 (no silent fallback)
    rej_resume = c.post(
        "/api/v1/conversations/ask",
        headers=_headers(user_a_token),
        json={"question": "Resume broken", "conversation_id": conv_a_id},
    )
    assert rej_resume.status_code == 422, rej_resume.text

    # Resuming conv_a with explicit valid published model succeeds
    ok_resume = c.post(
        "/api/v1/conversations/ask",
        headers=_headers(user_a_token),
        json={
            "question": "Resume fixed",
            "conversation_id": conv_a_id,
            "provider_id": pid1,
            "model": "m-comp#claude",
        },
    )
    assert ok_resume.status_code == 200, ok_resume.text
    assert claude_runner.requests[-1].model == "m-comp#claude"

    # 6) ask-with-attachments supports agent_profile_id & personal/company default resolution
    attach_resp = c.post(
        "/api/v1/conversations/ask-with-attachments",
        headers=_headers(user_b_token),
        data={"question": "With attachment", "agent_profile_id": prof_id},
    )
    assert attach_resp.status_code == 200, attach_resp.text
    attach_cid = _sse_events(attach_resp.text)[0][1]["conversation_id"]
    attach_detail = c.get(
        f"/api/v1/conversations/{attach_cid}", headers=_headers(user_b_token)
    ).json()
    assert attach_detail["agent_profile_id"] == prof_id
    assert attach_detail["model"] == "m-comp#claude"


def _mcp_call(mcp, name: str, arguments: dict, requester):
    import asyncio

    from semantica_dataagent.mcp_context import bind_mcp_context

    async def invoke():
        with bind_mcp_context(requester):
            return await mcp._mcp_server.request_handlers[
                __import__("mcp.types", fromlist=["CallToolRequest"]).CallToolRequest
            ](
                __import__("mcp.types", fromlist=["CallToolRequest"]).CallToolRequest(
                    method="tools/call",
                    params=__import__(
                        "mcp.types", fromlist=["CallToolRequestParams"]
                    ).CallToolRequestParams(name=name, arguments=arguments),
                )
            )

    return asyncio.run(invoke()).root


def _requester_for(token: str):
    from semantica_dataagent.auth import Requester
    from semantica_dataagent.db import Token, digest, sessions

    with sessions()() as db:
        row = db.get(Token, digest(token))
        return Requester(id=row.id, role=row.role, token=token)


def test_mcp_ask_dataagent_uses_unified_resolver_and_rejects_invalid(
    client, monkeypatch
):
    from semantica_dataagent import agent_runner, provider_api
    from semantica_dataagent.app import create_mcp
    from semantica_dataagent.db import Conversation, sessions

    c, admin_token, user_a_token, user_b_token = client
    claude_runner = StubRunner("claude", ok=True)
    monkeypatch.setitem(agent_runner.RUNNERS, "claude", claude_runner)

    p1 = _create_catalog_provider(
        c,
        admin_token,
        monkeypatch,
        name="MCP GW",
        models=["m-comp#claude", "m-pers#claude", "m-unpub#claude"],
        api_key="sk-mcp-secret",
    )
    pid1 = p1["id"]

    prof = c.post(
        "/api/v1/agent-profiles",
        headers=_headers(admin_token),
        json={
            "name": "Claude · MCP",
            "runner_id": "claude",
            "enabled": True,
            "is_system_default": True,
            "default_provider_id": pid1,
            "default_model": "m-comp#claude",
            "provider_models": {pid1: ["m-comp#claude", "m-pers#claude"]},
        },
    ).json()
    prof_id = prof["id"]

    assert (
        c.put(
            f"/api/v1/me/agent-defaults/{prof_id}",
            headers=_headers(user_a_token),
            json={"provider_id": pid1, "model": "m-pers#claude"},
        ).status_code
        == 200
    )

    req_a = _requester_for(user_a_token)
    req_b = _requester_for(user_b_token)

    res_a = _mcp_call(create_mcp(), "ask_dataagent", {"question": "A的问题"}, req_a)
    assert not res_a.isError, res_a.structuredContent
    cid_a = res_a.structuredContent["conversation_id"]
    assert claude_runner.requests[-1].model == "m-pers#claude"

    res_b = _mcp_call(create_mcp(), "ask_dataagent", {"question": "B的问题"}, req_b)
    assert not res_b.isError, res_b.structuredContent
    cid_b = res_b.structuredContent["conversation_id"]
    assert claude_runner.requests[-1].model == "m-comp#claude"

    with sessions()() as db:
        assert db.get(Conversation, cid_a).agent_profile_id == prof_id
        assert db.get(Conversation, cid_b).agent_profile_id == prof_id

    # Catalog removes m-pers#claude -> continuing cid_a fails cleanly; new ask for A falls back to company default m-comp#claude
    monkeypatch.setattr(
        provider_api,
        "_catalog_transport",
        httpx.MockTransport(
            lambda _req: httpx.Response(
                200,
                json={"data": [{"id": "m-comp#claude"}, {"id": "m-unpub#claude"}]},
            )
        ),
    )
    assert (
        c.post(
            f"/api/v1/providers/{pid1}/refresh-models",
            headers=_headers(admin_token),
        ).status_code
        == 200
    )

    broken_cont = _mcp_call(
        create_mcp(),
        "ask_dataagent",
        {"question": "继续A", "conversation_id": cid_a},
        req_a,
    )
    assert broken_cont.isError
    assert broken_cont.structuredContent["code"] == "invalid_input"
    assert "sk-mcp-secret" not in json.dumps(broken_cont.structuredContent)

    new_a = _mcp_call(create_mcp(), "ask_dataagent", {"question": "新问A"}, req_a)
    assert not new_a.isError, new_a.structuredContent
    assert claude_runner.requests[-1].model == "m-comp#claude"

    # Catalog removes m-comp#claude too -> no valid system default; must NOT fall back to m-unpub#claude
    monkeypatch.setattr(
        provider_api,
        "_catalog_transport",
        httpx.MockTransport(
            lambda _req: httpx.Response(200, json={"data": [{"id": "m-unpub#claude"}]})
        ),
    )
    c.post(f"/api/v1/providers/{pid1}/refresh-models", headers=_headers(admin_token))

    no_default = _mcp_call(
        create_mcp(), "ask_dataagent", {"question": "无可用模型"}, req_b
    )
    assert no_default.isError
    assert no_default.structuredContent["code"] == "invalid_input"
    assert claude_runner.requests[-1].model != "m-unpub#claude"


def test_knowledge_synthesizer_and_assistant_defaults_follow_system_profile(
    client, monkeypatch
):
    from semantica_dataagent import agent_runner, knowledge_synthesizer, provider_api

    c, admin_token, member_token, _ = client
    monkeypatch.setitem(agent_runner.RUNNERS, "claude", StubRunner("claude", ok=True))

    p1 = _create_catalog_provider(
        c,
        admin_token,
        monkeypatch,
        name="Synth GW",
        models=["m-synth#claude", "m-unpub#claude"],
        api_key="sk-synth-key",
    )
    pid1 = p1["id"]

    prof = c.post(
        "/api/v1/agent-profiles",
        headers=_headers(admin_token),
        json={
            "name": "Claude · Synth",
            "runner_id": "claude",
            "enabled": True,
            "is_system_default": True,
            "default_provider_id": pid1,
            "default_model": "m-synth#claude",
            "provider_models": {pid1: ["m-synth#claude"]},
        },
    ).json()

    # /api/v1/assistant-defaults reflects system default profile when Settings are unset
    defaults_resp = c.get("/api/v1/assistant-defaults", headers=_headers(member_token))
    assert defaults_resp.status_code == 200, defaults_resp.text
    assert defaults_resp.json()["assistant_agent"] == "claude"
    assert defaults_resp.json()["assistant_provider_id"] == pid1
    assert defaults_resp.json()["assistant_model"] == "m-synth#claude"

    resolved = knowledge_synthesizer.resolve_llm_provider("default")
    assert resolved is not None
    assert resolved["id"] == pid1
    assert resolved["model"] == "m-synth#claude"
    assert resolved["api_key"] == "sk-synth-key"

    # Remove m-synth#claude from catalog -> must NOT fall back to unpublished m-unpub#claude
    monkeypatch.setattr(
        provider_api,
        "_catalog_transport",
        httpx.MockTransport(
            lambda _req: httpx.Response(200, json={"data": [{"id": "m-unpub#claude"}]})
        ),
    )
    c.post(f"/api/v1/providers/{pid1}/refresh-models", headers=_headers(admin_token))

    assert knowledge_synthesizer.resolve_llm_provider("default") is None
    assert prof["id"]


def test_migrated_pending_verification_blocked_until_catalog_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import sqlite3

    from alembic import command
    from alembic.config import Config

    from semantica_dataagent import agent_runner, cli, provider_api
    from semantica_dataagent.db import digest

    runner = StubRunner("claude", ok=True)
    monkeypatch.setitem(agent_runner.RUNNERS, "claude", runner)

    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(db_path))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_AGENT_ROOT", str(tmp_path / "agents"))

    cfg = Config()
    cfg.set_main_option(
        "script_location", str(Path(cli.__file__).parent / "migrations")
    )
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "0049")

    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO tokens (digest, name, role, id, created_at, revoked) "
            "VALUES (?, 'admin', 'admin', 'admin-id', '2026-01-01', 0)",
            (digest("admin-tok"),),
        )
        db.execute(
            "INSERT INTO providers "
            "(id, name, type, base_url, api_key, models, model_capabilities, created_at, updated_at) "
            "VALUES ('p-old', 'Legacy GW', 'openai-completions', 'https://old.example.test', NULL, ?, NULL, '2026-01-01', '2026-01-01')",
            (json.dumps(["kept-model", "removed-model"]),),
        )
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('assistant_agent', 'claude')"
        )
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('assistant_provider_id', 'p-old')"
        )
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('assistant_model', 'kept-model')"
        )

    command.upgrade(cfg, "0050")

    from semantica_dataagent.app import app

    with TestClient(app) as c:
        headers = _headers("admin-tok")
        # Before catalog refresh: pending_verification models are NOT published and cannot be invoked
        cat_before = c.get("/api/v1/assistant-catalog", headers=headers).json()
        assert cat_before["agents"] == []
        blocked_ask = c.post(
            "/api/v1/conversations/ask",
            headers=headers,
            json={
                "question": "Before refresh",
                "agent_profile_id": "claude",
                "provider_id": "p-old",
                "model": "kept-model",
            },
        )
        assert blocked_ask.status_code == 422, blocked_ask.text

        # Refresh catalog: kept-model exists, removed-model is gone
        monkeypatch.setattr(
            provider_api,
            "_catalog_transport",
            httpx.MockTransport(
                lambda _req: httpx.Response(
                    200,
                    json={"data": [{"id": "kept-model"}, {"id": "brand-new-model"}]},
                )
            ),
        )
        assert (
            c.post(
                "/api/v1/providers/p-old/refresh-models", headers=headers
            ).status_code
            == 200
        )

        cat_after = c.get("/api/v1/assistant-catalog", headers=headers).json()
        assert [a["id"] for a in cat_after["agents"]] == ["claude"]
        assert cat_after["agents"][0]["providers"][0]["models"] == ["kept-model"]

        # kept-model works; removed-model and brand-new-model (unpublished) are rejected
        ok_ask = c.post(
            "/api/v1/conversations/ask",
            headers=headers,
            json={"question": "After refresh", "agent_profile_id": "claude"},
        )
        assert ok_ask.status_code == 200, ok_ask.text
        assert runner.requests[-1].model == "kept-model"

        bad_ask = c.post(
            "/api/v1/conversations/ask",
            headers=headers,
            json={
                "question": "Unpublished",
                "agent_profile_id": "claude",
                "provider_id": "p-old",
                "model": "brand-new-model",
            },
        )
        assert bad_ask.status_code == 422
