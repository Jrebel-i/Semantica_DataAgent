"""End-to-end conversation project binding (#184).

The harness has already built the default project.  This check creates a
second document project, then observes project binding through REST and the
real MCP HTTP endpoint:

- a member of the default project cannot start a conversation in the other
  project;
- a conversation stays on its project when it is continued, and a different
  project is rejected;
- the conversation list filters by project and hides conversations whose
  project the caller can no longer access, while direct detail stays readable
  for its owner;
- an agent credential is bound to the conversation project through MCP;
- ``ask_dataagent`` creates its conversation in the MCP-resolved project.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

_CHECKS_DIR = str(Path(__file__).resolve().parent)
if _CHECKS_DIR not in sys.path:
    sys.path.append(_CHECKS_DIR)

from _conversation import create_provider

PROVIDER_SECRET = "{{SECRET_issue184_e2e_key}}"
NOT_FOUND = "项目不存在"


def _issue_token(ctx, name, projects):
    response = ctx.rest.post(
        "/api/v1/tokens",
        json={"name": name, "role": "user", "projects": projects},
    )
    ctx.require(response.status_code == 201, response.text[:300])
    return response.json()


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def _tool_error(session, name, arguments=None, timeout=None):
    result = session.call_tool(name, arguments or {}, timeout=timeout)
    ctx = getattr(result, "structuredContent", None) or {}
    return getattr(result, "isError", False), ctx


def _tool_value(session, name, arguments=None, timeout=None):
    result = session.call_tool(name, arguments or {}, timeout=timeout)
    structured = getattr(result, "structuredContent", None) or {}
    if getattr(result, "isError", False):
        raise AssertionError(
            f"{name} failed: {structured.get('code')}: {structured.get('message')}"
        )
    return structured.get("result", structured)


def _insert_conversation(owner_id, project_id, *, conversation_id=None, turns=0):
    from semantica_dataagent.db import Conversation, Turn, sessions

    conversation_id = conversation_id or "issue184-" + uuid.uuid4().hex
    now = datetime.now(UTC)
    with sessions().begin() as db:
        db.add(
            Conversation(
                id=conversation_id,
                owner_id=owner_id,
                project_id=project_id,
                title="issue 184 conversation",
                agent="test",
                provider_id="test",
                model="test",
                created_at=now,
                updated_at=now,
            )
        )
        for index in range(turns):
            db.add(
                Turn(
                    conversation_id=conversation_id,
                    provider_id="test",
                    model="test",
                    sequence=index + 1,
                    question="issue 184",
                    status="completed",
                    answer="ok",
                    sql="[]",
                    citations="[]",
                    started_at=now,
                    finished_at=now,
                )
            )
        db.flush()
    return conversation_id


def _token_ids(ctx):
    response = ctx.rest.get("/api/v1/tokens")
    ctx.require(response.status_code == 200, response.text[:300])
    return response.json()


def check_issue_184_conversation_project_scope(ctx):
    os.environ["DATAAGENT_DB_PATH"] = str(ctx.runtime.db_path)
    suffix = uuid.uuid4().hex[:8]
    created = ctx.rest.post(
        "/api/v1/projects",
        json={"name": f"Issue 184 B {suffix}", "type": "warehouse"},
    )
    ctx.require(created.status_code == 201, created.text[:300])
    project_b = created.json()
    project_b_id = project_b["id"]

    ua = _issue_token(
        ctx, f"issue184-ua-{suffix}", [{"project_id": "default", "role": "member"}]
    )
    ub = _issue_token(
        ctx, f"issue184-ub-{suffix}", [{"project_id": project_b_id, "role": "member"}]
    )
    uab = _issue_token(
        ctx,
        f"issue184-uab-{suffix}",
        [
            {"project_id": "default", "role": "member"},
            {"project_id": project_b_id, "role": "member"},
        ],
    )
    provider_id, model = create_provider(
        ctx, name=f"Issue 184 Provider {suffix}", secret=PROVIDER_SECRET
    )
    settings = ctx.rest.put(
        "/api/v1/settings",
        json={
            "values": {
                "assistant_agent": "pi",
                "assistant_provider_id": provider_id,
                "assistant_model": model,
            }
        },
    )
    ctx.require(settings.status_code == 200, settings.text[:300])

    denied = ctx.rest.post(
        "/api/v1/conversations/ask",
        headers=_headers(ua["token"]),
        json={
            "question": "should not start",
            "agent": "pi",
            "provider_id": provider_id,
            "model": model,
            "project": project_b_id,
        },
    )
    ctx.require(
        denied.status_code == 404,
        f"无权限项目创建对话应 404，实际 {denied.status_code}: {denied.text[:300]}",
    )

    conv_a = _insert_conversation(uab["id"], "default", turns=1)
    conv_b = _insert_conversation(uab["id"], project_b_id, turns=1)
    _insert_conversation(ub["id"], project_b_id, turns=1)

    listing = ctx.rest.get("/api/v1/conversations", headers=_headers(uab["token"]))
    ctx.require(listing.status_code == 200, listing.text[:300])
    rows = {row["id"]: row for row in listing.json()}
    ctx.require(
        {conv_a, conv_b} <= set(rows),
        f"多项目成员应看到两个项目的会话: {sorted(rows)}",
    )
    ctx.require(
        rows[conv_b]["project_id"] == project_b_id
        and rows[conv_b]["project_name"] == project_b["name"],
        f"会话项目字段错误: {rows[conv_b]}",
    )

    filtered = ctx.rest.get(
        "/api/v1/conversations",
        params={"project": project_b_id},
        headers=_headers(uab["token"]),
    )
    ctx.require(filtered.status_code == 200, filtered.text[:300])
    ctx.require(
        [row["id"] for row in filtered.json()] == [conv_b],
        f"项目过滤结果错误: {filtered.json()}",
    )

    changed = ctx.rest.post(
        "/api/v1/conversations/ask",
        headers=_headers(uab["token"]),
        json={
            "question": "switch project",
            "conversation_id": conv_b,
            "project": "default",
        },
    )
    ctx.require(
        changed.status_code == 409,
        f"续写换项目应 409，实际 {changed.status_code}: {changed.text[:300]}",
    )
    ctx.require("对话已绑定其它项目，请新建对话" in changed.text, changed.text[:300])

    first_ask = ctx.open_mcp(ub["token"], project=project_b_id)
    outcome = _tool_value(
        first_ask,
        "ask_dataagent",
        {"question": "只回复 OK，不要调用任何工具。"},
        timeout=600,
    )
    ctx.require(
        outcome.get("status") == "completed" and outcome.get("conversation_id"),
        f"ask_dataagent 未完成: {outcome!r}",
    )
    created_conversation = ctx.rest.get(
        f"/api/v1/conversations/{outcome['conversation_id']}",
        headers=_headers(ub["token"]),
    )
    ctx.require(
        created_conversation.status_code == 200, created_conversation.text[:300]
    )
    ctx.require(
        created_conversation.json()["project_id"] == project_b_id,
        f"ask_dataagent 创建的会话未绑定 B: {created_conversation.json()}",
    )

    mismatch = ctx.open_mcp(uab["token"], project="default")
    is_error, body = _tool_error(
        mismatch, "ask_dataagent", {"question": "continue", "conversation_id": conv_b}
    )
    ctx.require(
        is_error and "对话已绑定其它项目，请新建对话" in body.get("message", ""),
        f"MCP 续写换项目应拒绝: {body}",
    )

    credential_conversation = _insert_conversation(ub["id"], project_b_id)
    from semantica_dataagent.auth import issue_agent_credential
    from semantica_dataagent.db import Turn, sessions

    with sessions().begin() as db:
        now = datetime.now(UTC)
        turn = Turn(
            conversation_id=credential_conversation,
            provider_id="test",
            model="test",
            sequence=1,
            question="credential",
            status="running",
            answer="",
            sql="[]",
            citations="[]",
            started_at=now,
        )
        db.add(turn)
        db.flush()
        turn_id = turn.id
    credential = issue_agent_credential(
        ub["id"], credential_conversation, turn_id, project_id=project_b_id
    )
    credential_headers = {
        "x-dataagent-conversation-id": credential_conversation,
        "x-dataagent-turn-id": str(turn_id),
    }
    bound = ctx.open_mcp(credential, headers=credential_headers)
    capabilities = _tool_value(bound, "get_capabilities")
    ctx.require(
        capabilities.get("project", {}).get("id") == project_b_id,
        f"agent 凭据未绑定 B: {capabilities.get('project')}",
    )
    overriding = ctx.open_mcp(credential, project="default", headers=credential_headers)
    override, body = _tool_error(overriding, "get_capabilities")
    ctx.require(
        override and body.get("message") == NOT_FOUND,
        f"agent 凭据不应被项目参数覆盖: {body}",
    )

    removed = ctx.rest.delete(f"/api/v1/projects/{project_b_id}/members/{uab['id']}")
    ctx.require(removed.status_code == 204, removed.text[:300])
    remaining = ctx.rest.get("/api/v1/conversations", headers=_headers(uab["token"]))
    ctx.require(
        [row["id"] for row in remaining.json()] == [conv_a],
        f"失去项目权限后的列表错误: {remaining.json()}",
    )
    detail = ctx.rest.get(
        f"/api/v1/conversations/{conv_b}", headers=_headers(uab["token"])
    )
    ctx.require(detail.status_code == 200, detail.text[:300])
    ctx.require(detail.json()["project_id"] == project_b_id, detail.json())

    with sqlite3.connect(ctx.runtime.db_path) as db:
        projects = db.execute(
            "SELECT COUNT(*) FROM conversations WHERE project_id = ?",
            (project_b_id,),
        ).fetchone()[0]
    return {
        "project_b": project_b_id,
        "conversations_in_b": projects,
        "tokens": [row["id"] for row in _token_ids(ctx) if suffix in row["name"]],
    }
