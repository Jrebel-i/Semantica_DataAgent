"""Exercise conversation management through the public REST API."""

import importlib.util
from pathlib import Path


def check_conversation_management(ctx):
    spec = importlib.util.spec_from_file_location(
        "issue_135_helpers", Path(__file__).with_name("issue_135.py")
    )
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    owner = helpers._owner_id(ctx)
    conversation_id = helpers._add_conversation(ctx, owner, "issue 144")
    running_id = helpers._add_conversation(
        ctx, owner, "issue 144 running", running=True
    )
    path = f"/api/v1/conversations/{conversation_id}"
    try:
        response = ctx.rest.patch(path, json={"title": "  新名称  ", "pinned": True})
        ctx.require(response.status_code == 200, response.text)
        ctx.require(response.json()["title"] == "新名称", "title was not trimmed")
        ctx.require(response.json()["pinned"], "pin was not saved")
        response = ctx.rest.patch(path, json={"archived": True})
        ctx.require(response.status_code == 200, response.text)
        ctx.require(
            conversation_id not in helpers._listed_ids(ctx), "archive still listed"
        )
        archived = ctx.rest.get("/api/v1/conversations?archived=true")
        ctx.require(archived.status_code == 200, archived.text)
        ctx.require(
            any(
                row["id"] == conversation_id and row["pinned"] and row["archived"]
                for row in archived.json()
            ),
            "archived pinned conversation missing",
        )
        response = ctx.rest.get(path)
        ctx.require(
            response.status_code == 200 and response.json()["archived"],
            "archived detail unavailable",
        )
        response = ctx.rest.patch(path, json={"archived": False})
        ctx.require(
            response.status_code == 200 and response.json()["pinned"],
            "restore lost pin",
        )
        ctx.require(
            conversation_id in helpers._listed_ids(ctx), "restored conversation missing"
        )
        response = ctx.rest.patch(path, json={"title": "   "})
        ctx.require(response.status_code == 422, "empty title accepted")
        response = ctx.rest.patch(
            f"/api/v1/conversations/{running_id}", json={"archived": True}
        )
        ctx.require(response.status_code == 409, "running conversation archived")
        response = ctx.rest.delete(path)
        ctx.require(response.status_code == 204, response.text)
        ctx.require(
            conversation_id not in helpers._listed_ids(ctx),
            "deleted conversation remains",
        )
    finally:
        helpers._remove_turn(ctx, running_id)
        ctx.rest.delete(f"/api/v1/conversations/{running_id}")
        ctx.rest.delete(path)
