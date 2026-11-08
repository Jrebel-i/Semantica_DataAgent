"""End-to-end checks for the project foundation and default project (#174)."""

from __future__ import annotations


def check_default_project_visibility_and_rename(ctx):
    response = ctx.rest.get(
        "/api/v1/projects", headers={"Authorization": "Bearer invalid"}
    )
    ctx.require(response.status_code == 401, response.text)

    admin_projects = ctx.rest.get("/api/v1/projects")
    ctx.require(admin_projects.status_code == 200, admin_projects.text)
    defaults = [item for item in admin_projects.json() if item["id"] == "default"]
    ctx.require(len(defaults) == 1, admin_projects.text)
    default = defaults[0]
    ctx.require(default["type"] == "warehouse", default)
    ctx.require(default["status"] == "active", default)
    ctx.require(default["my_role"] == "admin", default)

    created = ctx.rest.post(
        "/api/v1/tokens", json={"name": "issue-174 user", "role": "user"}
    )
    ctx.require(created.status_code == 201, created.text)
    issued = created.json()
    user_headers = {"Authorization": f"Bearer {issued['token']}"}
    original_name = default["name"]
    try:
        user_projects = ctx.rest.get("/api/v1/projects", headers=user_headers)
        ctx.require(user_projects.status_code == 200, user_projects.text)
        ctx.require(
            [(item["id"], item["my_role"]) for item in user_projects.json()]
            == [("default", "member")],
            user_projects.text,
        )

        denied = ctx.rest.patch(
            "/api/v1/projects/default",
            headers=user_headers,
            json={"name": "普通用户改名"},
        )
        ctx.require(denied.status_code == 403, denied.text)

        renamed = ctx.rest.patch(
            "/api/v1/projects/default", json={"name": "业务数据升级演练"}
        )
        ctx.require(renamed.status_code == 200, renamed.text)
        ctx.require(renamed.json()["name"] == "业务数据升级演练", renamed.text)
        ctx.require(renamed.json()["type"] == "warehouse", renamed.text)
        after = ctx.rest.get("/api/v1/projects").json()
        ctx.require(
            next(item for item in after if item["id"] == "default")["name"]
            == "业务数据升级演练",
            after,
        )
    finally:
        ctx.rest.patch("/api/v1/projects/default", json={"name": original_name})
        ctx.rest.delete(f"/api/v1/tokens/{issued['id']}")

    return {"default": original_name, "user": issued["id"]}
