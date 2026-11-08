"""Authoritative project-type capabilities for tools, adapters and skills.

A project's type decides which MCP tools an agent can see and call, which
source adapters a build may run, and which retrieval skill the agent receives.
Keep the tool table exhaustive: ``test_every_registered_tool_has_one_authoritative_project_type``
fails when a newly registered tool is missing here.
"""

from __future__ import annotations

WAREHOUSE = "warehouse"
DOCUMENT = "document"
PROJECT_TYPES = (WAREHOUSE, DOCUMENT)

DOCUMENT_TOOL_NAMES = frozenset(
    {
        "search_knowledge",
        "read_document",
        "list_categories",
        "read_category_index",
        "request_clarification",
        "propose_correction",
        "report_knowledge_gap",
        "get_capabilities",
    }
)

TOOL_PROJECT_TYPES: dict[str, tuple[str, ...]] = {
    "describe_tables": (WAREHOUSE,),
    "search_knowledge": (WAREHOUSE, DOCUMENT),
    "lookup_dim_values": (WAREHOUSE,),
    "list_categories": (WAREHOUSE, DOCUMENT),
    "read_category_index": (WAREHOUSE, DOCUMENT),
    "read_document": (WAREHOUSE, DOCUMENT),
    "get_lineage": (WAREHOUSE,),
    "read_task_sql": (WAREHOUSE,),
    "validate_sql": (WAREHOUSE,),
    "find_join_path": (WAREHOUSE,),
    "request_clarification": (WAREHOUSE, DOCUMENT),
    "report_knowledge_gap": (WAREHOUSE, DOCUMENT),
    "propose_correction": (WAREHOUSE, DOCUMENT),
    "ask_dataagent": (WAREHOUSE,),
    "sample_table_data": (WAREHOUSE,),
    "propose_query": (WAREHOUSE,),
    "execute_query": (WAREHOUSE,),
    "get_capabilities": (WAREHOUSE, DOCUMENT),
}


class AdapterIncompatible(ValueError):
    """A source adapter cannot read the requested project type."""


def _check_project_type(project_type: str) -> str:
    if project_type not in PROJECT_TYPES:
        raise ValueError(f"未知项目类型：{project_type}")
    return project_type


def unclassified_tools(names) -> set[str]:
    """Registered tool names with no entry in the authoritative table."""
    return set(names) - set(TOOL_PROJECT_TYPES)


def tools_for_project_type(project_type: str) -> set[str]:
    """Tool names visible to an agent working in this project type."""
    project_type = _check_project_type(project_type)
    if project_type == DOCUMENT:
        return set(DOCUMENT_TOOL_NAMES)
    return set(TOOL_PROJECT_TYPES)


def supports_tool(project_type: str, tool_name: str) -> bool:
    project_type = _check_project_type(project_type)
    project_types = TOOL_PROJECT_TYPES.get(tool_name)
    if project_types is None:
        return True
    return project_type in project_types


def ensure_tool_supported(project_type: str, tool_name: str) -> None:
    if supports_tool(project_type, tool_name):
        return
    from .mcp_contract import ToolInvalidInput

    raise ToolInvalidInput("该项目类型不支持此工具")


def source_project_types(plugin) -> tuple[str, ...]:
    """Declared project types, defaulting legacy adapters to warehouse only."""
    declared = getattr(plugin, "project_types", None)
    if declared:
        return tuple(declared)
    return (WAREHOUSE,)


def supports_project_type(plugin, project_type: str) -> bool:
    return _check_project_type(project_type) in source_project_types(plugin)


def adapter_project_types(adapter_id: str) -> tuple[str, ...]:
    from .plugins import discover

    plugin = discover().get(("source", adapter_id))
    if plugin is None:
        raise AdapterIncompatible(f"未知源适配器：{adapter_id}")
    return source_project_types(plugin)


def ensure_adapter_compatible(project_type: str, adapter_id: str) -> None:
    """Reject a source adapter that cannot read this project type.

    Used by the per-project adapter configuration tracked in #187.
    """
    project_type = _check_project_type(project_type)
    supported = adapter_project_types(adapter_id)
    if project_type not in supported:
        raise AdapterIncompatible(
            f"源适配器 {adapter_id} 不支持项目类型 {project_type}"
        )
