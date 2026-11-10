"""Query execution and download recheck project access (#233).

Every entry point is exercised through its real contract: REST execution,
conversation confirmation, the MCP tool, direct download and a signed link.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from test_mcp_contract import call_registered


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    token = initialize()
    with TestClient(app) as c:
        yield c, token


@pytest.fixture
def executor(monkeypatch):
    from semantica_dataagent import knowledge, query_api

    calls = []

    class Plugin:
        def bind(self, config, token):
            return self

        async def estimate(self, sql):
            return {"partitions": None, "rows": None}

        async def submit(self, sql, limit):
            calls.append(sql)
            return "fake-key"

        async def wait(self, key):
            return {"columns": ["v"], "data": [[1]]}

        async def download(self, key, format):
            return b"v\n1\n", "text/csv"

    monkeypatch.setattr(knowledge, "describe", lambda **_: [])
    monkeypatch.setattr(
        query_api,
        "_config",
        lambda: (
            Plugin(),
            {
                "row_limit": 100,
                "download_limit": 100,
                "confirmation_ttl_seconds": 1800,
            },
        ),
    )
    return calls


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create_project(c, admin: str) -> dict:
    response = c.post(
        "/api/v1/projects",
        headers=headers(admin),
        json={"name": "Query Access", "type": "warehouse"},
    )
    assert response.status_code == 201, response.text
    return response.json()


DEFAULT_PROJECT_ID = "default"


def create_member(
    c, admin: str, project_id: str, *, also_default: bool = False
) -> dict:
    projects = []
    if also_default:
        projects.append({"project_id": DEFAULT_PROJECT_ID, "role": "member"})
    projects.append({"project_id": project_id, "role": "member"})
    response = c.post(
        "/api/v1/tokens",
        headers=headers(admin),
        json={
            "name": "member",
            "role": "user",
            "projects": projects,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_admin(c, admin: str) -> dict:
    response = c.post(
        "/api/v1/tokens",
        headers=headers(admin),
        json={"name": "other-admin", "role": "admin"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def add_query(
    owner_id: str,
    project_id: str,
    *,
    status: str = "planned",
    source: str | None = None,
    conversation_id: str | None = None,
    turn_id: int | None = None,
    rows: str | None = None,
) -> str:
    from semantica_dataagent.db import QueryExecution, sessions

    query_id = uuid.uuid4().hex
    plan = {"project_id": project_id, "execution_limit": 100}
    if source:
        plan["source"] = source
    with sessions().begin() as db:
        db.add(
            QueryExecution(
                id=query_id,
                owner_id=owner_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                plan_data=json.dumps(plan),
                sql="SELECT 1",
                status=status,
                rows=rows,
            )
        )
    return query_id


def add_query_plan(
    owner_id: str,
    plan_data,
    *,
    status: str = "planned",
    conversation_id: str | None = None,
    turn_id: int | None = None,
    rows: str | None = None,
    sql: str = "SELECT 1",
) -> str:
    from semantica_dataagent.db import QueryExecution, sessions

    query_id = uuid.uuid4().hex
    stored = json.dumps(plan_data) if isinstance(plan_data, dict) else plan_data
    with sessions().begin() as db:
        db.add(
            QueryExecution(
                id=query_id,
                owner_id=owner_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                plan_data=stored,
                sql=sql,
                status=status,
                rows=rows,
            )
        )
    return query_id


def succeeded_rows() -> str:
    return json.dumps({"columns": ["v"], "data": [[1]]})


def add_conversation(
    owner_id: str, project_id: str, query_id: str, provider_id: str
) -> tuple[str, int]:
    from semantica_dataagent.db import (
        Conversation,
        QueryExecution,
        Turn,
        TurnEvent,
        sessions,
    )

    conversation_id = uuid.uuid4().hex
    with sessions().begin() as db:
        db.add(
            Conversation(
                id=conversation_id,
                owner_id=owner_id,
                project_id=project_id,
                title="query",
                agent="pi",
                provider_id=provider_id,
                model="model",
            )
        )
        db.flush()
        turn = Turn(
            conversation_id=conversation_id,
            provider_id=provider_id,
            model="model",
            sequence=1,
            question="query",
            status="awaiting_confirmation",
            answer="",
            sql="",
            citations="",
        )
        db.add(turn)
        db.flush()
        query = db.get(QueryExecution, query_id)
        query.conversation_id = conversation_id
        query.turn_id = turn.id
        db.add(
            TurnEvent(
                turn_id=turn.id,
                seq=1,
                type="query_plan",
                data=json.dumps({"id": query_id}),
            )
        )
        return conversation_id, turn.id


@pytest.fixture
def conversation_runner(monkeypatch):
    from semantica_dataagent import agent_runner
    from semantica_dataagent.agent_runner import DONE, AgentEvent

    class Runner:
        agent_id = "pi"
        effort_levels = ("low", "medium", "high")

        def available(self):
            return True

        async def run(self, request):
            yield AgentEvent(DONE, {"text": "ok", "sql": [], "citations": []})

    monkeypatch.setitem(agent_runner.RUNNERS, "pi", Runner())


def create_provider(c, admin: str) -> str:
    response = c.post(
        "/api/v1/providers",
        headers=headers(admin),
        json={
            "name": "provider",
            "base_url": "http://127.0.0.1:1",
            "models": ["model"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def mcp_execute(owner_id: str, role: str, token: str, project_id: str, query_id: str):
    from semantica_dataagent.app import create_mcp
    from semantica_dataagent.auth import Requester
    from semantica_dataagent.mcp_context import bind_mcp_context

    with bind_mcp_context(
        Requester(id=owner_id, role=role, token=token), project_id=project_id
    ):
        return call_registered(
            create_mcp(), "execute_query", {"confirmation_id": query_id}
        )


def audit_rows():
    from semantica_dataagent.db import QueryAudit, sessions

    with sessions()() as db:
        return [
            (row.kind, row.status)
            for row in db.query(QueryAudit).order_by(QueryAudit.created_at).all()
        ]


def audit_records():
    from semantica_dataagent.db import QueryAudit, sessions

    with sessions()() as db:
        return [
            {
                "kind": row.kind,
                "status": row.status,
                "sql": row.sql,
                "owner_id": row.owner_id,
            }
            for row in db.query(QueryAudit).order_by(QueryAudit.created_at).all()
        ]


def query_status(query_id: str) -> str:
    from semantica_dataagent.db import QueryExecution, sessions

    with sessions()() as db:
        return db.get(QueryExecution, query_id).status


def turn_status(turn_id: int) -> str:
    from semantica_dataagent.db import Turn, sessions

    with sessions()() as db:
        return db.get(Turn, turn_id).status


def set_project_status(project_id: str, status: str) -> None:
    from semantica_dataagent.db import Project, sessions

    with sessions().begin() as db:
        db.get(Project, project_id).status = status


def remove_member(c, admin: str, project_id: str, token_id: str) -> None:
    response = c.delete(
        f"/api/v1/projects/{project_id}/members/{token_id}", headers=headers(admin)
    )
    assert response.status_code == 204, response.text


def revoke_token(c, admin: str, token_id: str) -> None:
    assert (
        c.delete(f"/api/v1/tokens/{token_id}", headers=headers(admin)).status_code
        == 204
    )


def assert_rejected_audit(kind: str, status: str) -> None:
    entries = audit_rows()
    assert (kind, status) in entries, entries


def prepare(c, admin: str, scenario: str, executor, conversation_runner):
    project = create_project(c, admin)
    project_id = project["id"]
    member = create_member(c, admin, project_id)
    assert (
        c.put(
            "/api/v1/me/dtb-token",
            headers=headers(member["token"]),
            json={"token": "fake-dtb"},
        ).status_code
        == 200
    )
    provider_id = create_provider(c, admin)

    rest_id = add_query(member["id"], project_id)
    conversation_plan = add_query(member["id"], project_id)
    conversation_id, turn_id = add_conversation(
        member["id"], project_id, conversation_plan, provider_id
    )
    mcp_id = add_query(member["id"], project_id)
    download_id = add_query(
        member["id"],
        project_id,
        status="succeeded",
        source="mcp",
        rows=succeeded_rows(),
    )
    link_id = add_query(
        member["id"],
        project_id,
        status="succeeded",
        source="mcp",
        rows=succeeded_rows(),
    )
    links = c.post(
        f"/api/v1/queries/{link_id}/download-links", headers=headers(member["token"])
    )
    assert links.status_code == 200, links.text
    link_path = links.json()["csv"]

    if scenario == "removed":
        remove_member(c, admin, project_id, member["id"])
    elif scenario == "archived":
        assert (
            c.post(
                f"/api/v1/projects/{project_id}/archive", headers=headers(admin)
            ).status_code
            == 200
        )
    elif scenario == "deleting":
        set_project_status(project_id, "deleting")
    elif scenario == "revoked":
        revoke_token(c, admin, member["id"])
    else:
        assert scenario == "active"
    return {
        "project_id": project_id,
        "member": member,
        "rest_id": rest_id,
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "conversation_plan": conversation_plan,
        "mcp_id": mcp_id,
        "download_id": download_id,
        "link_path": link_path,
    }


@pytest.mark.parametrize(
    ("scenario", "rest_status", "download_status", "link_status", "mcp_detail"),
    [
        ("removed", 404, 404, 404, "查询不存在"),
        ("archived", 409, 409, 409, "项目已归档"),
        ("deleting", 409, 409, 409, "项目已归档"),
        ("revoked", 401, 401, 404, "查询不存在"),
    ],
)
def test_permission_revocation_rejects_all_entrances(
    client,
    executor,
    conversation_runner,
    scenario,
    rest_status,
    download_status,
    link_status,
    mcp_detail,
):
    c, admin = client
    state = prepare(c, admin, scenario, executor, conversation_runner)
    member = state["member"]

    response = c.post(
        f"/api/v1/queries/{state['rest_id']}/execute",
        headers=headers(member["token"]),
        json={},
    )
    assert response.status_code == rest_status, response.text

    response = c.post(
        f"/api/v1/conversations/{state['conversation_id']}/turns/{state['turn_id']}"
        f"/queries/{state['conversation_plan']}/execute",
        headers=headers(member["token"]),
    )
    assert response.status_code == rest_status, response.text

    mcp_result = mcp_execute(
        member["id"],
        member["role"],
        member["token"],
        state["project_id"],
        state["mcp_id"],
    )
    assert mcp_result.isError, mcp_result
    assert mcp_detail in mcp_result.structuredContent["message"]

    response = c.get(
        f"/api/v1/queries/{state['download_id']}/download",
        headers=headers(member["token"]),
    )
    assert response.status_code == download_status, response.text

    response = c.post(
        f"/api/v1/queries/{state['download_id']}/download-links",
        headers=headers(member["token"]),
    )
    assert response.status_code == download_status, response.text

    response = c.get(state["link_path"])
    assert response.status_code == link_status, response.text

    assert executor == []
    for query_id in (
        state["rest_id"],
        state["conversation_plan"],
        state["mcp_id"],
    ):
        assert query_status(query_id) == "planned"
    assert turn_status(state["turn_id"]) == "awaiting_confirmation"

    if scenario != "revoked":
        assert_rejected_audit("query", f"rejected_{rest_status}")
        assert_rejected_audit("mcp", f"rejected_{rest_status}")
        assert_rejected_audit("download", f"rejected_{download_status}")
        assert_rejected_audit("download_link_created", f"rejected_{download_status}")
        assert_rejected_audit("download_link_used", f"rejected_{link_status}")
    else:
        assert_rejected_audit("mcp", "rejected_404")
        assert_rejected_audit("download_link_used", "rejected_404")


def test_active_member_keeps_all_entrances_working(
    client, executor, conversation_runner
):
    c, admin = client
    state = prepare(c, admin, "active", executor, conversation_runner)
    member = state["member"]

    response = c.post(
        f"/api/v1/queries/{state['rest_id']}/execute",
        headers=headers(member["token"]),
        json={},
    )
    assert response.status_code == 200, response.text
    assert response.json()["rows"] == [[1]]

    response = c.post(
        f"/api/v1/conversations/{state['conversation_id']}/turns/{state['turn_id']}"
        f"/queries/{state['conversation_plan']}/execute",
        headers=headers(member["token"]),
    )
    assert response.status_code == 200, response.text

    mcp_result = mcp_execute(
        member["id"],
        member["role"],
        member["token"],
        state["project_id"],
        state["mcp_id"],
    )
    assert not mcp_result.isError, mcp_result
    assert mcp_result.structuredContent["row_count"] == 1

    response = c.get(
        f"/api/v1/queries/{state['download_id']}/download",
        headers=headers(member["token"]),
    )
    assert response.status_code == 200, response.text
    assert response.content == b"v\n1\n"

    response = c.get(state["link_path"])
    assert response.status_code == 200, response.text
    assert response.content == b"v\n1\n"

    assert len(executor) == 3
    assert query_status(state["rest_id"]) == "succeeded"
    assert query_status(state["conversation_plan"]) == "succeeded"
    assert query_status(state["mcp_id"]) == "succeeded"
    assert ("query", "succeeded") in audit_rows()
    assert ("mcp", "succeeded") in audit_rows()
    assert ("download_link_created", "succeeded") in audit_rows()
    assert ("download_link_used", "succeeded") in audit_rows()


def test_system_admin_keeps_access_without_membership(client, executor):
    c, boot_admin = client
    project = create_project(c, boot_admin)
    project_id = project["id"]
    admin = create_admin(c, boot_admin)
    assert (
        c.put(
            "/api/v1/me/dtb-token",
            headers=headers(admin["token"]),
            json={"token": "fake-dtb"},
        ).status_code
        == 200
    )

    planned = add_query(admin["id"], project_id)
    response = c.post(
        f"/api/v1/queries/{planned}/execute",
        headers=headers(admin["token"]),
        json={},
    )
    assert response.status_code == 200, response.text

    succeeded = add_query(
        admin["id"], project_id, status="succeeded", source="mcp", rows=succeeded_rows()
    )
    response = c.get(
        f"/api/v1/queries/{succeeded}/download",
        headers=headers(admin["token"]),
    )
    assert response.status_code == 200, response.text
    assert response.content == b"v\n1\n"

    links = c.post(
        f"/api/v1/queries/{succeeded}/download-links",
        headers=headers(admin["token"]),
    )
    assert links.status_code == 200, links.text
    response = c.get(links.json()["csv"])
    assert response.status_code == 200, response.text
    assert response.content == b"v\n1\n"

    assert ("query", "succeeded") in audit_rows()
    assert ("download_link_used", "succeeded") in audit_rows()


def _signed_link(query_id: str, fmt: str = "csv") -> str:
    from semantica_dataagent.query_api import (
        DOWNLOAD_LINK_TTL_SECONDS,
        _download_signature,
    )

    expires = int(time.time()) + DOWNLOAD_LINK_TTL_SECONDS
    return (
        f"/api/v1/queries/{query_id}/download-link/{fmt}/{expires}/"
        f"{_download_signature(query_id, fmt, expires)}"
    )


def _legacy_state(c, admin, *, project_id: str, also_default: bool, plan_data) -> dict:
    member = create_member(c, admin, project_id, also_default=also_default)
    assert (
        c.put(
            "/api/v1/me/dtb-token",
            headers=headers(member["token"]),
            json={"token": "fake-dtb"},
        ).status_code
        == 200
    )
    provider_id = create_provider(c, admin)

    planned = add_query_plan(member["id"], plan_data)
    conversation_id, turn_id = add_conversation(
        member["id"], project_id, planned, provider_id
    )
    download_id = add_query_plan(
        member["id"],
        plan_data,
        status="succeeded",
        conversation_id=conversation_id,
        rows=succeeded_rows(),
    )
    return {
        "project_id": project_id,
        "member": member,
        "planned": planned,
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "download_id": download_id,
    }


def test_legacy_non_default_plan_without_project_id_fails_closed(
    client, executor, conversation_runner
):
    c, admin = client
    project = create_project(c, admin)
    state = _legacy_state(
        c,
        admin,
        project_id=project["id"],
        also_default=True,
        plan_data={"execution_limit": 100},
    )
    member = state["member"]

    response = c.post(
        f"/api/v1/conversations/{state['conversation_id']}/turns/{state['turn_id']}"
        f"/queries/{state['planned']}/execute",
        headers=headers(member["token"]),
    )
    assert response.status_code == 404, response.text

    response = c.get(
        f"/api/v1/queries/{state['download_id']}/download",
        headers=headers(member["token"]),
    )
    assert response.status_code == 404, response.text

    response = c.post(
        f"/api/v1/queries/{state['download_id']}/download-links",
        headers=headers(member["token"]),
    )
    assert response.status_code == 404, response.text

    response = c.get(_signed_link(state["download_id"]))
    assert response.status_code == 404, response.text

    assert executor == []
    assert query_status(state["planned"]) == "planned"
    assert turn_status(state["turn_id"]) == "awaiting_confirmation"
    assert_rejected_audit("query", "rejected_404")
    assert_rejected_audit("download", "rejected_404")
    assert_rejected_audit("download_link_created", "rejected_404")
    assert_rejected_audit("download_link_used", "rejected_404")


@pytest.mark.parametrize("failure", ["removed", "archived"])
def test_legacy_non_default_plan_without_project_id_stays_denied(
    client, executor, conversation_runner, failure
):
    c, admin = client
    project = create_project(c, admin)
    state = _legacy_state(
        c,
        admin,
        project_id=project["id"],
        also_default=True,
        plan_data={"execution_limit": 100},
    )
    member = state["member"]

    if failure == "removed":
        remove_member(c, admin, state["project_id"], member["id"])
    else:
        assert (
            c.post(
                f"/api/v1/projects/{state['project_id']}/archive",
                headers=headers(admin),
            ).status_code
            == 200
        )

    response = c.post(
        f"/api/v1/conversations/{state['conversation_id']}/turns/{state['turn_id']}"
        f"/queries/{state['planned']}/execute",
        headers=headers(member["token"]),
    )
    assert response.status_code == 404, response.text

    response = c.get(
        f"/api/v1/queries/{state['download_id']}/download",
        headers=headers(member["token"]),
    )
    assert response.status_code == 404, response.text

    response = c.post(
        f"/api/v1/queries/{state['download_id']}/download-links",
        headers=headers(member["token"]),
    )
    assert response.status_code == 404, response.text

    assert executor == []
    assert query_status(state["planned"]) == "planned"
    assert turn_status(state["turn_id"]) == "awaiting_confirmation"
    assert_rejected_audit("query", "rejected_404")
    assert_rejected_audit("download", "rejected_404")
    assert_rejected_audit("download_link_created", "rejected_404")


def test_legacy_default_conversation_plan_without_project_id_still_works(
    client, executor, conversation_runner
):
    c, admin = client
    state = _legacy_state(
        c,
        admin,
        project_id=DEFAULT_PROJECT_ID,
        also_default=False,
        plan_data={"execution_limit": 100},
    )
    member = state["member"]

    response = c.post(
        f"/api/v1/conversations/{state['conversation_id']}/turns/{state['turn_id']}"
        f"/queries/{state['planned']}/execute",
        headers=headers(member["token"]),
    )
    assert response.status_code == 200, response.text

    response = c.get(
        f"/api/v1/queries/{state['download_id']}/download",
        headers=headers(member["token"]),
    )
    assert response.status_code == 200, response.text
    assert response.content == b"v\n1\n"

    links = c.post(
        f"/api/v1/queries/{state['download_id']}/download-links",
        headers=headers(member["token"]),
    )
    assert links.status_code == 200, links.text

    response = c.get(links.json()["csv"])
    assert response.status_code == 200, response.text
    assert response.content == b"v\n1\n"

    assert len(executor) == 1


@pytest.mark.parametrize(
    "plan_data",
    [
        json.dumps({"project_id": None}),
        json.dumps({"project_id": ""}),
        json.dumps({"project_id": " default "}),
        json.dumps({"project_id": 42}),
        json.dumps(["not", "an", "object"]),
        json.dumps({"source": "unknown"}),
        "{not json",
    ],
)
def test_corrupt_or_ambiguous_plan_project_fails_closed(client, executor, plan_data):
    c, admin = client
    project = create_project(c, admin)
    member = create_member(c, admin, project["id"], also_default=True)
    assert (
        c.put(
            "/api/v1/me/dtb-token",
            headers=headers(member["token"]),
            json={"token": "fake-dtb"},
        ).status_code
        == 200
    )

    planned = add_query_plan(member["id"], plan_data)
    succeeded = add_query_plan(
        member["id"], plan_data, status="succeeded", rows=succeeded_rows()
    )

    response = c.post(
        f"/api/v1/queries/{planned}/execute",
        headers=headers(member["token"]),
        json={},
    )
    assert response.status_code == 404, response.text

    response = c.get(
        f"/api/v1/queries/{succeeded}/download",
        headers=headers(member["token"]),
    )
    assert response.status_code == 404, response.text

    response = c.post(
        f"/api/v1/queries/{succeeded}/download-links",
        headers=headers(member["token"]),
    )
    assert response.status_code == 404, response.text

    assert executor == []
    assert query_status(planned) == "planned"
    assert_rejected_audit("query", "rejected_404")
    assert_rejected_audit("download", "rejected_404")
    assert_rejected_audit("download_link_created", "rejected_404")


def test_plan_and_conversation_project_conflict_fails_closed(
    client, executor, conversation_runner
):
    c, admin = client
    project = create_project(c, admin)
    project_id = project["id"]
    member = create_member(c, admin, project_id, also_default=True)
    assert (
        c.put(
            "/api/v1/me/dtb-token",
            headers=headers(member["token"]),
            json={"token": "fake-dtb"},
        ).status_code
        == 200
    )
    provider_id = create_provider(c, admin)

    planned = add_query_plan(member["id"], {"project_id": DEFAULT_PROJECT_ID})
    conversation_id, turn_id = add_conversation(
        member["id"], project_id, planned, provider_id
    )
    succeeded = add_query_plan(
        member["id"],
        {"project_id": DEFAULT_PROJECT_ID},
        status="succeeded",
        conversation_id=conversation_id,
        rows=succeeded_rows(),
    )

    response = c.post(
        f"/api/v1/conversations/{conversation_id}/turns/{turn_id}"
        f"/queries/{planned}/execute",
        headers=headers(member["token"]),
    )
    assert response.status_code == 404, response.text

    response = c.get(
        f"/api/v1/queries/{succeeded}/download",
        headers=headers(member["token"]),
    )
    assert response.status_code == 404, response.text

    response = c.post(
        f"/api/v1/queries/{succeeded}/download-links",
        headers=headers(member["token"]),
    )
    assert response.status_code == 404, response.text

    response = c.get(_signed_link(succeeded))
    assert response.status_code == 404, response.text

    assert executor == []
    assert query_status(planned) == "planned"
    assert turn_status(turn_id) == "awaiting_confirmation"
    assert_rejected_audit("query", "rejected_404")
    assert_rejected_audit("download", "rejected_404")
    assert_rejected_audit("download_link_created", "rejected_404")
    assert_rejected_audit("download_link_used", "rejected_404")


def test_missing_bound_conversation_fails_closed(client, executor):
    c, admin = client
    project = create_project(c, admin)
    member = create_member(c, admin, project["id"], also_default=True)
    assert (
        c.put(
            "/api/v1/me/dtb-token",
            headers=headers(member["token"]),
            json={"token": "fake-dtb"},
        ).status_code
        == 200
    )
    succeeded = add_query_plan(
        member["id"],
        {"execution_limit": 100},
        status="succeeded",
        conversation_id="missing-conversation",
        rows=succeeded_rows(),
    )

    response = c.get(
        f"/api/v1/queries/{succeeded}/download",
        headers=headers(member["token"]),
    )
    assert response.status_code == 404, response.text

    response = c.post(
        f"/api/v1/queries/{succeeded}/download-links",
        headers=headers(member["token"]),
    )
    assert response.status_code == 404, response.text

    assert_rejected_audit("download", "rejected_404")
    assert_rejected_audit("download_link_created", "rejected_404")


@pytest.mark.parametrize(
    "plan_data",
    [
        None,
        json.dumps({"execution_limit": 100}),
        json.dumps({"source": "mcp", "execution_limit": 100}),
    ],
)
def test_provably_default_legacy_plans_still_work(client, executor, plan_data):
    c, admin = client
    member = create_member(c, admin, DEFAULT_PROJECT_ID)
    assert (
        c.put(
            "/api/v1/me/dtb-token",
            headers=headers(member["token"]),
            json={"token": "fake-dtb"},
        ).status_code
        == 200
    )
    planned = add_query_plan(member["id"], plan_data)
    succeeded = add_query_plan(
        member["id"], plan_data, status="succeeded", rows=succeeded_rows()
    )

    response = c.post(
        f"/api/v1/queries/{planned}/execute",
        headers=headers(member["token"]),
        json={},
    )
    assert response.status_code == 200, response.text

    response = c.get(
        f"/api/v1/queries/{succeeded}/download",
        headers=headers(member["token"]),
    )
    assert response.status_code == 200, response.text
    assert response.content == b"v\n1\n"

    links = c.post(
        f"/api/v1/queries/{succeeded}/download-links",
        headers=headers(member["token"]),
    )
    assert links.status_code == 200, links.text
    response = c.get(links.json()["csv"])
    assert response.status_code == 200, response.text


def test_revoked_token_query_attempts_are_audited(
    client, executor, conversation_runner
):
    c, admin = client
    state = prepare(c, admin, "revoked", executor, conversation_runner)
    member = state["member"]
    token = member["token"]

    assert (
        c.post(
            f"/api/v1/queries/{state['rest_id']}/execute",
            headers=headers(token),
            json={},
        ).status_code
        == 401
    )
    assert (
        c.post(
            f"/api/v1/conversations/{state['conversation_id']}/turns/{state['turn_id']}"
            f"/queries/{state['conversation_plan']}/execute",
            headers=headers(token),
        ).status_code
        == 401
    )
    assert (
        c.get(
            f"/api/v1/queries/{state['download_id']}/download",
            headers=headers(token),
        ).status_code
        == 401
    )
    assert (
        c.post(
            f"/api/v1/queries/{state['download_id']}/download-links",
            headers=headers(token),
        ).status_code
        == 401
    )

    assert executor == []
    assert query_status(state["rest_id"]) == "planned"
    assert query_status(state["conversation_plan"]) == "planned"
    assert turn_status(state["turn_id"]) == "awaiting_confirmation"

    records = audit_records()

    def rejected(kind: str, query_id: str) -> list[dict]:
        return [
            record
            for record in records
            if record["kind"] == kind
            and record["status"] == "rejected_401"
            and query_id in record["sql"]
        ]

    assert rejected("query", state["rest_id"])
    assert rejected("query", state["conversation_plan"])
    assert rejected("download", state["download_id"])
    assert rejected("download_link_created", state["download_id"])
    assert all(record["owner_id"] == member["id"] for record in records)
    assert token not in json.dumps(records)

    before = len(records)
    assert c.get("/api/v1/projects", headers=headers(token)).status_code == 401
    assert len(audit_records()) == before
    assert (
        c.post(
            f"/api/v1/queries/{state['rest_id']}/execute",
            headers=headers("not-a-real-token"),
            json={},
        ).status_code
        == 401
    )
    assert len(audit_records()) == before
