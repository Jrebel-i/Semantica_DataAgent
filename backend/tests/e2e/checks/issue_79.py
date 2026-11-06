"""Verify configured defaults and online conversation selection through REST."""

import json
import os


def check_assistant_defaults_and_online_selection(ctx):
    model = os.environ.get("TEST_PI_MODEL", "deepseek-v4.1-flash#claude")
    provider = ctx.rest.post(
        "/api/v1/providers",
        json={
            "name": "issue 79 e2e",
            "type": "anthropic-messages",
            "base_url": os.environ.get("TEST_PI_BASE_URL", "http://127.0.0.1:3425"),
            "api_key": "e2e-placeholder",
            "models": [model],
        },
    )
    ctx.require(
        provider.status_code == 201, f"provider: {provider.status_code} {provider.text}"
    )
    provider_id = provider.json()["id"]
    values = {
        "assistant_agent": "pi",
        "assistant_provider_id": provider_id,
        "assistant_model": model,
        "assistant_agent_profiles": json.dumps(
            {
                "pi": {
                    "providers": [provider_id],
                    "default_provider": provider_id,
                    "default_model": model,
                }
            }
        ),
    }
    saved = ctx.rest.put("/api/v1/settings", json={"values": values})
    ctx.require(
        saved.status_code == 200, f"settings write: {saved.status_code} {saved.text}"
    )
    defaults = ctx.rest.get("/api/v1/assistant-defaults")
    ctx.require(
        defaults.status_code == 200
        and all(defaults.json().get(key) == value for key, value in values.items()),
        f"defaults: {defaults.status_code} {defaults.text}",
    )
    with ctx.rest.stream(
        "POST",
        "/api/v1/conversations/ask",
        json={
            "question": "列出可用于查询的表",
            "agent": "pi",
            "provider_id": provider_id,
            "model": model,
        },
        timeout=600,
    ) as response:
        ctx.require(response.status_code == 200, f"ask: HTTP {response.status_code}")
        body = "".join(response.iter_text())
    events = []
    for block in body.split("\n\n"):
        lines = block.splitlines()
        name = next((line[7:] for line in lines if line.startswith("event: ")), "")
        data = next((line[6:] for line in lines if line.startswith("data: ")), "{}")
        if name:
            events.append((name, json.loads(data)))
    ctx.require(
        any(name == "done" for name, _ in events),
        f"answer did not finish: {events[-3:]}",
    )
    conversation_id = next(
        data["conversation_id"] for name, data in events if name == "conversation"
    )
    detail = ctx.rest.get(f"/api/v1/conversations/{conversation_id}")
    ctx.require(detail.status_code == 200, f"conversation: {detail.status_code}")
    row = detail.json()
    ctx.require(
        (row["agent"], row["provider_id"], row["model"]) == ("pi", provider_id, model),
        "conversation did not retain requested agent and model",
    )
    changed = ctx.rest.post(
        "/api/v1/conversations/ask",
        json={
            "question": "继续",
            "conversation_id": conversation_id,
            "agent": "claude",
        },
    )
    ctx.require(
        changed.status_code == 409,
        f"conversation allowed agent change: {changed.status_code}",
    )
    return {"agent": row["agent"], "model": row["model"]}


def check_citation_lookup(ctx):
    response = ctx.rest.get(
        "/api/v1/tables", params={"database": "shop", "search": "orders"}
    )
    ctx.require(
        response.status_code == 200
        and any(row["ref"] == "table:shop.orders" for row in response.json()),
        "table citation lookup failed",
    )
    response = ctx.rest.get(
        "/api/v1/document", params={"doc_id": "document:metrics/sales.md"}
    )
    ctx.require(
        response.status_code == 200
        and response.json()["ref"] == "document:metrics/sales.md",
        "document citation lookup failed",
    )


def check_correction_submission(ctx):
    rows = ctx.rest.get("/api/v1/tables", params={"database": "shop"})
    ctx.require(
        rows.status_code == 200 and rows.json(), "expected a synthetic shop table"
    )
    table = rows.json()[0]
    response = ctx.rest.post(
        "/api/v1/corrections",
        json={
            "target": table["ref"],
            "patch": (table.get("comment") or "") + " 修正",
            "reason": "端到端验证",
        },
    )
    ctx.require(
        response.status_code == 201,
        f"correction submission: {response.status_code} {response.text}",
    )
    proposals = ctx.rest.get("/api/v1/corrections")
    ctx.require(
        proposals.status_code == 200
        and any(
            row["id"] == response.json()["proposal_id"] for row in proposals.json()
        ),
        "submitted correction absent from review",
    )
