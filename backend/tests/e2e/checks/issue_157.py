"""REST contracts used by the split ChatPage modules.

#157 is a front-end-only refactor. These checks exercise the REST payloads
that ChatPage, useProviders, useConversations, ContextDrawer and tableDetail
read, so a shape regression introduced while moving code is observable
through the real API.
"""

from __future__ import annotations


def check_chatpage_rest_contracts(ctx):
    agents = ctx.rest.get("/api/v1/agents")
    agents.raise_for_status()
    agents_body = agents.json()
    ctx.require(isinstance(agents_body, list), "/api/v1/agents is not a list")
    ctx.require(agents_body, "/api/v1/agents returned no agents")
    ctx.require(
        all({"id", "name", "available"} <= set(row) for row in agents_body),
        "an agent row is missing id/name/available",
    )

    providers = ctx.rest.get("/api/v1/providers")
    providers.raise_for_status()
    ctx.require(isinstance(providers.json(), list), "/api/v1/providers is not a list")

    defaults = ctx.rest.get("/api/v1/assistant-defaults")
    defaults.raise_for_status()
    defaults_body = defaults.json()
    ctx.require(
        {
            "assistant_agent",
            "assistant_provider_id",
            "assistant_model",
            "assistant_agent_profiles",
        }
        <= set(defaults_body),
        "assistant defaults are missing a key used by the composer",
    )

    conversations = ctx.rest.get("/api/v1/conversations")
    conversations.raise_for_status()
    ctx.require(
        isinstance(conversations.json(), list),
        "/api/v1/conversations is not a list",
    )
    archived = ctx.rest.get("/api/v1/conversations", params={"archived": True})
    archived.raise_for_status()
    ctx.require(
        isinstance(archived.json(), list),
        "archived conversations response is not a list",
    )

    database, table = ctx.expect_table.split(".", 1)
    detail = ctx.rest.get(f"/api/v1/tables/{database}/{table}")
    detail.raise_for_status()
    detail_body = detail.json()
    ctx.require(
        {"name", "database", "comment", "ref", "fields", "documents"}
        <= set(detail_body),
        "table detail is missing fields read by ContextDrawer",
    )
    ctx.require(detail_body["fields"], "table detail returned no fields")
    ctx.require(
        all(
            {"name", "type", "comment", "partition"} <= set(field)
            for field in detail_body["fields"]
        ),
        "a table field is missing columns read by ContextDrawer",
    )

    hits = ctx.mcp_call(
        "search_knowledge",
        {"query": ctx.config.search_query, "scope": "docs", "top_k": 1},
    )
    ctx.require(
        isinstance(hits, list) and hits, "search_knowledge returned no document"
    )
    doc_ref = str(hits[0]["ref"])
    doc_id = "document:" + doc_ref[len("document:") :].split("#", 1)[0]
    document = ctx.rest.get("/api/v1/document", params={"doc_id": doc_id})
    document.raise_for_status()
    document_body = document.json()
    ctx.require(
        {"ref", "category", "path", "content", "start_line", "end_line", "generated"}
        <= set(document_body),
        "document payload is missing fields read by ContextDrawer",
    )
    ctx.require(document_body["content"], "document payload has no content")

    return {
        "ok": True,
        "info": {
            "agents": len(agents_body),
            "providers": len(providers.json()),
            "table_fields": len(detail_body["fields"]),
            "document_ref": document_body["ref"],
        },
    }
