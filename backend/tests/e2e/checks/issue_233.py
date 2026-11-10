"""确认执行与下载时重查项目权限 (#233).

The harness has built the default project.  This check creates a second
project so MCP requests can still resolve a project after the member is removed
from the plan's project.  Execution attempts, direct downloads and issued links
must all be refused after membership removal, archive, and token revocation;
plans stay planned, and refusals are audited.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime


def _project(ctx, name):
    response = ctx.rest.post(
        "/api/v1/projects", json={"name": name, "type": "warehouse"}
    )
    ctx.require(response.status_code == 201, response.text[:300])
    return response.json()


def _issue(ctx, name, projects):
    response = ctx.rest.post(
        "/api/v1/tokens",
        json={"name": name, "role": "user", "projects": projects},
    )
    ctx.require(response.status_code == 201, response.text[:300])
    return response.json()


def _user(ctx, method, path, token, **kwargs):
    return ctx.rest.request(
        method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs
    )


def _value(result):
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured.get("result", structured)
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return None


def _tool_error(result):
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and structured.get("message"):
        return structured["message"]
    value = _value(result)
    if isinstance(value, dict) and value.get("message"):
        return value["message"]
    return json.dumps(value, ensure_ascii=False)


def _add_member(ctx, project_id, token_id):
    response = ctx.rest.put(
        f"/api/v1/projects/{project_id}/members/{token_id}", json={"role": "member"}
    )
    ctx.require(response.status_code == 200, response.text[:300])


def _remove_member(ctx, project_id, token_id):
    response = ctx.rest.delete(f"/api/v1/projects/{project_id}/members/{token_id}")
    ctx.require(response.status_code == 204, response.text[:300])


def _insert_query(ctx, owner_id, project_id, status, rows=None):
    query_id = uuid.uuid4().hex
    plan = json.dumps({"project_id": project_id, "source": "mcp"})
    created = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
    with sqlite3.connect(ctx.runtime.db_path) as db:
        db.execute(
            "INSERT INTO query_executions "
            "(id, owner_id, sql, status, rows, row_count, elapsed_ms, "
            " created_at, plan_data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                query_id,
                owner_id,
                "SELECT 1",
                status,
                rows,
                1 if rows else None,
                1 if rows else None,
                created,
                plan,
            ),
        )
    return query_id


def _insert_succeeded(ctx, owner_id, project_id):
    return _insert_query(
        ctx,
        owner_id,
        project_id,
        "succeeded",
        json.dumps({"columns": ["v"], "data": [[1]]}),
    )


def _status(ctx, query_id):
    with sqlite3.connect(ctx.runtime.db_path) as db:
        row = db.execute(
            "SELECT status FROM query_executions WHERE id = ?", (query_id,)
        ).fetchone()
    ctx.require(row is not None, f"查询 {query_id} 不存在")
    return row[0]


def _audit(ctx):
    response = ctx.rest.get("/api/v1/query-audit")
    ctx.require(response.status_code == 200, response.text[:300])
    return response.json()


def _assert_audit(ctx, owner_id, kind, status):
    rows = _audit(ctx)
    ctx.require(
        any(
            row.get("owner_id") == owner_id
            and row.get("kind") == kind
            and row.get("status") == status
            for row in rows
        ),
        f"缺少审计 {kind}/{status} for {owner_id}: "
        f"{[(row.get('kind'), row.get('status')) for row in rows[:10]]}",
    )


def check_issue_233_recheck_execution_and_downloads(ctx):
    suffix = ctx.generation[-8:]
    project_b = _project(ctx, f"Issue 233 B {suffix}")
    member = _issue(
        ctx,
        f"issue233-{suffix}",
        [
            {"project_id": "default", "role": "member"},
            {"project_id": project_b["id"], "role": "member"},
        ],
    )
    token = member["token"]
    owner = member["id"]

    # Planning needs a configured executor; insert the planned row directly so
    # this check stays focused on the permission recheck at execution time.
    plan_id = _insert_query(ctx, owner, "default", "planned")

    # REMOVED: REST and the MCP tool both refuse, and the plan stays planned.
    _remove_member(ctx, "default", owner)
    rest_removed = _user(
        ctx, "POST", f"/api/v1/queries/{plan_id}/execute", token, json={}
    )
    ctx.require(rest_removed.status_code == 404, rest_removed.text[:300])
    ctx.require("查询不存在" in rest_removed.text, rest_removed.text[:300])
    mcp_session = ctx.open_mcp(token, project=project_b["id"])
    mcp_removed = mcp_session.call_tool("execute_query", {"confirmation_id": plan_id})
    ctx.require(getattr(mcp_removed, "isError", False), "MCP 未拒绝被移出的成员")
    ctx.require("查询不存在" in _tool_error(mcp_removed), _tool_error(mcp_removed))
    ctx.require(
        _status(ctx, plan_id) == "planned",
        "被拒后查询计划不再是 planned",
    )
    _assert_audit(ctx, owner, "query", "rejected_404")

    # ARCHIVED: REST and MCP refuse with the archive message.
    _add_member(ctx, "default", owner)
    archived = ctx.rest.post("/api/v1/projects/default/archive")
    ctx.require(archived.status_code == 200, archived.text[:300])
    try:
        rest_archived = _user(
            ctx, "POST", f"/api/v1/queries/{plan_id}/execute", token, json={}
        )
        ctx.require(rest_archived.status_code == 409, rest_archived.text[:300])
        ctx.require("项目已归档" in rest_archived.text, rest_archived.text[:300])
        mcp_archived = ctx.open_mcp(token, project="default").call_tool(
            "execute_query", {"confirmation_id": plan_id}
        )
        ctx.require(getattr(mcp_archived, "isError", False), "MCP 未拒绝归档项目")
        ctx.require(
            "项目已归档" in _tool_error(mcp_archived), _tool_error(mcp_archived)
        )
        ctx.require(
            _status(ctx, plan_id) == "planned",
            "归档被拒后查询计划不再是 planned",
        )
        _assert_audit(ctx, owner, "query", "rejected_409")
    finally:
        restored = ctx.rest.post("/api/v1/projects/default/restore")
        ctx.require(restored.status_code == 200, restored.text[:300])

    # DOWNLOAD AND ISSUED LINK after membership removal.
    query_id = _insert_succeeded(ctx, owner, "default")
    links = _user(ctx, "POST", f"/api/v1/queries/{query_id}/download-links", token)
    ctx.require(links.status_code == 200, links.text[:300])
    link_path = links.json()["csv"]
    ctx.require(ctx.rest.get(link_path).status_code == 200, "有效链接应可下载")
    _remove_member(ctx, "default", owner)
    direct = _user(ctx, "GET", f"/api/v1/queries/{query_id}/download", token)
    ctx.require(direct.status_code == 404, direct.text[:300])
    used = ctx.rest.get(link_path)
    ctx.require(used.status_code == 404, used.text[:300])
    reissue = _user(ctx, "POST", f"/api/v1/queries/{query_id}/download-links", token)
    ctx.require(reissue.status_code == 404, reissue.text[:300])
    _assert_audit(ctx, owner, "download", "rejected_404")
    _assert_audit(ctx, owner, "download_link_created", "rejected_404")
    _assert_audit(ctx, owner, "download_link_used", "rejected_404")

    # The link stops working when the owner token is revoked, even though the
    # membership row is restored first.
    _add_member(ctx, "default", owner)
    revoked_query = _insert_succeeded(ctx, owner, "default")
    links = _user(ctx, "POST", f"/api/v1/queries/{revoked_query}/download-links", token)
    ctx.require(links.status_code == 200, links.text[:300])
    revoked_link = links.json()["csv"]
    revoked = ctx.rest.delete(f"/api/v1/tokens/{owner}")
    ctx.require(revoked.status_code == 204, revoked.text[:300])
    ctx.require(ctx.rest.get(revoked_link).status_code == 404, "撤销令牌后链接仍可用")
    _assert_audit(ctx, owner, "download_link_used", "rejected_404")

    return {
        "rest_removed": rest_removed.status_code,
        "mcp_removed": _tool_error(mcp_removed),
        "rest_archived": rest_archived.status_code,
        "mcp_archived": _tool_error(mcp_archived),
        "download_removed": direct.status_code,
        "link_removed": used.status_code,
        "link_revoked": 404,
    }
