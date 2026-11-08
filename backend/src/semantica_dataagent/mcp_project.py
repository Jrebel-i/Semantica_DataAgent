"""Resolve the one project an MCP request works against.

The access decision itself lives in ``project_api``.  This module only adds the
MCP selection order required by #178:

1. a credential issued to an online agent is locked to its project and may not
   be overridden by request parameters;
2. an explicit ``project`` query parameter or ``X-Dataagent-Project`` header;
3. single-project tokens keep working without a selection.  A token that can
   read more than one project must choose one, and the error lists the choices.
   System administrator tokens keep the default project when nothing is chosen
   so existing administrator MCP clients keep working.
"""

from __future__ import annotations

from fastapi import HTTPException

from .auth import Requester
from .mcp_contract import ToolInvalidInput
from .project_api import accessible_projects, resolve_project_scope
from .project_context import DEFAULT_PROJECT_ID

NOT_FOUND = "项目不存在"


def _resolve(requester: Requester, project_id: str) -> str:
    try:
        return resolve_project_scope(project_id, requester).project_id
    except HTTPException as exc:
        raise ToolInvalidInput(NOT_FOUND) from exc


def _selection_error(requester: Requester, projects: list) -> ToolInvalidInput:
    if not projects:
        return ToolInvalidInput("请指定项目：当前令牌没有可访问的项目")
    choices = "、".join(f"{row.id}（{row.name}）" for row in projects)
    return ToolInvalidInput(f"请指定项目：可访问的项目有 {choices}")


def resolve_mcp_project(
    requester: Requester,
    credential_project_id: str | None = None,
    requested_project_id: str | None = None,
) -> str:
    if credential_project_id is not None:
        if (
            requested_project_id is not None
            and requested_project_id != credential_project_id
        ):
            raise ToolInvalidInput(NOT_FOUND)
        return _resolve(requester, credential_project_id)
    if requested_project_id is not None:
        return _resolve(requester, requested_project_id)
    projects = accessible_projects(requester)
    if requester.role == "admin":
        return _resolve(requester, DEFAULT_PROJECT_ID)
    if len(projects) == 1:
        return projects[0].id
    raise _selection_error(requester, projects)
